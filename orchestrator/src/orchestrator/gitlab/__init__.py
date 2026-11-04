"""GitLab Data Center adapter. Parallel to `orchestrator.github`; unused unless configured."""

from orchestrator.gitlab.client import GitLabClient, GitLabError, quote_project
from orchestrator.gitlab.contents import GitLabContents
from orchestrator.gitlab.contract import GitLabContractOps
from orchestrator.gitlab.issues import GitLabIssueWriter
from orchestrator.gitlab.media import GitLabMedia
from orchestrator.gitlab.observer import GitLabStateSource, list_issues
from orchestrator.gitlab.projector import GitLabProjector

__all__ = [
    "GitLabClient",
    "GitLabContents",
    "GitLabContractOps",
    "GitLabError",
    "GitLabIssueWriter",
    "GitLabMedia",
    "GitLabProjector",
    "GitLabStateSource",
    "list_issues",
    "quote_project",
]
