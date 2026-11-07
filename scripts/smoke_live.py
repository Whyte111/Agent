"""D1 检查点：一次性验证 LLM key 与 SMTP 是否真的可用。

用法:
  python -m scripts.smoke_live --to 你的邮箱@qq.com          # 测 LLM + SMTP
  python -m scripts.smoke_live --to 你的邮箱@qq.com --skip-smtp
  python -m scripts.smoke_live --to 你的邮箱@qq.com --skip-llm

注意：SMTP 真发不可撤回。先用小号或自己的邮箱试，确认能进收件箱（含垃圾箱）。
"""
from __future__ import annotations

import argparse
import time

from app.config.settings import load_settings
from app.llm.client import LiveLLM, LLMError
from app.mail.sender import Mailer


def test_llm(settings) -> bool:
    print("\n[1/2] LLM")
    if not settings.llm_live:
        print("  跳过：未配置 LLM_API_KEY / 或 LLM_MOCK=1（当前为 mock 模式）")
        return True
    print("  endpoint: %s" % settings.llm_base_url)
    print("  model   : %s" % settings.llm_model)
    client = LiveLLM(settings.llm_base_url, settings.llm_api_key, settings.llm_model, settings.llm_timeout)
    started = time.time()
    try:
        out = client.chat([{"role": "user", "content": "只回复两个字符：pong"}], temperature=0)
    except LLMError as exc:
        print("  ❌ 失败：%s" % exc)
        return False
    elapsed = time.time() - started
    print("  ✅ 返回（%.1fs）：%s" % (elapsed, (out.get("content") or "").strip()[:80]))
    return True


def _mask(addr: str) -> str:
    if not addr or "@" not in addr:
        return addr or "-"
    name, domain = addr.split("@", 1)
    return "%s***@%s" % (name[:2], domain)


def test_smtp(settings, to: str) -> bool:
    print("\n[2/2] SMTP")
    if not settings.mail_live:
        print("  跳过：未配置 SMTP_HOST/USER/PASS（当前为 dry-run 模式）")
        mailer = Mailer(settings)
        result = mailer.send([to], "[AI 外脑] dry-run 自检", "这是一封 dry-run 自检邮件。\n")
        print("  → 已写 %s" % result.detail.replace("未配置 SMTP，已写入 ", ""))
        return True
    print("  server  : %s:%d (ssl=%s)" % (settings.smtp_host, settings.smtp_port, settings.smtp_ssl))
    print("  from    : %s" % _mask(settings.smtp_from or settings.smtp_user))
    print("  to      : %s" % _mask(to))
    mailer = Mailer(settings)
    result = mailer.send(
        [to],
        "[AI 外脑] SMTP 自检",
        "这是一封自检邮件。收到它说明 SMTP 凭据与端口都通了。\n\n"
        "如果没在收件箱看到，先查垃圾箱，再确认服务商是否开启了 SMTP 服务。\n",
    )
    if result.status == "sent":
        print("  ✅ 发送成功 message_id=%s" % result.message_id)
        print("  → 现在去收件箱（含垃圾箱）确认能收到")
        return True
    print("  ❌ 失败：%s" % result.detail)
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--to", default="", help="接收测试邮件的邮箱；留空则发给自己（SMTP_USER）")
    parser.add_argument("--skip-llm", action="store_true")
    parser.add_argument("--skip-smtp", action="store_true")
    args = parser.parse_args()

    settings = load_settings()
    target = (args.to or settings.smtp_from or settings.smtp_user or "").strip()
    if not args.skip_smtp and not target:
        print("缺少收件人：请用 --to 指定，或先配置 SMTP_USER")
        return 2

    print("=" * 60)
    print("当前模式：LLM=%s  MAIL=%s" % (settings.llm_mode, settings.mail_mode))
    print("=" * 60)

    ok = True
    if not args.skip_llm:
        ok = test_llm(settings) and ok
    if not args.skip_smtp:
        ok = test_smtp(settings, target) and ok

    print("\n" + "=" * 60)
    print("D1 检查点：%s" % ("通过" if ok else "未通过（先修这个再往下写业务）"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
