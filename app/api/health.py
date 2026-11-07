"""依赖探活：DB / LLM / SMTP 三项状态。缺 key 时如实显示 mock、缺 SMTP 时显示 dry-run。"""
from __future__ import annotations

import socket
from typing import Any, Dict

from app.api.server import Request, Response, json_response
from app.config.settings import load_settings
from app.store import repo


def _db_status(settings) -> Dict[str, Any]:
    try:
        with repo.db(settings.db_path) as conn:
            tables = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                ).fetchall()
            ]
        return {"ok": True, "path": str(settings.db_path), "tables": len(tables)}
    except Exception as exc:
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}


def _llm_status(settings) -> Dict[str, Any]:
    if not settings.llm_live:
        return {
            "ok": True,
            "mode": "mock",
            "detail": "未配置 LLM_API_KEY（或 LLM_MOCK=1），使用本地确定性替身，不产生外部调用",
        }
    return {
        "ok": bool(settings.llm_api_key),
        "mode": "live",
        "model": settings.llm_model,
        "base_url": settings.llm_base_url,
        "detail": "已配置；未发真实请求（避免每次探活都烧 token）",
    }


def _search_status(settings) -> Dict[str, Any]:
    if not settings.search_live:
        return {
            "ok": True,
            "mode": "mock",
            "detail": "未配置 SEARCH_API，web_search 会明确报错，不会伪造联网结果",
        }
    return {
        "ok": True,
        "mode": "live",
        "provider": "baidu_qianfan",
        "detail": "已配置百度千帆 AI 搜索；未发真实请求（避免每次探活都消耗配额）",
    }


def _mask_email(value: str) -> str:
    value = (value or "").strip()
    if "@" not in value:
        return value[:1] + "***" if value else ""
    local, domain = value.split("@", 1)
    return (local[:1] + "***@" + domain) if local else "***@" + domain


def _smtp_status(settings) -> Dict[str, Any]:
    if not settings.mail_live:
        return {
            "ok": True,
            "mode": "dry-run",
            "detail": "未配置 SMTP（或 MAIL_DRY_RUN=1），邮件写入 data/outbox/*.eml，不真发",
        }
    host, port = settings.smtp_host, settings.smtp_port
    try:
        with socket.create_connection((host, port), timeout=5):
            reachable = True
    except OSError as exc:
        reachable = False
        return {"ok": False, "mode": "smtp", "detail": "无法连接 %s:%d: %s" % (host, port, exc)}
    return {
        "ok": reachable,
        "mode": "smtp",
        "host": host,
        "port": port,
        "from": _mask_email(settings.smtp_from or settings.smtp_user),
        "detail": "端口可达（未做登录校验）",
    }


def handle_health(req: Request) -> Response:
    settings = load_settings()
    payload = {
        "ok": True,
        "db": _db_status(settings),
        "llm": _llm_status(settings),
        "search": _search_status(settings),
        "smtp": _smtp_status(settings),
    }
    payload["ok"] = (
        bool(payload["db"]["ok"])
        and bool(payload["llm"]["ok"])
        and bool(payload["search"]["ok"])
        and bool(payload["smtp"]["ok"])
    )
    return json_response(payload, 200 if payload["ok"] else 503)
