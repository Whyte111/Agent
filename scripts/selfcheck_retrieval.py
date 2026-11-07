"""G2 PDF 自检（离线）：抽取 / 扫描件 / 切块 / FTS5 命中 / 空结果 / 跨文档。

用法: python -m scripts.selfcheck_retrieval
"""
from __future__ import annotations

import dataclasses
import io
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]

from app.config.settings import settings as base_settings  # noqa: E402
from app.ingest.chunker import OVERLAP_CHARS, chunk_pages, chunk_text  # noqa: E402
from app.ingest.pdf import (  # noqa: E402
    PdfLimitError,
    ScannedPdfError,
    extract_pdf,
)
from app.ingest.pipeline import run_pdf_ingest  # noqa: E402
from app.retrieval.index import DocIndex, tokenize  # noqa: E402
from app.store import repo  # noqa: E402
from app.tools.doc_search import build as build_doc_search  # noqa: E402
from app.tools.registry import ToolRegistry  # noqa: E402

SAMPLE_PDF = ROOT / "data" / "samples" / "G2_星云项目背景.pdf"
RESULTS: List[tuple] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((ok, name, detail))


def _blank_pdf(pages: int = 1) -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=595, height=842)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def main() -> int:
    if not SAMPLE_PDF.is_file():
        print("缺少样本 PDF：%s" % SAMPLE_PDF)
        return 1
    raw = SAMPLE_PDF.read_bytes()

    # ---------- ① PDF 抽取 ----------
    extracted = extract_pdf(raw)
    page1 = extracted["page_texts"][0]
    page2 = extracted["page_texts"][1]
    check("样本 PDF 解析出 2 页", extracted["pages"] == 2, str(extracted["pages"]))
    check("第 1 页含项目预算事实", "480" in page1 and "预算" in page1)
    check("第 2 页含验收标准事实", "验收标准" in page2 and "孙磊" in page2)
    check("中文文本层可抽取", "\u661f\u4e91" in page1)

    # ---------- ② 扫描件 / 上限 ----------
    try:
        extract_pdf(_blank_pdf(1))
    except ScannedPdfError as exc:
        check("空白 PDF 判为扫描件并报错", "扫描版" in str(exc), str(exc))
    except Exception as exc:  # noqa: BLE001
        check("空白 PDF 判为扫描件并报错", False, "%s: %s" % (type(exc).__name__, exc))
    else:
        check("空白 PDF 判为扫描件并报错", False, "没有抛异常")

    try:
        extract_pdf(_blank_pdf(51))
    except PdfLimitError as exc:
        check("51 页触发页数上限", "50" in str(exc), str(exc))
    except Exception as exc:  # noqa: BLE001
        check("51 页触发页数上限", False, "%s: %s" % (type(exc).__name__, exc))
    else:
        check("51 页触发页数上限", False, "没有抛异常")

    try:
        extract_pdf(raw, max_bytes=10)
    except PdfLimitError:
        check("超过字节上限被拒", True)
    except Exception as exc:  # noqa: BLE001
        check("超过字节上限被拒", False, type(exc).__name__)
    else:
        check("超过字节上限被拒", False, "没有抛异常")

    # ---------- ③ 切块边界 ----------
    paragraphs = [
        "第%d段：" % i + "会员数据平台需要打通线上商城与线下门店的数据。" * 12
        for i in range(1, 8)
    ]
    chunks = chunk_text("\n\n".join(paragraphs), page=3)
    check("长文切出多块", len(chunks) >= 2, "chunks=%d" % len(chunks))
    check("每块 ≤ 600 字", all(len(c["text"]) <= 600 for c in chunks),
          str([len(c["text"]) for c in chunks]))
    check("ordinal 连续", [c["ordinal"] for c in chunks] == list(range(1, len(chunks) + 1)))
    check("块间有 80 字重叠",
          chunks[1]["text"].startswith(chunks[0]["text"][-OVERLAP_CHARS:]),
          chunks[1]["text"][:20])
    joined = "".join(c["text"].replace("\n", "") for c in chunks)
    check("切块不丢段落", all(p[:20] in joined for p in paragraphs))
    check("单页短文本一块", len(chunk_text("只有一句话。", page=7)) == 1)

    two_pages = chunk_pages(
        [{"page": 1, "text": "甲" * 500}, {"page": 2, "text": "乙" * 500}]
    )
    check("跨页切块保留页码", {c["page"] for c in two_pages} == {1, 2},
          str([(c["ordinal"], c["page"], len(c["text"])) for c in two_pages]))

    # ---------- ④ FTS5 + jieba ----------
    check("jieba 把预算切成检索词", "预算" in tokenize("星云项目的预算"))

    tmp = Path(tempfile.mkdtemp(prefix="aiwainao-pdf-"))
    try:
        s = dataclasses.replace(base_settings, data_dir=tmp)
        repo.init_db(s.db_path)
        index = DocIndex(s)

        first = run_pdf_ingest(s, session_id="s1", filename=SAMPLE_PDF.name, raw=raw)
        check("PDF 入库返回 doc_id/pages/chunks",
              bool(first["doc_id"]) and first["pages"] == 2 and first["chunks"] >= 1,
              str(first))
        check("首次入库 status=ready", first["status"] == "ready", first["status"])

        again = run_pdf_ingest(s, session_id="s2", filename=SAMPLE_PDF.name, raw=raw)
        check("重复上传复用同一 doc", again["doc_id"] == first["doc_id"] and again["status"] == "reused",
              str(again))
        with repo.db(s.db_path) as conn:
            check("重复上传不膨胀 chunks",
                  repo.count_chunks(conn, first["doc_id"]) == first["chunks"],
                  str(repo.count_chunks(conn, first["doc_id"])))

        hits = index.search("预算", 5)
        check("中文 query 命中", bool(hits), "hits=%d" % len(hits))
        check("命中带页码与 chunk_id",
              bool(hits) and hits[0]["page"] == 1 and hits[0]["chunk_id"].endswith(":c001"),
              str(hits[:1]))
        check("命中带 score", bool(hits) and isinstance(hits[0]["score"], float),
              str(hits[:1]))
        check("score 按相关度降序", all(
            hits[i]["score"] >= hits[i + 1]["score"] for i in range(len(hits) - 1)
        ), str([h["score"] for h in hits]))

        check("无答案返回空", index.search("量子引力波", 5) == [])
        check("空 query 返回空", index.search("", 5) == [])
        check("纯标点 query 返回空", index.search("？？？", 5) == [])

        # 第二份文档：跨文档检索
        with repo.db(s.db_path) as conn:
            doc2 = repo.insert_doc(
                conn, session_id="s1", kind="pdf", path=str(tmp / "fake2.pdf"),
                sha256="f" * 64, pages=1,
            )
        index.add(doc2, [{"ordinal": 1, "page": 1,
                          "text": "办公室咖啡机采购由行政部负责，预算 2 万元。"}])
        cross = index.search("预算 咖啡机", 10)
        doc_ids = {h["doc_id"] for h in cross}
        check("跨文档检索命中两份文档", doc_ids == {first["doc_id"], doc2}, str(doc_ids))
        only2 = index.search("预算 咖啡机", 10, doc_id=doc2)
        check("doc_id 过滤生效", only2 and {h["doc_id"] for h in only2} == {doc2}, str(only2))

        # 同一 doc 重建索引：旧块与旧 FTS 行都要清掉
        index.add(doc2, [{"ordinal": 1, "page": 1,
                          "text": "办公室打印机耗材由行政部统一采购。"}])
        with repo.db(s.db_path) as conn:
            check("重建索引不膨胀 chunks",
                  repo.count_chunks(conn, doc2) == 1,
                  str(repo.count_chunks(conn, doc2)))
        check("重建后旧词不再命中", index.search("咖啡机", 5, doc_id=doc2) == [])
        check("重建后新词可命中", bool(index.search("打印机", 5, doc_id=doc2)))

        # ---------- ⑤ doc_search 工具 ----------
        registry = ToolRegistry()
        registry.register(build_doc_search(s))
        result = registry.call("doc_search", {"query": "预算", "k": 3})
        check("registry.call(doc_search) 成功", result["ok"], str(result.get("error")))
        data = result.get("data") or {}
        check("工具返回 chunks + references",
              bool(data.get("chunks")) and bool(data.get("references")),
              "chunks=%d refs=%d" % (len(data.get("chunks") or []), len(data.get("references") or [])))
        ref = (data.get("references") or [{}])[0]
        check("reference 带 page/chunk_id",
              ref.get("page") is not None and bool(ref.get("chunk_id")), str(ref))

        bad = registry.call("doc_search", {"query": "预算", "k": 999})
        check("k 被夹到 5", bad["ok"] and len(bad["data"]["chunks"]) <= 5,
              str(len(bad["data"]["chunks"]) if bad["ok"] else bad.get("error")))
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
