"""PPT 接口：生成与下载。只做校验、序列化，生成逻辑在 app.ppt.builder。"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Dict

from app.api.server import (
    Request,
    Response,
    check_session_id,
    error_response,
    json_response,
)
from app.config.settings import load_settings
from app.ppt.builder import PptError, run_ppt
from app.store import repo

PPT_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
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


def handle_ppt(req: Request) -> Response:
    settings = load_settings()
    if "application/json" not in req.header("content-type").lower():
        return error_response("Content-Type 必须是 application/json", 415)
    if len(req.body) > MAX_JSON_BYTES:
        return error_response("PPT 请求体超过 256KB", 413)
    try:
        payload = _json_body(req)
    except ValueError as exc:
        return error_response(str(exc), 400)

    spec = payload.get("spec")
    if spec is None:
        return error_response("缺少 spec（PPT 主题或大纲对象）", 400)
    session_id = str(payload.get("session_id") or "").strip()
    if not session_id:
        session_id = "sess-" + uuid.uuid4().hex[:8]
    try:
        session_id = check_session_id(session_id)
    except ValueError as exc:
        return error_response(str(exc), 400)

    try:
        result = run_ppt(settings, session_id=session_id, spec=spec)
    except PptError as exc:
        message = str(exc)
        status = 400 if ("主题" in message or "spec" in message) else 500
        return error_response(message, status)
    except Exception as exc:  # 未预期故障：不白屏
        return error_response("PPT 生成失败：%s: %s" % (type(exc).__name__, exc), 502)
    return json_response(result)


def handle_file(req: Request) -> Response:
    settings = load_settings()
    ppt_id = req.params["ppt_id"]
    with repo.db(settings.db_path) as conn:
        job = repo.get_ppt_job(conn, ppt_id)
    if job is None:
        return error_response("PPT 不存在: %s" % ppt_id, 404)

    path = Path(job["path"]).resolve()
    generated = settings.generated_dir.resolve()
    try:
        path.relative_to(generated)  # 防穿越：只允许下载 generated/ 下的文件
    except ValueError:
        return error_response("文件路径不合法", 403)
    if not path.is_file():
        return error_response("PPT 文件已丢失: %s" % ppt_id, 404)

    return Response(
        status=200,
        body=path.read_bytes(),
        content_type=PPT_MIME,
        headers={
            "Content-Disposition": 'attachment; filename="aiwainao-%s.pptx"' % ppt_id,
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )
