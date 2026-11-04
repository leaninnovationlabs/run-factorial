"""Who counts as the factory on GitLab delivery comments.

Agent runs mint a project access token; notes on issues and merge requests appear as
`project_<id>_bot_<hash>`, not the operator's configured `bot_login`. GitHub keeps one App
login — GitLab run delivery uses this pattern instead.
"""

from __future__ import annotations

import re

__all__ = ["gitlab_delivery_author"]

_GITLAB_RUN_BOT = re.compile(r"^project_\d+_bot_", re.IGNORECASE)


def gitlab_delivery_author(author: str, bot_login: str) -> bool:
    """Whether `author` may carry factory delivery evidence on GitLab."""
    if not author:
        return False
    if bot_login and author.casefold() == bot_login.casefold():
        return True
    return bool(_GITLAB_RUN_BOT.match(author))
