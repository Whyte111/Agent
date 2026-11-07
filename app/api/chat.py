"""对话接口：只做校验与序列化，循环在 orchestrator.loop。"""
from __future__ import annotations

import json
import uuid
from typing import Any, Dict

from app.api.server import Request, Response, check_session_id, error_response, json_response
from app.config.settings import load_settings
from app.orchestrator.loop import ChatError, run_chat
from app.store import repo

MAX_JSON_BYTES = 256 * 1024


def _json_body(req: Request) -> Dict[str, Any]:
    if not req.body:
        raise ValueError("请求体为空")
    try:
        data = json.loads(req.body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ValueError("请求体不是合法 JSON")
    if not isinstance(data, dict):
        raise ValueError("请求体必须是 JSON 对象")
    return data


def handle_chat(req: Request) -> Response:
    settings = load_settings()
    if "application/json" not in req.header("content-type").lower():
        return error_response("Content-Type 必须是 application/json", 415)
    if len(req.body) > MAX_JSON_BYTES:
        return error_response("请求体超过 256KB", 413)
    try:
        payload = _json_body(req)
    except ValueError as exc:
        return error_response(str(exc), 400)

    text = str(payload.get("text") or "").strip()
    session_id = str(payload.get("session_id") or "").strip()
    if not session_id:
        session_id = "sess-" + uuid.uuid4().hex[:8]
    try:
        session_id = check_session_id(session_id)
    except ValueError as exc:
        return error_response(str(exc), 400)

    try:
        result = run_chat(settings, session_id=session_id, text=text)
    except ChatError as exc:
        message = str(exc)
        status = 400 if ("为空" in message or "过长" in message or "缺少" in message) else 502
        return error_response(message, status)
    return json_response(result)


def handle_messages(req: Request) -> Response:
    settings = load_settings()
    session_id = req.params["session_id"]
    try:
        session_id = check_session_id(session_id)
    except ValueError as exc:
        return error_response(str(exc), 400)
    limit = 50
    with repo.db(settings.db_path) as conn:
        messages = repo.recent_messages(conn, session_id, limit=limit)
    return json_response({"session_id": session_id, "messages": messages})
