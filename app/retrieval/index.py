"""FTS5 + jieba 检索（骨架 retrieval：建索引、按 query 召回 top-k、返回出处）。

为什么必须预分词：FTS5 默认 unicode61 把整句中文当一个 token，搜「环境」命中 0。
建索引与查询都用 jieba.cut_for_search，查询词用 OR 连接，靠 bm25 排序。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from app.store import repo

_TOKEN_RE = re.compile(r"[0-9A-Za-z_\u3400-\u9fff]+")


def tokenize(text: str) -> List[str]:
    import jieba

    tokens: List[str] = []
    for raw in jieba.cut_for_search(text or ""):
        token = (raw or "").strip().lower()
        if token and _TOKEN_RE.fullmatch(token):
            tokens.append(token)
    return tokens


def index_text(text: str) -> str:
    return " ".join(tokenize(text))


def _match_query(query: str) -> str:
    terms: List[str] = []
    seen = set()
    for term in tokenize(query):
        if term not in seen:
            seen.add(term)
            terms.append(term)
    if not terms:
        return ""
    # 引号包住单个词，避免 * ( ) 等被 FTS5 当语法
    return " OR ".join('"%s"' % term.replace('"', '""') for term in terms)


class DocIndex:
    def __init__(self, settings) -> None:
        self.settings = settings

    def add(self, doc_id: str, chunks: List[Dict[str, Any]], conn=None) -> int:
        """重建某文档的 chunks 与 FTS 行；返回写入块数。"""
        def _write(c) -> int:
            repo.delete_chunks_for_doc(c, doc_id)
            for chunk in chunks:
                ordinal = int(chunk["ordinal"])
                page = int(chunk.get("page") or 1)
                text = chunk["text"]
                repo.insert_chunk(
                    c,
                    chunk_id="%s:c%03d" % (doc_id, ordinal),
                    doc_id=doc_id,
                    ordinal=ordinal,
                    text=text,
                    page=page,
                )
                repo.insert_fts_chunk(
                    c,
                    chunk_id="%s:c%03d" % (doc_id, ordinal),
                    doc_id=doc_id,
                    page=page,
                    ordinal=ordinal,
                    text=text,
                    body=index_text(text),
                )
            return len(chunks)

        if conn is not None:
            return _write(conn)
        with repo.db(self.settings.db_path) as c:
            return _write(c)

    def search(
        self, query: str, k: int = 5, doc_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        match = _match_query(query)
        if not match:
            return []
        k = max(1, min(int(k or 5), 50))
        with repo.db(self.settings.db_path) as conn:
            rows = repo.search_chunks(conn, match, k, doc_id=doc_id)
        results: List[Dict[str, Any]] = []
        for row in rows:
            raw = float(row.get("raw_score") or 0.0)
            results.append(
                {
                    "chunk_id": row["chunk_id"],
                    "doc_id": row["doc_id"],
                    "page": row["page"],
                    "ordinal": row["ordinal"],
                    "text": row["text"],
                    # bm25 越负越相关，转成正分便于展示与排序断言
                    "score": round(-raw, 8),
                }
            )
        return results
