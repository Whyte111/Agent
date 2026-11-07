"""web_search：百度千帆 AI 搜索（Baidu AI Search，search_source=baidu_search_v2）。

返回 title / url / date / snippet，供对话模型组织带 URL 的回答。
失败时按归一化 query 回退到最近一次成功缓存，并标注 cached=true（骨架 7 节）。
"""
from __future__ import annotations

import html as html_lib
import json
import re
from typing import Any, Dict, List, Optional

from app.store import repo

SEARCH_ENDPOINT = "https://qianfan.baidubce.com/v2/ai_search"
DEFAULT_TOP_K = 5
MAX_TOP_K = 8
MAX_QUERY_CHARS = 300
MAX_SNIPPET_CHARS = 800


class SearchError(RuntimeError):
    """搜索不可用（未配置 key / 上游错误 / 无缓存）。"""


def _norm_query(query: str) -> str:
    return re.sub(r"\s+", " ", (query or "")).strip().lower()


def _safe_error(settings: Any, message: str) -> str:
    text = str(message or "")
    key = getattr(settings, "search_api_key", "") or ""
    if key:
        text = text.replace(key, "***")
    return text[:300]


def _clean_text(text: Any) -> str:
    value = html_lib.unescape(str(text or ""))
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:MAX_SNIPPET_CHARS]


def _payload(query: str, top_k: int) -> Dict[str, Any]:
    return {
        "messages": [{"role": "user", "content": query}],
        "stream": False,
        "search_source": "baidu_search_v2",
        "resource_type_filter": [{"type": "web", "top_k": top_k}],
    }


def _request(settings: Any, query: str, top_k: int) -> Dict[str, Any]:
    if not settings.search_api_key:
        raise SearchError("未配置 SEARCH_API，联网搜索不可用")
    import requests

    try:
        resp = requests.post(
            SEARCH_ENDPOINT,
            headers={
                "Authorization": "Bearer %s" % settings.search_api_key,
                "Content-Type": "application/json",
            },
            json=_payload(query, top_k),
            timeout=max(3, int(settings.search_timeout or 20)),
        )
    except Exception as exc:
        raise SearchError("百度千帆搜索请求失败：%s" % _safe_error(settings, exc))

    if resp.status_code >= 400:
        detail = _clean_text(_safe_error(settings, resp.text))[:200]
        raise SearchError("百度千帆搜索 HTTP %d：%s" % (resp.status_code, detail))
    try:
        data = resp.json()
    except ValueError:
        raise SearchError("百度千帆搜索返回的不是 JSON")
    if not isinstance(data, dict):
        raise SearchError("百度千帆搜索返回格式异常")
    return data


def _parse(settings: Any, data: Dict[str, Any], top_k: int) -> List[Dict[str, Any]]:
    raw = data.get("references")
    if not isinstance(raw, list):
        return []
    results: List[Dict[str, Any]] = []
    seen_urls = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        results.append(
            {
                "id": str(item.get("id") or len(results) + 1),
                "title": _clean_text(item.get("title"))[:200] or url,
                "url": url,
                "date": _clean_text(item.get("date"))[:40],
                "content": _clean_text(item.get("content")),
            }
        )
        if len(results) >= top_k:
            break
    return results


def _references(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "type": "web",
            "title": item["title"],
            "url": item["url"],
            "date": item["date"],
            "snippet": item["content"][:300],
            "source": "baidu_qianfan",
        }
        for item in results
    ]


def _load_cache(settings: Any, query_norm: str) -> Optional[Dict[str, Any]]:
    try:
        with repo.db(settings.db_path) as conn:
            row = repo.get_search_cache(conn, query_norm)
    except Exception:
        return None
    if not row:
        return None
    try:
        results = json.loads(row["results_json"])
    except (TypeError, ValueError):
        return None
    if not isinstance(results, list):
        return None
    return {"results": results, "created_at": row["created_at"]}


def _save_cache(settings: Any, query: str, query_norm: str, results: List[Dict[str, Any]]) -> None:
    if not results:
        return
    try:
        with repo.db(settings.db_path) as conn:
            repo.upsert_search_cache(
                conn,
                query_norm=query_norm,
                query=query,
                results_json=json.dumps(results, ensure_ascii=False),
            )
    except Exception:
        # 缓存是增强，写失败不影响本次搜索结果
        pass


def web_search(
    query: str,
    top_k: int = DEFAULT_TOP_K,
    *,
    settings: Any = None,
    fetcher: Any = None,
) -> Dict[str, Any]:
    """执行搜索；fetcher 仅供离线自检注入，生产走百度千帆。"""
    if settings is None:
        raise RuntimeError("web_search 缺少 settings")
    if not settings.search_api_key:
        raise SearchError("未配置 SEARCH_API，联网搜索不可用")
    query = re.sub(r"\s+", " ", (query or "")).strip()
    if not query:
        raise SearchError("搜索词不能为空")
    if len(query) > MAX_QUERY_CHARS:
        raise SearchError("搜索词过长（上限 %d 字）" % MAX_QUERY_CHARS)
    top_k = max(1, min(int(top_k or DEFAULT_TOP_K), MAX_TOP_K))
    query_norm = _norm_query(query)

    try:
        data = (fetcher or (lambda s, q, k: _request(s, q, k)))(settings, query, top_k)
        results = _parse(settings, data, top_k)
    except Exception as exc:
        error = _safe_error(settings, exc)
        cached = _load_cache(settings, query_norm)
        if cached:
            return {
                "query": query,
                "results": cached["results"][:top_k],
                "references": _references(cached["results"][:top_k]),
                "count": len(cached["results"][:top_k]),
                "cached": True,
                "cache_time": cached["created_at"],
                "note": "实时搜索失败，返回缓存结果（%s）：%s" % (cached["created_at"], error),
            }
        raise SearchError("联网搜索失败且无同 query 缓存：%s" % error)

    _save_cache(settings, query, query_norm, results)
    return {
        "query": query,
        "results": results,
        "references": _references(results),
        "count": len(results),
        "cached": False,
        "cache_time": "",
        "note": "" if results else "搜索无结果；回答时如实说没有查到，不要编。",
    }
