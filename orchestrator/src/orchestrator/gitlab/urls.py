"""Web URLs for a GitLab project. GitHub callers keep building github.com links themselves."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

__all__ = [
    "gitlab_blob_url",
    "gitlab_commit_url",
    "gitlab_compare_url",
    "gitlab_issue_url",
    "gitlab_origin",
]


def gitlab_origin(settings: Any, *, fallback: str = "") -> str:
    """The GitLab host this project named, or `fallback`, or gitlab.com."""
    bag = settings.get("gitlab") if isinstance(settings, dict) else None
    raw = bag.get("base_url") if isinstance(bag, dict) else None
    base = raw.strip() if isinstance(raw, str) else ""
    extra = fallback.strip() if fallback else ""
    return (base or extra or "https://gitlab.com").rstrip("/")


def gitlab_issue_url(settings: Any, repo: str, number: int | str) -> str:
    return f"{gitlab_origin(settings)}/{repo}/-/issues/{number}"


def gitlab_compare_url(settings: Any, repo: str, base: str, head: str) -> str:
    return f"{gitlab_origin(settings)}/{repo}/-/compare/{base}...{head}"


def gitlab_commit_url(settings: Any, repo: str, sha: str, *, fallback: str = "") -> str:
    return f"{gitlab_origin(settings, fallback=fallback)}/{repo}/-/commit/{sha}"


def gitlab_blob_url(
    settings: Any, repo: str, commit: str, path: str, *, fallback: str = ""
) -> str:
    return (
        f"{gitlab_origin(settings, fallback=fallback)}/{repo}/-/blob/"
        f"{commit}/{quote(path)}"
    )
