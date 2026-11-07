"""memory_read 工具：读取结构化长期记忆（人物 / 时间 / 地点 / 主题，全局跨会话）。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.memory.service import MemoryService
from app.tools.registry import Tool

SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "要查的实体名或关键词；传空字符串表示列出最近实体"},
        "types": {
            "type": "array",
            "items": {"type": "string", "enum": ["person", "time", "place", "topic"]},
            "description": "限定实体类型；不填则四类都查",
        },
        "limit": {"type": "integer", "description": "最多返回条数，默认 10"},
    },
    "required": ["query"],
}


def memory_read(
    query: str,
    types: Optional[List[str]] = None,
    limit: int = 10,
    *,
    settings=None,
) -> Dict[str, Any]:
    if settings is None:
        raise RuntimeError("memory_read 缺少 settings")
    limit = max(1, min(int(limit or 10), 50))
    keyword = (query or "").strip()[:200]
    entities = MemoryService(settings).recall(keyword, types, limit)
    return {"entities": entities, "count": len(entities), "query": keyword}


def build(settings) -> Tool:
    return Tool(
        name="memory_read",
        description=(
            "查询长期记忆里的结构化实体（人物 / 时间 / 地点 / 主题），跨会话共享。"
            "当用户问到之前的会议、之前聊过的人或事时先调用它；query 传空字符串可列出最近实体。"
            "没有命中就是没有，不要编。"
        ),
        parameters=SCHEMA,
        func=lambda query, types=None, limit=10: memory_read(
            query, types, limit, settings=settings
        ),
    )
