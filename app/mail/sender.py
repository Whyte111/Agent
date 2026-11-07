"""SMTP 发信：只负责"发"，不判风险，不查幂等（幂等在 pipeline）。"""
from __future__ import annotations

import smtplib
import ssl
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path
from typing import List, Optional


@dataclass
class SendResult:
    status: str  # sent | dry_run | failed
    detail: str = ""
    recipients: List[str] = field(default_factory=list)
    message_id: Optional[str] = None


class Mailer:
    def __init__(self, settings):
        self.settings = settings

    @property
    def mode(self) -> str:
        return self.settings.mail_mode

    def _build(self, to: List[str], subject: str, text: str, html: Optional[str]) -> EmailMessage:
        msg = EmailMessage()
        sender = self.settings.smtp_from or self.settings.smtp_user
        msg["From"] = formataddr(("AI 外脑", sender))
        msg["To"] = ", ".join(to)
        msg["Subject"] = subject
        msg["Message-ID"] = make_msgid(domain="aiwainao.local")
        msg.set_content(text)
        if html:
            msg.add_alternative(html, subtype="html")
        return msg

    def send(
        self,
        to: List[str],
        subject: str,
        text: str,
        html: Optional[str] = None,
    ) -> SendResult:
        recipients = [addr.strip() for addr in to if addr and addr.strip()]
        if not recipients:
            return SendResult("failed", "收件人为空", [])
        try:
            msg = self._build(recipients, subject, text, html)
        except Exception as exc:  # 非法 header / 编码异常：不拖垮会议链
            return SendResult("failed", "构建邮件失败：%s: %s" % (type(exc).__name__, exc), recipients)
        if not self.settings.mail_live:
            try:
                return self._write_eml(msg, recipients)
            except Exception as exc:
                return SendResult("failed", "写 .eml 失败：%s: %s" % (type(exc).__name__, exc), recipients)
        last_error = ""
        for attempt in (1, 2):  # 骨架约定：失败重试 1 次
            try:
                self._smtp_send(msg, recipients)
                return SendResult(
                    "sent", "smtp ok (attempt %d)" % attempt, recipients, msg["Message-ID"]
                )
            except Exception as exc:  # 网络/认证/收件人拒收都可能
                last_error = "%s: %s" % (type(exc).__name__, exc)
        return SendResult("failed", last_error, recipients)

    def _smtp_send(self, msg: EmailMessage, recipients: List[str]) -> None:
        s = self.settings
        # 显式校验证书：SMTP_SSL 默认会退化成 CERT_NONE，不能依赖默认值。
        context = ssl.create_default_context()
        if s.smtp_ssl:
            server = smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, timeout=30, context=context)
        else:
            server = smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=30)
        try:
            server.ehlo()
            if not s.smtp_ssl:
                # STARTTLS 失败就失败，绝不继续明文登录/发信
                server.starttls(context=context)
                server.ehlo()
            if s.smtp_user:
                server.login(s.smtp_user, s.smtp_pass)
            server.send_message(msg, from_addr=s.smtp_from or s.smtp_user, to_addrs=recipients)
        finally:
            try:
                server.quit()
            except Exception:
                pass

    def _write_eml(self, msg: EmailMessage, recipients: List[str]) -> SendResult:
        """dry-run：不碰网络，落一份 .eml 供人工核验（仍算可验证的产物）。"""
        outbox = Path(self.settings.outbox_dir)
        outbox.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        path = outbox / ("%s.eml" % stamp)
        path.write_bytes(bytes(msg))
        return SendResult(
            "dry_run",
            "未配置 SMTP，已写入 %s" % path,
            recipients,
            msg["Message-ID"],
        )
