"""对话主循环（骨架 6 节 A）。

收消息 → 落 Message → 记忆召回(拼进 system) → LLM(带工具) → 有 tool_calls? 执行并回填(≤5轮)
→ 生成回复 → 落 Message → 返回 reply + refs + trace

关键约束：
- 工具循环最多 5 步，超了强制收敛（不让模型无限调工具）；
- 历史按「条数 + 字符」双阈值滑动截断（U6 的默认实现），截断与否会如实返回；
- 任何工具失败都不抛给用户，只进 trace。
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Tuple

from app.config.settings import Settings
from app.llm.client import LLMError, get_llm
from app.orchestrator.prompts import build_system_prompt
from app.store import repo
from app.tools import doc_search, memory_tool, ppt_tool, web_search
from app.tools.registry import ToolRegistry

MAX_STEPS = 5           # 骨架 7 节：max_steps=5
MAX_INPUT_CHARS = 4000
HISTORY_MAX_MESSAGES = 12
HISTORY_MAX_CHARS = 6000

CONVERGE_HINT = "请基于已经拿到的信息直接给出最终回答，不要再调用工具。"


class ChatError(ValueError):
    """对话侧可预期错误（空消息 / 过长 / 模型故障）。"""


def build_registry(settings: Settings, session_id: str = "") -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(memory_tool.build(settings))
    registry.register(doc_search.build(settings))
    registry.register(web_search.build(settings))
    registry.register(ppt_tool.build(settings, session_id))
    return registry


def build_window(
    history: List[Dict[str, Any]],
    max_messages: int = HISTORY_MAX_MESSAGES,
    max_chars: int = HISTORY_MAX_CHARS,
) -> Tuple[List[Dict[str, Any]], bool]:
    """滑动窗口：先按条数截，再按字符截，但最后一条（当前问题）永远保留。"""
    truncated = len(history) > max_messages
    window = history[-max_messages:] if len(history) > max_messages else list(history)

    kept: List[Dict[str, Any]] = []
    total = 0
    for item in reversed(window):
        cost = len(item.get("content") or "")
        if kept and total + cost > max_chars:
            truncated = True
            break
        kept.append(item)
        total += cost
    kept.reverse()
    return [{"role": item["role"], "content": item["content"]} for item in kept], truncated


def run_chat(
    settings: Settings,
    *,
    session_id: str,
    text: str,
    llm: Any = None,
    registry: Optional[ToolRegistry] = None,
) -> Dict[str, Any]:
    text = (text or "").strip()
    if not text:
        raise ChatError("消息不能为空")
    if len(text) > MAX_INPUT_CHARS:
        raise ChatError("消息过长（上限 %d 字）" % MAX_INPUT_CHARS)
    if not session_id:
        raise ChatError("缺少 session_id")

    llm = llm or get_llm(settings)
    registry = registry or build_registry(settings, session_id)

    with repo.db(settings.db_path) as conn:
        repo.ensure_session(conn, session_id)
        user_message_id = repo.insert_message(
            conn, session_id=session_id, role="user", content=text
        )
        history = repo.recent_messages(conn, session_id, limit=HISTORY_MAX_MESSAGES * 2)
        window, truncated = build_window(history)

        # 记忆召回：G4 接入前 entities 表为空，这里如实拿到空结果
        memory_entities: List[Dict[str, Any]] = []
        try:
            recalled = memory_tool.memory_read("", None, 20, settings=settings)
            memory_entities = recalled.get("entities") or []
        except Exception:
            memory_entities = []

        # 会议行也要进 system prompt：Entity 是零散标签，回答“上次会议四要素”需要
        # topic/held_at/place/attendees 这组完整记录。
        recent_meetings: List[Dict[str, Any]] = []
        try:
            recent_meetings = repo.recent_meeting_summaries(conn, 5)
        except Exception:
            recent_meetings = []

        conversation: List[Dict[str, Any]] = [
            {"role": "system", "content": build_system_prompt(memory_entities, recent_meetings)}
        ] + window

        trace: List[Dict[str, Any]] = []
        references: List[Dict[str, Any]] = []
        artifacts: List[Dict[str, Any]] = []
        seen_refs: set = set()
        seen_artifacts: set = set()
        reply = ""
        steps = 0

        try:
            for step in range(1, MAX_STEPS + 1):
                steps = step
                out = llm.chat(conversation, tools=registry.schemas(), temperature=0.3)
                tool_calls = out.get("tool_calls") or []
                if not tool_calls:
                    reply = (out.get("content") or "").strip()
                    break

                conversation.append(
                    {
                        "role": "assistant",
                        "content": out.get("content") or "",
                        "tool_calls": tool_calls,
                    }
                )
                for call in tool_calls:
                    function = call.get("function") or {}
                    name = function.get("name") or ""
                    raw_args = function.get("arguments") or "{}"
                    started = time.time()
                    try:
                        args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                    except (ValueError, TypeError):
                        args = {}
                        result = {"ok": False, "data": None, "error": "参数不是合法 JSON"}
                    else:
                        # make_ppt 这类工具有自己的写库连接；先提交当前事务释放写锁，
                        # 否则工具插入 ppt_jobs 会撞上 "database is locked"。
                        conn.commit()
                        result = registry.call(name, args)

                    elapsed_ms = int((time.time() - started) * 1000)
                    trace.append(
                        {
                            "step": step,
                            "name": name,
                            "args": args,
                            "ok": bool(result.get("ok")),
                            "error": result.get("error"),
                            "ms": elapsed_ms,
                        }
                    )
                    repo.insert_tool_call(
                        conn,
                        session_id=session_id,
                        name=name,
                        args=args,
                        ok=bool(result.get("ok")),
                        ms=elapsed_ms,
                        result_digest=registry.digest(result),
                    )
                    data = result.get("data") or {}
                    if isinstance(data, dict):
                        for ref in data.get("references") or []:
                            key = (
                                ref.get("chunk_id")
                                or ref.get("url")
                                or ref.get("title")
                                or json.dumps(ref, ensure_ascii=False, sort_keys=True)
                            )
                            if key in seen_refs:
                                continue
                            seen_refs.add(key)
                            references.append(ref)
                        ppt_id = data.get("ppt_id")
                        if ppt_id and data.get("download_url") and ppt_id not in seen_artifacts:
                            seen_artifacts.add(ppt_id)
                            artifacts.append(
                                {
                                    "type": "ppt",
                                    "ppt_id": ppt_id,
                                    "title": data.get("title") or "演示文稿",
                                    "url": data["download_url"],
                                    "pages": data.get("pages"),
                                }
                            )
                    conversation.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.get("id") or "",
                            "content": json.dumps(result, ensure_ascii=False)[:6000],
                        }
                    )
            else:
                # 用完 5 步还在调工具：强制收敛
                out = llm.chat(
                    conversation + [{"role": "user", "content": CONVERGE_HINT}],
                    temperature=0.3,
                )
                reply = (out.get("content") or "").strip()
        except LLMError as exc:
            raise ChatError("模型调用失败：%s" % exc)

        if not reply:
            reply = "（模型没有返回内容，请重试一次。）"

        repo.insert_message(
            conn, session_id=session_id, role="assistant", content=reply, refs=references
        )

        # 记忆写入：从用户这句话里抽实体（失败不拖累对话）
        memory_info: Dict[str, Any] = {"extracted": 0, "added": 0, "merged": 0, "note": ""}
        try:
            from app.memory.extract import extract_chat_entities
            from app.memory.service import MemoryService

            entities, note = extract_chat_entities(llm, text)
            memory_info["extracted"] = len(entities)
            memory_info["note"] = note
            if entities:
                stats = MemoryService(settings).upsert(
                    entities, "message:%s" % user_message_id, conn=conn
                )
                memory_info["added"] = stats["added"]
                memory_info["merged"] = stats["merged"]
        except Exception as exc:  # 记忆是增强，不是前置条件
            memory_info["note"] = "记忆写入失败：%s" % exc

    return {
        "session_id": session_id,
        "reply": reply,
        "references": references,
        "artifacts": artifacts,
        "tool_trace": trace,
        "steps": steps,
        "truncated": truncated,
        "memory": memory_info,
        "mode": getattr(llm, "mode", "unknown"),
    }
