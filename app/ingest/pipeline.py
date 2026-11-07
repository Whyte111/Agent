"""PDF 上传链路：抽取 → 切块 → 落 docs/chunks → 建 FTS 索引。

与 meeting/pipeline.py 同构：ingest 只负责把文件变成可检索的块，
检索能力委托 retrieval.index，落库委托 store.repo。
扫描件 / 超页 / 超大在这里抛 PdfError 子类，由 API 层映射状态码。
"""
from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from typing import Any, Dict

from app.config.settings import Settings
from app.ingest.chunker import chunk_pages
from app.ingest.pdf import MAX_PAGES, PdfError, PdfLimitError, extract_pdf
from app.retrieval.index import DocIndex
from app.store import repo


def _save_pdf(settings: Settings, raw: bytes) -> Path:
    """落盘名由服务端生成 UUID，不用用户文件名（骨架 7 节防穿越）。"""
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    path = settings.upload_dir / ("%s.pdf" % uuid.uuid4().hex)
    path.write_bytes(raw)
    return path


def _reused(session_id: str, doc: Dict[str, Any], chunk_count: int) -> Dict[str, Any]:
    return {
        "session_id": session_id,
        "doc_id": doc["id"],
        "pages": int(doc.get("pages") or 0),
        "chunks": chunk_count,
        "status": "reused",
        "sha256": doc["sha256"],
    }


def run_pdf_ingest(
    settings: Settings,
    *,
    session_id: str,
    filename: str,
    raw: bytes,
) -> Dict[str, Any]:
    if not (filename or "").lower().endswith(".pdf"):
        raise PdfError("PDF 上传只接受 .pdf 文件")
    if len(raw) > settings.max_upload_bytes:
        raise PdfLimitError(
            "文件超过上限 %d MB" % (settings.max_upload_bytes // (1024 * 1024))
        )

    sha256 = hashlib.sha256(raw).hexdigest()

    # 先做一次只读幂等检查：同一内容重复上传不重复建索引。
    with repo.db(settings.db_path) as conn:
        existing = repo.find_doc_by_sha(conn, sha256)
        if existing is not None:
            if existing["kind"] != "pdf":
                raise PdfError(
                    "该文件内容已作为 %s 上传过，不能按 PDF 知识库处理" % existing["kind"]
                )
            count = repo.count_chunks(conn, existing["id"])
            if count > 0:
                return _reused(session_id, existing, count)

    extracted = extract_pdf(
        raw, max_pages=MAX_PAGES, max_bytes=settings.max_upload_bytes
    )
    pages = [{"page": i, "text": text} for i, text in enumerate(extracted["page_texts"], 1)]
    chunks = chunk_pages(pages)
    if not chunks:
        raise PdfError("PDF 未切出任何文本块")

    saved_path = _save_pdf(settings, raw)
    with repo.db(settings.db_path) as conn:
        doc = repo.find_doc_by_sha(conn, sha256)
        if doc is not None:
            if doc["kind"] != "pdf":
                raise PdfError(
                    "该文件内容已作为 %s 上传过，不能按 PDF 知识库处理" % doc["kind"]
                )
            doc_id = doc["id"]
            status = "reused"
        else:
            doc_id = repo.insert_doc(
                conn,
                session_id=session_id,
                kind="pdf",
                path=str(saved_path),
                sha256=sha256,
                pages=extracted["pages"],
                status="ready",
            )
            status = "ready"
        DocIndex(settings).add(doc_id, chunks, conn=conn)

    return {
        "session_id": session_id,
        "doc_id": doc_id,
        "pages": extracted["pages"],
        "chunks": len(chunks),
        "status": status,
        "sha256": sha256,
        "chars": extracted["chars"],
    }
