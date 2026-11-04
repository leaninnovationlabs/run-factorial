"""GitLab run credentials stay behind E2B's network proxy."""

from __future__ import annotations

import base64

from runtime.adapters.e2b.adapter import _gitlab_inject


def test_gitlab_project_token_is_injected_for_git_and_api() -> None:
    rules = _gitlab_inject("gitlab.example.com", "project-token")

    basic = base64.b64encode(b"oauth2:project-token").decode()
    assert rules == {
        "gitlab.example.com": {
            "Authorization": f"Basic {basic}",
            "PRIVATE-TOKEN": "project-token",
        }
    }
