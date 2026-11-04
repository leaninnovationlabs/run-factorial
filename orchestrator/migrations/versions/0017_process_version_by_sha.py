"""A process snapshot is identified by its commit, not by a number in the file

`process.yaml` carried an integer `version:` an author maintained by hand, and a snapshot was
`(project, process, version)`. That number decided nothing a sha does not decide better, and it
could disagree with the commit: editing a file without bumping it produced the "you forgot to
bump `version`" refusal, which is the whole reason sync had a `stale` outcome. Identity moves to
`source_commit_sha`, which is immutable, already recorded, and already what `agent_pins` freezes
against — so re-syncing a commit finds the row it wrote, and a new commit writes a new one.

`source_commit_sha` becomes NOT NULL, so `POST /process-versions` — the JSON-body route that
carried no commit — goes with it. A row it wrote has no identity to give, and it pinned no agents
either. The upgrade REFUSES while any such row exists rather than inventing a sha for it: the only
honest values would be a lie about which commit that config came from.

Which snapshot a new ticket pins is now the most recently registered one for its lane, where
`version DESC` used to answer. The two agree wherever versions were bumped in order, which is
every project this replaces; where they would differ, "the last sync wins" is the rule an operator
can predict without reading a number back out of a file.

Hand-written, both directions, like every revision before it (tech-stack.md decision 2). The
downgrade re-adds the column and renumbers each lane from registration order, because a NOT NULL
unique column has to hold something and the original numbers are not recoverable. That is safe
where renumbering usually would not be: a ticket pins `process_version_id`, so no pin re-points,
and only the label a reader sees changes.

Revision ID: 0017
Revises: 0016
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_UQ = "uq_process_version_project_id_process_key_version"
_NEW_UQ = "uq_process_version_project_id_process_key_source_commit_sha"


def upgrade() -> None:
    bind = op.get_bind()
    unpinned = bind.execute(
        sa.text("SELECT count(*) FROM process_version WHERE source_commit_sha IS NULL")
    ).scalar_one()
    if unpinned:
        raise RuntimeError(
            f"{unpinned} process_version row(s) carry no source_commit_sha — they were uploaded "
            "through POST /process-versions rather than synced from a repository, and that route "
            "is removed by this revision. A snapshot is identified by its commit now, and there "
            "is no honest sha to give these. Sync the same config from the repository and delete "
            "them, or delete them outright if no ticket pins one:\n"
            "  SELECT t.id, t.external_key FROM ticket t JOIN process_version p "
            "ON p.id = t.process_version_id WHERE p.source_commit_sha IS NULL;"
        )

    op.drop_constraint(op.f(_OLD_UQ), "process_version", type_="unique")
    op.alter_column("process_version", "source_commit_sha", nullable=False)
    op.create_unique_constraint(
        op.f(_NEW_UQ), "process_version", ["project_id", "process_key", "source_commit_sha"]
    )
    op.drop_column("process_version", "version")


def downgrade() -> None:
    op.add_column("process_version", sa.Column("version", sa.Integer(), nullable=True))
    # Registration order per lane, which is the ordering `version DESC` was read for. Not the
    # numbers the files carried — those are gone — so this restores the SHAPE the older
    # revision needs, and says so rather than implying the values came back.
    op.get_bind().execute(
        sa.text(
            """
            UPDATE process_version p
               SET version = n.seq
              FROM (SELECT id, row_number() OVER (PARTITION BY project_id, process_key
                                                      ORDER BY created_at, id) AS seq
                      FROM process_version) n
             WHERE n.id = p.id
            """
        )
    )
    op.alter_column("process_version", "version", nullable=False)
    op.drop_constraint(op.f(_NEW_UQ), "process_version", type_="unique")
    op.alter_column("process_version", "source_commit_sha", nullable=True)
    op.create_unique_constraint(
        op.f(_OLD_UQ), "process_version", ["project_id", "process_key", "version"]
    )
