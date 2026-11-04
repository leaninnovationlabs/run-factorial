"""process_version.process_key and .retired_at — several ADLC lanes per project

A project used to have exactly one live process: admission took the highest `version` row and
every ticket pinned it. `process_key` gives each lane in `.factorial/processes/` its own version
series, so uniqueness moves from `(project_id, version)` to `(project_id, process_key, version)`
and two lanes may both sit at v1.

`process_key` backfills from `content->>'name'`, which `ProcessConfig` has always required and
never allowed empty, so there is no sentinel and no NULL. The backfill is a mechanical restatement
of history, NOT a statement about what is selectable: a lane is selectable only while the last
sync still found its file, which is what `retired_at` records. A project whose config was renamed
at some point therefore backfills two keys and, after its next sync, offers one.

`project.settings.default_process` is seeded with each project's previously current lane — the
only one it had — so behaviour is unchanged for every project that never adds a second. Seeded
only where the key is absent, so a downgrade followed by a re-upgrade cannot overwrite a choice an
operator made in between.

Hand-written, both directions, like every revision before it (tech-stack.md decision 2). The
downgrade refuses when two lanes share a version number, the guarded pattern revisions 0004, 0007
and 0008 use: the narrower constraint cannot express those rows, and renumbering them would
rewrite which snapshot a pinned ticket resolves to. `default_process` is left in the settings bag —
nothing reads it at the older revision, and discarding it would throw away an operator's choice.

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_UQ = "uq_process_version_project_id_version"
_NEW_UQ = "uq_process_version_project_id_process_key_version"


def upgrade() -> None:
    op.add_column("process_version", sa.Column("process_key", sa.Text(), nullable=True))
    op.add_column(
        "process_version", sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True)
    )

    bind = op.get_bind()
    nameless = bind.execute(
        sa.text(
            "SELECT count(*) FROM process_version "
            "WHERE coalesce(content->>'name', '') = ''"
        )
    ).scalar_one()
    if nameless:
        raise RuntimeError(
            f"{nameless} process_version row(s) have no `name` in their snapshot, so this "
            "revision cannot say which lane they belong to. `ProcessConfig.name` has always been "
            "required, so these were written by something other than the loader; set "
            "content->>'name' on them explicitly before upgrading."
        )
    bind.execute(sa.text("UPDATE process_version SET process_key = content->>'name'"))
    op.alter_column("process_version", "process_key", nullable=False)

    op.drop_constraint(op.f(_OLD_UQ), "process_version", type_="unique")
    op.create_unique_constraint(
        op.f(_NEW_UQ), "process_version", ["project_id", "process_key", "version"]
    )

    # The lane a ticket admitted the moment before this revision would have pinned: the project's
    # highest version, which under the old uniqueness was its only current process.
    bind.execute(
        sa.text(
            """
            UPDATE project p
               SET settings = p.settings || jsonb_build_object('default_process', d.process_key)
              FROM (SELECT DISTINCT ON (project_id) project_id, process_key
                      FROM process_version
                     ORDER BY project_id, version DESC) d
             WHERE d.project_id = p.id
               AND NOT (p.settings ? 'default_process')
            """
        )
    )


def downgrade() -> None:
    clashes = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT count(*) FROM (SELECT project_id, version FROM process_version "
                "GROUP BY project_id, version HAVING count(*) > 1) t"
            )
        )
        .scalar_one()
    )
    if clashes:
        raise RuntimeError(
            f"{clashes} (project, version) pair(s) are claimed by more than one process lane, "
            "which revision 0015's uniqueness cannot express. Decide which lane each project "
            "keeps and delete the other lanes' rows explicitly before downgrading — this "
            "migration will not renumber them, because a ticket pinned to one of those rows "
            "would then resolve to a different process."
        )
    op.drop_constraint(op.f(_NEW_UQ), "process_version", type_="unique")
    op.create_unique_constraint(op.f(_OLD_UQ), "process_version", ["project_id", "version"])
    op.drop_column("process_version", "retired_at")
    op.drop_column("process_version", "process_key")
