"""make_ppt 工具：对话里明确要求生成 PPT 时调用。"""
from __future__ import annotations

from typing import Any, Dict, Optional

from app.ppt.builder import run_ppt
from app.tools.registry import Tool

SCHEMA = {
    "type": "object",
    "properties": {
        "topic": {"type": "string", "description": "PPT 主题，例如「星云项目季度汇报」"},
        "audience": {"type": "string", "description": "汇报对象/场合，可省略"},
        "context": {
            "type": "string",
            "description": "可选：要放进 PPT 的事实/数据/要点；基于 PDF 时先把 doc_search 命中内容摘要放这里",
        },
    },
    "required": ["topic"],
}


def make_ppt(
    topic: str,
    audience: str = "",
    context: str = "",
    *,
    settings=None,
    session_id: str = "",
) -> Dict[str, Any]:
    if settings is None:
        raise RuntimeError("make_ppt 缺少 settings")
    request = (topic or "").strip()
    audience = (audience or "").strip()
    context = (context or "").strip()
    if audience:
        request = "%s；汇报对象：%s" % (request, audience)
    if context:
        request = "%s；参考要点：%s" % (request, context)
    result = run_ppt(settings, session_id=session_id, spec=request)
    result["note"] = result.get("note") or ""
    return result


def build(settings, session_id: str = "") -> Tool:
    return Tool(
        name="make_ppt",
        description=(
            "生成一份可下载的 .pptx（固定 5 页：封面 + 背景/核心/计划/总结）。"
            "只在用户明确要求「做 PPT / 生成演示文稿」时调用；"
            "如果用户要求基于已上传的 PDF，先 doc_search，把关键事实摘要放进 context。"
            "调用后把返回的 download_url 原样告诉用户，不要自己编链接。"
        ),
        parameters=SCHEMA,
        func=lambda topic, audience="", context="": make_ppt(
            topic, audience, context, settings=settings, session_id=session_id
        ),
    )
