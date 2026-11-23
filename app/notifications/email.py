"""Email over SMTP (Gmail with an app password, Outlook, Zoho, Brevo, any provider)."""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from html import escape

from app.notifications.base import DeliveryError, Message, Provider


class EmailProvider(Provider):
    name = "email"

    def __init__(
        self, host: str, port: int, security: str, username: str, password: str, sender: str, recipients: str
    ) -> None:
        self.host, self.port, self.security = host, port, security
        self.username, self.password = username, password
        self.sender = sender or username
        self.recipients = [r.strip() for r in recipients.split(",") if r.strip()]

    def build(self, message: Message) -> EmailMessage:
        mail = EmailMessage()
        mail["Subject"] = message.title
        mail["From"] = self.sender
        mail["To"] = ", ".join(self.recipients)
        mail["Date"] = formatdate(localtime=False)
        mail["Message-ID"] = make_msgid(domain="doomsday-watcher")
        if message.urgent:
            mail["X-Priority"] = "1"
            mail["Importance"] = "high"
        text = message.body + (f"\n\n{message.link_label}: {message.url}" if message.url else "")
        mail.set_content(text)
        lines = "".join(f"<div>{escape(line) or '&nbsp;'}</div>" for line in message.body.split("\n"))
        button = (
            f'<p style="margin-top:20px"><a href="{escape(message.url, quote=True)}" style="background:#1f9d55;color:#fff;'
            f'padding:12px 20px;border-radius:6px;text-decoration:none;font-weight:600">{escape(message.link_label)}</a></p>'
            f'<p style="color:#666;font-size:13px">{escape(message.url)}</p>'
            if message.url
            else ""
        )
        mail.add_alternative(
            f'<div style="font:15px/1.5 -apple-system,Segoe UI,Arial,sans-serif;color:#111">'
            f'<h2 style="font-size:18px;margin:0 0 12px">{escape(message.title)}</h2>{lines}{button}</div>',
            subtype="html",
        )
        return mail

    def _send_blocking(self, mail: EmailMessage) -> None:
        context = ssl.create_default_context()
        smtp: smtplib.SMTP
        if self.security == "ssl":
            smtp = smtplib.SMTP_SSL(self.host, self.port, timeout=15, context=context)
        else:
            smtp = smtplib.SMTP(self.host, self.port, timeout=15)
        with smtp:
            if self.security == "starttls":
                smtp.starttls(context=context)
            if self.username:
                try:
                    smtp.login(self.username, self.password)
                except smtplib.SMTPException as exc:
                    # Some servers drop the connection instead of answering 535.
                    raise smtplib.SMTPAuthenticationError(535, b"login refused") from exc
            smtp.send_message(mail)

    async def send(self, message: Message) -> None:
        if not self.recipients:
            raise DeliveryError("no recipient address", permanent=True)
        try:
            await asyncio.to_thread(self._send_blocking, self.build(message))
        except smtplib.SMTPAuthenticationError:
            raise DeliveryError("email login was refused", permanent=True) from None
        except smtplib.SMTPRecipientsRefused:
            raise DeliveryError("email recipient was refused", permanent=True) from None
        except (smtplib.SMTPException, OSError) as exc:
            # The type only: SMTP replies can echo the login name.
            raise DeliveryError(f"email failed: {type(exc).__name__}") from None
