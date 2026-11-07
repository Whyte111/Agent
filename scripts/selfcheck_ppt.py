"""G6 PPT 自检（离线）：大纲→5 页渲染→落库→工具→下载接口。

用法: python -m scripts.selfcheck_ppt
"""
from __future__ import annotations

import dataclasses
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]

from app.api import ppt as ppt_api  # noqa: E402
from app.api.server import Request  # noqa: E402
from app.config.settings import settings as base_settings  # noqa: E402
from app.ppt.builder import PAGE_COUNT, PptError, run_ppt  # noqa: E402
from app.store import repo  # noqa: E402
from app.tools.ppt_tool import build as build_ppt_tool  # noqa: E402
from app.tools.registry import ToolRegistry  # noqa: E402

RESULTS: List[tuple] = []

OUTLINE = {
    "title": "星云项目汇报",
    "subtitle": "2026 年项目进展",
    "sections": [
        {"title": "背景与目标", "bullets": ["统一会员数据平台", "覆盖华东 120 家门店", "打通线上与线下"]},
        {"title": "核心内容", "bullets": ["数据打通", "准实时同步", "会员唯一视图", "权限分级"]},
        {"title": "计划与里程碑", "bullets": ["6 月杭州试点", "9 月全量上线", "12 月旧系统下线"]},
        {"title": "总结与下一步", "bullets": ["按计划推进", "确认试点名单", "准备评审材料"]},
    ],
}


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((ok, name, detail))


class FakeLLM:
    mode = "fake"

    def __init__(self, payload: Any = None, fail: bool = False):
        self.payload = payload
        self.fail = fail
        self.calls = 0

    def structured(self, prompt, schema, *, name="", system=None, fixture_key=None):
        self.calls += 1
        if self.fail:
            raise RuntimeError("model down")
        return self.payload


def _slides(path: Path) -> int:
    from pptx import Presentation

    return len(Presentation(str(path)).slides)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="aiwainao-ppt-"))
    try:
        s = dataclasses.replace(
            base_settings,
            data_dir=tmp,
            llm_api_key="",
            llm_mock_forced=True,
        )
        repo.init_db(s.db_path)

        # ---------- 直接给大纲：跳过 LLM ----------
        first = run_ppt(s, session_id="s1", spec=OUTLINE)
        check("返回 ppt_id / download_url / 5 页",
              bool(first["ppt_id"]) and first["download_url"].startswith("/api/files/")
              and first["pages"] == PAGE_COUNT, str(first))
        check("source=provided", first["outline_source"] == "provided", first["outline_source"])
        path = s.generated_dir / ("%s.pptx" % first["ppt_id"])
        check("文件真实落盘", path.is_file() and path.stat().st_size > 10000,
              "%s bytes" % (path.stat().st_size if path.exists() else 0))
        check("pptx 可被 python-pptx 重新打开", _slides(path) == PAGE_COUNT, str(_slides(path)))

        from pptx import Presentation
        prs = Presentation(str(path))
        texts = []
        for slide in prs.slides:
            for shape in slide.shapes:
                if shape.has_text_frame:
                    texts.append(shape.text_frame.text)
        joined = "\n".join(texts)
        check("封面标题进入 PPT", "星云项目汇报" in joined)
        check("4 个内容部分都在", all(t in joined for t in ["背景与目标", "核心内容", "计划与里程碑", "总结与下一步"]))
        check("要点进入 PPT", "覆盖华东 120 家门店" in joined)

        # ---------- LLM 正常 ----------
        fake = FakeLLM(OUTLINE)
        second = run_ppt(s, session_id="s2", spec="做一份星云项目汇报", llm=fake)
        check("LLM 正常时 source=llm", second["outline_source"] == "llm", second["outline_source"])
        check("LLM 只调一次 structured", fake.calls == 1, str(fake.calls))

        # ---------- LLM 失败降级 ----------
        bad = FakeLLM(fail=True)
        third = run_ppt(s, session_id="s3", spec="降级测试", llm=bad)
        check("LLM 失败仍产出 5 页", third["pages"] == PAGE_COUNT and _slides(s.generated_dir / ("%s.pptx" % third["ppt_id"])) == 5)
        check("降级如实标注 note", third["outline_source"] == "fallback" and "model down" in third["note"],
              third["note"])

        # ---------- 错误输入 ----------
        try:
            run_ppt(s, session_id="s1", spec="   ")
        except PptError as exc:
            check("空主题报错", "主题" in str(exc), str(exc))
        else:
            check("空主题报错", False, "没有抛异常")

        # ---------- 工具 ----------
        registry = ToolRegistry()
        registry.register(build_ppt_tool(s, "s-tool"))
        result = registry.call("make_ppt", {"topic": "星云项目汇报", "audience": "管理层"})
        check("registry.call(make_ppt) 成功", result["ok"], str(result.get("error")))
        data = result.get("data") or {}
        check("工具返回 ppt_id/download_url", bool(data.get("ppt_id")) and bool(data.get("download_url")), str(data))
        with repo.db(s.db_path) as conn:
            job = repo.get_ppt_job(conn, data.get("ppt_id") or "")
        check("ppt_jobs 落库且 session 正确",
              job is not None and job["session_id"] == "s-tool" and job["pages"] == PAGE_COUNT,
              str(job and {k: job[k] for k in ("session_id", "pages", "status")}))

        # ---------- HTTP 接口（直接调 handler，不启服务） ----------
        ppt_api.load_settings = lambda: s
        body = json.dumps({"session_id": "s-api", "spec": OUTLINE}, ensure_ascii=False).encode("utf-8")
        req = Request(
            method="POST",
            path="/api/ppt",
            query={},
            headers={"content-type": "application/json"},
            body=body,
        )
        resp = ppt_api.handle_ppt(req)
        check("POST /api/ppt 返回 200", resp.status == 200, "status=%s" % resp.status)
        payload = json.loads(resp.body.decode("utf-8"))
        check("接口返回 download_url", bool(payload.get("download_url")), str(payload))
        file_req = Request(
            method="GET",
            path=payload["download_url"],
            query={},
            headers={},
            body=b"",
            params={"ppt_id": payload["ppt_id"]},
        )
        file_resp = ppt_api.handle_file(file_req)
        check("GET /api/files 返回 pptx 字节",
              file_resp.status == 200 and file_resp.body[:2] == b"PK"
              and "presentationml" in file_resp.content_type,
              "status=%s bytes=%d" % (file_resp.status, len(file_resp.body)))
        check("下载头是 attachment",
              "attachment" in file_resp.headers.get("Content-Disposition", ""),
              file_resp.headers.get("Content-Disposition", ""))
        missing = ppt_api.handle_file(
            Request(method="GET", path="/api/files/nope", query={}, headers={}, body=b"", params={"ppt_id": "nope"})
        )
        check("不存在的 ppt 返回 404", missing.status == 404, str(missing.status))
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
