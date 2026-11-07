"""PDF 抽文本（骨架 ingest：只做抽取，不做检索）。

只支持文本层 PDF；扫描件（图片型）明确报错，不静默返回空。
上限在 API 层与这里各校验一次：≤50 页、≤20MB。
"""
from __future__ import annotations

import io
import re
from typing import Any, Dict, List, Optional

from app.ingest.txt import IngestError, normalize_text

MAX_PAGES = 50
# 全文字数 / 页数低于该值 → 判为扫描件。实测文本层 PDF 每页通常数百字，
# 扫描件为 0；阈值取 20 给封面/图表页留余量。
MIN_CHARS_PER_PAGE = 20


class PdfError(IngestError):
    """PDF 不可用（损坏 / 加密 / 格式不对）。"""


class PdfLimitError(PdfError):
    """超过页数或大小上限，API 层映射为 413。"""


class ScannedPdfError(PdfError):
    """没有文本层（扫描件），API 层给出明确提示，不做 OCR。"""


def _reader(raw: bytes) -> Any:
    if not raw:
        raise PdfError("文件为空")
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # .vendor 没装上时给出可操作提示
        raise PdfError("缺少 pypdf：请执行 pip install --target .vendor pypdf") from exc
    try:
        return PdfReader(io.BytesIO(raw))
    except Exception as exc:
        raise PdfError("PDF 解析失败（文件可能损坏或不是 PDF）: %s" % exc)


def extract_pdf(
    raw: bytes,
    *,
    max_pages: int = MAX_PAGES,
    max_bytes: Optional[int] = None,
) -> Dict[str, Any]:
    """逐页抽文本，返回 {pages, page_texts, chars, empty_pages}。

    - 超页数 / 超大小 → PdfLimitError
    - 平均每页字数 < MIN_CHARS_PER_PAGE → ScannedPdfError
    """
    if max_bytes is not None and len(raw) > max_bytes:
        raise PdfLimitError(
            "文件超过上限 %d MB" % max(1, max_bytes // (1024 * 1024))
        )

    reader = _reader(raw)
    if getattr(reader, "is_encrypted", False):
        try:
            unlocked = reader.decrypt("")
        except Exception:
            unlocked = 0
        if not unlocked:
            raise PdfError("PDF 已加密，无法读取；请上传解密后的版本")

    try:
        total_pages = len(reader.pages)
    except Exception as exc:
        raise PdfError("PDF 页数读取失败: %s" % exc)
    if total_pages == 0:
        raise PdfError("PDF 没有页面")
    if total_pages > max_pages:
        raise PdfLimitError("PDF 共 %d 页，超过上限 %d 页" % (total_pages, max_pages))

    page_texts: List[str] = []
    empty_pages: List[int] = []
    for index, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        text = normalize_text(text)
        if not text:
            empty_pages.append(index)
        page_texts.append(text)

    chars = sum(len(_compact(text)) for text in page_texts)
    if chars / total_pages < MIN_CHARS_PER_PAGE:
        raise ScannedPdfError(
            "不支持扫描版 PDF：%d 页仅提取到 %d 个字符（阈值为平均每页 %d 字）。"
            "本 Demo 不做 OCR，请上传带文本层的 PDF。"
            % (total_pages, chars, MIN_CHARS_PER_PAGE)
        )

    return {
        "pages": total_pages,
        "page_texts": page_texts,
        "chars": chars,
        "empty_pages": empty_pages,
    }


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def page_preview(page_texts: List[str], width: int = 80) -> List[str]:
    """验收用：每页前 width 字。"""
    return [text[:width] for text in page_texts]
