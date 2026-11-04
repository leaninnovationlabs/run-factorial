"""{slug} carry-forward: `_resolve_slug` against seeded artifacts, and the
resolved slug flowing end to end through workspace branches and preconditions.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from orchestrator.config import ProcessConfig
from orchestrator.db.models import Artifact as DbArtifact
from orchestrator.db.models import (
    ArtifactDirection,
    StepKind,
    StepRun,
    StepRunStatus,
    Ticket,
)
from orchestrator.github import (
    BranchState,
    PullRequestState,
)
from orchestrator.worker import templates as templates_module
from orchestrator.worker.templates import _resolve_slug
from runtime.contract import (
    Artifact,
    ArtifactKind,
    RunStatus,
)
from tests.reconcile_world import (
    _build_world,
    _process,
    _process_with_a_build_step,
    _process_with_a_slug_precondition,
    run_tick,
    step_runs,
    ticket_row,
)

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]


# ------------------------------------------------------- {slug} carry-forward: _resolve_slug


async def _seed_artifact(
    session: AsyncSession,
    ticket_id: int,
    kind: ArtifactKind,
    locator: dict[str, str],
    *,
    attempt: int = 1,
    also: dict[str, str] | None = None,
) -> None:
    """A durable BRANCH/PR artifact row, exactly the shape `_write_artifacts` leaves — a fresh
    `step_run` plus an `Artifact` row on it, and `also` for a second artifact the SAME run
    observed. Lets a test seed the ticket's observed-name history directly, without driving a
    real agent run through spawn/poll/accept. Which run an artifact hangs off matters:
    `_resolve_slug` reads the runs in order, so a later call supersedes an earlier one under the
    same branch shape. `attempt` only needs to be distinct per call on the SAME ticket —
    `step_run`'s own `(ticket_id, step_name, attempt)` uniqueness (the idempotent-spawn key
    everywhere else) applies here too."""
    run = StepRun(
        ticket_id=ticket_id,
        step_name="plan",
        kind=StepKind.AGENT,
        attempt=attempt,
        status=StepRunStatus.SUCCEEDED,
    )
    session.add(run)
    await session.flush()
    for one in (locator, *(() if also is None else (also,))):
        session.add(
            DbArtifact(
                step_run_id=run.id,
                direction=ArtifactDirection.OUTPUT,
                kind=str(kind),
                locator=one,
            )
        )
    await session.flush()


async def test_resolve_slug_is_none_with_no_artifacts_on_record(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Zero matches: `{slug}` stays unresolved — today's behaviour for every ticket before this
    task, unchanged."""
    world = await _build_world(maker)
    async with world.sessions() as session:
        ticket = await session.get(Ticket, world.ticket_id)
        assert ticket is not None
        values = {"ticket": ticket.external_key}
        assert await _resolve_slug(session, ticket, _process("resolve-zero"), values) is None


async def test_resolve_slug_resolves_the_one_candidate_a_delivered_branch_carries(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Exactly one distinct candidate across the ticket's artifacts -> use it. The prefix comes
    from the config's own `workspace.branch` template (`spec/{ticket}-{slug}`), never a
    hardcoded `spec/` guess."""
    world = await _build_world(maker)
    async with world.sessions() as session:
        ticket = await session.get(Ticket, world.ticket_id)
        assert ticket is not None
        await _seed_artifact(session, ticket.id, ArtifactKind.BRANCH, {"branch": "spec/12-widget"})
        values = {"ticket": ticket.external_key}
        assert await _resolve_slug(session, ticket, _process("resolve-one"), values) == "widget"


async def test_resolve_slug_also_reads_a_pr_artifacts_head(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A PR artifact's locator carries `head`, not `branch` (`runtime/adapters/*`'s
    `_probe_pulls`) — the same observed name, under the other key."""
    world = await _build_world(maker)
    async with world.sessions() as session:
        ticket = await session.get(Ticket, world.ticket_id)
        assert ticket is not None
        await _seed_artifact(
            session,
            ticket.id,
            ArtifactKind.PR,
            {"number": "101", "head": "spec/12-widget"},
        )
        values = {"ticket": ticket.external_key}
        assert await _resolve_slug(session, ticket, _process("resolve-pr"), values) == "widget"


async def test_resolve_slug_takes_the_newest_run_of_a_branch_shape_over_an_abandoned_one(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A rework that landed on a different slug supersedes the attempt it replaced.

    Two attempts of one step are not two live answers: the earlier branch is abandoned, and
    reading them as ambiguous left `{slug}` literal for the whole remaining ticket — the state a
    plan agent puts a ticket into whenever a rejected spec makes it open a fresh branch rather
    than push to the one it already had.
    """
    world = await _build_world(maker)
    async with world.sessions() as session:
        ticket = await session.get(Ticket, world.ticket_id)
        assert ticket is not None
        await _seed_artifact(
            session, ticket.id, ArtifactKind.BRANCH, {"branch": "spec/12-widget"}, attempt=1
        )
        await _seed_artifact(
            session, ticket.id, ArtifactKind.BRANCH, {"branch": "spec/12-gadget"}, attempt=2
        )
        values = {"ticket": ticket.external_key}
        slug = await _resolve_slug(session, ticket, _process("resolve-newest"), values)

    assert slug == "gadget"


async def test_resolve_slug_with_two_distinct_candidates_in_one_run_is_unresolved_and_logs(
    maker: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two names under one branch shape from a SINGLE run are a live disagreement, not a
    supersession: `{slug}` stays literal, and a debug log names every candidate so the ambiguity
    is visible without ever being resolved wrongly.

    Asserted via a patched `logger.debug`, not `caplog`: the TRUE cause (found reviewing this
    task, now fixed at `migrations/env.py`'s `fileConfig` call) was that Alembic's `fileConfig`
    defaults `disable_existing_loggers=True`, and `alembic.ini` names only root/sqlalchemy/
    alembic — so the session-scoped `migrated_database` fixture disabled every OTHER project
    logger, process-wide, from the first Postgres test onward. `caplog` itself works fine now
    (verified live with a scratch probe test); the monkeypatch stays here anyway because it is
    still the more direct idiom for THIS test — it asserts exactly what `_resolve_slug` handed
    the logging call, with no dependency on logger/handler level plumbing at all."""
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        templates_module.logger, "debug", lambda *args, **_kwargs: calls.append(args)
    )
    world = await _build_world(maker)
    async with world.sessions() as session:
        ticket = await session.get(Ticket, world.ticket_id)
        assert ticket is not None
        await _seed_artifact(
            session,
            ticket.id,
            ArtifactKind.BRANCH,
            {"branch": "spec/12-widget"},
            also={"branch": "spec/12-gadget"},
        )
        values = {"ticket": ticket.external_key}
        slug = await _resolve_slug(session, ticket, _process("resolve-two"), values)

    assert slug is None
    (call,) = calls  # exactly one debug log, never one per candidate
    message = call[0] % call[1:]
    assert "widget" in message
    assert "gadget" in message


async def test_resolve_slug_ignores_an_artifact_that_does_not_match_any_template(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """An artifact whose name does not fit any step's `workspace.branch` shape (a different
    repo's stray branch reported by an undeclared-path probe, say) contributes no candidate —
    it is simply not counted, not an error."""
    world = await _build_world(maker)
    async with world.sessions() as session:
        ticket = await session.get(Ticket, world.ticket_id)
        assert ticket is not None
        await _seed_artifact(
            session, ticket.id, ArtifactKind.BRANCH, {"branch": "unrelated/branch-name"}
        )
        values = {"ticket": ticket.external_key}
        assert await _resolve_slug(session, ticket, _process("resolve-none-match"), values) is None


async def test_resolve_slug_strips_a_non_empty_suffix(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """`{slug}` need not sit at the end of a template: `_slug_prefixes` splits on it via
    `partition`, keeping whatever comes AFTER as a suffix the observed name must also end with —
    proven here against `spec/{slug}/{ticket}` (`{slug}` in the middle), not just the sample's
    own end-of-string shape."""
    cfg = _process("resolve-suffix").model_dump(mode="json", by_alias=True)
    cfg["steps"][0]["workspace"]["branch"] = "spec/{slug}/{ticket}"
    config = ProcessConfig.model_validate(cfg)

    world = await _build_world(maker)
    async with world.sessions() as session:
        ticket = await session.get(Ticket, world.ticket_id)
        assert ticket is not None
        await _seed_artifact(session, ticket.id, ArtifactKind.BRANCH, {"branch": "spec/widget/12"})
        values = {"ticket": ticket.external_key}
        assert await _resolve_slug(session, ticket, config, values) == "widget"


async def test_resolve_slug_discards_a_degenerate_prefix_suffix_overlap(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The empty-slug guard's most pointed case: a template `abc{slug}bc` against the observed
    name `abc` — `startswith("abc")` and `endswith("bc")` both hold trivially (the name IS the
    prefix, which also happens to end with the suffix), but the prefix and suffix OVERLAP inside
    a name this short, so the naive slice (`name[len(prefix):len(name)-len(suffix)]`) has a start
    past its end and yields `""`. That must not be treated as an agent's slug — the guard
    discards it, same as a genuinely empty match."""
    cfg = _process("resolve-degenerate").model_dump(mode="json", by_alias=True)
    cfg["steps"][0]["workspace"]["branch"] = "abc{slug}bc"
    config = ProcessConfig.model_validate(cfg)

    world = await _build_world(maker)
    async with world.sessions() as session:
        ticket = await session.get(Ticket, world.ticket_id)
        assert ticket is not None
        await _seed_artifact(session, ticket.id, ArtifactKind.BRANCH, {"branch": "abc"})
        values = {"ticket": ticket.external_key}
        assert await _resolve_slug(session, ticket, config, values) is None


async def test_resolve_slug_expands_the_full_base_values_not_just_ticket(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """`_slug_prefixes` expands `workspace.branch` against the tick's WHOLE base values
    (`ticket`/`ticket_owner`/`repo`), not a bare `{"ticket": ...}` — a template using `{repo}`
    (unusual, but legal) still fires. Discriminating: with only `{ticket}` supplied, `{repo}`
    would survive expansion literally and the derived prefix would carry the literal text
    `"{repo}"`, which the seeded branch below (built from the REAL repo name) would never match —
    this assertion is `None` under that bug and `"widget"` once `{repo}` actually expands."""
    cfg = _process("resolve-repo").model_dump(mode="json", by_alias=True)
    cfg["steps"][0]["workspace"]["branch"] = "{repo}/spec/{ticket}-{slug}"
    config = ProcessConfig.model_validate(cfg)

    world = await _build_world(maker)
    async with world.sessions() as session:
        ticket = await session.get(Ticket, world.ticket_id)
        assert ticket is not None
        await _seed_artifact(
            session,
            ticket.id,
            ArtifactKind.BRANCH,
            {"branch": f"{world.repo}/spec/12-widget"},
        )
        values = {"ticket": ticket.external_key, "ticket_owner": "operator", "repo": world.repo}
        assert await _resolve_slug(session, ticket, config, values) == "widget"


async def test_resolve_slug_ignores_an_input_direction_artifact(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Only OUTPUT artifacts are a delivery an agent made — an INPUT-direction row (were one
    ever written) must not be read back as an observed slug."""
    world = await _build_world(maker)
    async with world.sessions() as session:
        ticket = await session.get(Ticket, world.ticket_id)
        assert ticket is not None
        run = StepRun(
            ticket_id=ticket.id,
            step_name="plan",
            kind=StepKind.AGENT,
            status=StepRunStatus.SUCCEEDED,
        )
        session.add(run)
        await session.flush()
        session.add(
            DbArtifact(
                step_run_id=run.id,
                direction=ArtifactDirection.INPUT,
                kind=str(ArtifactKind.BRANCH),
                locator={"branch": "spec/12-widget"},
            )
        )
        await session.flush()
        values = {"ticket": ticket.external_key}
        assert await _resolve_slug(session, ticket, _process("resolve-input"), values) is None


async def test_resolve_slug_ignores_a_file_kind_artifact(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Only BRANCH/PR artifacts carry an observable ref name — a FILE-kind row (`file_exists`
    is not derivable, `_expected_artifacts`'s own docstring, but a future adapter or an unrelated
    kind could still write one) must not be read back as an observed slug."""
    world = await _build_world(maker)
    async with world.sessions() as session:
        ticket = await session.get(Ticket, world.ticket_id)
        assert ticket is not None
        run = StepRun(
            ticket_id=ticket.id,
            step_name="plan",
            kind=StepKind.AGENT,
            status=StepRunStatus.SUCCEEDED,
        )
        session.add(run)
        await session.flush()
        session.add(
            DbArtifact(
                step_run_id=run.id,
                direction=ArtifactDirection.OUTPUT,
                kind=str(ArtifactKind.FILE),
                locator={"branch": "spec/12-widget", "path": "specs/12-widget/spec.md"},
            )
        )
        await session.flush()
        values = {"ticket": ticket.external_key}
        assert await _resolve_slug(session, ticket, _process("resolve-file"), values) is None


# --------------------------------------------------------- {slug} carry-forward: end to end


async def test_a_resolved_slug_makes_the_rework_attempts_workspace_branch_concrete(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """Once `plan`'s first attempt has a durable BRANCH/PR artifact on record, the ticket's
    `{slug}` resolves — and the NEXT tick's `workspace.branch` expansion sees the CONCRETE
    branch, not the still-templated `{slug}` the agent chose on attempt 1 (the carry-forward's
    whole point: claude_cli's mechanical worktree becomes viable on a rework). Zero artifacts is
    the unresolved case, pinned by this test's own first assertion: attempt 1's own spawn, before
    anything has been delivered, still carries the literal `{slug}`."""
    world = await _build_world(maker)
    await run_tick(world)  # plan attempt 1 spawns; nothing delivered yet
    assert world.adapter.spawns[0].workspace.branch == "spec/12-{slug}"

    world.github.deliver_spec(world.issue_number)
    world.adapter.complete(
        RunStatus.SUCCEEDED,
        artifacts=(
            Artifact(kind=ArtifactKind.BRANCH, locator={"branch": "spec/12-widget"}),
            Artifact(kind=ArtifactKind.PR, locator={"number": "101", "head": "spec/12-widget"}),
        ),
    )
    await run_tick(world)  # accept + advance to spec_gate — the artifact is now durable
    await run_tick(world)  # open the gate

    world.github.request_changes("reviewer-1")
    await run_tick(world)  # spec_gate rejects -> current_step back to "plan"

    world.adapter.restart()
    await run_tick(world)  # plan attempt 2 spawns — the FIRST tick after the artifact landed

    assert world.adapter.spawns[1].workspace.branch == "spec/12-widget"


async def test_a_resolved_slug_precondition_evaluates_concretely(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """`predicates.evaluator._evaluate_call` already expands EVERY predicate argument against
    `tick.values` — `{slug}` is no exception once the carry-forward puts one there. A step whose
    preconditions reference `{slug}` literally (`_process_with_a_slug_precondition`'s `confirm`
    step) is unreachable until `plan` has delivered — proven here by driving the whole loop
    through delivery, approval, and one more tick, and observing `confirm` actually spawn."""
    world = await _build_world(maker, config=_process_with_a_slug_precondition("proc"))
    await run_tick(world)  # plan attempt 1 spawns
    world.github.deliver_spec(world.issue_number)
    world.adapter.complete(
        RunStatus.SUCCEEDED,
        artifacts=(Artifact(kind=ArtifactKind.BRANCH, locator={"branch": "spec/12-widget"}),),
    )
    await run_tick(world)  # accept + advance to spec_gate
    await run_tick(world)  # open the gate

    world.github.approve("reviewer-1")
    await run_tick(world)  # gate approves, merges, advances to "confirm"
    ticket = await ticket_row(world)
    assert ticket.current_step == "confirm"

    await run_tick(world)  # confirm's own preconditions now see the resolved slug

    assert len(world.adapter.spawns) == 2
    confirm_run = next(r for r in await step_runs(world) if r.step_name == "confirm")
    assert confirm_run.status == StepRunStatus.RUNNING


async def test_a_divergent_build_slug_still_gets_a_concrete_pr_url_comment(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """I1 (review regression, reproduced live against real Postgres): `_glob` is a MATCHER over
    OBSERVED state — "which PR does `{pr_url}` mean?" — never a DIRECTIVE, so it must not consume
    the ticket-level resolved slug. `plan` delivers `spec/12-widget` (slug resolves to "widget"),
    but `build`'s own agent legitimately chooses a DIFFERENT slug (`impl/12-other`) — its own
    postconditions are star-globbed and accept it. Before the `_glob` fix, the resolved "widget"
    made the `{pr_url}` lookup expand to the exact literal `impl/12-widget`, `_find_pr` missed the
    REAL `impl/12-other` pull request, `_pr_values` returned no `pr_url`, and the onSuccess
    comment posted with the LITERAL `{pr_url}` placeholder still in it — permanently, since an
    outcome fires once. This asserts the comment carries the real, concrete URL."""
    world = await _build_world(maker, config=_process_with_a_build_step("proc"))
    await run_tick(world)  # plan attempt 1 spawns
    world.github.deliver_spec(world.issue_number)
    world.adapter.complete(
        RunStatus.SUCCEEDED,
        artifacts=(Artifact(kind=ArtifactKind.BRANCH, locator={"branch": "spec/12-widget"}),),
    )
    await run_tick(world)  # accept + advance to spec_gate — slug "widget" now on record
    await run_tick(world)  # open the gate

    world.github.approve("reviewer-1")
    await run_tick(world)  # gate approves, merges, advances to "build"
    ticket = await ticket_row(world)
    assert ticket.current_step == "build"

    await run_tick(world)  # build spawns

    # build's own agent legitimately chooses a DIFFERENT slug than plan's.
    world.github.branches.append(BranchState(name="impl/12-other", sha="sha-impl"))
    world.github.pulls.append(
        PullRequestState(
            number=202,
            head_ref="impl/12-other",
            base_ref="main",
            head_sha="sha-impl",
            state="open",
            merged=False,
            url="https://github.test/pr/202",
        )
    )
    world.adapter.restart()
    world.adapter.complete(
        RunStatus.SUCCEEDED,
        artifacts=(Artifact(kind=ArtifactKind.BRANCH, locator={"branch": "impl/12-other"}),),
    )
    await run_tick(world)  # accept build -> onSuccess comment renders {pr_url}

    comments = [body for kind, body in world.projector.calls if kind == "comment"]
    assert comments[-1] == "Implementation ready for testing: https://github.test/pr/202"
