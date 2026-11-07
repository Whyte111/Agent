"""PDF/TXT 切块：按段落 → 句子 → 硬切，块间保留 80 字重叠。

约束（HANDOFF 7.3）：
- 单块正文 430–500 字，加上前一块的 80 字重叠后 ≤ 600；
- 每块带 page + ordinal，供回答标出处；
- 只做切块，不建索引、不检索。
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List

from app.ingest.txt import normalize_text

TARGET_CHARS = 430
MAX_CHARS = 500       # 正文上限；加 80 字重叠后仍 ≤ 600
OVERLAP_CHARS = 80

_PARA_SPLIT = re.compile(r"\n\s*\n")
_SENT_SPLIT = re.compile(r"(?<=[。！？；!?;])\s*")
_CJK = re.compile(r"[\u3400-\u9fff]")


def _soft_join_lines(text: str) -> str:
    """PDF 每行一个 \\n：中文行去掉换行，英文行换成空格，避免把句子切碎。"""
    def repl(match: "re.Match[str]") -> str:
        left = text[match.start() - 1] if match.start() > 0 else ""
        right = text[match.end()] if match.end() < len(text) else ""
        return "" if (_CJK.search(left) or _CJK.search(right)) else " "

    return re.sub(r"(?<!\n)\n(?!\n)", repl, text)


def _split_long(text: str, limit: int) -> List[str]:
    """无标点的长段硬切；保证每段 ≤ limit。"""
    return [text[i : i + limit] for i in range(0, len(text), limit)] if text else []


def _units_for_page(text: str) -> List[str]:
    text = _soft_join_lines(normalize_text(text).replace("\x00", ""))
    units: List[str] = []
    for block in _PARA_SPLIT.split(text):
        block = block.strip()
        if not block:
            continue
        if len(block) <= MAX_CHARS:
            units.append(block)
            continue
        for sentence in _SENT_SPLIT.split(block):
            sentence = sentence.strip()
            if not sentence:
                continue
            if len(sentence) <= MAX_CHARS:
                units.append(sentence)
            else:
                units.extend(_split_long(sentence, MAX_CHARS))
    return units


def chunk_pages(pages: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """pages: [{page: 1, text: "..."}] → [{ordinal, page, text}]，ordinal 从 1 开始。

    按页切：一个 chunk 只包含同一页的文字，保证回答引用的页码准确。
    重叠只发生在同一页内（跨页重叠会让「第 N 页」指到上一页的内容）。
    """
    chunks: List[Dict[str, Any]] = []

    for item in pages:
        page = int(item.get("page") or 1)
        units = _units_for_page(item.get("text") or "")
        page_chunks: List[Dict[str, Any]] = []
        current: List[str] = []
        current_len = 0

        def flush() -> None:
            nonlocal current, current_len
            text = "\n".join(current).strip()
            if text:
                page_chunks.append({"page": page, "text": text})
            current = []
            current_len = 0

        for unit in units:
            if current and current_len + len(unit) > MAX_CHARS:
                flush()
            current.append(unit)
            current_len += len(unit) + 1
            if current_len >= TARGET_CHARS:
                flush()
        flush()

        # 同页块间重叠：把上一块末尾 80 字前置到当前块。首块不加。
        for index in range(1, len(page_chunks)):
            tail = page_chunks[index - 1]["text"][-OVERLAP_CHARS:]
            if tail:
                page_chunks[index]["text"] = tail + "\n" + page_chunks[index]["text"]

        chunks.extend(page_chunks)

    for index, chunk in enumerate(chunks, start=1):
        chunk["ordinal"] = index
        chunk["chars"] = len(chunk["text"])
    return chunks


def chunk_text(text: str, page: int = 1) -> List[Dict[str, Any]]:
    return chunk_pages([{"page": page, "text": text}])
