"""Tracker and forge adapters: shared snapshot types and the protocols that split them.

`github/` and `gitlab/` implement the hosts. This package holds what they share so a later
tracker (Jira) can pair with either forge without the worker growing another branch
(tech-stack.md decision 90).
"""

from orchestrator.adapters.bindings import Bindings, ForgePort, SplitProjector, TrackerPort
from orchestrator.adapters.display import HostDisplay, display_for
from orchestrator.adapters.registry import (
    display_of,
    forge_kind_for,
    host_registries,
    resolve_bindings,
    tracker_kind_for,
)
from orchestrator.adapters.split import (
    FORGE_KINDS,
    CompositeStateSource,
    Forge,
    SourceForge,
    SourceTracker,
    Tracker,
    forge_kind_of,
    views_from_source,
)
from orchestrator.adapters.state import (
    AsyncKeyedMemo,
    AsyncOnce,
    BranchState,
    CheckRunState,
    CommentState,
    IssueState,
    PullRequestState,
    RepoPermission,
    ReviewState,
    ReviewVerdict,
    SeededIssue,
    SnapshotState,
    StateSource,
    permission_meets,
)

__all__ = [
    "FORGE_KINDS",
    "AsyncKeyedMemo",
    "AsyncOnce",
    "Bindings",
    "BranchState",
    "CheckRunState",
    "CommentState",
    "CompositeStateSource",
    "Forge",
    "ForgePort",
    "HostDisplay",
    "IssueState",
    "PullRequestState",
    "RepoPermission",
    "ReviewState",
    "ReviewVerdict",
    "SeededIssue",
    "SnapshotState",
    "SourceForge",
    "SourceTracker",
    "SplitProjector",
    "StateSource",
    "Tracker",
    "TrackerPort",
    "display_for",
    "display_of",
    "forge_kind_for",
    "forge_kind_of",
    "host_registries",
    "permission_meets",
    "resolve_bindings",
    "tracker_kind_for",
    "views_from_source",
]
