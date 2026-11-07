"""会议链（骨架第 6 节 B）：上传 → 抽取 → 三闸门 → 待办 → 幂等 → 发信 → send_log。

分界线：LLM 负责"判什么 + 给证据"；证据真实性 / 枚举合法性 / 是否发信由代码决定。
"""
from __future__ import annotations

import hashlib
import html as html_lib
import re
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config.settings import Settings
from app.ingest.txt import IngestError, normalize_txt
from app.llm.client import get_llm
from app.mail.sender import Mailer, SendResult
from app.memory.service import MemoryService, meeting_entities
from app.meeting import extract as extract_mod
from app.meeting import rules
from app.store import repo

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_RECIPIENTS = 5
MAX_RECIPIENT_CHARS = 254
MAX_MEETING_CHARS = 30000
STALE_SEND_MINUTES = 10
# 同一进程内串行化“检查 sent_log → 发送 → 落日志”，避免并发重复发信。
_SEND_LOCK = threading.Lock()
_MEETING_LOCK = threading.Lock()


class MeetingError(ValueError):
    """会议链的可预期错误（空文件 / 收件人不合法等），API 层映射成 4xx。"""


def parse_recipients(raw: str) -> List[str]:
    parts = re.split(r"[,;\s]+", raw or "")
    recipients = [p.strip() for p in parts if p.strip()]
    if len(recipients) > MAX_RECIPIENTS:
        raise MeetingError("收件人最多 %d 个" % MAX_RECIPIENTS)
    for addr in recipients:
        if len(addr) > MAX_RECIPIENT_CHARS:
            raise MeetingError("收件人邮箱过长")
        if not _EMAIL_RE.match(addr):
            raise MeetingError("收件人邮箱不合法: %s" % addr)
    return recipients


def save_upload(settings: Settings, raw: bytes, suffix: str = ".txt") -> Path:
    """落盘名由服务端生成（骨架 7 节：不用用户文件名，防穿越）。"""
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    path = settings.upload_dir / ("%s%s" % (uuid.uuid4().hex, suffix))
    path.write_bytes(raw)
    return path


def _build_mail(
    meeting: Dict[str, Any], high_risks: List[Dict[str, Any]], todos: List[Dict[str, Any]]
) -> Dict[str, str]:
    topic = meeting.get("topic") or "未命名会议"
    subject = "[AI 外脑] 会议风险提醒：%s（%d 项高风险）" % (topic, len(high_risks))

    lines = ["AI 外脑 · 会议风险提醒", "", "会议主题：%s" % topic]
    if meeting.get("held_at"):
        lines.append("会议时间：%s" % meeting["held_at"])
    if meeting.get("place"):
        lines.append("会议地点：%s" % meeting["place"])
    if meeting.get("attendees"):
        lines.append("参会人：%s" % "、".join(meeting["attendees"]))
    lines += ["", "高风险项（%d 条）：" % len(high_risks)]
    for i, risk in enumerate(high_risks, 1):
        lines.append("%d. [%s] %s" % (i, risk["basis"], risk["evidence_quote"]))
        if risk.get("suggestion"):
            lines.append("   建议：%s" % risk["suggestion"])
    lines += ["", "待办（%d 条）：" % len(todos)]
    for i, todo in enumerate(todos, 1):
        lines.append(
            "%d. %s | %s | 截止：%s"
            % (i, todo.get("owner") or "未指定负责人", todo["action"], todo.get("due") or "未定")
        )
    lines += ["", "—— 本邮件由规则自动触发（severity=high 且证据已校验）。"]
    text_body = "\n".join(lines)

    rows = "".join(
        "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
        % (
            html_lib.escape(r["basis"]),
            html_lib.escape(r["severity"]),
            html_lib.escape(r["evidence_quote"]),
            html_lib.escape(r.get("suggestion") or ""),
        )
        for r in high_risks
    )
    todo_rows = "".join(
        "<tr><td>%s</td><td>%s</td><td>%s</td></tr>"
        % (
            html_lib.escape(t.get("owner") or "未指定负责人"),
            html_lib.escape(t["action"]),
            html_lib.escape(t.get("due") or "未定"),
        )
        for t in todos
    )
    html_body = """<html><body style="font-family:system-ui,sans-serif">
<h2>AI 外脑 · 会议风险提醒</h2>
<p><b>会议主题：</b>{topic}<br/><b>时间：</b>{held}<br/><b>地点：</b>{place}</p>
<h3>高风险项</h3>
<table border="1" cellpadding="6" cellspacing="0">
<tr><th>类型</th><th>级别</th><th>原文证据</th><th>建议</th></tr>{rows}</table>
<h3>待办</h3>
<table border="1" cellpadding="6" cellspacing="0">
<tr><th>负责人</th><th>动作</th><th>截止</th></tr>{todo_rows}</table>
<p style="color:#888">本邮件由规则自动触发（severity=high 且证据已校验）。</p>
</body></html>""".format(
        topic=html_lib.escape(topic),
        held=html_lib.escape(str(meeting.get("held_at") or "-")),
        place=html_lib.escape(str(meeting.get("place") or "-")),
        rows=rows,
        todo_rows=todo_rows,
    )
    return {"subject": subject, "text": text_body, "html": html_body}


def _run_meeting(
    settings: Settings,
    *,
    session_id: str,
    filename: str,
    raw: bytes,
    mail_to: str,
    llm: Any = None,
    mailer: Optional[Mailer] = None,
) -> Dict[str, Any]:
    if len(raw) > settings.max_upload_bytes:
        raise MeetingError("文件超过上限 %d MB" % (settings.max_upload_bytes // (1024 * 1024)))
    if not (filename or "").lower().endswith(".txt"):
        raise MeetingError("会议原文只接受 .txt 文件")

    try:
        text = normalize_txt(raw)
    except IngestError as exc:
        raise MeetingError(str(exc))
    if len(text) > MAX_MEETING_CHARS:
        raise MeetingError(
            "会议原文过长（%d 字，上限 %d 字），请拆分后再上传"
            % (len(text), MAX_MEETING_CHARS)
        )

    recipients = parse_recipients(mail_to)
    if not recipients:
        raise MeetingError("请填写收件人邮箱（骨架 U3：收件人由上传表单填写）")

    sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    dedupe_key = "meeting:%s" % sha256
    llm = llm or get_llm(settings)
    mailer = mailer or Mailer(settings)
    memory_service = MemoryService(settings)
    memory_stats: Dict[str, Any] = {"added": 0, "merged": 0, "skipped": 0}
    saved_path = save_upload(settings, raw)

    with repo.db(settings.db_path) as conn:
        doc = repo.find_doc_by_sha(conn, sha256)
        if doc and doc["kind"] != "meeting":
            raise MeetingError("该文件内容已作为 %s 上传过，不能按会议原文处理" % doc["kind"])

        meeting: Optional[Dict[str, Any]] = None
        if doc:
            meeting = repo.get_meeting_by_doc(conn, doc["id"])
        reused = meeting is not None

        extraction: Dict[str, Any] = {"held_at": None, "place": None, "topic": None, "attendees": [], "action_items": []}
        if reused:
            doc_id = doc["id"]
            meeting_id = meeting["id"]
            kept = [
                {
                    "id": r["id"],
                    "basis": r["basis"],
                    "severity": r["severity"],
                    "evidence_quote": r["evidence_quote"],
                    "suggestion": r["suggestion"],
                    "evidence_tier": "stored",
                }
                for r in repo.risks_for(conn, meeting_id)
            ]
            dropped: List[Dict[str, Any]] = []
            notes: List[str] = ["重复上传：复用已有会议与风险结论，未重新调用模型（幂等）"]
        else:
            # ---- ① 抽取（LLM）----
            extraction = extract_mod.extract_meeting(llm, text)
            # ---- ② 闸门：证据校验 + 枚举校验（代码）----
            gated = rules.apply_gates(extraction["risks"], text)
            kept, dropped, notes = gated["kept"], gated["dropped"], gated["notes"]

            if doc is None:
                doc_id = repo.insert_doc(
                    conn,
                    session_id=session_id,
                    kind="meeting",
                    path=str(saved_path),
                    sha256=sha256,
                )
            else:
                doc_id = doc["id"]
            meeting_id = repo.insert_meeting(
                conn,
                doc_id=doc_id,
                session_id=session_id,
                held_at=extraction["held_at"],
                place=extraction["place"],
                attendees=extraction["attendees"],
                topic=extraction["topic"],
                raw_text=text,
            )
            meeting = {
                "id": meeting_id,
                "held_at": extraction["held_at"],
                "place": extraction["place"],
                "attendees": extraction["attendees"],
                "topic": extraction["topic"],
            }

            # ---- 写记忆（四类实体，全局跨会话）----
            memory_stats = memory_service.upsert(
                meeting_entities(extraction), "meeting:%s" % meeting_id, conn=conn
            )

            # ---- ③ 待办生成（high/mid 才有）----
            for risk in kept:
                risk_id = repo.insert_risk(
                    conn,
                    meeting_id=meeting_id,
                    basis=risk["basis"],
                    severity=risk["severity"],
                    evidence_quote=risk["evidence_quote"],
                    suggestion=risk.get("suggestion"),
                )
                risk["id"] = risk_id
                if rules.needs_todo(risk["severity"]):
                    owner, action, due = _todo_fields(risk, extraction["action_items"])
                    repo.insert_todo(
                        conn, risk_id=risk_id, owner=owner, action=action, due=due
                    )

        # 风险结论与待办先提交；发信在事务外，邮件失败不影响已生成的结论。
        high_risks = [r for r in kept if r["severity"] == "high"]
        todos = repo.todos_for(conn, meeting_id)

    mail_status: str
    mail_detail = ""
    if not high_risks:
        mail_status = "not_needed"
        mail_detail = "无 high 风险，按闸门③不发信"
    else:
        # 先占住 dedupe_key（DB 唯一约束），再在事务外发信；即使多进程并发，
        # 也只有一个请求能拿到 claim。进程崩溃留下的 sending 行由超时回收。
        with _SEND_LOCK:
            with repo.db(settings.db_path) as conn:
                already_sent = repo.sent_log_for(conn, meeting_id) is not None
                cutoff = (
                    datetime.now().astimezone() - timedelta(minutes=STALE_SEND_MINUTES)
                ).isoformat(timespec="seconds")
                repo.cleanup_stale_sends(conn, cutoff)

            if already_sent:
                mail_status = "skipped_duplicate"
                mail_detail = "同内容已成功发送过，dedupe_key=%s" % dedupe_key
            else:
                try:
                    payload = _build_mail(meeting, high_risks, todos)
                except Exception as exc:
                    payload = {"subject": "[AI 外脑] 会议风险提醒（发送失败）"}
                    send = SendResult(
                        "failed", "构建邮件失败：%s: %s" % (type(exc).__name__, exc), recipients
                    )
                else:
                    send = None

                claim_id = None
                claim_error = False
                try:
                    with repo.db(settings.db_path) as conn:
                        claim_id = repo.claim_send_log(
                            conn,
                            meeting_id=meeting_id,
                            dedupe_key=dedupe_key,
                            recipients=recipients,
                            subject=payload["subject"],
                        )
                except Exception as exc:
                    claim_error = True
                    mail_status = "failed"
                    mail_detail = "发送锁写入失败：%s: %s" % (type(exc).__name__, exc)

                if claim_id is None:
                    if not claim_error:
                        mail_status = "skipped_duplicate"
                        mail_detail = "同一 dedupe_key 正在发送或已发送，跳过"
                else:
                    if send is None:
                        try:
                            send = mailer.send(
                                recipients,
                                payload["subject"],
                                payload["text"],
                                payload["html"],
                            )
                        except Exception as exc:  # 发信失败不能拖垮风险结论
                            send = SendResult(
                                "failed",
                                "发送异常：%s: %s" % (type(exc).__name__, exc),
                                recipients,
                            )

                    mail_status = send.status
                    mail_detail = send.detail
                    try:
                        with repo.db(settings.db_path) as conn:
                            repo.finish_send_log(
                                conn,
                                claim_id,
                                status=send.status,
                                dedupe_key=dedupe_key if send.status == "sent" else None,
                                error=None if send.status != "failed" else send.detail,
                            )
                            _sync_todo_status(conn, todos, send.status)
                    except Exception as exc:
                        # 邮件可能已经发出，但不能因此丢掉已提交的风险结论。
                        mail_detail += "；send_log 写入失败：%s: %s" % (
                            type(exc).__name__,
                            exc,
                        )

    with repo.db(settings.db_path) as conn:
        bundle = repo.meeting_bundle(conn, meeting_id)

    return {
        "session_id": session_id,
        "meeting_id": meeting_id,
        "doc_id": doc_id,
        "sha256": sha256,
        "dedupe_key": dedupe_key,
        "reused": reused,
        "extraction": extraction,
        "gates": {
            "kept": len(kept),
            "dropped": len(dropped),
            "dropped_items": dropped,
            "notes": notes,
        },
        "memory": memory_stats,
        "meeting": {
            "held_at": bundle["meeting"].get("held_at"),
            "place": bundle["meeting"].get("place"),
            "attendees": bundle["meeting"].get("attendees"),
            "topic": bundle["meeting"].get("topic"),
        },
        "risks": kept,
        "todos": bundle["todos"],
        "mail_status": mail_status,
        "mail_detail": mail_detail,
        "mail_mode": mailer.mode,
        "send_logs": bundle["send_logs"],
    }


def run_meeting(
    settings: Settings,
    *,
    session_id: str,
    filename: str,
    raw: bytes,
    mail_to: str,
    llm: Any = None,
    mailer: Optional[Mailer] = None,
) -> Dict[str, Any]:
    """单进程内串行化会议上传，避免同 sha256 并发插入撞唯一约束。"""
    with _MEETING_LOCK:
        return _run_meeting(
            settings,
            session_id=session_id,
            filename=filename,
            raw=raw,
            mail_to=mail_to,
            llm=llm,
            mailer=mailer,
        )


_SPEAKER_PREFIX = re.compile(r"^[\u4e00-\u9fa5A-Za-z][\u4e00-\u9fa5A-Za-z0-9]{0,5}[:：]")
_NORM_STRIP = re.compile(r"[\s，。、！？；：,\.!\?;:\"'“”‘’（）\(\)《》<>【】\[\]…—\-~·`]+")


def _norm_quote(text: str) -> str:
    """去说话人前缀 + 去标点空白：模型同一句话两次引用常差个冒号或句号。"""
    value = _SPEAKER_PREFIX.sub("", (text or "").strip())
    return _NORM_STRIP.sub("", value).lower()


def _todo_fields(
    risk: Dict[str, Any], action_items: List[Dict[str, Any]]
) -> tuple:
    """把风险挂到对应的行动项上，尽量补齐 谁 / 何时 / 做什么。

    匹配顺序：原文相等 → 归一化相等 → 归一化互相包含（≥6 字）。
    匹配上就用行动项的 owner/action/due，并用风险自带的 owner/due 兜底补齐；
    都对不上时保留风险自己带的字段，仍然没有就留空——这是**诚实结果**：
    这条风险本身就是"没人认领"。
    """
    quote = risk["evidence_quote"]
    target = _norm_quote(quote)
    risk_owner = risk.get("owner")
    risk_due = risk.get("due")

    def merge(item: Dict[str, Any]) -> tuple:
        return (
            item.get("owner") or risk_owner,
            item["action"],
            item.get("due") or risk_due,
        )

    for item in action_items:  # ① 原文相等
        if item["evidence_quote"] == quote:
            return merge(item)

    for item in action_items:  # ② 归一化相等
        if target and _norm_quote(item["evidence_quote"]) == target:
            return merge(item)

    for item in action_items:  # ③ 归一化互相包含
        item_norm = _norm_quote(item["evidence_quote"])
        if len(item_norm) < 6 or not target:
            continue
        if target in item_norm or item_norm in target:
            return merge(item)

    return risk_owner, risk.get("suggestion") or quote, risk_due


def _sync_todo_status(conn, todos: List[Dict[str, Any]], status: str) -> None:
    mapping = {"sent": "sent", "failed": "failed"}
    target = mapping.get(status)
    if target is None:  # dry_run：邮件未真正发出，待办保持 pending
        return
    for todo in todos:
        repo.update_todo_status(conn, todo["id"], target)
