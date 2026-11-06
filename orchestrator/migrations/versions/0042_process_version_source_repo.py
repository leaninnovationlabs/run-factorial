"""A process snapshot records the repository it was read from, beside the commit

A sha is not an address without its repository. `process_version` kept the commit a sync read
(`source_commit_sha`, and `agent_pins` beside it) but not where, and every reader joined that
commit with the project's CURRENT `github_repo`. Changing a project's repository therefore broke
every synced version: the pages, the editor and a spawn all asked the new repository for a commit
only the old one has (issue #441). `source_repo` is the repository the sync read, and it joins the
row's identity, so a fork synced at its parent's commit is a snapshot of its own rather than the
parent's row read from the wrong place.

The backfill copies each row's project `github_repo`. That is right for every project whose
repository never changed and wrong for one whose repository already did, because nothing recorded
the earlier repository: no event names it, so there is nothing truer to copy.

The downgrade REFUSES while two rows share `(project_id, process_key, source_commit_sha)`, which
only two repositories at one commit can produce. The older identity cannot hold both, and neither
is a lie to delete.

Hand-written, both directions, like every revision before it (tech-stack.md decision 2).

Revision ID: 0042
Revises: 0041
Create Date: 2026-11-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0042"
down_revision: str | None = "0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_UQ = "uq_process_version_project_id_process_key_source_commit_sha"
#: Named, not left to the convention: the convention's name for four columns is 71 characters,
#: past Postgres's 63.
_NEW_UQ = "uq_process_version_project_repo_key_sha"

_SHARED = (
    "SELECT project_id, process_key, source_commit_sha FROM process_version "
    "GROUP BY project_id, process_key, source_commit_sha HAVING count(*) > 1"
)


def upgrade() -> None:
    op.add_column("process_version", sa.Column("source_repo", sa.Text(), nullable=True))
    op.get_bind().execute(
        sa.text(
            "UPDATE process_version v SET source_repo = p.github_repo "
            "FROM project p WHERE p.id = v.project_id"
        )
    )
    op.alter_column("process_version", "source_repo", nullable=False)
    op.drop_constraint(op.f(_OLD_UQ), "process_version", type_="unique")
    op.create_unique_constraint(
        op.f(_NEW_UQ),
        "process_version",
        ["project_id", "source_repo", "process_key", "source_commit_sha"],
    )


def downgrade() -> None:
    shared = op.get_bind().execute(sa.text(f"SELECT count(*) FROM ({_SHARED}) s")).scalar_one()
    if shared:
        raise RuntimeError(
            f"{shared} process and commit pair(s) were synced from two repositories, so rows "
            "share a process and a commit, and the older revision identifies a snapshot by "
            "those alone. Delete the rows of the repository the project no longer uses, where "
            "no ticket pins one, then downgrade again:\n"
            f"  {_SHARED};"
        )
    op.drop_constraint(op.f(_NEW_UQ), "process_version", type_="unique")
    op.create_unique_constraint(
        op.f(_OLD_UQ), "process_version", ["project_id", "process_key", "source_commit_sha"]
    )
    op.drop_column("process_version", "source_repo")
