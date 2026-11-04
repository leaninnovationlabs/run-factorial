"""The operator's default ADLC lane — which process a ticket enters when it names none.

`.factorial/` is the folder agents open pull requests against, so the default cannot live there.
A repo-side `process-default.yaml`, or a `default: true` key, would let an agent redirect every
future ticket into a lane of its choosing by renaming one file. The default is therefore
`project.settings` — operator-owned Postgres state no agent can reach — the same trust direction
as `mcp_policy` and `permission_policy` (design.md §7.5): the repo SELECTS which lanes exist,
Postgres PERMITS one of them as the default.

A lane is named by key, so promoting one renames nothing and retires nothing; both lanes keep
their version series. That is the other reason `loader.RESERVED_PROCESS_KEYS` forbids a lane
called `default`.

Pure functions, like its two siblings: `worker.selection` owns "this issue cannot be admitted".
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

__all__ = ["DEFAULT_KEY", "DefaultProcessInvalid", "parse_default_process"]

#: The `project.settings` key (data-model.md, "Project settings"). Absent means no default: every
#: issue must then carry a `factorial:process:<key>` label of its own.
DEFAULT_KEY: Final = "default_process"


class DefaultProcessInvalid(ValueError):
    """`project.settings.default_process` is present but is not a lane key."""


def parse_default_process(settings: Mapping[str, Any]) -> str | None:
    """Read `default_process` out of a project's settings bag.

    Not slug-checked. A project migrated onto multiple lanes keeps its one existing lane's real
    name, whatever that is, and a legacy root `process.yaml` was never asked to carry a slug —
    identity rules bind a NEW lane (`loader.check_process_key`), never an established one.

    Whether the named lane still EXISTS is deliberately not asked here: availability is decided
    by the last sync (`process_version.retired_at`), and this bag is not where that is recorded.
    """
    raw = settings.get(DEFAULT_KEY)
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise DefaultProcessInvalid(
            f"project.settings.{DEFAULT_KEY} must be a process name, got {raw!r}"
        )
    return raw
