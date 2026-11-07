"""零依赖 HTTP 层（stdlib http.server）。

为什么不是 FastAPI：本机无法访问 PyPI（无网络），装不上 fastapi/uvicorn。
骨架的 api 层职责（路由/校验/序列化）不变，只是换了个同构的适配器；
routes 表 + handler 签名与 FastAPI 版本一一对应，后续能机械替换。
"""
from __future__ import annotations

import json
import mimetypes
import re
import socket
import sys
import traceback
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Pattern, Tuple
from urllib.parse import parse_qs, unquote, urlparse

from app.config.settings import ROOT, Settings, load_settings
from app.store import repo

WEB_DIR = ROOT / "web"


@dataclass
class Request:
    method: str
    path: str
    query: Dict[str, List[str]]
    headers: Dict[str, str]
    body: bytes
    params: Dict[str, str] = field(default_factory=dict)

    def header(self, name: str) -> str:
        return self.headers.get(name.lower(), "")

    def query_one(self, name: str, default: str = "") -> str:
        values = self.query.get(name) or []
        return values[0] if values else default


@dataclass
class Response:
    status: int = 200
    body: bytes = b""
    content_type: str = "application/json; charset=utf-8"
    headers: Dict[str, str] = field(default_factory=dict)


def json_response(payload: Any, status: int = 200) -> Response:
    data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    return Response(status=status, body=data)


def is_same_origin(value: str, host: str) -> bool:
    """Origin/Referer 与 Host 同源才放行；空的非浏览器请求由调用方决定。"""
    if not value:
        return True
    if value.strip().lower() == "null":
        return False
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return False
    return bool(host) and parsed.netloc.lower() == host.strip().lower()


def check_session_id(value: str) -> str:
    value = (value or "").strip()
    if len(value) > 100 or any(ord(ch) < 32 for ch in value):
        raise ValueError("session_id 不合法（最长 100 字符，不含控制字符）")
    return value


def error_response(message: str, status: int = 400, **extra: Any) -> Response:
    payload = {"error": message, "status": status}
    payload.update(extra)
    return json_response(payload, status)


# ---------------- multipart（stdlib，避免 cgi 模块的 3.13 移除问题） ----------------
class MultipartPart:
    def __init__(self, name: str, filename: Optional[str], content_type: str, data: bytes):
        self.name = name
        self.filename = filename
        self.content_type = content_type
        self.data = data


def parse_multipart(body: bytes, content_type: str) -> Dict[str, MultipartPart]:
    match = re.search(r'boundary="?([^";]+)"?', content_type or "")
    if not match:
        raise ValueError("缺少 multipart boundary")
    boundary = b"--" + match.group(1).encode()
    parts: Dict[str, MultipartPart] = {}
    for chunk in body.split(boundary):
        chunk = chunk.lstrip(b"\r\n")
        if not chunk or chunk in (b"--", b"--\r\n"):
            continue
        head, sep, data = chunk.partition(b"\r\n\r\n")
        if not sep:
            continue
        if data.endswith(b"\r\n"):
            data = data[:-2]
        headers: Dict[str, str] = {}
        for line in head.decode("utf-8", "replace").split("\r\n"):
            if ":" in line:
                key, value = line.split(":", 1)
                headers[key.strip().lower()] = value.strip()
        disposition = headers.get("content-disposition", "")
        name_match = re.search(r'name="([^"]*)"', disposition)
        if not name_match:
            continue
        filename_match = re.search(r'filename="([^"]*)"', disposition)
        parts[name_match.group(1)] = MultipartPart(
            name_match.group(1),
            unquote(filename_match.group(1)) if filename_match else None,
            headers.get("content-type", ""),
            data,
        )
    return parts


# ---------------- 路由 ----------------
class Application:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.routes: List[Tuple[str, Pattern[str], Callable[[Request], Response]]] = []
        self._register_routes()

    def add(self, method: str, pattern: str, handler: Callable[[Request], Response]) -> None:
        self.routes.append((method.upper(), re.compile("^%s$" % pattern), handler))

    def _register_routes(self) -> None:
        from app.api import chat, health, memory, ppt, upload

        self.add("GET", r"/api/health", health.handle_health)
        self.add("POST", r"/api/chat", chat.handle_chat)
        self.add("GET", r"/api/sessions/(?P<session_id>[^/]+)/messages", chat.handle_messages)
        self.add("GET", r"/api/sessions/(?P<session_id>[^/]+)/memory", memory.handle_memory)
        self.add("POST", r"/api/upload/meeting", upload.handle_upload_meeting)
        self.add("POST", r"/api/upload/pdf", upload.handle_upload_pdf)
        self.add("POST", r"/api/ppt", ppt.handle_ppt)
        self.add("GET", r"/api/files/(?P<ppt_id>[^/]+)", ppt.handle_file)
        self.add("GET", r"/api/meetings/(?P<meeting_id>[^/]+)", upload.handle_meeting_detail)
        self.add("GET", r"/", self.handle_index)
        self.add("GET", r"/index\.html", self.handle_index)
        self.add("GET", r"/(?P<asset>[A-Za-z0-9_\-]+\.(?:js|css))", self.handle_asset)

    # ---- 静态 ----
    def handle_index(self, req: Request) -> Response:
        path = WEB_DIR / "index.html"
        return Response(200, path.read_bytes(), "text/html; charset=utf-8")

    def handle_asset(self, req: Request) -> Response:
        name = req.params["asset"]
        path = (WEB_DIR / name).resolve()
        if WEB_DIR.resolve() not in path.parents or not path.is_file():  # 防穿越
            return error_response("not found", 404)
        mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        if mime.startswith("text/") or mime in ("application/javascript",):
            mime += "; charset=utf-8"
        return Response(200, path.read_bytes(), mime)

    def dispatch(self, req: Request) -> Response:
        for method, pattern, handler in self.routes:
            match = pattern.match(req.path)
            if not match:
                continue
            if method != req.method:
                return error_response("method not allowed", 405)
            req.params = {k: unquote(v) for k, v in match.groupdict().items()}
            return handler(req)
        return error_response("not found: %s" % req.path, 404)


class Handler(BaseHTTPRequestHandler):
    server_version = "AIWaiNao/0.1"
    app: Application  # 由工厂注入
    timeout = 30  # 慢客户端不无限占线程

    def log_message(self, fmt: str, *args: Any) -> None:  # 安静一点
        sys.stderr.write("[http] %s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, resp: Response) -> None:
        self.send_response(resp.status)
        self.send_header("Content-Type", resp.content_type)
        self.send_header("Content-Length", str(len(resp.body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in resp.headers.items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(resp.body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _reject(self, message: str, status: int) -> None:
        resp = error_response(message, status)
        resp.headers["Connection"] = "close"
        self._send(resp)

    def _handle(self) -> None:
        parsed = urlparse(self.path)
        raw_length = self.headers.get("Content-Length")
        try:
            declared = int(raw_length or 0)
        except (TypeError, ValueError):
            self._reject("Content-Length 不合法", 400)
            return
        if declared < 0:
            self._reject("Content-Length 不合法", 400)
            return
        limit = self.app.settings.max_upload_bytes + 2 * 1024 * 1024
        if declared > limit:
            self._reject("请求体超过上限（%d MB）" % (limit // (1024 * 1024)), 413)
            return

        # CSRF：浏览器跨站 POST 必带 Origin/Referer；非浏览器（curl）两者都没有。
        if self.command in ("POST", "PUT", "PATCH", "DELETE"):
            host = self.headers.get("Host", "")
            origin = self.headers.get("Origin", "")
            referer = self.headers.get("Referer", "")
            fetch_site = self.headers.get("Sec-Fetch-Site", "")
            if fetch_site and fetch_site not in ("same-origin", "none"):
                self._reject("跨站请求被拒绝", 403)
                return
            if origin and not is_same_origin(origin, host):
                self._reject("跨站请求被拒绝", 403)
                return
            if not origin and referer and not is_same_origin(referer, host):
                self._reject("跨站请求被拒绝", 403)
                return

        try:
            body = self.rfile.read(declared) if declared else b""
        except (socket.timeout, OSError):
            self.close_connection = True
            return

        req = Request(
            method=self.command,
            path=unquote(parsed.path),
            query=parse_qs(parsed.query),
            headers={k.lower(): v for k, v in self.headers.items()},
            body=body,
        )
        try:
            resp = self.app.dispatch(req)
        except Exception:  # 兜底：不白屏、不静默
            traceback.print_exc()
            resp = error_response("服务器内部错误，详见服务端日志", 500)
        self._send(resp)

    do_GET = _handle
    do_POST = _handle
    do_HEAD = _handle


def build_server(settings: Optional[Settings] = None) -> ThreadingHTTPServer:
    settings = settings or load_settings()
    loopback = {"127.0.0.1", "localhost", "::1"}
    if settings.host not in loopback and not settings.allow_remote:
        raise RuntimeError(
            "APP_HOST=%s 不是本机地址；如确需局域网/公网暴露，"
            "设置 APP_ALLOW_REMOTE=1 并自行加认证/反代" % settings.host
        )
    repo.init_db(settings.db_path)
    app = Application(settings)
    handler_cls = type("BoundHandler", (Handler,), {"app": app})
    server = ThreadingHTTPServer((settings.host, settings.port), handler_cls)
    server.daemon_threads = True
    return server


def serve() -> None:
    settings = load_settings()
    try:
        server = build_server(settings)
    except RuntimeError as exc:
        print("启动失败：%s" % exc)
        raise SystemExit(1)
    url = "http://%s:%d/" % (settings.host, settings.port)
    print("=" * 60)
    print("AI 外脑 Demo 已启动：%s" % url)
    print("  DB      : %s" % settings.db_path)
    print("  LLM     : %s" % ("live:" + settings.llm_model if settings.llm_live else "mock（无 key，确定性替身）"))
    print("  MAIL    : %s" % ("smtp:" + settings.smtp_host if settings.mail_live else "dry-run（写 data/outbox/*.eml）"))
    print("  健康检查 : %sapi/health" % url)
    print("=" * 60)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()
