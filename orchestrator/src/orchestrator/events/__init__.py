"""The append-only event log: one writer, and the read models derived from it (design.md §5)."""

from __future__ import annotations

from orchestrator.events.channels import (
    WAKE_CHANNEL,
    WAKE_LISTENER,
    wake_admission,
    wake_fleet,
    wake_inspect,
    wake_ticket,
)
from orchestrator.events.read_models import (
    ProjectSpend,
    TicketRow,
    cost_per_ticket,
    factory_overview,
    in_flight_runs,
    pipeline_board,
    project_spend,
    project_tickets,
    recent_events,
    ticket_park_reason,
    ticket_timeline,
)
from orchestrator.events.types import EventType, RequestedVia, ResumedVia
from orchestrator.events.writer import UnsafeEventDetails, append

__all__ = [
    "WAKE_CHANNEL",
    "WAKE_LISTENER",
    "EventType",
    "ProjectSpend",
    "RequestedVia",
    "ResumedVia",
    "TicketRow",
    "UnsafeEventDetails",
    "append",
    "cost_per_ticket",
    "factory_overview",
    "in_flight_runs",
    "pipeline_board",
    "project_spend",
    "project_tickets",
    "recent_events",
    "ticket_park_reason",
    "ticket_timeline",
    "wake_admission",
    "wake_fleet",
    "wake_inspect",
    "wake_ticket",
]
