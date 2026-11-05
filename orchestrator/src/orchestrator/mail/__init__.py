"""The deployment's mailer: one SMTP server, Python's `smtplib` (tech-stack decision 91).

It knows nothing about accounts. A caller builds a `Message` and awaits `send`; the text of
each message lives beside the code that sends it. `mailer(settings)` is None when
`FACTORIAL_MAIL__SMTP_HOST` is empty, and a caller with nothing to send through sends nothing.

A send runs on one of the mailer's own threads with a timeout, so a slow server never holds
the event loop. Every failure, from the message, the network or the server, is `MailError`: a
caller logs it and carries on.
"""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr
from typing import Final, Protocol

from orchestrator.settings import MailSettings

__all__ = ["SEND_TIMEOUT", "MailError", "Mailer", "Message", "SmtpMailer", "mailer"]

#: Seconds for the connection and for each command after it.
SEND_TIMEOUT: Final = 10.0
#: Sends wait on their own threads: on the default pool a slow server would hold the threads
#: that password hashing needs, and every sign-in would wait behind mail.
_SENDERS: Final = ThreadPoolExecutor(max_workers=2, thread_name_prefix="mail")


class MailError(Exception):
    pass


@dataclass(frozen=True)
class Message:
    to: str
    subject: str
    text: str
    html: str | None = None


class Mailer(Protocol):
    async def send(self, message: Message) -> None: ...


class SmtpMailer:
    def __init__(self, settings: MailSettings, *, timeout: float = SEND_TIMEOUT) -> None:
        self._settings = settings
        self._timeout = timeout

    async def send(self, message: Message) -> None:
        # ValueError: a header the policy refuses, or an address that does not encode.
        try:
            email = self._compose(message)
            await asyncio.get_running_loop().run_in_executor(_SENDERS, self._deliver, email)
        except (smtplib.SMTPException, OSError, ValueError) as exc:
            raise MailError(f"{type(exc).__name__}: {exc}") from exc

    def _compose(self, message: Message) -> EmailMessage:
        sender = self._settings.from_address
        email = EmailMessage()
        # The default policy refuses a CR or LF in a header, so no value can add one.
        email["From"] = sender
        email["To"] = message.to
        email["Subject"] = message.subject
        email["Date"] = formatdate(usegmt=True)
        email["Message-ID"] = make_msgid(domain=parseaddr(sender)[1].rpartition("@")[2])
        email.set_content(message.text)
        if message.html is not None:
            email.add_alternative(message.html, subtype="html")
        return email

    def _deliver(self, email: EmailMessage) -> None:
        settings = self._settings
        context = ssl.create_default_context()
        smtp: smtplib.SMTP
        if settings.security == "tls":
            smtp = smtplib.SMTP_SSL(
                settings.smtp_host, settings.smtp_port, timeout=self._timeout, context=context
            )
        else:
            smtp = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=self._timeout)
        with smtp:
            if settings.security == "starttls":
                smtp.starttls(context=context)
            if settings.smtp_username:
                smtp.login(settings.smtp_username, settings.smtp_password.get_secret_value())
            smtp.send_message(email)


def mailer(settings: MailSettings) -> Mailer | None:
    return SmtpMailer(settings) if settings.enabled else None
