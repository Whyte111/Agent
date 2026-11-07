"""D6 加固自检（离线）：请求体上限 / 慢客户端超时 / 错误路径 / 并发参数。

用法: python -m scripts.selfcheck_hardening
"""
from __future__ import annotations

import dataclasses
import http.client
import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import List

ROOT = Path(__file__).resolve().parents[1]

from app.api.server import Application, Handler, Request, parse_multipart  # noqa: E402
from app.api import chat, ppt, upload  # noqa: E402
from app.config.settings import settings as base_settings  # noqa: E402
from app.store import repo  # noqa: E402

RESULTS: List[tuple] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((ok, name, detail))


def main() -> int:
    # ---------- 超时 / 锁等待 ----------
    check("慢客户端超时 30s", Handler.timeout == 30, str(Handler.timeout))
    tmp = Path(__file__).resolve().parents[1] / "data" / "_hardening_tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        db_path = tmp / "hardening.db"
        if db_path.exists():
            db_path.unlink()
        conn = repo.connect(db_path)
        busy = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        conn.close()
        check("SQLite busy_timeout=5000", busy == 5000, str(busy))

        # ---------- multipart 解析错误 ----------
        try:
            parse_multipart(b"x", "multipart/form-data")
        except ValueError as exc:
            check("缺 boundary 报错", "boundary" in str(exc), str(exc))
        else:
            check("缺 boundary 报错", False, "没有抛异常")

        # ---------- 真实 HTTP 层：超限请求体 ----------
        app = Application(dataclasses.replace(base_settings, data_dir=tmp))
        handler_cls = type("HardeningHandler", (Handler,), {"app": app})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        server.daemon_threads = True
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.putrequest("POST", "/api/chat")
            conn.putheader("Content-Length", str(999 * 1024 * 1024))
            conn.putheader("Content-Type", "application/json")
            conn.endheaders()
            resp = conn.getresponse()
            payload = json.loads(resp.read().decode("utf-8"))
            check("超大请求体 413", resp.status == 413, "status=%s" % resp.status)
            check("413 有中文错误", "上限" in payload.get("error", ""), str(payload))
            conn.close()

            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.putrequest("POST", "/api/chat")
            conn.putheader("Content-Length", "abc")
            conn.endheaders()
            resp = conn.getresponse()
            resp.read()
            check("非法 Content-Length 400", resp.status == 400, "status=%s" % resp.status)
            conn.close()

            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.putrequest("GET", "/nope")
            conn.endheaders()
            resp = conn.getresponse()
            resp.read()
            check("未知路由 404", resp.status == 404, "status=%s" % resp.status)
            conn.close()
        finally:
            server.shutdown()
            server.server_close()

        # ---------- 接口错误路径（不触发 DB） ----------
        req = Request(
            method="POST", path="/api/ppt", query={},
            headers={"content-type": "application/json"}, body=b"{bad",
        )
        check("PPT 非法 JSON 400", ppt.handle_ppt(req).status == 400)
        req = Request(
            method="POST", path="/api/chat", query={},
            headers={"content-type": "application/json"}, body=b"{bad",
        )
        check("对话非法 JSON 400", chat.handle_chat(req).status == 400)
        req = Request(
            method="POST", path="/api/upload/pdf", query={},
            headers={"content-type": "text/plain"}, body=b"x",
        )
        check("PDF 非 multipart 415", upload.handle_upload_pdf(req).status == 415)
    finally:
        import shutil

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
