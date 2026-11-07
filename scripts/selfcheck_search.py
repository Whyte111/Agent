"""G5 联网搜索自检（离线）：解析 / 引用 / 缓存降级 / 错误路径。

用法: python -m scripts.selfcheck_search
"""
from __future__ import annotations

import dataclasses
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]

from app.config.settings import settings as base_settings  # noqa: E402
from app.store import repo  # noqa: E402
from app.tools.baidu_search import SearchError, web_search  # noqa: E402

RESULTS: List[tuple] = []

PAYLOAD = {
    "request_id": "test",
    "references": [
        {
            "id": 1,
            "title": "北京天气",
            "url": "https://example.com/beijing",
            "date": "2026-11-08 08:00:00",
            "content": "<b>晴</b> 6℃ ~ 18℃，北风微风",
        },
        {
            "id": 2,
            "title": "重复链接",
            "url": "https://example.com/beijing",
            "date": "2026-11-08",
            "content": "duplicate",
        },
        {
            "id": 3,
            "title": "中央气象台",
            "url": "https://example.com/weather",
            "date": "2026-11-08",
            "content": "未来三天全国天气预报",
        },
    ],
}


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((ok, name, detail))


class Fetcher:
    def __init__(self, payload: Any = None, fail: bool = False):
        self.payload = payload
        self.fail = fail
        self.calls = []

    def __call__(self, settings, query, top_k):
        self.calls.append((query, top_k))
        if self.fail:
            raise RuntimeError("network down")
        return self.payload


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="aiwainao-search-"))
    try:
        s = dataclasses.replace(
            base_settings,
            data_dir=tmp,
            search_api_key="test-key-not-real",
            search_timeout=5,
        )
        repo.init_db(s.db_path)

        # ---------- 解析 / 去重 / 清洗 ----------
        fetcher = Fetcher(PAYLOAD)
        result = web_search("2026年11月8日 北京 天气", 5, settings=s, fetcher=fetcher)
        check("返回结果", result["count"] == 2, str(result["count"]))
        check("重复 URL 被去重", len({r["url"] for r in result["results"]}) == 2)
        check("HTML 被清洗", "<b>" not in result["results"][0]["content"],
              result["results"][0]["content"])
        check("带 URL/日期/标题",
              all(r["url"].startswith("https://") and r["date"] and r["title"] for r in result["results"]))
        check("references 可给对话层", len(result["references"]) == 2
              and result["references"][0]["type"] == "web"
              and result["references"][0]["url"].startswith("https://"))
        check("cached=false", result["cached"] is False)

        # ---------- top_k 夹取 ----------
        many = Fetcher(PAYLOAD)
        web_search("测试", 999, settings=s, fetcher=many)
        check("top_k 上限 8", many.calls and many.calls[0][1] == 8, str(many.calls))

        # ---------- 缓存降级 ----------
        fallback = web_search(
            "2026年11月8日 北京 天气", 5, settings=s,
            fetcher=Fetcher(fail=True),
        )
        check("失败回退缓存", fallback["cached"] is True and fallback["count"] == 2,
              str(fallback["count"]))
        check("缓存结果带说明", "缓存" in fallback["note"], fallback["note"][:80])
        check("缓存时间可追溯", bool(fallback["cache_time"]), fallback["cache_time"])

        def leaky(settings, query, top_k):
            raise RuntimeError("boom test-key-not-real")

        leak = web_search("2026年11月8日 北京 天气", 5, settings=s, fetcher=leaky)
        check("错误信息脱敏", "test-key-not-real" not in leak["note"], leak["note"][:100])

        # ---------- 无缓存 / 无 key / 参数 ----------
        try:
            web_search("从未搜过 query-xyz-987", 5, settings=s, fetcher=Fetcher(fail=True))
        except SearchError as exc:
            check("无缓存时明确报错", "无同 query 缓存" in str(exc), str(exc)[:100])
        else:
            check("无缓存时明确报错", False, "没有抛异常")

        no_key = dataclasses.replace(s, search_api_key="")
        try:
            web_search("没有缓存-key-test", 5, settings=no_key, fetcher=Fetcher(PAYLOAD))
        except SearchError as exc:
            check("缺 SEARCH_API 明确报错", "SEARCH_API" in str(exc), str(exc)[:100])
        else:
            check("缺 SEARCH_API 明确报错", False, "没有抛异常")

        try:
            web_search("   ", 5, settings=s, fetcher=Fetcher(PAYLOAD))
        except SearchError:
            check("空 query 报错", True)
        else:
            check("空 query 报错", False, "没有抛异常")

        try:
            web_search("长" * 301, 5, settings=s, fetcher=Fetcher(PAYLOAD))
        except SearchError:
            check("超长 query 报错", True)
        else:
            check("超长 query 报错", False, "没有抛异常")

        # ---------- 空结果 ----------
        empty = web_search("无结果", 5, settings=s, fetcher=Fetcher({"references": []}))
        check("空结果如实返回", empty["count"] == 0 and "没有查到" in empty["note"],
              empty["note"])
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
