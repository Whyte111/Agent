"""记忆查询接口：给页面和验收提供可核对的实体清单。

作用域说明（骨架 U1）：记忆全局跨会话，session_id 只用于展示上下文。
"""
from __future__ import annotations

from app.api.server import Request, Response, check_session_id, error_response, json_response
from app.config.settings import load_settings
from app.memory.service import MemoryService, VALID_TYPES
from app.store import repo


def handle_memory(req: Request) -> Response:
    settings = load_settings()
    session_id = req.params["session_id"]
    try:
        session_id = check_session_id(session_id)
    except ValueError as exc:
        return error_response(str(exc), 400)
    raw_types = req.query_one("types", "")
    types = [t.strip() for t in raw_types.split(",") if t.strip()] or None
    if types:
        types = [t for t in types if t in VALID_TYPES] or None

    service = MemoryService(settings)
    entities = service.recall("", types, limit=50)
    with repo.db(settings.db_path) as conn:
        for item in entities:
            item["sources"] = repo.entity_sources(conn, item["id"])
        events = repo.recent_meetings(conn, limit=5)

    return json_response(
        {
            "session_id": session_id,
            "types": types or list(VALID_TYPES),
            "entities": entities,
            "events": events,
            "scope": "global",  # U1：跨会话共享
        }
    )
