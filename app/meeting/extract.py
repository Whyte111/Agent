"""会议原文 → 结构化抽取（LLM 只负责"判什么"和"给证据"）。

这里只做 prompt 与输出规整；证据真实性 / 枚举合法性 / 是否发信在 rules.py。
"""
from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional

# 8.2 的判定标准 + 8.6 的明确不判，原样写进 prompt（给例子优于给规则）
SYSTEM_PROMPT = """你是会议风控助手。你的任务是判断会议记录里"承诺过的动作是否闭环"。

风险定义：会上承诺了要做某件事，但"谁做 / 何时完成"至少缺一个。
三条件缺一不可：有承诺、缺闭环、有原文证据。缺一条都不算风险。

算风险（示例）：
- "那谁跟进一下环境？"          —— 承诺了，但没人认领
- "下周一交付" vs "下周三上线"  —— 同一件事两个时间（时间冲突）

不算风险（示例）：
- "张伟下周一给客户演示"        —— 人、时间、事都齐了
- "这个方案再讨论一下"          —— 没有承诺，只是讨论

明确不判：情绪冲突 / 人身攻击、战略方向分歧、会上没提的事、
需要行业知识才能判断的事、纯技术方案优劣。

硬性要求：
1. evidence_quote 必须是从原文逐字复制的句子，不得改写、不得拼接、不得加省略号。
2. 判不出原文句子就不要输出这条风险。
3. severity: high=对外承诺且闭环要素缺失或时间冲突；mid=内部承诺但缺负责人或时间；
   low=表述含糊、信息不足。只允许 high / mid / low。
4. basis: owner(缺责任人) / deadline(截止冲突或缺失) / action(动作本身不明确)。
5. 每条风险如果原文里**明确写出了**截止时间或负责人，请填进 due / owner；
   原文没有就填 null。不要推测、不要补全。
"""

EXTRACT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["action_items", "risks"],
    "properties": {
        "held_at": {"type": ["string", "null"]},
        "place": {"type": ["string", "null"]},
        "attendees": {"type": "array", "items": {"type": "string"}},
        "topic": {"type": ["string", "null"]},
        "action_items": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["action", "evidence_quote"],
                "properties": {
                    "owner": {"type": ["string", "null"]},
                    "action": {"type": "string"},
                    "due": {"type": ["string", "null"]},
                    "evidence_quote": {"type": "string"},
                },
            },
        },
        "risks": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["basis", "severity", "evidence_quote"],
                "properties": {
                    "basis": {"type": "string", "enum": ["owner", "deadline", "action"]},
                    "severity": {"type": "string", "enum": ["high", "mid", "low"]},
                    "evidence_quote": {"type": "string"},
                    "suggestion": {"type": ["string", "null"]},
                    "owner": {"type": ["string", "null"]},
                    "due": {"type": ["string", "null"]},
                },
            },
        },
    },
}

PROMPT_TEMPLATE = """下面是一份会议原文。请抽取会议元信息、行动项、以及按上述标准算作风险的项目。

=== 会议原文开始 ===
{text}
=== 会议原文结束 ===
"""


def build_prompt(text: str) -> str:
    return PROMPT_TEMPLATE.format(text=text)


def _s(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _str_list(value: Any) -> List[str]:
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    out: List[str] = []
    for item in items:
        text = _s(item)
        if text:
            out.append(text)
    return out


def normalize_extraction(data: Dict[str, Any]) -> Dict[str, Any]:
    """把模型输出规整成内部统一形状（不做判定）。"""
    action_items = []
    for item in data.get("action_items") or []:
        if not isinstance(item, dict):
            continue
        action = _s(item.get("action"))
        quote = _s(item.get("evidence_quote"))
        if not action or not quote:
            continue
        action_items.append(
            {
                "owner": _s(item.get("owner")),
                "action": action,
                "due": _s(item.get("due")),
                "evidence_quote": quote,
            }
        )

    risks = []
    for item in data.get("risks") or []:
        if not isinstance(item, dict):
            continue
        quote = _s(item.get("evidence_quote"))
        if not quote:
            continue
        risks.append(
            {
                "basis": _s(item.get("basis")),
                "severity": _s(item.get("severity")),
                "evidence_quote": quote,
                "suggestion": _s(item.get("suggestion")),
                "owner": _s(item.get("owner")),
                "due": _s(item.get("due")),
                "raw_severity": _s(item.get("severity")),
                "raw_basis": _s(item.get("basis")),
            }
        )

    return {
        "held_at": _s(data.get("held_at")),
        "place": _s(data.get("place")),
        "topic": _s(data.get("topic")),
        "attendees": _str_list(data.get("attendees")),
        "action_items": action_items,
        "risks": risks,
    }


def extract_meeting(llm: Any, text: str) -> Dict[str, Any]:
    data = llm.structured(
        build_prompt(text),
        EXTRACT_SCHEMA,
        name="meeting_extract",
        system=SYSTEM_PROMPT,
        fixture_key=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )
    return normalize_extraction(data)
