"""Input resolution for an agent run: declared `inputs:` refs, the comment digests, the rework
bundle a rejected step's next attempt receives, a redo's note, and the secrets the substrate
injects."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.config import (
    AgentStep,
    FileInput,
    InputRef,
    expand,
)
from orchestrator.db.models import (
    Artifact,
    ArtifactDirection,
    ArtifactKind,
    EnteredBy,
    Event,
    Runtime,
    StepRun,
)
from orchestrator.events import EventType
from orchestrator.factorial import ResolvedAgent, requested_permissions
from orchestrator.factorial.bedrock_token import ENV_BEARER, mint_bearer
from orchestrator.factorial.runtimes import (
    E2B_CREDENTIAL,
    bedrock_model_of,
    bedrock_region_of,
    credential_name_for,
    requires_stored_credential,
)
from orchestrator.github import GH_TOKEN_KEY
from orchestrator.github.connection import CONNECTION_SECRETS
from orchestrator.github.media import ItemKind, ItemRef
from orchestrator.github.state import CommentState, ReviewState
from orchestrator.secrets.store import (
    deliveries,
    get_for_runtime,
    resolve_for_run,
    resolve_for_runtime,
)
from orchestrator.worker.attachments import MediaFactory, TextItem, resolve_attachments
from orchestrator.worker.reviewers import GateReviewers, gate_reviewers, qualify
from orchestrator.worker.rework import _latest_details, _rejecting_gates
from orchestrator.worker.templates import _find_pr, _glob, _ref_name
from orchestrator.worker.tick_context import _Tick
from runtime.contract import (
    MissingAttachment,
    RunAttachment,
    RunInput,
    SecretRoute,
)

logger = logging.getLogger(__name__)

#: The input a rework attempt receives the dashboard reviewer's feedback under.
CHANGE_REQUEST_INPUT: Final = "change_request"

#: The input the attempt a redo entered receives the person's note under.
REDO_NOTE_INPUT: Final = "redo_note"

#: The inputs a person's Retry gives its attempt: why the failed attempt was not accepted, and,
#: when the session could not be continued, what that attempt delivered.
RETRY_INPUT: Final = "previous_attempt"
RETRY_DELIVERED_INPUT: Final = "previous_attempt_delivered"


@dataclass(frozen=True, slots=True)
class RetryBriefing:
    failure: RunInput
    #: None when the failed attempt delivered no branch or pull request.
    delivered: RunInput | None


async def retry_briefing(tick: _Tick, run: StepRun) -> RetryBriefing:
    """What the attempt a Retry starts is told about `run`, the failed one. Its text is the
    run's own failure reason and failed checks, data the adapter fences like any input."""
    leaves = [
        str(leaf)
        for leaf in (await _latest_details(tick, run, EventType.POSTCONDITION_FAILED)).get(
            "failures"
        )
        or ()
    ]
    ended = f"Attempt {run.attempt} ended {run.status}"
    # An unmet postcondition's reason is its leaves joined, which are listed below instead.
    reason = run.failure_reason if run.failure_reason != "; ".join(leaves) else None
    failure = "\n".join(
        [
            f"{ended}: {reason}" if reason else f"{ended}.",
            *(["Postconditions that did not hold:"] if leaves else []),
            *(f"- {leaf}" for leaf in leaves),
        ]
    )
    artifacts = (
        await tick.session.execute(
            select(Artifact.kind, Artifact.locator, Artifact.url)
            .where(
                Artifact.step_run_id == run.id,
                Artifact.direction == ArtifactDirection.OUTPUT,
                Artifact.kind.in_((ArtifactKind.BRANCH, ArtifactKind.PR)),
            )
            .order_by(Artifact.id)
        )
    ).all()
    lines: list[str] = []
    for kind, locator, url in artifacts:
        name = _ref_name(locator)
        for line in (
            f"- branch: {name}" if name else None,
            f"- pull request: {url}" if url and kind == ArtifactKind.PR else None,
        ):
            if line is not None and line not in lines:
                lines.append(line)
    delivered = (
        RunInput(
            name=RETRY_DELIVERED_INPUT,
            value="\n".join([f"Attempt {run.attempt} delivered:", *lines]),
        )
        if lines
        else None
    )
    return RetryBriefing(RunInput(name=RETRY_INPUT, value=failure), delivered)

#: `ticket_comments` / `pr_review_comments` digest cap: env has real size limits and a long
#: conversation can exceed them.
_MAX_COMMENT_DIGEST_BYTES: Final = 16_000


#: Prepended, not appended: the digest cuts from the START (oldest first) to keep the newest
#: comment whole.
_COMMENT_DIGEST_TRUNCATION_MARKER: Final = (
    f"[Earlier comments were truncated to keep the digest under {_MAX_COMMENT_DIGEST_BYTES} "
    "bytes. The most recent comments are kept.]\n\n"
)


@dataclass(frozen=True, slots=True)
class _Part:
    """One observed item a digest entry was built from, with its own text."""

    kind: ItemKind
    id: int | None
    text: str


@dataclass(frozen=True, slots=True)
class _Entry:
    created_at: str
    author: str
    text: str
    parts: tuple[_Part, ...]


@dataclass(frozen=True, slots=True)
class _Resolved:
    """One resolved input, and the observed items its text came from — the only items an image
    it names can be attributed to (`worker/attachments.py`)."""

    input: RunInput
    items: tuple[TextItem, ...] = ()


@dataclass(frozen=True, slots=True)
class ResolvedInputs:
    """A run's inputs, and the images their text embeds."""

    inputs: tuple[RunInput, ...]
    attachments: tuple[RunAttachment, ...] = ()
    missing: tuple[MissingAttachment, ...] = ()


async def _resolve_inputs(
    tick: _Tick, step: AgentStep, attempt: int, entered_by: str | None
) -> ResolvedInputs:
    """Render the step's `inputs:` from observed state, plus — on a rework attempt — whatever the
    rejecting gate's `onReject.inputs` declared (`_rework_inputs`), and on the attempt a redo
    entered (`entered_by`), the person's note (`_redo_inputs`).

    Every `BareInputSource` resolves to something observed: `ticket_body` is the issue body;
    `ticket_comments`/`pr_review_comments` resolve through `_kept_entries`, which holds the
    fences. File inputs pass their expanded path pattern, not the content — every run gets a
    repo-scoped token and reads the repo itself (tech-stack decision 10). The images those
    inputs embed resolve last, from the same final texts (`worker/attachments.py`).
    """
    pr_number = (
        await _step_pr_number(tick, step.workspace.branch)
        if _wants_pr_review_comments(step.inputs)
        else None
    )
    resolved = [
        *await _resolve_input_refs(tick, step.inputs, pr_number),
        *await _rework_inputs(tick, step, attempt),
        *await _redo_inputs(tick, step, entered_by),
    ]
    attachments, missing = await resolve_attachments(
        media_for(tick),
        tick.source.repo,
        [(item.input.value, item.items) for item in resolved],
        prefix="image",
        refs=tick.host().media_refs,
    )
    return ResolvedInputs(tuple(item.input for item in resolved), attachments, missing)


def media_for(tick: _Tick) -> MediaFactory | None:
    """The images an input embeds are fetched with the ticket's own tracker credentials."""
    return tick.host().media(tick.project.github_repo)


def _wants_pr_review_comments(inputs: Sequence[InputRef]) -> bool:
    """Whether `_step_pr_number`'s extra `pull_requests()` fetch is worth paying for. Most steps
    declare only `ticket_body` / `ticket_comments` / file inputs — a step that never declares
    `pr_review_comments` must not pay for a PR lookup nothing on it needs."""
    return any(
        not isinstance(item, FileInput) and item.source == "pr_review_comments" for item in inputs
    )


async def _step_pr_number(tick: _Tick, branch_template: str) -> int | None:
    """Which pull request `pr_review_comments` means for a step's own `inputs:` — the same
    head-glob lookup `_pr_values` already uses for `{pr_url}`, against `base="*"` because an
    input has no base to narrow by the way a gate's `pr:` block does."""
    head = _glob(branch_template, tick.values)
    pull_request = _find_pr(await tick.source.pull_requests(), head, "*")
    return None if pull_request is None else pull_request.number


async def _resolve_input_refs(
    tick: _Tick,
    inputs: Sequence[InputRef],
    pr_number: int | None,
    *,
    reviewers: GateReviewers | None = None,
) -> tuple[_Resolved, ...]:
    """One `inputs:` list, resolved against one PR context — shared by a step's own `inputs:` and
    a rejecting gate's `onReject.inputs`, which is a different list against a different PR (the
    one the gate was looking at).

    `reviewers` is the rejecting gate's own rule, supplied only by `_rework_inputs`. A step's own
    `inputs:` passes none, so `pr_review_comments` there stays owner-only: outside a rejection
    there is no gate deciding this PR's fate, and so no principled second identity to trust.
    """
    resolved: list[_Resolved] = []
    for item in inputs:
        if isinstance(item, FileInput):
            resolved.append(_Resolved(RunInput(name="file", value=expand(item.file, tick.values))))
        elif item.source == "ticket_body":
            body = tick.issue.body
            resolved.append(
                _Resolved(
                    RunInput(name=item.source, value=body),
                    (TextItem(item.source, ItemRef("issue", tick.issue.number), body),),
                )
            )
        elif item.source == "ticket_comments":
            entries = _entries(tick, await tick.source.issue_comments())
            resolved.append(
                _Resolved(
                    RunInput(name=item.source, value=_render_digest(entries)),
                    _text_items(item.source, tick.issue.number, entries),
                )
            )
        elif item.source == "pr_review_comments":
            if pr_number is None:
                # Distinguishable from "the owner said nothing": there is no matching pull
                # request to read a conversation from at all yet.
                logger.info(
                    "ticket %s: pr_review_comments has no matching pull request yet; "
                    "resolved empty",
                    tick.ticket.external_key,
                )
                comments = ()
                reviews: Sequence[ReviewState] = ()
            else:
                comments = await tick.source.pr_comments(pr_number)
                # Skipped, not just discarded: an open gate lets anyone decide it, so no review
                # text is trusted and nothing pays for the round trip.
                reviews = (
                    await tick.source.pr_reviews(pr_number)
                    if reviewers is not None and not reviewers.open
                    else ()
                )
            trusted = await _trusted_authors(tick, reviewers, reviews)
            entries = _entries(tick, comments, reviews, trusted)
            resolved.append(
                _Resolved(
                    RunInput(name=item.source, value=_render_digest(entries)),
                    () if pr_number is None else _text_items(item.source, pr_number, entries),
                )
            )
        else:
            # BareInputSource (config/models.py) is a closed Literal; reaching here means it
            # grew a member this dispatch was never updated for. A missing agent input is a
            # correctness bug, not something to log past.
            raise ValueError(
                f"input source {item.source!r} has no resolution in _resolve_input_refs, "
                "because BareInputSource has a member this dispatch does not handle"
            )
    return tuple(resolved)


async def _trusted_authors(
    tick: _Tick, reviewers: GateReviewers | None, reviews: Sequence[ReviewState]
) -> tuple[str, ...]:
    """The review authors the rejecting gate would let decide it. `reviews` is empty for an open
    gate (the caller skips the fetch), so every author kept here qualified by name or by role."""
    if reviewers is None or not reviews:
        return ()
    weighed = await qualify(
        reviewers,
        (review.author for review in reviews),
        bots={review.author for review in reviews if review.bot},
        permission_of=tick.source.repo_permission,
    )
    return tuple(weighed.counted)


def _entries(
    tick: _Tick,
    comments: Sequence[CommentState],
    reviews: Sequence[ReviewState] = (),
    approvers: Sequence[str] = (),
) -> list[_Entry]:
    return _kept_entries(
        comments,
        owner=tick.ticket.owner_at_admission,
        bot_login=tick.ctx.bot_login,
        reviews=reviews,
        approvers=approvers,
    )


def _text_items(source: str, number: int, entries: Sequence[_Entry]) -> tuple[TextItem, ...]:
    """Each kept item of a digest, on issue or PR `number`. One with no id cannot be fetched, so
    it carries no image."""
    return tuple(
        TextItem(source, ItemRef(part.kind, number, part.id), part.text)
        for entry in entries
        for part in entry.parts
        if part.id is not None
    )


def _kept_entries(
    comments: Sequence[CommentState],
    *,
    owner: str,
    bot_login: str,
    reviews: Sequence[ReviewState] = (),
    approvers: Sequence[str] = (),
) -> list[_Entry]:
    """`ticket_comments` / `pr_review_comments`, filtered to what an agent may actually read.

    The owner filter is a security boundary: a comment becomes agent input only when its author
    casefold-equals the ticket's owner at admission. Everything else is data the orchestrator may
    log, never text an agent receives — an agent acts on its inputs unsupervised, so admitting a
    third party's comment would open a prompt-injection channel.

    The bot check is a whole-digest refusal, not per-comment filtering. Every qualifying comment
    already has `author == owner_cf`, so the only case to exclude is the ticket owner BEING the
    bot's account — unusual, not impossible. An unconfigured `bot_login` cannot establish the
    App's own identity, so it refuses closed rather than treating "not provably the bot" as safe;
    otherwise `owner_cf != bot_cf` compares a real login against `""`, is trivially true, and lets
    the bot's own words through as the owner's. An unassigned ticket has no owner either, so
    nothing matches it.

    `reviews`/`approvers` is a second trust anchor layered on top, never replacing the owner
    filter. `approvers` is the review authors the rejecting gate qualifies (`_trusted_authors`) —
    the rule `_approvals`/`_change_requests` use to decide the same PR's fate. Reviews qualify
    by casefold membership in it. The bot exclusion covers this population too. Empty
    `approvers`, the default everywhere outside `_rework_inputs`, means `reviews` is never
    consulted.

    A qualifying review contributes one block — its body, then its inline comments, author-fenced
    AGAIN against the review's own author. That second fence is needed because `ReviewState`
    groups comments by GitHub's `pull_request_review_id` alone, which does not guarantee every
    comment carrying a review's id was written by that review's author; the observer stays
    neutral, so the trust decision belongs here. The block is dated by the review's own
    `created_at` so body and inline remarks travel as one unit.
    """
    if not owner or not bot_login:
        return []
    owner_cf = owner.casefold()
    bot_cf = bot_login.casefold()
    if owner_cf == bot_cf:
        return []
    entries = [
        _Entry(c.created_at, c.author, c.body, (_Part("issue_comment", c.id, c.body),))
        for c in comments
        if c.author.casefold() == owner_cf
    ]
    if approvers:
        approver_cf = {login.casefold() for login in approvers} - {bot_cf}
        for review in reviews:
            review_author_cf = review.author.casefold()
            if review_author_cf not in approver_cf:
                continue
            text = review.body
            # Fenced against the review's OWN author, not merely `approver_cf` — see the
            # docstring.
            inline_comments = [
                c for c in review.comments if c.author.casefold() == review_author_cf
            ]
            if inline_comments:
                inline = "\n".join(f"{c.author}{_where(c)}: {c.body}" for c in inline_comments)
                text = f"{text}\n\n{inline}" if text else inline
            if not text:  # nothing to add — an empty review with no qualifying inline comments
                continue
            parts = (
                _Part("review", review.id, review.body),
                *(_Part("review_comment", c.id, c.body) for c in inline_comments),
            )
            entries.append(_Entry(review.created_at, review.author, text, parts))
    entries.sort(key=lambda entry: entry.created_at)
    return entries


def _render_digest(entries: Sequence[_Entry]) -> str:
    """Entries merge chronologically and truncate from the START (see
    `_COMMENT_DIGEST_TRUNCATION_MARKER`). `errors="ignore"` keeps the kept half valid UTF-8 when
    the cut lands mid-codepoint.
    """
    if not entries:
        return ""
    body = "\n\n".join(f"{e.author} at {e.created_at}:\n{e.text}" for e in entries)
    raw = body.encode("utf-8", "replace")
    if len(raw) <= _MAX_COMMENT_DIGEST_BYTES:
        return body
    tail = raw[-_MAX_COMMENT_DIGEST_BYTES:].decode("utf-8", errors="ignore")
    return _COMMENT_DIGEST_TRUNCATION_MARKER + tail


def _where(comment: CommentState) -> str:
    if comment.path is None:
        return ""
    return f" on {comment.path}" if comment.line is None else f" on {comment.path}:{comment.line}"


async def _rework_inputs(tick: _Tick, step: AgentStep, attempt: int) -> tuple[_Resolved, ...]:
    """If a gate rejected and sent the ticket back here, resolve that gate's declared
    `onReject.inputs` too, so the reviewer's conversation reaches the rework attempt.

    `pr_review_comments` here carries the review bodies and inline comments of every reviewer the
    rejecting gate qualifies — named, or holding its `approverPermission` — as well as the PR
    conversation, reconnecting "changes requested by X" with X's actual text, through the same
    rule (`worker/reviewers.py`) that decided X's review counted as a rejection. An open gate
    qualifies anyone, so its review text stays out and the digest stays owner-only: a rejection
    can still happen, but there is no bounded identity to filter review text through, so it fails
    closed.

    It applies only inside the rework window (`worker.rework._rejecting_gates`), which a retry
    OF a rework is still in; the digest is re-read from live state every tick. Where two gates
    stand rejected, config order picks WHICH gate's inputs apply, not what they resolve to: the
    PR lookup, approver list and digest are all re-read per tick.

    A rejection the dashboard asked for adds `change_request`, the reviewer's text exactly as
    accepted (`_dashboard_feedback`), whether or not the gate declares `onReject.inputs`. That
    text comes from a signed-in operator rather than from the issue, so the owner filter above
    does not apply to it (design.md §7.8).
    """
    for rejection in await _rejecting_gates(tick, step, attempt):
        gate, on_reject = rejection.gate, rejection.on_reject
        resolved: list[_Resolved] = []
        if on_reject.inputs:
            head = _glob(gate.gate.pr.head, tick.values)
            base = _glob(gate.gate.pr.base, tick.values)
            pull_request = _find_pr(await tick.source.pull_requests(), head, base)
            pr_number = None if pull_request is None else pull_request.number
            reviewers = gate_reviewers(
                gate.gate, tick.config.roles, tick.values, bot_login=tick.ctx.bot_login
            )
            resolved.extend(
                await _resolve_input_refs(tick, on_reject.inputs, pr_number, reviewers=reviewers)
            )
        feedback = await _dashboard_feedback(tick, rejection.run.id)
        if feedback is not None:
            resolved.append(_Resolved(RunInput(name=CHANGE_REQUEST_INPUT, value=feedback)))
        if resolved:
            return tuple(resolved)
    return ()


async def _redo_inputs(
    tick: _Tick, step: AgentStep, entered_by: str | None
) -> tuple[_Resolved, ...]:
    """The note of the redo that entered this attempt, read back from its request, the one place
    the text is kept. Only that attempt: a retry of it starts without the note."""
    if entered_by != EnteredBy.REDO:
        return ()
    redone = await tick.session.scalar(
        select(Event.details)
        .where(Event.ticket_id == tick.ticket.id, Event.type == EventType.STEP_REDONE)
        .order_by(Event.id.desc())
        .limit(1)
    )
    if (
        redone is None
        or redone.get("target_step") != step.name
        or redone.get("generation") != tick.ticket.generation
    ):
        return ()
    asked = await tick.session.scalar(
        select(Event.details).where(
            Event.id == redone.get("request_id"),
            Event.ticket_id == tick.ticket.id,
            Event.type == EventType.REDO_REQUESTED,
        )
    )
    note = None if asked is None else asked.get("note")
    if not isinstance(note, str) or not note.strip():
        return ()
    return (_Resolved(RunInput(name=REDO_NOTE_INPUT, value=note)),)


async def _dashboard_feedback(tick: _Tick, gate_run_id: int) -> str | None:
    """The feedback of the dashboard change request this gate run's rejection honoured, if it
    honoured one."""
    request_id = (
        await tick.session.scalars(
            select(Event.details["change_request_id"].as_integer()).where(
                Event.ticket_id == tick.ticket.id,
                Event.step_run_id == gate_run_id,
                Event.type == EventType.GATE_REJECTED,
            )
        )
    ).first()
    if request_id is None:
        return None
    details = (
        await tick.session.scalars(
            select(Event.details).where(
                Event.id == request_id, Event.type == EventType.STEP_CHANGES_REQUESTED
            )
        )
    ).first()
    feedback = None if details is None else details.get("feedback")
    return feedback if isinstance(feedback, str) else None


def _minted_bedrock(owned: str, details: object) -> dict[str, SecretStr]:
    """The short-lived bearer plus the region Codex needs for the Runtime URL."""
    bag = details if isinstance(details, dict) else {}
    region = bedrock_region_of(bag)
    return {
        owned: SecretStr(mint_bearer(region=region)),
        "AWS_REGION": SecretStr(region),
        "AWS_DEFAULT_REGION": SecretStr(region),
        "BEDROCK_MODEL": SecretStr(bedrock_model_of(bag)),
    }


async def view_credentials(session: AsyncSession, run: StepRun) -> dict[str, SecretStr]:
    """The model credential a read-only view of `run`'s session starts with, and nothing else:
    no GitHub token, minted or stored, and not the E2B key.

    The one credential the run's runtime type names, whatever else the runtime holds; nothing
    for a run on no runtime, or whose row has since been deleted.
    """
    if run.runtime_id is None:
        return {}
    runtime_type, details = (
        await session.execute(
            select(Runtime.type, Runtime.details).where(Runtime.id == run.runtime_id)
        )
    ).one()
    owned = credential_name_for(runtime_type)
    if not requires_stored_credential(runtime_type):
        return _minted_bedrock(owned, details)
    value = await get_for_runtime(session, run.runtime_id, owned)
    return {} if value is None else {owned: value}


@dataclass(frozen=True, slots=True)
class RunSecrets:
    """A run's secrets, and which names the runtime itself holds: a runtime's credential is never
    delivered by the project's choice, so `sandbox_delivery` must know them."""

    values: dict[str, SecretStr]
    runtime_held: frozenset[str] = frozenset()


async def run_secrets(
    tick: _Tick,
    resolved: ResolvedAgent,
    repo: str,
    *,
    runtime_id: int | None = None,
    run_id: int,
) -> RunSecrets:
    """Everything the substrate injects for one run: the agent's declarations, the registry
    runtime's own credentials when the run is on one, plus a `GH_TOKEN` and `FACTORIAL_BOT_LOGIN`
    from the project's own forge connection.

    Shared with the resume path. An adapter must never read its own process environment for a
    credential — that made the encrypted store authoritative in name only, and it failed exactly
    where the design intends, on a host that keeps credentials out of the service environment.
    """
    # The agent's own declarations *plus* every key its MCP servers reference: the adapter renders
    # `{secrets.KEY}` out of what is injected here, and an unresolved reference is a spawn failure
    # (contract.McpServerSpec).
    keys = resolved.secret_keys()
    # Set and read only under the two `runtime_id` checks below; bound here for the type checker.
    runtime_type, details = "", None
    runtime_held: frozenset[str] = frozenset()
    if runtime_id is not None:
        # A bound runtime is the sole owner of the vendor key its type names (decision 36): the
        # project's copy is not read, not even as a fallback, so a run cannot quietly sign in on
        # a key the operator believes retired. The manifest may still declare it — inert here.
        runtime_type, details = (
            await tick.session.execute(
                select(Runtime.type, Runtime.details).where(Runtime.id == runtime_id)
            )
        ).one()
        owned = credential_name_for(runtime_type)
        keys = tuple(key for key in keys if key != owned)
    secrets = await resolve_for_run(tick.session, tick.ticket.project_id, keys)
    if runtime_id is not None:
        # Every credential the runtime holds, and a runtime's wins over a project secret of the
        # same name: it is the more specific owner of a credential shaped for its adapter (the
        # subscription bundle under `CODEX_AUTH_JSON`, which the adapter reads from here). All
        # but the E2B key: that is the adapter's own client credential (`workspace_key`), and
        # everything here is rendered for the agent.
        held = await resolve_for_runtime(tick.session, runtime_id)
        runtime_held = frozenset(held)
        secrets.update({name: value for name, value in held.items() if name != E2B_CREDENTIAL})
        if not requires_stored_credential(runtime_type):
            secrets.update(_minted_bedrock(ENV_BEARER, details))
    # A connection's credentials only mint the run's: none reaches a run under its own name,
    # whatever the manifest declares. Token mode's stored token arrives as `GH_TOKEN` below.
    for key in CONNECTION_SECRETS:
        secrets.pop(key, None)
    forge = tick.host().forge
    for key in forge.withheld_secrets:
        secrets.pop(key, None)
    # Minted per run on the project's own forge, scoped to this agent's permission request
    # (design.md §7.1). A resume mints a NEW one rather than reusing the run's original:
    # that token was scoped for an hour at spawn and the ticket may have sat parked past it.
    secrets.update(
        await forge.grant_run_secrets(
            repo,
            run_id,
            now=tick.now,
            permissions=requested_permissions(resolved.manifest.permissions),
        )
    )
    return RunSecrets(secrets, runtime_held)


#: A GitLab run's minted project token, which rides the proxy as `GH_TOKEN` does.
MINTED_GITLAB_TOKEN: Final = "GITLAB_TOKEN"

#: The forge login a run's commits name and its comment probe fences on.
BOT_LOGIN_KEY: Final = "FACTORIAL_BOT_LOGIN"


async def sandbox_delivery(
    tick: _Tick, resolved: ResolvedAgent, runtime_type: str, secrets: RunSecrets
) -> tuple[tuple[SecretRoute, ...], tuple[str, ...]]:
    """How a sandbox run is supplied each project secret its agent reads itself: the header
    routes the operator chose on the Secrets page, and every other one in its environment
    (decision 75). Never a key the orchestrator mints or the runtime holds; those reach the run
    their own way."""
    owned = {
        GH_TOKEN_KEY,
        MINTED_GITLAB_TOKEN,
        BOT_LOGIN_KEY,
        ENV_BEARER,
        credential_name_for(runtime_type),
    }
    owned |= secrets.runtime_held
    chosen = await deliveries(tick.session, tick.ticket.project_id)
    routes: list[SecretRoute] = []
    exposed: list[str] = []
    for key in resolved.env_secret_keys():
        if key in owned or key not in secrets.values:
            continue
        delivery = chosen.get(key)
        if delivery and delivery.mode == "header" and delivery.host and delivery.header:
            routes.append(SecretRoute(name=key, host=delivery.host, header=delivery.header))
        else:
            exposed.append(key)
    return tuple(routes), tuple(exposed)


async def release_run_credential(tick: _Tick, run_id: int) -> None:
    """Delete the per-run credential the forge minted. A GitHub run has nothing to delete."""
    try:
        await tick.host().forge.release_run_credential(tick.project.github_repo, run_id)
    except Exception:
        logger.exception(
            "ticket %s: could not release the run credential for run %s", tick.ticket.id, run_id
        )

