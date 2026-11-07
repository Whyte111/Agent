"""三道闸门（骨架 8.4）——全项目唯一"代码说了算"的地方。

① 证据校验: evidence_quote 在原文匹配不到 → 整条丢弃，不入库
② 枚举校验: severity / basis 非法 → severity 降为 low，basis 归 action
③ 发信门控: severity == high 且通过① → 才允许发信

不使用置信度分值。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

SEVERITIES = ("high", "mid", "low")
BASES = ("owner", "deadline", "action")

MIN_EVIDENCE_CHARS = 4  # 太短的"证据"没有判别力

# 去空白后仍允许对比；标点级差异（中英标点混排）再降一级匹配
_PUNCT = re.compile(
    r"[\s，。、！？；：,\.!\?;:\"'“”‘’（）\(\)《》<>【】\[\]…—\-~·`]+"
)


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _loose(text: str) -> str:
    return _PUNCT.sub("", (text or "").lower())


def evidence_matches(quote: str, raw_text: str) -> Dict[str, Any]:
    """闸门①：返回 {ok, tier, reason}。tier 用于页面解释"为什么这条证据被采信"。"""
    compact_quote = _compact(quote)
    if len(compact_quote) < MIN_EVIDENCE_CHARS:
        return {"ok": False, "tier": None, "reason": "evidence_too_short"}
    if compact_quote in _compact(raw_text):
        return {"ok": True, "tier": "exact", "reason": "matched_ignoring_whitespace"}
    loose_quote = _loose(quote)
    if len(loose_quote) >= MIN_EVIDENCE_CHARS and loose_quote in _loose(raw_text):
        return {"ok": True, "tier": "loose", "reason": "matched_ignoring_whitespace_and_punct"}
    return {"ok": False, "tier": None, "reason": "evidence_not_in_source"}


def normalize_severity(value: Optional[str]) -> Tuple[str, bool]:
    """闸门②：非法值降为 low。返回 (severity, 是否被改写)。"""
    lowered = (value or "").strip().lower()
    if lowered in SEVERITIES:
        return lowered, lowered != value
    return "low", True


def normalize_basis(value: Optional[str]) -> Tuple[str, bool]:
    lowered = (value or "").strip().lower()
    if lowered in BASES:
        return lowered, lowered != value
    return "action", True


def apply_gates(risks: List[Dict[str, Any]], raw_text: str) -> Dict[str, Any]:
    """顺序执行：证据校验 → 枚举校验。输出 kept / dropped / notes。"""
    kept: List[Dict[str, Any]] = []
    dropped: List[Dict[str, Any]] = []
    downgraded = 0
    notes: List[str] = []

    for index, risk in enumerate(risks):
        quote = risk.get("evidence_quote") or ""
        check = evidence_matches(quote, raw_text)
        if not check["ok"]:
            dropped.append(
                {
                    "index": index,
                    "evidence_quote": quote,
                    "severity": risk.get("severity"),
                    "reason": check["reason"],
                    "detail": "证据在原文中找不到，整条丢弃（闸门①）",
                }
            )
            continue

        severity, sev_changed = normalize_severity(risk.get("severity"))
        basis, basis_changed = normalize_basis(risk.get("basis"))
        if sev_changed:
            downgraded += 1
            notes.append(
                "第 %d 条 severity=%r 非法，已降为 low（闸门②）"
                % (index + 1, risk.get("raw_severity", risk.get("severity")))
            )
        if basis_changed:
            notes.append(
                "第 %d 条 basis=%r 非法，已归为 action（闸门②）"
                % (index + 1, risk.get("raw_basis", risk.get("basis")))
            )

        kept.append(
            {
                "basis": basis,
                "severity": severity,
                "evidence_quote": quote,
                "suggestion": risk.get("suggestion"),
                "owner": risk.get("owner"),
                "due": risk.get("due"),
                "evidence_tier": check["tier"],
            }
        )

    return {"kept": kept, "dropped": dropped, "downgraded": downgraded, "notes": notes}


def should_send(kept_risks: List[Dict[str, Any]]) -> bool:
    """闸门③：只认 high，且此处入参已经过闸门①。"""
    return any(r.get("severity") == "high" for r in kept_risks)


def needs_todo(severity: str) -> bool:
    """high / mid 生成待办；low 仅记录。"""
    return severity in ("high", "mid")


def mail_decision(kept_risks: List[Dict[str, Any]]) -> str:
    """给页面/日志用的一句话结论。"""
    if not kept_risks:
        return "not_needed"
    high = [r for r in kept_risks if r["severity"] == "high"]
    if high:
        return "send"
    return "no_high_risk"
