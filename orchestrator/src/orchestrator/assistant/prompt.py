"""What the assistant's model is told: the system prompt, and a person's message with the page
they sent it from.

The prompt is package data (`prompts/system.md`), followed by the tools' own rules
(`mcp_server/chat/teaching.py`): the in-process server carries no instructions of its own. The
page is the dashboard's route, sent by the browser; only a path of a route the dashboard has gets
through, so a client cannot put words in front of the person's. A project's or a ticket's page
names it by factorial's id, which no tool reads as a name, so the message says which `id:N` ref
the page is.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Final

from orchestrator.mcp_server.chat.routes import ID_PARAMS, route_for
from orchestrator.mcp_server.chat.teaching import INSTRUCTIONS

__all__ = ["system_prompt", "turn_message"]

_SYSTEM: Final = Path(__file__).parent / "prompts" / "system.md"


@cache
def system_prompt() -> str:
    return f"{_SYSTEM.read_text().rstrip()}\n\n{INSTRUCTIONS}"


def turn_message(text: str, page: str | None) -> str:
    if not page or (route := route_for(page)) is None:
        return text
    refs = [
        f"{ID_PARAMS[param]} id:{value}"
        for param, value in zip(route.split("/"), page.split("/"), strict=True)
        if param in ID_PARAMS
    ]
    about = f" ({', '.join(refs)})" if refs else ""
    return f"The person is viewing {page}{about}.\n{text}"
