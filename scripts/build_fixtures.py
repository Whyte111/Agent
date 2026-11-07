"""生成 MOCK 模式用的"模型输出"fixture。

为什么需要: 本机没有 LLM key / 没有网络时, 也要能验证三道闸门与发信门控是真实代码。
Mock 只替换"模型会怎么答", 其余全是真代码。fixture 以原文 sha256 命名,
样本文本一改就命中 _default 并在日志里报警——不会静默给出错误结论。

用法: python -m scripts.build_fixtures
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict

from app.ingest.txt import normalize_txt

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "data" / "samples"
OUT = ROOT / "data" / "mock" / "llm" / "meeting_extract"

SPEC: Dict[str, Dict[str, Any]] = {
    # S1: 对外承诺 + 责任人不明 + 截止冲突 → 2 high → 发 1 封
    # 注意：S1 的"无人认领"必须挂在**客户**要求上，否则按 8.2 的 rubric 只是"内部承诺缺负责人"（mid）。
    "S1_高风险.txt": {
        "held_at": "2025-09-18 14:00-15:00",
        "place": "上海张江 3 号楼 5 层会议室",
        "topic": "华东区客户交付复盘会",
        "attendees": ["张伟", "李娜", "王强", "陈静"],
        "action_items": [
            {"owner": "张伟", "action": "给客户演示新版本", "due": "下周一",
             "evidence_quote": "下周一我要去给客户演示新版本"},
            {"owner": None, "action": "搭建客户现场演示环境", "due": "周五前",
             "evidence_quote": "客户要求周五前把现场演示环境搭好"},
            {"owner": "陈静", "action": "把报价单给财务审核", "due": "下周二",
             "evidence_quote": "报价单我这边下周二给到财务审核"},
            {"owner": "王强", "action": "补文档并发群里", "due": "周五前",
             "evidence_quote": "文档我来补，周五前发群里"},
        ],
        "risks": [
            {"basis": "owner", "severity": "high",
             "evidence_quote": "客户要求周五前把现场演示环境搭好，但现在还没有人认领",
             "owner": None, "due": "周五前",
             "suggestion": "明确现场演示环境责任人（候选：王强）并锁定周五前完成"},
            {"basis": "deadline", "severity": "high",
             "evidence_quote": "对外我们说下周一交付，但内部排期写的是下周三上线",
             "suggestion": "对外口径与内部排期二选一，发出前由销售与交付共同确认"},
        ],
    },
    # S2: 每件事都有人、有截止 → 0 风险 → not_needed
    "S2_无风险.txt": {
        "held_at": "2025-09-19 10:00-11:00",
        "place": "线上会议（飞书）",
        "topic": "周例会",
        "attendees": ["李娜", "王强", "陈静", "赵敏"],
        "action_items": [
            {"owner": "王强", "action": "补完文档并发群里", "due": "今天下午",
             "evidence_quote": "文档我今天下午补完并发到群里"},
            {"owner": "陈静", "action": "把预算表发给李娜", "due": "下周三前",
             "evidence_quote": "预算表我下周三前发给李娜"},
            {"owner": "赵敏", "action": "把测试报告给到李娜", "due": "下周一",
             "evidence_quote": "下周一我把测试报告给到李娜"},
        ],
        "risks": [],
    },
    # S3: 3 条责任真空(mid) + 2 条时点缺失(low) + 1 条模型编造的证据(必被闸门①丢弃)
    "S3_边界.txt": {
        "held_at": "2025-09-20 15:30-16:20",
        "place": "北京望京 B 座 7 层会议室",
        "topic": "产品迭代需求评审",
        "attendees": ["赵敏", "孙磊", "周涛", "吴倩"],
        "action_items": [
            {"owner": "孙磊", "action": "给出报表导出技术方案结论", "due": "下周三",
             "evidence_quote": "报表导出的技术方案我下周三给结论"},
        ],
        "risks": [
            {"basis": "owner", "severity": "mid",
             "evidence_quote": "权限模块的测试用例需要有人认领一下，这个还没人负责",
             "suggestion": "指定测试用例负责人"},
            {"basis": "owner", "severity": "mid",
             "evidence_quote": "接口文档得补，但具体谁写还没定",
             "suggestion": "指定接口文档撰写人"},
            {"basis": "owner", "severity": "mid",
             "evidence_quote": "运营活动的方案还没有人牵头，先记一下",
             "suggestion": "指定运营方案牵头人"},
            {"basis": "deadline", "severity": "low",
             "evidence_quote": "权限模块什么时候上线还没排",
             "suggestion": "补充上线时间"},
            {"basis": "deadline", "severity": "low",
             "evidence_quote": "测试大概要做一轮，具体时间到时候再看",
             "suggestion": "补充测试窗口"},
            # 故意编造：原文没有这句话，闸门①必须整条丢弃
            {"basis": "deadline", "severity": "high",
             "evidence_quote": "数据库迁移的时间还没定，下周再看",
             "suggestion": "（这条用来验证证据校验，不应入库）",
             "_expect_dropped": True},
        ],
    },
}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, spec in SPEC.items():
        path = SAMPLES / name
        text = normalize_txt(path.read_bytes())
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()

        quotes = [r["evidence_quote"] for r in spec["risks"]]
        quotes += [a["evidence_quote"] for a in spec["action_items"]]
        for quote in quotes:
            present = quote in text
            expect_drop = any(
                r.get("_expect_dropped") and r["evidence_quote"] == quote
                for r in spec["risks"]
            )
            if expect_drop and present:
                raise SystemExit("fixture 错误: %s 里 '编造' 的证据居然存在于原文" % name)
            if not expect_drop and not present:
                raise SystemExit("fixture 错误: %s 引用的证据不在原文中: %r" % (name, quote))

        payload = {k: v for k, v in spec.items()}
        payload["risks"] = [
            {k: v for k, v in r.items() if not k.startswith("_")} for r in payload["risks"]
        ]
        out = OUT / ("%s.json" % sha)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print("%-16s sha=%s... -> %s" % (name, sha[:12], out.name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
