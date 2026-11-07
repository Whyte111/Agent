"""安全修复自检（离线）：CSRF / Content-Type / 发信上限 / TLS / 失败隔离。

用法: python -m scripts.selfcheck_security
"""
from __future__ import annotations

import dataclasses
import http.client
import json
import ssl
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import List

ROOT = Path(__file__).resolve().parents[1]

from app.api import chat, ppt, upload  # noqa: E402
from app.api.server import Application, Handler, Request, build_server, check_session_id  # noqa: E402
from app.config.settings import settings as base_settings  # noqa: E402
from app.mail.sender import Mailer  # noqa: E402
from app.meeting.pipeline import (  # noqa: E402
    MAX_MEETING_CHARS,
    MAX_RECIPIENTS,
    MeetingError,
    parse_recipients,
    run_meeting,
)
from app.store import repo  # noqa: E402

RESULTS: List[tuple] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((ok, name, detail))


def _server(settings):
    app = Application(settings)
    handler_cls = type("SecHandler", (Handler,), {"app": app})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _post(port: int, origin: str, body: bytes = b"{}", content_type: str = "application/json",
          fetch_site: str = ""):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.putrequest("POST", "/api/chat")
    conn.putheader("Host", "127.0.0.1:%d" % port)
    conn.putheader("Origin", origin)
    conn.putheader("Content-Type", content_type)
    if fetch_site:
        conn.putheader("Sec-Fetch-Site", fetch_site)
    conn.putheader("Content-Length", str(len(body)))
    conn.endheaders()
    conn.send(body)
    resp = conn.getresponse()
    payload = resp.read()
    conn.close()
    return resp.status, payload


class SecFakeLLM:
    mode = "fake"

    def structured(self, prompt, schema, **kwargs):
        return {
            "held_at": "2026-11-08",
            "place": "北京",
            "topic": "安全测试",
            "attendees": ["张伟"],
            "action_items": [
                {"owner": "张伟", "action": "交付", "due": "下周一", "evidence_quote": "张伟下周交付"}
            ],
            "risks": [
                {
                    "basis": "owner",
                    "severity": "high",
                    "evidence_quote": "这个事没人认领",
                    "suggestion": "指定负责人",
                    "owner": None,
                    "due": None,
                }
            ],
        }


class BoomMailer:
    mode = "smtp"

    def send(self, *args, **kwargs):
        raise RuntimeError("smtp build failed")


def main() -> int:
    import shutil
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="aiwainao-sec-"))
    try:
        s = dataclasses.replace(
            base_settings,
            data_dir=tmp,
            mail_dry_run_forced=True,
            llm_api_key="",
            llm_mock_forced=True,
        )
        repo.init_db(s.db_path)

        # ---------- 输入上限 / 发送 claim ----------
        try:
            check_session_id("x" * 101)
        except ValueError:
            check("session_id 长度上限", True)
        else:
            check("session_id 长度上限", False, "没有抛异常")

        req = Request(
            method="POST", path="/api/chat", query={},
            headers={"content-type": "application/json"}, body=b"{" + b" " * (300 * 1024),
        )
        check("chat 请求体上限 256KB", chat.handle_chat(req).status == 413)
        req = Request(
            method="POST", path="/api/ppt", query={},
            headers={"content-type": "application/json"}, body=b"{" + b" " * (300 * 1024),
        )
        check("ppt 请求体上限 256KB", ppt.handle_ppt(req).status == 413)

        with repo.db(s.db_path) as conn:
            doc = repo.insert_doc(
                conn, session_id="s", kind="meeting", path="x", sha256="a" * 64
            )
            mtg = repo.insert_meeting(
                conn, doc_id=doc, session_id="s", held_at=None, place=None,
                attendees=[], topic="t", raw_text="t",
            )
            first = repo.claim_send_log(
                conn, meeting_id=mtg, dedupe_key="k", recipients=["a@example.com"], subject="s"
            )
            second = repo.claim_send_log(
                conn, meeting_id=mtg, dedupe_key="k", recipients=["a@example.com"], subject="s"
            )
            check("同一 dedupe_key 只能 claim 一次", bool(first) and second is None)
            repo.finish_send_log(conn, first, status="failed", dedupe_key=None, error="x")
            third = repo.claim_send_log(
                conn, meeting_id=mtg, dedupe_key="k", recipients=["a@example.com"], subject="s"
            )
            check("失败后 claim 可释放重发", bool(third))

        # ---------- CSRF ----------
        server = _server(s)
        try:
            port = server.server_address[1]
            status, _ = _post(port, "http://evil.example")
            check("跨站 POST 被 403", status == 403, "status=%s" % status)
            status, _ = _post(port, "null")
            check("Origin: null 被 403", status == 403, "status=%s" % status)
            status, _ = _post(port, "http://127.0.0.1:%d" % port, fetch_site="cross-site")
            check("Sec-Fetch-Site: cross-site 被 403", status == 403, "status=%s" % status)
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/")
            resp = conn.getresponse()
            resp.read()
            check("X-Frame-Options: DENY",
                  resp.getheader("X-Frame-Options") == "DENY",
                  str(resp.getheader("X-Frame-Options")))
            conn.close()
        finally:
            server.shutdown()
            server.server_close()

        # ---------- JSON Content-Type ----------
        req = Request(
            method="POST", path="/api/chat", query={},
            headers={"content-type": "text/plain"}, body=b'{"session_id":"x","text":"hi"}',
        )
        check("chat 拒绝 text/plain", chat.handle_chat(req).status == 415)
        req = Request(
            method="POST", path="/api/ppt", query={},
            headers={"content-type": "text/plain"}, body=b'{"session_id":"x","spec":"x"}',
        )
        check("ppt 拒绝 text/plain", ppt.handle_ppt(req).status == 415)

        # ---------- 收件人 / 原文上限 ----------
        try:
            parse_recipients(",".join("a%d@example.com" % i for i in range(MAX_RECIPIENTS + 1)))
        except MeetingError:
            check("收件人数量上限", True)
        else:
            check("收件人数量上限", False, "没有抛异常")

        try:
            run_meeting(
                s, session_id="s1", filename="x.txt",
                raw=("长" * (MAX_MEETING_CHARS + 1)).encode("utf-8"),
                mail_to="a@example.com",
            )
        except MeetingError as exc:
            check("会议原文长度上限", "过长" in str(exc), str(exc)[:60])
        else:
            check("会议原文长度上限", False, "没有抛异常")

        # ---------- 秘密不进 repr ----------
        repr_settings = dataclasses.replace(
            s,
            llm_api_key="secret-llm-key",
            smtp_pass="secret-smtp-pass",
            search_api_key="secret-search-key",
        )
        check("Settings repr 不含 SEARCH_API", "secret-search-key" not in repr(repr_settings))
        check("Settings repr 不含 SMTP_PASS", "secret-smtp-pass" not in repr(repr_settings))
        check("Settings repr 不含 LLM_API_KEY", "secret-llm-key" not in repr(repr_settings))

        # ---------- 非法 Subject 不抛 ----------
        mailer = Mailer(s)
        res = mailer.send(["a@example.com"], "ok\nBcc: x@example.com", "body")
        check("非法 Subject 返回 failed", res.status == "failed", res.detail[:80])

        # ---------- 邮件异常不回滚风险结论 ----------
        s2 = dataclasses.replace(s, data_dir=tmp / "mail")
        repo.init_db(s2.db_path)
        result = run_meeting(
            s2, session_id="s1", filename="x.txt",
            raw="这个事没人认领。张伟下周交付。".encode("utf-8"),
            mail_to="a@example.com", llm=SecFakeLLM(), mailer=BoomMailer(),
        )
        check("邮件异常仍返回风险结论", len(result["risks"]) == 1, "risks=%d" % len(result["risks"]))
        check("邮件异常 mail_status=failed", result["mail_status"] == "failed", result["mail_status"])
        with repo.db(s2.db_path) as conn:
            check("风险结论已提交", conn.execute("SELECT COUNT(*) FROM risk_items").fetchone()[0] == 1)

        # ---------- 非本机地址默认拒绝 ----------
        try:
            build_server(dataclasses.replace(base_settings, host="0.0.0.0", port=0))
        except RuntimeError:
            check("APP_HOST 非 loopback 拒绝", True)
        else:
            check("APP_HOST 非 loopback 拒绝", False, "没有抛异常")

        # ---------- SMTP_SSL 必须校验证书 ----------
        captured = {}
        class FakeSMTP:
            def __init__(self, host, port, timeout=None, context=None):
                captured["context"] = context
            def ehlo(self): pass
            def login(self, *a): pass
            def send_message(self, *a, **k): pass
            def quit(self): pass
        from app.mail import sender as sender_mod
        old = sender_mod.smtplib.SMTP_SSL
        sender_mod.smtplib.SMTP_SSL = FakeSMTP
        try:
            ssl_settings = dataclasses.replace(
                base_settings, smtp_host="smtp.example.com", smtp_port=465,
                smtp_ssl=True, smtp_user="u", smtp_pass="p", mail_dry_run_forced=False,
            )
            Mailer(ssl_settings)._smtp_send(
                Mailer(ssl_settings)._build(["a@example.com"], "s", "b", None),
                ["a@example.com"],
            )
            ctx = captured.get("context")
            check("SMTP_SSL 使用 CERT_REQUIRED",
                  ctx is not None and ctx.verify_mode == ssl.CERT_REQUIRED,
                  str(ctx.verify_mode if ctx else None))
            check("SMTP_SSL 校验 hostname",
                  ctx is not None and ctx.check_hostname is True,
                  str(ctx.check_hostname if ctx else None))
        finally:
            sender_mod.smtplib.SMTP_SSL = old
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    passed = sum(1 for ok, _, _ in RESULTS if ok)
    print("=" * 68)
    for ok, name, detail in RESULTS:
        print("%s %-34s %s" % ("PASS" if ok else "FAIL", name, detail))
    print("=" * 68)
    print("%d/%d 项通过" % (passed, len(RESULTS)))
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
