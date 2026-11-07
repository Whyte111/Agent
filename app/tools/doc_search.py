"""doc_search 工具：检索已上传 PDF 的 chunk（全局知识库，跨会话）。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.retrieval.index import DocIndex
from app.tools.registry import Tool

MAX_K = 5          # 7.3：只把 top-3~5 个块拼进 prompt
MAX_CHUNK_CHARS = 800

SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "要在 PDF 知识库里检索的问题或关键词"},
        "k": {"type": "integer", "description": "返回块数，默认 5，最多 5"},
        "doc_id": {"type": "string", "description": "只检索某份文档；不填则跨全部已上传 PDF"},
    },
    "required": ["query"],
}


def doc_search(
    query: str,
    k: int = MAX_K,
    doc_id: Optional[str] = None,
    *,
    settings=None,
) -> Dict[str, Any]:
    if settings is None:
        raise RuntimeError("doc_search 缺少 settings")
    query = (query or "").strip()[:500]
    k = max(1, min(int(k or MAX_K), MAX_K))
    if not query:
        return {"chunks": [], "references": [], "count": 0, "query": ""}

    chunks = DocIndex(settings).search(query, k, doc_id=doc_id)
    for chunk in chunks:
        chunk["text"] = chunk["text"][:MAX_CHUNK_CHARS]

    references: List[Dict[str, Any]] = [
        {
            "type": "pdf_chunk",
            "title": "PDF %s 第 %s 页" % (chunk["doc_id"], chunk["page"]),
            "doc_id": chunk["doc_id"],
            "chunk_id": chunk["chunk_id"],
            "page": chunk["page"],
            "ordinal": chunk["ordinal"],
            "score": chunk["score"],
        }
        for chunk in chunks
    ]
    return {
        "chunks": chunks,
        "references": references,
        "count": len(chunks),
        "query": query,
    }


def build(settings) -> Tool:
    return Tool(
        name="doc_search",
        description=(
            "检索已上传 PDF 的正文（全局知识库，跨会话累积）。"
            "用户问到 PDF / 文档里的内容时先调用它；返回 chunk 原文、doc_id、页码和 chunk_id。"
            "结果为空就如实说文档里没有，不要编。回答时标出页码或 chunk_id。"
        ),
        parameters=SCHEMA,
        func=lambda query, k=MAX_K, doc_id=None: doc_search(
            query, k, doc_id, settings=settings
        ),
    )
