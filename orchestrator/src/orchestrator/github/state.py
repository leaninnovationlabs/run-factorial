"""Observed snapshot types — re-exported from `orchestrator.adapters.state`.

New code should import from `orchestrator.adapters`. This module stays so existing callers
and the GitHub adapter's historical import path keep working.
"""

from orchestrator.adapters.state import (
    AsyncKeyedMemo,
    AsyncOnce,
    BranchState,
    CheckRunState,
    CommentState,
    IssueState,
    Mergeability,
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
    "AsyncKeyedMemo",
    "AsyncOnce",
    "BranchState",
    "CheckRunState",
    "CommentState",
    "IssueState",
    "Mergeability",
    "PullRequestState",
    "RepoPermission",
    "ReviewState",
    "ReviewVerdict",
    "SeededIssue",
    "SnapshotState",
    "StateSource",
    "permission_meets",
]
