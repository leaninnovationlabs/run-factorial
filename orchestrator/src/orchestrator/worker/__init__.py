"""The reconciliation loop: observe → evaluate → act → project (design.md §3.1).

The state machine lives here and nowhere else: the runtime adapter spawns and reports, agent
prompts do the work, and this package owns everything else (CLAUDE.md, division of labor).
"""

from __future__ import annotations

from orchestrator.worker.admission import (
    CandidateSource,
    InMemoryCandidates,
    admit_all,
    admit_project,
)
from orchestrator.worker.context import ReconcileDeps, production_deps
from orchestrator.worker.leases import acquire_leases, release, renew
from orchestrator.worker.loop import run_forever, tick
from orchestrator.worker.parked import GitHubParked, InMemoryParked, ParkedListings, ParkedSource
from orchestrator.worker.reconcile import reconcile_ticket
from orchestrator.worker.tick_context import LeaseLost

__all__ = [
    "CandidateSource",
    "GitHubParked",
    "InMemoryCandidates",
    "InMemoryParked",
    "LeaseLost",
    "ParkedListings",
    "ParkedSource",
    "ReconcileDeps",
    "acquire_leases",
    "admit_all",
    "admit_project",
    "production_deps",
    "reconcile_ticket",
    "release",
    "renew",
    "run_forever",
    "tick",
]
