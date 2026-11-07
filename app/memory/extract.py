"""从对话里抽取四类实体（人物 / 时间 / 地点 / 主题）。

只抽"明确出现"的，不推测；抽不到就返回空列表。
模型不可用 / 超时 / 结构化失败 → 返回空 + 原因，不影响主对话（记忆是增强，不是前置条件）。
"""
from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Tuple

CHAT_SYSTEM = """你是信息抽取器。从用户这句话里抽取四类实体：人物(person)、时间(time)、地点(place)、主题(topic)。

规则：
1. 只抽这句话里**明确出现**的实体，不要推测、不要补全、不要联系上文。
2. 用户自己说的话也算事实来源（例如"我叫张伟"→ person 张伟）。
   如果是用户自述本人身份（"我叫…"/"我是…"），给该 person 设 is_self=true。
3. 普通名词不算主题；主题是"在谈什么事的短语"，例如"华东区交付""权限模块改版"。
4. 抽不到就返回空数组。宁可少抽，不要编。"""

CHAT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["entities"],
    "properties": {
        "entities": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["type", "name"],
                "properties": {
                    "type": {"type": "string", "enum": ["person", "time", "place", "topic"]},
                    "name": {"type": "string"},
                    "detail": {"type": ["string", "null"]},
                    "is_self": {
                        "type": "boolean",
                        "description": "仅对 person 有效：这句话是用户自述本人身份（如「我叫…」「我是…」）时为 true",
                    },
                },
            },
        }
    },
}

PROMPT = "用户消息：\n{text}\n\n请输出实体列表。"

MIN_CHARS = 6  # 太短的消息（"好的"）不值得花一次模型调用


def extract_chat_entities(llm: Any, text: str) -> Tuple[List[Dict[str, Any]], str]:
    """返回 (entities, note)。note 非空表示这次抽取被跳过或失败。"""
    text = (text or "").strip()
    if len(text) < MIN_CHARS:
        return [], "消息过短，跳过抽取"
    try:
        data = llm.structured(
            PROMPT.format(text=text),
            CHAT_SCHEMA,
            name="chat_entities",
            system=CHAT_SYSTEM,
            fixture_key=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )
    except Exception as exc:  # 模型故障 / 无 fixture：记忆不拖累对话
        return [], "实体抽取失败：%s" % exc

    entities: List[Dict[str, Any]] = []
    for item in data.get("entities") or []:
        entity_type = (item.get("type") or "").strip()
        name = (item.get("name") or "").strip()
        if not name or entity_type not in ("person", "time", "place", "topic"):
            continue
        detail = (item.get("detail") or "").strip()
        attrs: Dict[str, Any] = {}
        if detail:
            attrs["detail"] = detail
        if entity_type == "person" and item.get("is_self") is True:
            attrs["self"] = True
        entities.append({"type": entity_type, "name": name, "attrs": attrs})
    return entities, ""
