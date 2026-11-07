"""实体名归一化：决定"同一个实体"的判定标准（骨架不变式：Entity 在 (type, 归一化名) 上唯一）。"""
from __future__ import annotations

import re
import unicodedata
from typing import Tuple

_ROLE_SUFFIX = re.compile(r"[（(]\s*([^）)]{1,24})\s*[）)]\s*$")
_PUNCT = re.compile(r"[\s，。、！？；：,\.!\?;:\"'“”‘’（）\(\)《》<>【】\[\]…—\-~·`|]+")


def normalize(text: str) -> str:
    """NFKC 统一全半角 → 去空白标点 → 小写。用于唯一键，不用于展示。"""
    value = unicodedata.normalize("NFKC", text or "")
    value = _PUNCT.sub("", value)
    return value.lower()


def split_person(name: str) -> Tuple[str, str]:
    """'张伟（销售）' → ('张伟', '销售')；没有角色后缀就原样返回。"""
    raw = (name or "").strip()
    match = _ROLE_SUFFIX.search(raw)
    if match:
        return raw[: match.start()].strip(), match.group(1).strip()
    return raw, ""


def clean_display(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())
