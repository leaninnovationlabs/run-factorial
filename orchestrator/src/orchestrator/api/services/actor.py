"""Who asked, and through which surface. Every request event records both."""

from __future__ import annotations

from dataclasses import dataclass

from orchestrator.events.types import RequestedVia

__all__ = ["Actor"]


@dataclass(frozen=True)
class Actor:
    by: str
    via: RequestedVia
    mcp_token_id: int | None = None
    chat_proposal_id: int | None = None

    @classmethod
    def dashboard(cls, by: str) -> Actor:
        return cls(by=by, via=RequestedVia.DASHBOARD)

    @classmethod
    def chat(cls, by: str, proposal_id: int) -> Actor:
        """`by` is the person who pressed Approve, never the model that proposed."""
        return cls(by=by, via=RequestedVia.CHAT, chat_proposal_id=proposal_id)

    def fields(self, person_key: str = "requested_by") -> dict[str, object]:
        """Event details that name the requester. `SESSION_RESUME_REQUESTED` passes `by`, the key
        `worker/interrupts.py` reads. Every other request event uses `requested_by`."""
        out: dict[str, object] = {person_key: self.by, "requested_via": self.via.value}
        if self.mcp_token_id is not None:
            out["mcp_token_id"] = self.mcp_token_id
        if self.chat_proposal_id is not None:
            out["chat_proposal_id"] = self.chat_proposal_id
        return out
