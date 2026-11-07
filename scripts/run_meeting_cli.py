"""命令行跑一次会议链（不经过 HTTP），用于 D2/D6 的回归与排障。

用法:
  python -m scripts.run_meeting_cli data/samples/S1_高风险.txt
  python -m scripts.run_meeting_cli data/samples/S1_高风险.txt --to someone@example.com
  python -m scripts.run_meeting_cli data/samples/S1_高风险.txt --data-dir data/tmp1
"""
from __future__ import annotations

import argparse
import dataclasses
import json
from pathlib import Path

from app.config.settings import load_settings
from app.store import repo
from app.meeting.pipeline import run_meeting


def _mask(addr: str) -> str:
    if not addr or "@" not in addr:
        return addr or "-"
    return "%s***@%s" % (addr.split("@")[0][:2], addr.split("@")[1])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("file", help="会议 TXT 路径")
    parser.add_argument("--to", default="", help="收件人；留空则发给自己")
    parser.add_argument("--data-dir", default="", help="覆盖 DATA_DIR（隔离试验用）")
    parser.add_argument("--json", action="store_true", help="输出完整 JSON")
    args = parser.parse_args()

    settings = load_settings()
    if args.data_dir:
        settings = dataclasses.replace(settings, data_dir=Path(args.data_dir))
    repo.init_db(settings.db_path)

    path = Path(args.file)
    to = (args.to or settings.smtp_from or settings.smtp_user or "").strip()
    result = run_meeting(
        settings,
        session_id="cli",
        filename=path.name,
        raw=path.read_bytes(),
        mail_to=to,
    )

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    m = result["meeting"]
    print("=" * 66)
    print("会议  : %s | %s | %s" % (m.get("topic") or "-", m.get("held_at") or "-", m.get("place") or "-"))
    print("参会  : %s" % "、".join(m.get("attendees") or []))
    print("模式  : LLM=%s MAIL=%s | 复用已有会议=%s" % (settings.llm_mode, settings.mail_mode, result["reused"]))
    print("闸门  : 模型给 %d 条 → 丢弃 %d → 保留 %d"
          % (result["gates"]["kept"] + result["gates"]["dropped"], result["gates"]["dropped"], result["gates"]["kept"]))
    for item in result["gates"]["dropped_items"]:
        print("   ✗ 丢弃(%s): %s" % (item["reason"], item["evidence_quote"][:50]))
    for note in result["gates"]["notes"][:5]:
        print("   · %s" % note)
    print("-" * 66)
    for i, r in enumerate(result["risks"], 1):
        print("%d. [%-6s] %-8s %s" % (i, r["severity"], r["basis"], r["evidence_quote"][:60]))
    print("-" * 66)
    for i, t in enumerate(result["todos"], 1):
        print("待办%d: %s | %s | 截止 %s | %s"
              % (i, t.get("owner") or "未指定", t["action"][:30], t.get("due") or "未定", t["status"]))
    print("-" * 66)
    print("发信  : %s" % result["mail_status"])
    print("详情  : %s" % result["mail_detail"])
    for log in result["send_logs"]:
        print("send_log: %s | %s | to=%s | dedupe=%s"
              % (log["ts"], log["status"], ",".join(_mask(x) for x in log["recipients"]), log["dedupe_key"]))
    print("=" * 66)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
