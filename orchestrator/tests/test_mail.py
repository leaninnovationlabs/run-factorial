"""The mailer: what it puts on the wire, and that it never falls back to plaintext."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from email import message_from_bytes, policy

import pytest
import pytest_asyncio
from pydantic import ValidationError

from orchestrator.mail import MailError, Message, SmtpMailer, mailer
from orchestrator.settings import MailSettings


@dataclass
class _Server:
    """A plain SMTP server that advertises nothing, and keeps each message and command."""

    port: int = 0
    commands: list[str] = field(default_factory=list)
    messages: list[bytes] = field(default_factory=list)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"220 test ESMTP\r\n")
        while line := await reader.readline():
            command = line.decode().strip()
            self.commands.append(command.split(" ", 1)[0].upper())
            verb = self.commands[-1]
            if verb == "EHLO":
                writer.write(b"250 test\r\n")
            elif verb == "DATA":
                writer.write(b"354 go on\r\n")
                await writer.drain()
                self.messages.append(await reader.readuntil(b"\r\n.\r\n"))
                writer.write(b"250 queued\r\n")
            elif verb == "QUIT":
                writer.write(b"221 bye\r\n")
                await writer.drain()
                break
            else:
                writer.write(b"250 ok\r\n")
            await writer.drain()
        writer.close()


@pytest_asyncio.fixture(loop_scope="function")
async def server() -> AsyncIterator[_Server]:
    state = _Server()
    listener = await asyncio.start_server(state.handle, "127.0.0.1", 0)
    state.port = listener.sockets[0].getsockname()[1]
    async with listener:
        yield state


def _settings(port: int, **overrides: object) -> MailSettings:
    return MailSettings.model_validate(
        {
            "smtp_host": "127.0.0.1",
            "smtp_port": port,
            "security": "none",
            "from_address": "Factorial <no-reply@example.com>",
            **overrides,
        }
    )


async def test_a_message_reaches_the_server_with_both_bodies(server: _Server) -> None:
    await SmtpMailer(_settings(server.port), timeout=2).send(
        Message(to="ada@example.com", subject="Verify", text="plain", html="<p>rich</p>")
    )

    assert len(server.messages) == 1
    sent = message_from_bytes(server.messages[0].removesuffix(b".\r\n"), policy=policy.default)
    assert sent["To"] == "ada@example.com"
    assert sent["From"] == "Factorial <no-reply@example.com>"
    assert sent["Message-ID"].endswith("@example.com>")
    plain, rich = sent.get_body(("plain",)), sent.get_body(("html",))
    assert plain is not None and rich is not None
    assert plain.get_content().strip() == "plain"
    assert rich.get_content().strip() == "<p>rich</p>"


async def test_starttls_refuses_a_server_that_cannot_upgrade(server: _Server) -> None:
    with pytest.raises(MailError):
        await SmtpMailer(_settings(server.port, security="starttls"), timeout=2).send(
            Message(to="ada@example.com", subject="s", text="t")
        )

    assert "MAIL" not in server.commands
    assert server.messages == []


async def test_a_header_with_a_line_break_is_refused_before_the_network(
    server: _Server,
) -> None:
    with pytest.raises(MailError):
        await SmtpMailer(_settings(server.port), timeout=2).send(
            Message(to="ada@example.com", subject="hi\r\nBcc: eve@example.com", text="t")
        )

    assert server.commands == []


async def test_an_unreachable_server_is_a_mail_error() -> None:
    listener = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = listener.sockets[0].getsockname()[1]
    listener.close()
    await listener.wait_closed()

    with pytest.raises(MailError):
        await SmtpMailer(_settings(port), timeout=2).send(
            Message(to="a@example.com", subject="s", text="t")
        )


def test_no_host_is_no_mailer() -> None:
    assert mailer(MailSettings()) is None


def test_a_host_needs_a_sender() -> None:
    with pytest.raises(ValidationError, match="FROM_ADDRESS"):
        MailSettings(smtp_host="smtp.example.com")


def test_implicit_tls_defaults_to_its_own_port() -> None:
    sender = {"smtp_host": "smtp.example.com", "from_address": "no-reply@example.com"}
    assert MailSettings.model_validate({**sender, "security": "tls"}).smtp_port == 465
    assert MailSettings.model_validate(sender).smtp_port == 587
    assert (
        MailSettings.model_validate({**sender, "security": "tls", "smtp_port": 2465}).smtp_port
        == 2465
    )


def test_a_password_is_never_sent_in_the_clear() -> None:
    with pytest.raises(ValidationError, match="in the clear"):
        MailSettings.model_validate(
            {
                "smtp_host": "smtp.example.com",
                "from_address": "no-reply@example.com",
                "security": "none",
                "smtp_password": "secret",
            }
        )
