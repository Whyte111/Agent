"""TXT 规范化：编码容错 + 换行统一 + 分段。

只做规范化，不做抽取；PDF 分支在 ingest/pdf.py（后续步骤）。
"""
from __future__ import annotations

import re
from typing import List

_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "gbk", "big5")


class IngestError(ValueError):
    """输入不合法（空文件 / 编码不可识别 / 超限）。"""


def decode_txt(raw: bytes) -> str:
    if not raw:
        raise IngestError("文件为空")
    for enc in _ENCODINGS:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise IngestError("无法识别的文本编码（已尝试 utf-8 / gb18030 / gbk / big5）")


def normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u3000", " ").replace("\ufeff", "")
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def paragraphs(text: str) -> List[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def normalize_txt(raw: bytes) -> str:
    text = normalize_text(decode_txt(raw))
    if not text:
        raise IngestError("文件为空或只有空白字符")
    return text


def compact(text: str) -> str:
    """去掉所有空白，用于证据匹配（代码判定，不受排版影响）。"""
    return re.sub(r"\s+", "", text or "")
