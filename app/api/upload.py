"""上传接口：会议 TXT（M0）与后续 PDF（M1）。只做校验 + 序列化，业务在 meeting.pipeline。"""
from __future__ import annotations

import uuid

from app.api.server import (
    Request,
    Response,
    check_session_id,
    error_response,
    json_response,
    parse_multipart,
)
from app.config.settings import load_settings
from app.ingest.pdf import PdfError, PdfLimitError, ScannedPdfError
from app.ingest.pipeline import run_pdf_ingest
from app.meeting.pipeline import MeetingError, run_meeting
from app.store import repo


def handle_upload_pdf(req: Request) -> Response:
    """PDF → 全局知识库。413 用于超页/超大，扫描件单独给 code。"""
    settings = load_settings()
    content_type = req.header("content-type")
    if "multipart/form-data" not in content_type:
        return error_response("请使用 multipart/form-data 上传", 415)
    try:
        parts = parse_multipart(req.body, content_type)
    except ValueError as exc:
        return error_response("表单解析失败: %s" % exc, 400)

    file_part = parts.get("file")
    if file_part is None or not file_part.filename:
        return error_response("缺少文件字段 file", 400)

    session_id = (
        parts["session_id"].data.decode("utf-8", "replace") if "session_id" in parts else ""
    ).strip()
    session_id = session_id or "sess-" + uuid.uuid4().hex[:8]
    try:
        session_id = check_session_id(session_id)
    except ValueError as exc:
        return error_response(str(exc), 400)

    try:
        result = run_pdf_ingest(
            settings,
            session_id=session_id,
            filename=file_part.filename,
            raw=file_part.data,
        )
    except PdfLimitError as exc:
        return error_response(str(exc), 413)
    except ScannedPdfError as exc:
        return error_response(str(exc), 400, code="scanned_pdf")
    except PdfError as exc:
        return error_response(str(exc), 400)
    except Exception as exc:  # IO / 索引故障：不白屏，给出可读原因
        return error_response("处理失败：%s: %s" % (type(exc).__name__, exc), 502)
    return json_response(result)


def handle_upload_meeting(req: Request) -> Response:
    settings = load_settings()
    content_type = req.header("content-type")
    if "multipart/form-data" not in content_type:
        return error_response("请使用 multipart/form-data 上传", 415)
    try:
        parts = parse_multipart(req.body, content_type)
    except ValueError as exc:
        return error_response("表单解析失败: %s" % exc, 400)

    file_part = parts.get("file")
    if file_part is None or not file_part.filename:
        return error_response("缺少文件字段 file", 400)

    session_id = (parts["session_id"].data.decode("utf-8", "replace") if "session_id" in parts else "").strip()
    session_id = session_id or "sess-" + uuid.uuid4().hex[:8]
    try:
        session_id = check_session_id(session_id)
    except ValueError as exc:
        return error_response(str(exc), 400)
    mail_to = (parts["mail_to"].data.decode("utf-8", "replace") if "mail_to" in parts else "").strip()
    if not mail_to:
        return error_response("请填写收件人邮箱（骨架 U3：收件人由上传表单填写）", 400)

    try:
        result = run_meeting(
            settings,
            session_id=session_id,
            filename=file_part.filename,
            raw=file_part.data,
            mail_to=mail_to,
        )
    except MeetingError as exc:
        return error_response(str(exc), 400)
    except Exception as exc:  # LLM / IO 故障：不白屏，给出可读原因
        return error_response("处理失败：%s: %s" % (type(exc).__name__, exc), 502)
    return json_response(result)


def handle_meeting_detail(req: Request) -> Response:
    settings = load_settings()
    meeting_id = req.params["meeting_id"]
    try:
        with repo.db(settings.db_path) as conn:
            bundle = repo.meeting_bundle(conn, meeting_id)
    except KeyError:
        return error_response("会议不存在: %s" % meeting_id, 404)
    meeting = bundle["meeting"]
    meeting.pop("raw_text", None)  # 原文太长，不整段回传；证据已逐条给出
    return json_response(
        {
            "meeting": meeting,
            "risks": bundle["risks"],
            "todos": bundle["todos"],
            "send_logs": bundle["send_logs"],
        }
    )
