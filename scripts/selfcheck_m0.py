"""M0 最小闭环自检（离线、零外部依赖、10 秒内跑完）。

验证的不是"模型准不准"，而是这四件事是真实代码：
  1. 闸门① 证据校验：原文找不到的 evidence_quote 整条丢弃
  2. 闸门② 枚举校验：非法 severity 降为 low
  3. 闸门③ 发信门控：只有 high 才发信；mid/low 不发
  4. 幂等：同一文件第二次上传不重复发信，且复用已有会议（不再调用模型）

用法: python -m scripts.selfcheck_m0
"""
from __future__ import annotations

import dataclasses
import json
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[1]

from app.config.settings import settings as base_settings  # noqa: E402
from app.ingest.txt import normalize_txt  # noqa: E402
from app.meeting import rules  # noqa: E402
from app.meeting.pipeline import run_meeting  # noqa: E402
from app.store import repo  # noqa: E402

RESULTS: List[Tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((ok, name, detail))


class FakeMailer:
    """测试替身：模拟 SMTP 成功，用于验证幂等（不产生真实副作用）。"""

    mode = "fake"

    def __init__(self):
        self.sent: List[Dict[str, Any]] = []

    def send(self, to, subject, text, html=None):
        from app.mail.sender import SendResult

        self.sent.append({"to": list(to), "subject": subject, "body": text})
        return SendResult("sent", "fake smtp ok", list(to), "fake-msg-id")


class FailingMailer:
    mode = "fake-fail"

    def send(self, to, subject, text, html=None):
        from app.mail.sender import SendResult

        return SendResult("failed", "smtp down", list(to))


def gate_unit_checks() -> None:
    raw = "李娜：那谁跟进一下环境？必须有人在周五前把环境搭起来。"
    ok = rules.evidence_matches("那谁跟进一下环境？", raw)
    check("闸门① 正常证据可匹配", ok["ok"], ok["tier"] or "")

    bad = rules.evidence_matches("数据库迁移的时间还没定", raw)
    check("闸门① 编造证据被拒", not bad["ok"], bad["reason"])

    short = rules.evidence_matches("好", raw)
    check("闸门① 过短证据被拒", not short["ok"], short["reason"])

    spaced = rules.evidence_matches("那谁跟进一下环境 ？", raw)
    check("闸门① 忽略空白差异", spaced["ok"], spaced["tier"] or "")

    sev, changed = rules.normalize_severity("CRITICAL")
    check("闸门② 非法 severity 降为 low", sev == "low" and changed, sev)
    sev2, changed2 = rules.normalize_severity("high")
    check("闸门② 合法 severity 不被改写", sev2 == "high" and not changed2, sev2)
    basis, _ = rules.normalize_basis("whatever")
    check("闸门② 非法 basis 归为 action", basis == "action", basis)

    check("闸门③ 只有 high 触发发信",
          rules.should_send([{"severity": "mid"}]) is False
          and rules.should_send([{"severity": "high"}]) is True)
    check("待办规则 high/mid 才有待办",
          rules.needs_todo("high") and rules.needs_todo("mid") and not rules.needs_todo("low"))


def run_samples(tmp: Path) -> None:
    s = dataclasses.replace(
        base_settings,
        data_dir=tmp,
        mock_dir=ROOT / "data" / "mock",
        mail_dry_run_forced=True,
        # 自检必须离线确定：即使 .env 里配了真 key，也强制走 mock，避免花钱且保证可复现
        llm_api_key="",
        llm_mock_forced=True,
    )
    repo.init_db(s.db_path)
    samples = ROOT / "data" / "samples"

    # ---------- S1 高风险 ----------
    s1 = repo  # 占位，避免 lint 误解
    res1 = run_meeting(
        s,
        session_id="sess-s1",
        filename="S1_高风险.txt",
        raw=(samples / "S1_高风险.txt").read_bytes(),
        mail_to="demo@example.com",
    )
    high1 = [r for r in res1["risks"] if r["severity"] == "high"]
    check("S1 有 ≥2 条 high", len(high1) >= 2, "high=%d" % len(high1))
    check("S1 生成待办", len(res1["todos"]) == 2, "todos=%d" % len(res1["todos"]))
    check("S1 触发发信（dry-run 落 .eml）", res1["mail_status"] == "dry_run", res1["mail_status"])
    check("S1 落 send_log", len(res1["send_logs"]) == 1, "send_logs=%d" % len(res1["send_logs"]))
    emls = list((tmp / "outbox").glob("*.eml"))
    check("S1 生成可核验的 .eml 文件", len(emls) == 1, str([e.name for e in emls]))
    if emls:
        import email as email_lib

        msg = email_lib.message_from_bytes(emls[0].read_bytes())
        parts = []
        for part in msg.walk():
            if part.get_content_maintype() == "text":
                payload = part.get_payload(decode=True) or b""
                parts.append(payload.decode(part.get_content_charset() or "utf-8", "replace"))
        body = "\n".join(parts)
        check("S1 .eml 正文含高风险证据", "客户要求周五前把现场演示环境搭好" in body, "len=%d" % len(body))
        check("S1 .eml 收件人正确", "demo@example.com" in (msg["To"] or ""), msg["To"] or "")

    # ---------- S2 无风险 ----------
    res2 = run_meeting(
        s,
        session_id="sess-s2",
        filename="S2_无风险.txt",
        raw=(samples / "S2_无风险.txt").read_bytes(),
        mail_to="demo@example.com",
    )
    check("S2 无风险项", len(res2["risks"]) == 0, "risks=%d" % len(res2["risks"]))
    check("S2 不生成待办", len(res2["todos"]) == 0, "todos=%d" % len(res2["todos"]))
    check("S2 mail_status=not_needed", res2["mail_status"] == "not_needed", res2["mail_status"])
    check("S2 不落 send_log", len(res2["send_logs"]) == 0, "send_logs=%d" % len(res2["send_logs"]))

    # ---------- S3 边界（关键：门控不能误触发）----------
    res3 = run_meeting(
        s,
        session_id="sess-s3",
        filename="S3_边界.txt",
        raw=(samples / "S3_边界.txt").read_bytes(),
        mail_to="demo@example.com",
    )
    check("S3 丢弃编造证据 1 条", res3["gates"]["dropped"] == 1,
          json.dumps(res3["gates"]["dropped_items"], ensure_ascii=False)[:120])
    check("S3 保留 5 条（3 mid + 2 low）", res3["gates"]["kept"] == 5,
          "kept=%d" % res3["gates"]["kept"])
    check("S3 0 条 high", not any(r["severity"] == "high" for r in res3["risks"]))
    check("S3 不发信（门控未误触发）", res3["mail_status"] == "not_needed", res3["mail_status"])
    check("S3 仅 mid 生成待办(3 条)", len(res3["todos"]) == 3, "todos=%d" % len(res3["todos"]))

    # ---------- 幂等：同文件第二次上传（真发信场景）----------
    fake = FakeMailer()
    dup = run_meeting(
        s,
        session_id="sess-s1-again",
        filename="S1_高风险.txt",
        raw=(samples / "S1_高风险.txt").read_bytes(),
        mail_to="demo@example.com",
        mailer=fake,
    )
    check("重复上传命中已有会议", dup["reused"] is True, "reused=%s" % dup["reused"])
    check("重复上传不再调用模型（notes 可查）",
          any("幂等" in n for n in dup["gates"]["notes"]),
          " / ".join(dup["gates"]["notes"])[:60])

    # 首次真发（fake 成功）→ 第二次必须 skipped_duplicate
    import dataclasses as dc

    s2 = dc.replace(s, data_dir=tmp / "idem")
    repo.init_db(s2.db_path)
    fake2 = FakeMailer()
    raw1 = (samples / "S1_高风险.txt").read_bytes()
    first = run_meeting(s2, session_id="a", filename="S1_高风险.txt", raw=raw1,
                        mail_to="demo@example.com", mailer=fake2)
    second = run_meeting(s2, session_id="b", filename="S1_高风险.txt", raw=raw1,
                         mail_to="demo@example.com", mailer=fake2)
    check("首次真发成功", first["mail_status"] == "sent", first["mail_status"])
    check("第二次跳过（幂等）", second["mail_status"] == "skipped_duplicate",
          second["mail_status"])
    check("只发出 1 封", len(fake2.sent) == 1, "sent=%d" % len(fake2.sent))
    check("待办状态变为 sent",
          all(t["status"] == "sent" for t in first["todos"]),
          ", ".join(sorted({t["status"] for t in first["todos"]})))

    # ---------- 发信失败不影响风险结论 ----------
    s3 = dc.replace(s, data_dir=tmp / "failmail")
    repo.init_db(s3.db_path)
    failed = run_meeting(s3, session_id="c", filename="S1_高风险.txt",
                         raw=raw1, mail_to="demo@example.com",
                         mailer=FailingMailer())
    check("发信失败仍返回风险结论", len(failed["risks"]) == 2,
          "risks=%d" % len(failed["risks"]))
    check("发信失败 mail_status=failed", failed["mail_status"] == "failed",
          failed["mail_status"])
    check("失败也落 send_log（error 可查）",
          failed["send_logs"] and failed["send_logs"][0]["status"] == "failed",
          failed["send_logs"][0]["error"] if failed["send_logs"] else "")
    retried = run_meeting(s3, session_id="d", filename="S1_高风险.txt",
                          raw=raw1, mail_to="demo@example.com", mailer=FakeMailer())
    check("失败后可重发（dedupe 未被失败占用）", retried["mail_status"] == "sent",
          retried["mail_status"])

    # ---------- 输入校验 ----------
    try:
        run_meeting(s, session_id="e", filename="S1_高风险.txt", raw=b"", mail_to="x@y.com")
        check("空文件报错", False, "未抛错")
    except ValueError as exc:
        check("空文件报错", True, str(exc)[:40])
    try:
        run_meeting(s, session_id="e", filename="a.pdf", raw=b"x", mail_to="x@y.com")
        check("错格式报错", False, "未抛错")
    except ValueError as exc:
        check("错格式报错", True, str(exc)[:40])
    try:
        run_meeting(s, session_id="e", filename="S1_高风险.txt",
                    raw=raw1, mail_to="not-an-email")
        check("非法收件人报错", False, "未抛错")
    except ValueError as exc:
        check("非法收件人报错", True, str(exc)[:40])

    # ---------- 落库核对 ----------
    conn = sqlite3.connect(str(s.db_path))
    conn.row_factory = sqlite3.Row
    n_meet = conn.execute("SELECT COUNT(*) c FROM meetings").fetchone()["c"]
    n_risk = conn.execute("SELECT COUNT(*) c FROM risk_items").fetchone()["c"]
    logs = [dict(r) for r in conn.execute("SELECT * FROM send_logs ORDER BY ts").fetchall()]
    conn.close()
    check("3 份样本落 3 个会议", n_meet == 3, "meetings=%d" % n_meet)
    check("风险总数 2+0+5=7", n_risk == 7, "risk_items=%d" % n_risk)
    check("send_log 只有 S1 的两条（dry-run + fake 重发）", len(logs) == 2,
          "send_logs=%d" % len(logs))
    if len(logs) == 2:
        check("dry-run 行不占用 dedupe_key", logs[0]["status"] == "dry_run" and logs[0]["dedupe_key"] is None,
              "%s/%s" % (logs[0]["status"], logs[0]["dedupe_key"]))
        check("成功行占用唯一 dedupe_key", logs[1]["status"] == "sent" and bool(logs[1]["dedupe_key"]),
              "%s/%s" % (logs[1]["status"], logs[1]["dedupe_key"]))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="aiwainao-m0-"))
    try:
        gate_unit_checks()
        run_samples(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    passed = sum(1 for ok, _, _ in RESULTS if ok)
    print("=" * 64)
    for ok, name, detail in RESULTS:
        print("%s %-34s %s" % ("PASS" if ok else "FAIL", name, detail))
    print("=" * 64)
    print("%d/%d 项通过" % (passed, len(RESULTS)))
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
