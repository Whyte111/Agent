"""对话链自检（离线、零外部调用）：验证多轮上下文、工具循环、5 步收敛、截断。

用法: python -m scripts.selfcheck_chat
"""
from __future__ import annotations

import dataclasses
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]

from app.config.settings import settings as base_settings  # noqa: E402
from app.orchestrator.loop import (  # noqa: E402
    HISTORY_MAX_MESSAGES,
    MAX_STEPS,
    ChatError,
    build_window,
    run_chat,
)
from app.store import repo  # noqa: E402

RESULTS: List[tuple] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((ok, name, detail))


class FakeLLM:
    """按脚本回答的假模型：可以指定这一轮返回 tool_calls 还是文本。"""

    mode = "fake"

    def __init__(self, script: List[Dict[str, Any]]):
        self.script = list(script)
        self.calls: List[Dict[str, Any]] = []

    def chat(self, messages, tools=None, json_mode=False, temperature=0.2):
        self.calls.append(
            {"messages": [dict(m) for m in messages], "tools": tools, "json_mode": json_mode}
        )
        if self.script:
            return self.script.pop(0)
        return {"content": "（脚本用尽）", "tool_calls": []}


def tool_call(name: str, args: Dict[str, Any], call_id: str = "call_1") -> Dict[str, Any]:
    import json

    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
    }


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="aiwainao-chat-"))
    try:
        s = dataclasses.replace(base_settings, data_dir=tmp, mail_dry_run_forced=True)
        repo.init_db(s.db_path)
        session = "sess-chat-1"

        # ---------- 1. 多轮上下文 ----------
        fake = FakeLLM(
            [
                {"content": "记住了：你叫张伟，下周一去上海。", "tool_calls": []},
                {"content": "你下周一要去上海。", "tool_calls": []},
            ]
        )
        first = run_chat(s, session_id=session, text="我叫张伟，下周一要去上海出差。", llm=fake)
        check("第 1 轮有回复", bool(first["reply"]), first["reply"][:20])

        second = run_chat(s, session_id=session, text="我刚才说下周要去哪？", llm=fake)
        sent = fake.calls[-1]["messages"]
        contents = [m["content"] for m in sent]
        check("第二轮把第一轮的用户消息带进去了",
              any("我叫张伟" in c for c in contents),
              "共 %d 条上下文" % len(sent))
        check("第二轮把第一轮的回答带进去了",
              any("记住了" in c for c in contents))
        check("system prompt 存在且只有一条", sent[0]["role"] == "system")

        with repo.db(s.db_path) as conn:
            rows = repo.recent_messages(conn, session, 50)
        check("消息已落库（2 问 2 答）", len(rows) == 4, "rows=%d" % len(rows))

        # ---------- 2. 工具循环 ----------
        fake_tool = FakeLLM(
            [
                {"content": "", "tool_calls": [tool_call("memory_read", {"query": "张伟"})]},
                {"content": "记忆里没有张伟的记录。", "tool_calls": []},
            ]
        )
        res = run_chat(s, session_id="sess-chat-2", text="你还记得张伟吗？", llm=fake_tool)
        check("工具被调用一次", len(res["tool_trace"]) == 1, str(res["tool_trace"]))
        check("工具调用成功（空结果也算成功）",
              res["tool_trace"][0]["ok"] is True, res["tool_trace"][0].get("error") or "")
        check("工具结果回填给模型",
              any(m.get("role") == "tool" for m in fake_tool.calls[-1]["messages"]))
        check("最终回复来自模型", "没有张伟" in res["reply"], res["reply"][:24])
        check("工具调用已落库", _count_tool_calls(s) == 1, "tool_calls=%d" % _count_tool_calls(s))

        # ---------- 3. 5 步收敛 ----------
        always = FakeLLM(
            [{"content": "", "tool_calls": [tool_call("memory_read", {"query": "x"}, "c%d" % i)]}
             for i in range(5)]
            + [{"content": "基于已有结果：没查到记录。", "tool_calls": []}]
        )
        res = run_chat(s, session_id="sess-chat-3", text="一直调工具试试", llm=always)
        check("工具循环最多 5 步", res["steps"] == MAX_STEPS, "steps=%d" % res["steps"])
        check("第 6 次调用不再带 tools（强制收敛）",
              always.calls[-1]["tools"] is None,
              "tools=%s" % type(always.calls[-1]["tools"]).__name__)
        check("收敛后仍有回复", bool(res["reply"]), res["reply"][:24])

        # ---------- 4. 未知工具 / 坏参数 ----------
        bad = FakeLLM(
            [
                {"content": "", "tool_calls": [tool_call("send_mail", {"to": "x@y.com"})]},
                {"content": "这个我做不到。", "tool_calls": []},
            ]
        )
        res = run_chat(s, session_id="sess-chat-4", text="帮我发邮件", llm=bad)
        check("未知工具不抛异常", res["tool_trace"][0]["ok"] is False,
              str(res["tool_trace"][0]["error"]))
        names = [t["name"] for t in _registry_names(s)]
        check("对话侧白名单不含 send_mail", "send_mail" not in names, ",".join(names))

        # ---------- 4.5 G4：会议行必须进 system prompt ----------
        with repo.db(s.db_path) as conn:
            g4_doc = repo.insert_doc(
                conn, session_id="sess-g4", kind="meeting", path="x.txt",
                sha256="f" * 64,
            )
            repo.insert_meeting(
                conn, doc_id=g4_doc, session_id="sess-g4",
                held_at="2025-09-20 15:30", place="北京望京 B 座",
                attendees=["吴倩", "周涛", "孙磊"], topic="产品迭代需求评审",
                raw_text="产品迭代需求评审",
            )
        g4_llm = FakeLLM([{"content": "上次会议是产品迭代需求评审。", "tool_calls": []}])
        run_chat(s, session_id="sess-chat-g4", text="上次会议的四要素是什么？", llm=g4_llm)
        system_prompt = g4_llm.calls[-1]["messages"][0]["content"]
        check("G4 system prompt 含会议主题", "产品迭代需求评审" in system_prompt)
        check("G4 system prompt 含会议时间/地点", "2025-09-20" in system_prompt and "北京望京" in system_prompt)
        check("G4 system prompt 含参会人", "吴倩" in system_prompt and "周涛" in system_prompt)

        # ---------- 5. 截断 ----------
        history = [
            {"role": "user" if i % 2 == 0 else "assistant", "content": "第 %d 条消息" % i}
            for i in range(40)
        ]
        window, truncated = build_window(history)
        check("条数截断生效", len(window) <= HISTORY_MAX_MESSAGES, "window=%d" % len(window))
        check("截断标记为真", truncated is True)
        check("最后一条（当前问题）被保留",
              window[-1]["content"] == history[-1]["content"], window[-1]["content"])

        long_history = [
            {"role": "user", "content": "字" * 2000} for _ in range(10)
        ]
        window2, truncated2 = build_window(long_history, max_chars=6000)
        check("字符预算截断生效", len(window2) <= 4, "window=%d" % len(window2))
        check("字符截断标记为真", truncated2 is True)

        # ---------- 6. 输入校验 ----------
        for name, text, expect in [
            ("空消息被拒", "   ", True),
            ("超长消息被拒", "字" * 5000, True),
            ("正常消息放行", "你好", False),
        ]:
            try:
                run_chat(s, session_id="sess-chat-5", text=text, llm=FakeLLM([]))
                raised = False
            except ChatError:
                raised = True
            check(name, raised is expect, "raised=%s" % raised)

        # ---------- 7. 工具失败不影响回复 ----------
        checker = FakeLLM(
            [
                {"content": "", "tool_calls": [tool_call("memory_read", {})]},  # missing required
                {"content": "查不到。", "tool_calls": []},
            ]
        )
        res = run_chat(s, session_id="sess-chat-6", text="查一下", llm=checker)
        check("工具参数错误只进 trace", res["tool_trace"][0]["ok"] is False,
              str(res["tool_trace"][0]["error"]))
        check("工具出错仍有回复", res["reply"] == "查不到。", res["reply"])

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    passed = sum(1 for ok, _, _ in RESULTS if ok)
    print("=" * 66)
    for ok, name, detail in RESULTS:
        print("%s %-32s %s" % ("PASS" if ok else "FAIL", name, detail))
    print("=" * 66)
    print("%d/%d 项通过" % (passed, len(RESULTS)))
    return 0 if passed == len(RESULTS) else 1


def _count_tool_calls(s) -> int:
    import sqlite3

    conn = sqlite3.connect(str(s.db_path))
    try:
        return conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0]
    finally:
        conn.close()


def _registry_names(s):
    from app.orchestrator.loop import build_registry

    registry = build_registry(s)
    return [{"name": n} for n in registry.names()]


if __name__ == "__main__":
    raise SystemExit(main())
