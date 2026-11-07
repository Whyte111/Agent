"""web_search 工具：把百度千帆 AI 搜索包成对话工具。"""
from __future__ import annotations

from typing import Any, Dict

from app.tools.registry import Tool
from app.tools.baidu_search import DEFAULT_TOP_K, MAX_TOP_K, web_search

SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "搜索词；时间敏感问题保留年份/日期"},
        "top_k": {
            "type": "integer",
            "description": "返回结果数，默认 5，最多 8",
        },
    },
    "required": ["query"],
}


def search(
    query: str, top_k: int = DEFAULT_TOP_K, *, settings=None
) -> Dict[str, Any]:
    if settings is None:
        raise RuntimeError("web_search 缺少 settings")
    result = web_search(query, top_k, settings=settings)
    result["top_k"] = max(1, min(int(top_k or DEFAULT_TOP_K), MAX_TOP_K))
    return result


def build(settings) -> Tool:
    return Tool(
        name="web_search",
        description=(
            "联网搜索时效性信息（今天/最近/最新/新闻/价格/天气等）。"
            "返回 title、url、date、snippet；回答必须带 URL。"
            "如果返回 cached=true，要明确告诉用户这是缓存结果、可能不是最新。"
        ),
        parameters=SCHEMA,
        func=lambda query, top_k=DEFAULT_TOP_K: search(query, top_k, settings=settings),
    )
