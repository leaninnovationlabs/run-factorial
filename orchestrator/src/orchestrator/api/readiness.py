"""Route-free reads behind a project's readiness: its activation blockers, its runtime and
secret gaps, its repo-side probes, and the label palette it needs.

Nothing here raises an HTTP error or owns a route. `routes/admin.py` calls these, in the pattern
`activity.py` and `bindings.py` set.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from githubkit.exception import RequestFailed
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.adapters.display import GITHUB_DISPLAY, HostDisplay, protection_command
from orchestrator.adapters.registry import display_of
from orchestrator.api.deps import (
    GITHUB_NOT_CONNECTED,
    GITLAB_NOT_CONFIGURED,
    AccessReader,
    ConfigBranchReader,
    LabelReader,
    ProtectionReader,
    WorkflowsReader,
)
from orchestrator.api.schemas import (
    ProcessSummary,
    ProcessVersionOut,
    ReadinessCheck,
    ReadinessItem,
    ReadinessKey,
    RuntimeProvidedSecret,
)
from orchestrator.api.services.errors import as_sentence
from orchestrator.config import (
    AgentStep,
    CheckStep,
    Condition,
    GateStep,
    Outcome,
    ProcessConfig,
    placeholders,
)
from orchestrator.config.conditions import iter_calls
from orchestrator.config.lint import condition_sites
from orchestrator.db.models import (
    Agent,
    ProcessVersion,
    Project,
    Runtime,
    Ticket,
    TicketSource,
)
from orchestrator.factorial import (
    CONFIG_BRANCH,
    DefaultProcessInvalid,
    RepoConfigSource,
    parse_default_process,
)
from orchestrator.factorial.registry import is_known_commit, known_base
from orchestrator.factorial.runtime_policy import SettingsInvalid, runtime_permitted
from orchestrator.factorial.runtimes import (
    E2B_CREDENTIAL,
    ISOLATION_PLACES,
    NOT_IMPLEMENTED_TYPES,
    RuntimeDetailsInvalid,
    credential_name_for,
    is_subscription,
    isolation_of,
    requires_stored_credential,
    template_of,
)
from orchestrator.github import (
    GH_TOKEN_KEY,
    LABEL_BLOCKED,
    LABEL_DONE,
    LABEL_EXIT,
    LABEL_INTERRUPTED,
    LABEL_WAITING,
    BranchProtection,
    ConfigBranchRules,
    step_from_label,
    step_label,
)
from orchestrator.github.client import AppNotInstalled, InstallationAccess
from orchestrator.github.connection import has_connection
from orchestrator.github.state import AsyncKeyedMemo
from orchestrator.predicates.globs import matches_ref
from orchestrator.secrets import store
from orchestrator.worker.selection import process_label

__all__ = [
    "APP_DOCS",
    "CONNECT_DOCS",
    "GitHubStanding",
    "SecretGap",
    "access_item",
    "all_label_palettes",
    "behind_access",
    "blockers",
    "ci_workflows_item",
    "config_branch_items",
    "core_items",
    "default_or_note",
    "gate_approvers_item",
    "github_standing",
    "label_item",
    "named_runtimes",
    "parse_snapshot",
    "parse_snapshots",
    "probe_keys",
    "process_summaries",
    "protection_item",
    "reset_command",
    "runtime_items",
    "secret_gap",
    "shared_protection",
    "unknown_head_detail",
]

logger = logging.getLogger(__name__)

#: The public docs pages for the setup the dashboard cannot do itself.
APP_DOCS: Final = "https://www.runfactorial.com/docs/github-app/"
CONNECT_DOCS: Final = "https://www.runfactorial.com/docs/connect-a-repository/"


def _process_summary(versions: Sequence[ProcessVersion], default: str | None) -> str:
    """`standard · a1b2c3d and 2 more` — the lane a ticket would enter by default, and its commit.

    One lane named, not all of them. A checklist row is read at a glance, and the lane an
    unlabelled issue enters is the one fact that answers "what does this project do"; the rest are
    a count. The Process config page lists them.

    With no default set, the first lane stands in and the row says so — a project in that state
    admits nothing that does not carry its own process label, which is worth reading here.
    """
    named = next((v for v in versions if v.process_key == default), versions[0])
    rest = len(versions) - 1
    line = f"{named.process_key} · {named.source_commit_sha[:7]}"
    if rest:
        line += f" and {rest} more"
    return line if default is not None else f"{line} · no default"


def default_or_note(project: Project) -> tuple[str | None, str | None]:
    """`(the project's default lane, why it could not be read)`.

    A settings value that will not parse is a warning rather than a 500, matching how the worker
    treats it (`worker.admission._lanes`): the page exists to show an operator what to fix, and
    it cannot do that by refusing to render.
    """
    try:
        return parse_default_process(project.settings), None
    except DefaultProcessInvalid as exc:
        return None, str(exc)


def process_summaries(
    versions: Sequence[ProcessVersion], *, default: str | None, adopted: Mapping[str, str]
) -> list[ProcessSummary]:
    """One row per available lane, with the version a ticket entering it right now would pin."""
    return [
        ProcessSummary(
            process_key=version.process_key,
            is_default=version.process_key == default,
            selection_label=process_label(version.process_key),
            latest=ProcessVersionOut.model_validate(version).model_copy(
                update={"adopted_by": adopted.get(version.source_commit_sha)}
            ),
        )
        for version in versions
    ]


@dataclass(frozen=True)
class GitHubStanding:
    """What the database says about a GitHub project's own connection: whether one is stored, and
    when a webhook delivery last matched or failed its secret."""

    connected: bool
    verified_at: datetime | None
    failed_at: datetime | None
    #: False when GitHub has no address to deliver to, so the webhook is not needed.
    webhook_reachable: bool = True


async def github_standing(
    session: AsyncSession,
    project: Project,
    source: TicketSource | None,
    *,
    webhook_reachable: bool = True,
) -> GitHubStanding | None:
    """None for a GitLab project, which has no GitHub connection to hold."""
    if not display_of(project, source.kind if source else None).github_app:
        return None
    return GitHubStanding(
        connected=await has_connection(session, project),
        verified_at=source.webhook_verified_at if source else None,
        failed_at=source.webhook_failed_at if source else None,
        webhook_reachable=webhook_reachable,
    )


def _github_items(standing: GitHubStanding) -> list[ReadinessItem]:
    items = [
        ReadinessItem(
            key="github_connection",
            state="ok" if standing.connected else "blocked",
            summary="connected" if standing.connected else "not connected",
            detail=(
                None
                if standing.connected
                else "No GitHub connection. Connect this project to GitHub with an App or a "
                "token on Ticket source."
            ),
        )
    ]
    if not standing.connected:
        return items
    failed, verified = standing.failed_at, standing.verified_at
    if not standing.webhook_reachable:
        items.append(
            ReadinessItem(
                key="github_webhook",
                state="ok",
                summary="not needed",
                detail=(
                    "GitHub can't reach this address, so leave the App's webhook off. "
                    "Factorial checks GitHub every 30 seconds."
                ),
            )
        )
    elif failed is not None and (verified is None or failed > verified):
        items.append(
            ReadinessItem(
                key="github_webhook",
                state="warn",
                summary="The last GitHub webhook did not match this project's webhook secret",
                detail=(
                    "Paste the webhook secret the GitHub App uses. Every project on the same "
                    "App needs the same secret."
                ),
            )
        )
    else:
        items.append(
            ReadinessItem(
                key="github_webhook",
                state="ok" if verified is not None else "unknown",
                summary="verified" if verified is not None else "no delivery yet",
            )
        )
    return items


def core_items(
    source: TicketSource | None,
    versions: Sequence[ProcessVersion],
    missing_secrets: Sequence[str],
    default_process: str | None,
    github: GitHubStanding | None,
) -> list[ReadinessItem]:
    """The requirements the orchestrator owns outright, one row each, pass or fail.

    Each mirrors something the worker needs: `admit_all` joins on a ticket source, `admit_project`
    gives up without a process version, and `_build_run_spec` resolves every declared secret at
    spawn. These are the only rows that can reach `blocked`, and they are decided entirely from
    the database — no GitHub call, so no outage can change a verdict here.

    `missing_secrets` is precomputed by `secret_gap` (manifests live in the repo, not the
    database). A blocker is only ever asserted on positive evidence, so a GitHub outage can
    neither brick a readiness read nor refuse an activation.

    The `runtime` rows are deliberately NOT here. There is one per lane that names a Runtime, an
    unusable one stops that lane and no other, and blurring them into a project-wide verdict
    would either hide a broken lane behind two working ones or block a whole project over a lane
    nobody is using (`runtime_items`).
    """
    return [
        ReadinessItem(
            key="ticket_source",
            state="ok" if source else "blocked",
            summary=source.external_ref if source else "not set",
            detail=(
                None
                if source
                else "No repository chosen. Choose the repository whose issues this project reads."
            ),
        ),
        *(_github_items(github) if github is not None else []),
        ReadinessItem(
            key="process_config",
            state="ok" if versions else "blocked",
            summary=(
                "never synced" if not versions else _process_summary(versions, default_process)
            ),
            detail=(None if versions else "No process yet. Sync one from the repository."),
        ),
        ReadinessItem(
            key="secrets",
            state="blocked" if missing_secrets else "ok",
            summary=(
                f"{len(missing_secrets)} declared secret(s) with no stored value"
                if missing_secrets
                else "every declared secret is stored"
            ),
            detail=(
                # `resolve_for_run` omits a missing key, so the run starts and the agent finds the
                # variable absent mid-work; only an MCP `{secrets.KEY}` reference to one hard-fails
                # the spawn.
                f"Store these secrets on the Secrets page: {', '.join(missing_secrets)}. Runs "
                "that need them fail until you do."
                if missing_secrets
                else None
            ),
        ),
    ]


def blockers(
    source: TicketSource | None,
    versions: Sequence[ProcessVersion],
    missing_secrets: Sequence[str],
    default_process: str | None,
    github: GitHubStanding | None,
) -> list[str]:
    """Why this project cannot be activated yet — a *projection* of `core_items`, never a second
    opinion about it.

    Shared by the project page's readiness display and the `PUT /active` check so the button and
    the server cannot disagree. Deriving both from one row set extends that guarantee to the
    checklist: a row cannot render a tick while this function reports the same requirement as
    missing, because they are the same verdict read twice.

    Only `core_items` rows are consulted, so the GitHub-backed rows (`labels`,
    `branch_protection`) can never gate activation however they resolve — deliberate, and the
    reason they are computed separately. The per-lane `runtime` rows are outside for a second
    reason: a lane's problems stay inside the lane (design.md §4).
    """
    return [
        item.detail or item.summary
        for item in core_items(source, versions, missing_secrets, default_process, github)
        if item.state == "blocked"
    ]


def _brief(exc: Exception) -> str:
    """One warning-sized line: the type plus the first line of the message."""
    first = (str(exc).splitlines() or [""])[0]
    return f"{type(exc).__name__}: {first[:160]}"


def parse_snapshot(version: ProcessVersion | None) -> tuple[ProcessConfig | None, list[str]]:
    """Re-parse the stored snapshot once per request; both probes share the result so they cannot
    disagree about whether it parses. A failure is a note, not an error: the snapshot was valid
    when written, so failing now means this codebase's models drifted."""
    if version is None:
        return None, []
    try:
        return ProcessConfig.model_validate(version.content), []
    except ValidationError as exc:
        return None, [f"The process snapshot could not be re-parsed ({_brief(exc)})."]


@dataclass
class SecretGap:
    """What the secrets probe found for the agents of every available lane."""

    #: Every declared key, `GH_TOKEN` included — the page's row list.
    declared: list[str] = field(default_factory=list)
    #: Declared, project-owned, and not stored: the blocker.
    missing: list[str] = field(default_factory=list)
    #: Declared or not, the vendor key each named runtime owns (decision 36), one row per lane's
    #: runtime — two lanes naming the same one are reported once.
    runtime_provided: list[RuntimeProvidedSecret] = field(default_factory=list)
    #: "not checked" notes, one per agent the probe could not read.
    notes: list[str] = field(default_factory=list)


def parse_snapshots(
    versions: Sequence[ProcessVersion],
) -> tuple[dict[str, ProcessConfig], list[str]]:
    """Every available lane's snapshot, by key, plus a note per lane that would not re-parse.

    One lane failing to parse does not blank the page: the readiness rows below union over what
    DID parse, and the note names the lane that did not. Blanking would report a models drift in
    one lane as "this project has no labels", which sends an operator to the wrong file.
    """
    configs: dict[str, ProcessConfig] = {}
    notes: list[str] = []
    for version in versions:
        config, problems = parse_snapshot(version)
        if config is not None:
            configs[version.process_key] = config
        notes += [f"{version.process_key}: {note}" for note in problems]
    return configs, notes


async def named_runtimes(
    session: AsyncSession, organization_id: int, configs: Mapping[str, ProcessConfig]
) -> dict[str, Runtime]:
    """The rows the lanes' `runtime:` slugs name in the project's organization, by slug. A slug
    with no row is simply absent — `runtime_items` is what tells "names none" apart from "the row
    was deleted"."""
    slugs = {config.runtime for config in configs.values() if config.runtime is not None}
    if not slugs:
        return {}
    rows = (
        await session.execute(
            select(Runtime).where(
                Runtime.organization_id == organization_id, Runtime.slug.in_(slugs)
            )
        )
    ).scalars()
    return {row.slug: row for row in rows}


async def _open_tickets_reading_copy(session: AsyncSession, project: Project, name: str) -> int:
    """Open tickets whose pinned snapshot still takes `name` from the project store — the rule
    `run_secrets` applies per ticket, not per project: only a snapshot naming a runtime whose type
    owns `name` retires the copy. A snapshot naming a runtime that no longer exists counts too:
    recreated with another type, it would read the copy again."""
    pinned = (
        await session.execute(
            select(ProcessVersion.content, func.count(Ticket.id))
            .join(Ticket, Ticket.process_version_id == ProcessVersion.id)
            .where(Ticket.project_id == project.id, Ticket.closed_at.is_(None))
            .group_by(ProcessVersion.id)
        )
    ).all()
    slugs = {content.get("runtime") for content, _ in pinned} - {None}
    owned = {
        row.slug
        for row in (
            await session.execute(
                select(Runtime).where(
                    Runtime.organization_id == project.organization_id, Runtime.slug.in_(slugs)
                )
            )
        ).scalars()
        if credential_name_for(row.type) == name
    }
    return sum(count for content, count in pinned if content.get("runtime") not in owned)


async def secret_gap(
    session: AsyncSession,
    config_source: RepoConfigSource | None,
    project: Project,
    configs: Mapping[str, ProcessConfig],
    runtimes: Mapping[str, Runtime],
) -> SecretGap:
    """The secrets probe for the agents of every available lane.

    Unioned across lanes: a secret one lane's agent declares is one a run in that lane needs, and
    a project is short of it however many other lanes do not use it.

    Declared keys come from the pinned agent manifests — `ResolvedAgent.secret_keys()`, the same
    list `_build_run_spec` resolves at spawn — read at the immutable pinned sha through the
    process-wide `RepoContents` memo. That memo gains a generation per synced sha and is never
    evicted; growth is bounded by sync frequency, eviction is future work.

    Two names are never the project's to store: `GH_TOKEN`, minted per run, and — when a lane
    names a runtime that exists — that runtime's own credential, which `run_secrets` takes from
    the runtime alone (decision 36). Both are discarded before the store is consulted, and the
    runtime's is reported back so the page can say who provides it. That discard is per LANE
    rather than over the union: `run_secrets` reads the snapshot the ticket pinned, so a key one
    lane's runtime owns is still the project's to store for a lane that names another runtime or
    none at all.

    Fail-open is per agent and GitHub-only: an unreadable manifest becomes a note naming that
    agent while keys from agents that did resolve still count, so one renamed agent folder cannot
    clear a provably-missing secret's blocker. Database reads stay outside the degrade path —
    failing to read our own store is not an outage to shrug at.
    """
    gap = SecretGap()
    for slug, runtime in sorted(runtimes.items()):
        name = credential_name_for(runtime.type)
        stored = (
            True
            if not requires_stored_credential(runtime.type)
            else name in await store.list_names_for_runtime(session, runtime.id)
        )
        gap.runtime_provided.append(
            RuntimeProvidedSecret(
                name=name,
                runtime_slug=slug,
                stored=stored,
                open_tickets_reading_copy=await _open_tickets_reading_copy(session, project, name),
            )
        )
    agents = sorted(
        {
            step.agent
            for config in configs.values()
            for step in config.steps
            if isinstance(step, AgentStep)
        }
    )
    if not agents:
        return gap
    if config_source is None:
        source = (
            await session.execute(
                select(TicketSource).where(TicketSource.project_id == project.id)
            )
        ).scalar_one_or_none()
        host = display_of(project, source.kind if source else None)
        gap.notes.append(f"Declared secrets not checked. {host.unconfigured}")
        return gap

    rows = {
        row.name: row
        for row in (
            await session.execute(
                select(Agent).where(Agent.project_id == project.id, Agent.name.in_(agents))
            )
        ).scalars()
    }

    async def declared(path: str, sha: str) -> set[str] | Exception:
        try:
            resolved = await config_source.load_agent(project.github_repo, path, sha)
        except Exception as exc:  # degrade this agent only; the others keep their evidence
            return exc
        return set(resolved.secret_keys())

    pinned = {
        name: (row.repo_path, row.pinned_commit_sha)
        for name in agents
        if (row := rows.get(name)) is not None and row.pinned_commit_sha is not None
    }
    loaded = dict(
        zip(
            pinned,
            await asyncio.gather(*(declared(path, sha) for path, sha in pinned.values())),
            strict=True,
        )
    )
    by_agent: dict[str, set[str]] = {}
    unchecked: list[str] = []
    for name in agents:
        if name not in pinned:
            unchecked.append(f"{name} (not synced yet)")
        elif isinstance(result := loaded[name], Exception):
            unchecked.append(f"{name} ({_brief(result)})")
        else:
            by_agent[name] = result
    gap.declared = sorted(set().union(*by_agent.values()) if by_agent else set())

    stored = set(await store.list_names(session, project.id))
    if unchecked:
        gap.notes.append(f"Declared secrets not checked for agent(s): {'; '.join(unchecked)}.")
    provided = {p.runtime_slug: p.name for p in gap.runtime_provided}
    missing: set[str] = set()
    for config in configs.values():
        owned = {GH_TOKEN_KEY}
        if config.runtime in provided:
            owned.add(provided[config.runtime])
        lane = {
            key
            for step in config.steps
            if isinstance(step, AgentStep)
            for key in by_agent.get(step.agent, ())
        }
        missing |= lane - owned - stored
    gap.missing = sorted(missing)
    return gap


async def runtime_items(
    session: AsyncSession,
    configs: Mapping[str, ProcessConfig],
    runtimes: Mapping[str, Runtime],
    settings: Mapping[str, object],
) -> list[ReadinessItem]:
    """Whether the Runtime each lane names can still run its agents — one row per lane that names
    one, attributed to that lane. No row at all for a lane that names none: the deployment
    default needs no checking here.

    This is the one readiness question that must NOT union. A missing or unimplemented Runtime is
    lane-specific, and one verdict over three lanes either hides a broken lane behind two working
    ones or blocks a whole project over a lane nobody is using. So an unusable Runtime is
    reported against its own lane and stops that lane's runs, and it is kept out of `blockers`
    so it cannot refuse the project's activation (design.md §4: a lane's problems stay inside the
    lane).

    Sync already refused a slug that did not exist, so a miss here means the row was deleted
    afterwards; the others are what the worker would refuse at spawn (`NOT_IMPLEMENTED_TYPES`,
    an E2B runtime the project's `runtime_allowlist` does not name, a runtime with no stored
    credential of its type — except a platform-minted type, which is ready without one). All
    database reads.
    """
    items: list[ReadinessItem] = []
    for key in sorted(configs):
        slug = configs[key].runtime
        if slug is None:
            continue
        items.append(await _runtime_item(session, key, slug, runtimes.get(slug), settings))
    return items


async def _runtime_item(
    session: AsyncSession,
    process_key: str,
    slug: str,
    row: Runtime | None,
    settings: Mapping[str, object],
) -> ReadinessItem:
    """One lane's Runtime verdict. `process` names the lane on every row — see `runtime_items`."""
    if row is None:
        return ReadinessItem(
            key="runtime",
            process=process_key,
            runtime_slug=slug,
            state="blocked",
            summary=f"{slug} · no longer exists",
            detail=f"Process {process_key!r} names runtime {slug!r}, which no longer exists. "
            "Create it again on the Runtime page with the same slug, or sync a version of that "
            "process that names another. Tickets in other processes are unaffected.",
        )
    if row.type in NOT_IMPLEMENTED_TYPES:
        return ReadinessItem(
            key="runtime",
            process=process_key,
            runtime_slug=slug,
            state="blocked",
            summary=f"{slug} · {row.type}",
            detail=f"Runtime {slug!r} has type {row.type}, which is not implemented yet, so "
            f"every spawn in process {process_key!r} would fail.",
        )
    try:
        isolation = isolation_of(row.details)
        permitted = runtime_permitted(settings, slug, isolation)
    except (RuntimeDetailsInvalid, SettingsInvalid) as exc:
        return ReadinessItem(
            key="runtime",
            process=process_key,
            runtime_slug=slug,
            state="blocked",
            summary=f"{slug} · cannot be checked",
            detail=f"Every spawn in process {process_key!r} would be refused: "
            + as_sentence(str(exc)),
        )
    if not permitted:
        return ReadinessItem(
            key="runtime",
            process=process_key,
            runtime_slug=slug,
            state="blocked",
            summary=f"{slug} · not permitted",
            detail=f"Runtime {slug!r} runs {ISOLATION_PLACES[isolation]}, which sends the "
            "repository's code to a third party, so it runs only after you permit it on this "
            f"project's Runtime page. Until then, every run in process {process_key!r} is "
            "refused.",
        )
    held = await store.list_names_for_runtime(session, row.id)
    if isolation == "e2b" and (missing := _e2b_workspace_gap(row, held)):
        return ReadinessItem(
            key="runtime",
            process=process_key,
            runtime_slug=slug,
            state="blocked",
            summary=f"{slug} · no sandbox {missing}",
            detail=f"Cloud sandbox runtime {slug!r} names no sandbox {missing}. Its sandboxes "
            f"run in the team its {E2B_CREDENTIAL} names, from a template built in that team. Set "
            f"both on the Runtime page before process {process_key!r} can run.",
        )
    if not requires_stored_credential(row.type):
        return ReadinessItem(
            key="runtime",
            process=process_key,
            runtime_slug=slug,
            state="ok",
            summary=f"{slug} · host role",
            detail=f"Runtime {slug!r} mints its Bedrock bearer from the host's AWS role, so "
            f"process {process_key!r} does not need a stored vendor key.",
        )
    name = credential_name_for(row.type)
    if name not in held:
        if is_subscription(row.type):
            return ReadinessItem(
                key="runtime",
                process=process_key,
                runtime_slug=slug,
                state="blocked",
                summary=f"{slug} · not connected",
                detail=f"Subscription runtime {slug!r} has no {name} credential. Connect it "
                "on the Runtime page (device sign-in, or paste auth.json) before process "
                f"{process_key!r} can run.",
            )
        # The project store is not a fallback for this name (decision 36), so this is what
        # every spawn in this lane would refuse.
        return ReadinessItem(
            key="runtime",
            process=process_key,
            runtime_slug=slug,
            state="blocked",
            summary=f"{slug} · no {name}",
            detail=f"Runtime {slug!r} holds no {name} credential, and a process that names a "
            "runtime takes that key from the runtime alone. Save it on the Runtime page before "
            f"process {process_key!r} can run.",
        )
    return ReadinessItem(
        key="runtime",
        process=process_key,
        runtime_slug=slug,
        state="ok",
        summary=f"{slug} · {row.type}",
    )


def _e2b_workspace_gap(row: Runtime, held: Sequence[str]) -> str | None:
    """What an E2B runtime lacks to spawn, as the page names it: its team's key, then the
    template chosen from that team. `held` is the runtime's credential names."""
    if E2B_CREDENTIAL not in held:
        return "key"
    if template_of(row.details) is None:
        return "template"
    return None


def _uses_ci_green(config: ProcessConfig) -> bool:
    """Whether any condition in this process references `ci_green` — what decides if the
    `ci_workflows` readiness row is relevant at all."""
    return any(
        call.name == "ci_green"
        for _, condition in condition_sites(config)
        for call in iter_calls(condition)
    )


def _ticket_label_args(condition: Condition) -> Iterator[str]:
    """Every `ticket_label(...)` argument in a condition tree — `entry.when` reads one at
    admission, and a step's pre/postconditions may read others every tick."""
    for call in iter_calls(condition):
        if call.name == "ticket_label" and call.args:
            yield call.args[0]


def _config_labels(config: ProcessConfig) -> set[str]:
    """Every literal label name the process will read or write (bootstrap step 6).

    Two families: names written in config (every outcome's `ticketLabel` and every `ticket_label`
    predicate argument), and one `factorial:<step>` per step — both the steady label and the
    blocked-at label cover every step. Templated names (`{ticket}`, `{roles.x}`) are dropped:
    they resolve per ticket, so their existence is unknowable at readiness time.
    """
    outcomes: list[Outcome | None] = [config.terminal.actions]
    if config.defaults.on_failure is not None:
        outcomes.append(config.defaults.on_failure.escalate)
    for step in config.steps:
        if isinstance(step, GateStep):
            outcomes += [step.gate.waiting, step.gate.on_approve, step.gate.on_reject]
        else:
            outcomes.append(getattr(step, "on_start", None))
            outcomes.append(step.on_success)
            if step.on_failure is not None:
                outcomes.append(step.on_failure.escalate)

    labels = {LABEL_BLOCKED, LABEL_DONE, LABEL_EXIT, LABEL_INTERRUPTED, LABEL_WAITING}
    labels.update(o.ticket_label for o in outcomes if o is not None and o.ticket_label)
    for _, condition in condition_sites(config):
        labels.update(_ticket_label_args(condition))
    labels.update(step_label(step.name) for step in config.steps)
    return {name for name in labels if not placeholders(name)}


async def gitlab_access_item(client: Any, project: Project) -> ReadinessItem:
    """Whether the stored GitLab token can read this project. Same row as the App check, so a
    GitHub project never reaches it."""
    from orchestrator.gitlab.client import GitLabError
    from orchestrator.gitlab.readiness import project_visible

    if client is None:
        return ReadinessItem(
            key="app_access",
            state="unknown",
            summary="not checked",
            detail=GITLAB_NOT_CONFIGURED,
        )
    try:
        path = await project_visible(client, project.github_repo)
    except GitLabError as exc:
        if exc.status_code == 404:
            return ReadinessItem(
                key="app_access",
                state="warn",
                summary=f"{project.github_repo} is not in reach",
                detail=f"The GitLab token cannot read {project.github_repo}.",
            )
        return ReadinessItem(
            key="app_access",
            state="unknown",
            summary="not checked",
            detail=f"GitLab project not checked ({_brief(exc)}).",
        )
    except Exception as exc:  # a probe degrades; it never takes down the readiness read
        return ReadinessItem(
            key="app_access",
            state="unknown",
            summary="not checked",
            detail=f"GitLab project not checked ({_brief(exc)}).",
        )
    return ReadinessItem(key="app_access", state="ok", summary=f"token can read {path}")


async def access_item(reader: AccessReader | None) -> ReadinessItem:
    """Whether the project's GitHub connection can reach its repositories at all: its own repo,
    and its GitHub intake repo when that is another.

    First on the checklist because it is the precondition for the rest: with the repo out of
    reach, the label read, the protection read and every manifest read fail the same anonymous
    way, and the page can only say "not checked" four times over. This row is what turns four
    shrugs into one cause.

    `blocked` on positive evidence that no run can work: the App is not installed on an owner,
    the installation leaves a repo out, GitHub rejects the token or the token cannot see a
    repo. It reads as the error it is, like a `runtime` row's, and still never refuses an
    activation, because `blockers` reads only `core_items`. A failed read is `unknown`, so an
    outage still cannot make a correctly configured project look broken. With two repos, each
    gets a check, so `behind_access` can tell whether the project's own repo is the one out of
    reach.
    """
    if reader is None:
        return ReadinessItem(
            key="app_access",
            state="unknown",
            summary="not checked",
            detail=f"{GITHUB_NOT_CONNECTED} See {APP_DOCS}.",
        )
    if reader.mode == "token":
        return await _token_access_item(reader)
    return await _app_access_item(reader)


def _repo_checks(reached: Mapping[str, bool]) -> list[ReadinessCheck]:
    if len(reached) < 2:
        return []
    return [
        ReadinessCheck(
            key=repo,
            title=repo,
            description="reachable" if passed else "not in reach",
            passed=passed,
        )
        for repo, passed in reached.items()
    ]


async def _app_access_item(reader: AccessReader) -> ReadinessItem:
    found: dict[str, InstallationAccess | Exception] = {}
    for repo in reader.repos:
        try:
            found[repo] = await reader.installation(repo)
        except Exception as exc:  # a probe degrades; it never takes down the readiness read
            found[repo] = exc
    checks = _repo_checks(
        {
            repo: isinstance(got, InstallationAccess) and got.repo_reachable
            for repo, got in found.items()
            if isinstance(got, InstallationAccess | AppNotInstalled)
        }
    )
    for repo, got in found.items():
        if isinstance(got, AppNotInstalled):
            return ReadinessItem(
                key="app_access",
                state="blocked",
                summary=f"App not installed on {got.owner}",
                detail=f"The GitHub App is not installed on {got.owner}. Install it there and "
                f"give it {repo}.",
                checks=checks,
            )
        if isinstance(got, InstallationAccess) and not got.repo_reachable:
            return ReadinessItem(
                key="app_access",
                state="blocked",
                summary=f"{repo} is not in reach",
                detail=f"The App is installed on {got.account} but cannot see {repo}. Add it to "
                "the App's selected repositories.",
                link=got.settings_url,
                checks=checks,
            )
    installed = [got for got in found.values() if isinstance(got, InstallationAccess)]
    # The granted permission set is deliberately NOT reported here. It was, so an operator could
    # confirm a queued permission request had been accepted — but `branch_protection` already
    # answers that by flipping off "not checked" the moment `administration: read` arrives, and
    # it answers it next to the row the permission is for. `workflows` is the one exception:
    # holding it is the defect (design.md §7.2).
    for got in installed:
        if got.permissions.get("workflows"):
            return ReadinessItem(
                key="app_access",
                state="warn",
                summary=f"App on {got.account} can change workflows",
                detail=f"The App's installation on {got.account} grants workflows access. "
                "Remove that permission from the App.",
                link=got.settings_url,
                checks=checks,
            )
    for got in found.values():
        if isinstance(got, Exception):
            return ReadinessItem(
                key="app_access",
                state="unknown",
                summary="not checked",
                detail=f"GitHub App installation not checked ({_brief(got)}).",
                checks=checks,
            )
    accounts = ", ".join(dict.fromkeys(got.account for got in installed))
    return ReadinessItem(
        key="app_access",
        state="ok",
        summary=f"{', '.join(reader.repos)} · App installed on {accounts}",
        link=installed[0].settings_url,
        checks=checks,
    )


async def _token_access_item(reader: AccessReader) -> ReadinessItem:
    statuses: dict[str, int] = {}
    for repo in reader.repos:
        try:
            statuses[repo] = await reader.repo_status(repo)
        except Exception as exc:  # a probe degrades; it never takes down the readiness read
            return ReadinessItem(
                key="app_access",
                state="unknown",
                summary="not checked",
                detail=f"Repository access not checked ({_brief(exc)}).",
            )
        if statuses[repo] == 401:
            return ReadinessItem(
                key="app_access",
                state="blocked",
                summary="GitHub rejected this project's access token",
                detail="Save a new token on Ticket source.",
            )
    checks = _repo_checks(
        {repo: code < 300 for repo, code in statuses.items() if code < 300 or code == 404}
    )
    for repo, code in statuses.items():
        if code == 404:
            return ReadinessItem(
                key="app_access",
                state="blocked",
                summary=f"The access token cannot see {repo}",
                detail=f"Give the token access to {repo}.",
                checks=checks,
            )
    for code in statuses.values():
        if code >= 300:
            return ReadinessItem(
                key="app_access",
                state="unknown",
                summary="not checked",
                detail=f"Repository access not checked (GitHub answered {code}).",
                checks=checks,
            )
    return ReadinessItem(
        key="app_access",
        state="ok",
        summary=f"{', '.join(reader.repos)} · token can read it. Factorial cannot check the "
        "token's workflows permission.",
        checks=checks,
    )


def behind_access(
    access: ReadinessItem, rows: Sequence[ReadinessItem], project: Project
) -> list[ReadinessItem]:
    """The other repo-side rows, unread when the project's own repo is out of reach.

    Every one of their reads fails the same way then, and each turned that into its own wrong
    verdict: a config branch "missing", a protection read GitHub "did not answer". None of those
    is what is wrong, so each row says it was not checked and why, and the cause is said once,
    on `app_access`. The detail is dropped so the warnings list carries that cause once. Only
    the intake repo out of reach leaves them alone: they read the project's repo.
    """
    if access.state != "blocked" or any(
        check.key == project.github_repo and check.passed for check in access.checks
    ):
        return list(rows)
    unread = f"not checked: Factorial cannot reach {project.github_repo}"
    return [
        row.model_copy(update={"state": "unknown", "summary": unread, "detail": None, "checks": []})
        for row in rows
    ]


#: GitHub label colours by role, matching bootstrap step 6's `gh label create` block.
_ADMIT, _RUNNING, _WAITING, _STOPPED, _TERMINAL = "0e8a16", "1d76db", "fbca04", "b60205", "5319e7"
_EXITED, _PARKED = "6e7781", "d93f0b"
#: `factorial:process:<key>`. Its own colour because it is the one family a human ADDS and the
#: orchestrator only reads — every other name here is something the projector writes.
_SELECT = "d4c5f9"


def all_label_palettes(configs: Mapping[str, ProcessConfig]) -> dict[str, tuple[str, str]]:
    """The union of every lane's palette, plus the label that selects each lane. Its names are the
    set the readiness row diffs and the create button writes.

    A label one lane projects is a label the repo has to define, whichever lane a given ticket
    happens to run. `factorial:process:<key>` joins them even though nothing projects it: GitHub's
    label picker offers only labels that already exist, so a lane whose selection label the repo
    does not define cannot be chosen on an issue at all. Listing it here is what makes it
    creatable from the configuration page rather than by hand.

    Lanes are merged in key order, so where two lanes give one name different roles the later
    key's wins — a colour is a hint, and refusing to produce a palette because two lanes disagree
    about one label would leave the repo with none. The selection labels go on last for the same
    reason: a step named `process:<key>` would project the identical name, and what an operator
    puts on an issue to choose a lane is the meaning worth keeping.
    """
    palette: dict[str, tuple[str, str]] = {}
    for key in sorted(configs):
        palette.update(_label_palette(configs[key]))
    for key in sorted(configs):
        palette[process_label(key)] = (_SELECT, f"Run this issue through the {key} process")
    return palette


def _label_palette(config: ProcessConfig) -> dict[str, tuple[str, str]]:
    """`name -> (colour, description)` for every label `_config_labels` expects.

    Derived from each label's role in *this* config rather than a fixed table, because half the
    names cannot be tabulated: `factorial:<step>` is spelled from a step name the config author
    chose, and bootstrap step 6 calls that family out as the one "never written in config". A table
    would be right for the sample process and wrong for every other one.

    Later assignments win, so the order below is a precedence: a step label overrides the generic
    entry, and the five fixed status labels override everything.
    """
    palette: dict[str, tuple[str, str]] = {}
    for name in _config_labels(config):
        palette[name] = (_RUNNING, f"Factorial: {step_from_label(name) or name}")
    for name in _ticket_label_args(config.entry.when):
        if name in palette:
            palette[name] = (_ADMIT, "Admit this issue to Factorial")
    for step in config.steps:
        label = step_label(step.name)
        if isinstance(step, GateStep):
            if step.gate.waiting is not None and step.gate.waiting.ticket_label in palette:
                palette[step.gate.waiting.ticket_label] = (
                    _WAITING,
                    f"Waiting: {step.name}",
                )
            if label in palette:  # projected alongside factorial:blocked, naming the stuck gate
                palette[label] = (_STOPPED, f"Blocked at {step.name}")
        elif isinstance(step, CheckStep):
            if label in palette:
                palette[label] = (_RUNNING, f"Waiting for {step.name} conditions")
        elif label in palette:
            palette[label] = (_RUNNING, f"{step.name} agent running")
    if LABEL_BLOCKED in palette:
        palette[LABEL_BLOCKED] = (
            _STOPPED,
            "Stopped on a failure. Fix it, then remove this label to retry.",
        )
    if LABEL_DONE in palette:
        palette[LABEL_DONE] = (_TERMINAL, "Factorial finished")
    if LABEL_EXIT in palette:
        palette[LABEL_EXIT] = (_EXITED, "Left Factorial. Remove this label to bring it back.")
    if LABEL_INTERRUPTED in palette:
        palette[LABEL_INTERRUPTED] = (
            _PARKED,
            "A person stopped the run. Remove this label to resume it.",
        )
    if LABEL_WAITING in palette:
        palette[LABEL_WAITING] = (
            _PARKED,
            "Agent waiting for an answer. Remove this label or press Resume to continue.",
        )
    return palette


def host_of(project: Project, source: TicketSource | None) -> HostDisplay:
    """Display metadata for the code host this project's repo-side probes talk to."""
    return display_of(project, source.kind if source else None)


def _unconfigured(host: HostDisplay) -> str:
    return host.unconfigured


def _protection_remedy(
    repo: str, branch: str, *, host: HostDisplay, checks: frozenset[str] = frozenset()
) -> str:
    return host.protection_remedy(repo, branch, checks)


async def label_item(
    read_labels: LabelReader | None,
    project: Project,
    configs: Mapping[str, ProcessConfig],
    *,
    host: HostDisplay = GITHUB_DISPLAY,
) -> ReadinessItem:
    """Whether the repo defines every label the process reads or writes (bootstrap step 6).

    Never `blocked`: whether GitHub's set-labels call auto-creates a missing label is unverified,
    so a missing label is not provably fatal. Checked against `project.github_repo`, the repo the
    projector writes to. Same degrade contract as `secret_gap` — any failure resolves to
    `unknown` with a "not checked" detail, never an exception and never a false cross.
    """
    if not configs:
        return ReadinessItem(
            key="labels", state="unknown", summary="no process config to derive labels from"
        )
    try:
        expected = set(all_label_palettes(configs))
        if read_labels is None:
            hand = host.labels_hand
            return ReadinessItem(
                key="labels",
                state="unknown",
                summary="not checked",
                detail=f"Ticket labels not checked. {host.unconfigured} {hand}",
            )
        missing = sorted(expected - await read_labels(project.github_repo))
        if missing:
            return ReadinessItem(
                key="labels",
                state="warn",
                summary=f"{len(missing)} missing",
                detail=f"Label(s) {', '.join(missing)} do not exist in {project.github_repo}. "
                "Factorial adds most of them to issues itself, and a person puts a "
                f"{process_label('<name>')} label on an issue to choose its process. GitHub "
                "offers only labels that already exist, so a missing one cannot be chosen. Create "
                "the missing labels below, or by hand.",
            )
        return ReadinessItem(key="labels", state="ok", summary="all defined in the repo")
    except Exception as exc:  # a probe degrades, never takes down the readiness read
        return ReadinessItem(
            key="labels",
            state="unknown",
            summary="not checked",
            detail=f"Ticket labels not checked ({_brief(exc)}).",
        )


#: Another install's seed is known only to that install's database.
_ADOPT_OR_RESEED: Final = (
    "No known commit is beneath it, which is what a branch written by another Factorial "
    "install, or created by hand, looks like. Review and adopt the head if you trust it, or "
    f"delete the `{CONFIG_BRANCH}` branch (an admin, once the ruleset protects it) and create "
    "it again."
)


def unknown_head_detail(head: str, known: str | None) -> str:
    """Why the builder refuses `head`, and how to get back to `known` (None: adopt or re-seed)."""
    what = (
        f"The `{CONFIG_BRANCH}` head {head[:7]} was pushed directly. Commit and sync did not "
        "write it, and no pull request merged it. Factorial can't tell whether a person or an "
        "agent pushed it, so it won't sync it or commit on top of it."
    )
    if known is None:
        return f"{what} {_ADOPT_OR_RESEED}"
    return (
        f"{what} Review the changes and adopt them, or have an admin "
        f"(the ruleset bypass) reset the branch to {known[:7]}, the last known commit."
    )


def reset_command(known: str) -> str:
    return f"git push --force-with-lease origin {known}:refs/heads/{CONFIG_BRANCH}"


async def config_branch_items(
    read_rules: ConfigBranchReader | None,
    session: AsyncSession,
    source: RepoConfigSource | None,
    project: Project,
    *,
    host: HostDisplay = GITHUB_DISPLAY,
) -> tuple[ReadinessItem, ReadinessItem]:
    """The `factorial` branch config is read from, as two rows off one read. `config_branch`:
    whether it exists on a commit the sync guard accepts, which the Process config page owns
    because its fixes are there. `config_branch_rules`: whether it cannot be deleted or
    force-pushed and, on GitHub, carries no rule that stops the builder's commits, which the Git
    Repo page draws beside the default branch's protection.

    Never `blocked`: admission reads stored snapshots, not the branch.
    """
    if read_rules is None or source is None:
        unread = ReadinessItem(
            key="config_branch",
            state="unknown",
            summary="not checked",
            detail=f"{GITHUB_NOT_CONNECTED} See {APP_DOCS}.",
        )
        return unread, unread.model_copy(update={"key": "config_branch_rules"})
    try:
        rules = await read_rules(project.github_repo)
        known = rules is None or await is_known_commit(session, source, project, rules.head_sha)
        base = (
            None
            if known or rules is None
            else await known_base(session, source, project, rules.head_sha)
        )
    except Exception as exc:  # a probe degrades; it never takes down the readiness read
        unread = ReadinessItem(
            key="config_branch",
            state="unknown",
            summary="not checked",
            detail=f"The `{CONFIG_BRANCH}` branch was not checked ({_brief(exc)}).",
        )
        return unread, unread.model_copy(update={"key": "config_branch_rules"})
    if rules is None:
        return ReadinessItem(
            key="config_branch",
            state="warn",
            summary="missing",
            detail=f"{project.github_repo} has no `{CONFIG_BRANCH}` branch. Config is synced from "
            "it and the builder commits to it. Create it from the Process config page, from "
            "the default branch's config folder or a template. A branch created by hand is "
            "refused.",
        ), ReadinessItem(
            key="config_branch_rules",
            state="unknown",
            summary=f"no `{CONFIG_BRANCH}` branch yet",
            detail=f"The `{CONFIG_BRANCH}` branch does not exist yet, so it has no rules to "
            "check. Create it from the Process config page.",
        )
    rules_row = _config_rules_item(rules, project.github_repo, host=host)
    if not known:
        # Every builder commit is refused until the reset happens, whatever the rules say.
        return ReadinessItem(
            key="config_branch",
            state="warn",
            summary=f"unknown head {rules.head_sha[:7]}",
            detail=unknown_head_detail(rules.head_sha, base),
            remedy=None if base is None else reset_command(base),
        ), rules_row
    return ReadinessItem(
        key="config_branch", state="ok", summary=f"{CONFIG_BRANCH} · on a known commit"
    ), rules_row


def _config_rules_item(rules: ConfigBranchRules, repo: str, *, host: HostDisplay) -> ReadinessItem:
    gaps = rules.shortfalls()
    checks = _config_rule_checks(rules, repo, host=host)
    if not gaps:
        return ReadinessItem(
            key="config_branch_rules",
            state="ok",
            summary=f"{CONFIG_BRANCH} · no force-push or deletion",
            checks=checks,
        )
    summary = f"{CONFIG_BRANCH}: {gaps[0] if len(gaps) == 1 else f'{len(gaps)} gaps'}"
    remove = (
        f"Remove the {', '.join(rules.blocking)} rule from the ruleset on "
        f"refs/heads/{CONFIG_BRANCH}"
        if rules.blocking
        else ""
    )
    remove_sentence = f"{remove}." if remove else ""
    if not rules.unprotected:
        return ReadinessItem(
            key="config_branch_rules",
            state="warn",
            summary=summary,
            detail=f"The builder commits to `{CONFIG_BRANCH}` directly. {remove_sentence}",
            checks=checks,
        )
    return ReadinessItem(
        key="config_branch_rules",
        state="warn",
        summary=summary,
        detail=f"The `{CONFIG_BRANCH}` branch's rules fall short ({', '.join(gaps)}). Without "
        "force-push and deletion protection, a push could rewrite or delete config history. "
        + host.config_rules_cannot_write
        + (f" {remove_sentence}" if remove else ""),
        remedy=_config_protection_remedy(repo, host=host),
        checks=checks,
    )


def _config_protection_remedy(repo: str, *, host: HostDisplay) -> str:
    """The one command that blocks force-push and deletion on the config branch. GitLab has no
    ruleset to copy: protecting the branch is what refuses a force-push or a deleting push.
    Developers keep push access, as a GitHub ruleset leaves writers theirs, because a protected
    branch otherwise takes pushes from Maintainers only and the builder's commits would stop."""
    return host.config_protection_remedy(repo)


def _config_rule_checks(
    rules: ConfigBranchRules, repo: str, *, host: HostDisplay
) -> list[ReadinessCheck]:
    """The config branch's rules one by one. Force-push and deletion share a remedy on purpose:
    bootstrap.md's one `factorial-config` ruleset holds both, as GitLab's branch protection does,
    and adding either only ever adds."""
    fix = _config_protection_remedy(repo, host=host)
    shared = host.config_rules_shared
    protection = [
        ReadinessCheck(
            key="no_force_push",
            title="Blocks force-push",
            description=f"Nobody can rewrite the config history on `{CONFIG_BRANCH}`." + shared,
            passed="non_fast_forward" in rules.rule_types,
            remedy=fix,
        ),
        ReadinessCheck(
            key="no_deletion",
            title="Blocks deletion",
            description=f"Nobody can delete `{CONFIG_BRANCH}` and the config history on it."
            + shared,
            passed="deletion" in rules.rule_types,
            remedy=fix,
        ),
    ]
    # GitLab's reading is the branch's protected flag alone. It cannot see a rule that stops the
    # builder, so it claims no check of one.
    if not host.github_app:
        return protection
    blocking = rules.blocking
    return [
        *protection,
        # No command: the rule may sit in a ruleset that also protects other branches, and the
        # App holds no permission that would let it tell which edit is safe.
        ReadinessCheck(
            key="builder_can_commit",
            title="Lets the builder commit",
            description=f"Commit and sync writes to `{CONFIG_BRANCH}` directly, and the App has "
            "no ruleset bypass, so no rule on the branch may require a pull request, status "
            "checks, signatures, deployments or a merge queue, or restrict updates."
            + (
                f" Remove the {', '.join(blocking)} rule from the ruleset that sets it."
                if blocking
                else ""
            ),
            passed=not blocking,
            link=f"https://github.com/{repo}/settings/rules" if blocking else None,
        ),
    ]


#: `policy.branchProtection`, strictest first. Several lanes may declare different levels and
#: there is one default branch, so the strictest wins: a lane that asks for a guarantee must not
#: have it silently downgraded by a lane that does not care.
_PROTECTION_ORDER: Final = ("required", "advisory", "ignored")


def _protection_unread(exc: Exception, repo: str) -> str:
    """Why branch protection could not be read, in words an operator can act on. The usual cause
    is a 403: the App holds no `administration: read` by default. GitHub's own text says nothing
    actionable, so it goes to the log and never onto a row."""
    if isinstance(exc, RequestFailed) and exc.response.status_code == 403:
        return (
            "Reading branch protection needs the App's `administration: read` permission, "
            "which is not granted by default."
        )
    logger.warning("%s: branch protection read failed", repo, exc_info=True)
    return "GitHub did not answer the branch protection read."


def _strictest_protection(configs: Mapping[str, ProcessConfig]) -> str:
    levels = {config.policy.branch_protection for config in configs.values()}
    return next((level for level in _PROTECTION_ORDER if level in levels), "advisory")


def probe_keys(configs: Mapping[str, ProcessConfig]) -> list[ReadinessKey]:
    """The repo-side rows `GET /readiness` will answer with, in its order. Which rows exist is
    decided by the config alone, so the first paint can hold a place for every one of them and
    the page never grows a row when the probes land."""
    keys: list[ReadinessKey] = ["app_access", "config_branch", "config_branch_rules", "labels"]
    if _strictest_protection(configs) != "ignored":
        keys.append("branch_protection")
    if any(_uses_ci_green(config) for config in configs.values()):
        keys.append("ci_workflows")
    if _permission_merge_gates(configs):
        keys.append("gate_approvers")
    return keys


async def protection_item(
    read_protection: ProtectionReader | None,
    project: Project,
    configs: Mapping[str, ProcessConfig],
    *,
    host: HostDisplay = GITHUB_DISPLAY,
) -> ReadinessItem | None:
    """Whether the default branch is protected — the control that actually stops a bad merge.

    Never `blocked`, for two independent reasons. The orchestrator cannot distinguish "no
    protection" from "not allowed to look" without positive evidence, and a project may
    legitimately run against an unprotected repo. Gating admission on an unreadable answer would
    strand a correctly-configured project, so this row informs and does not decide.

    No fix action accompanies it. Applying protection needs `administration: write`, and an App
    that can write protection can remove it — while being the author of every implementation PR
    that protection exists to hold back. The remedy is a command the operator runs under their
    own credentials.

    `policy.branchProtection` decides how loudly this reports, never whether it decides anything.
    `ignored` returns None and the row disappears — a row nobody will ever act on teaches a
    reader to skim the whole list. `required` turns a shortfall red and, more importantly, turns
    an UNREADABLE probe into a warning rather than a shrug: a process that asks for a guarantee
    nobody can verify is the one case where "not checked" is itself the finding.
    """
    level = _strictest_protection(configs)
    if level == "ignored":
        return None
    required = level == "required"
    if read_protection is None:
        return ReadinessItem(
            key="branch_protection",
            state="warn" if required else "unknown",
            summary="not checked",
            detail=f"Branch protection not checked. {_unconfigured(host)}"
            + (
                " This process sets policy.branchProtection: required, so nothing verifies the "
                "protection it requires."
                if required
                else ""
            ),
            remedy=_protection_remedy(project.github_repo, "main", host=host),
        )
    try:
        rule = await read_protection(project.github_repo)
    except Exception as exc:  # a probe degrades; a 403 here is the missing `administration: read`
        return ReadinessItem(
            key="branch_protection",
            # A process that declares `required` and cannot verify it has a real gap: it claims
            # a guarantee nothing is checking. That is a warning, not a shrug.
            state="warn" if required else "unknown",
            summary="required, but not verifiable" if required else "not checked",
            detail="Branch protection not checked. "
            + _protection_unread(exc, project.github_repo)
            + (
                " This process sets policy.branchProtection: required, so nothing verifies the "
                "protection it requires."
                if required
                else ""
            ),
            remedy=_protection_remedy(project.github_repo, "main", host=host),
        )
    gaps = rule.shortfalls()
    # Only GitHub's rule carries the settings a check fixes one at a time. A GitLab profile has
    # no checks, and the row's own remedy sets the whole of it.
    checks: list[ReadinessCheck] = []
    kept: frozenset[str] = frozenset()
    if isinstance(rule, BranchProtection):
        checks = _protection_checks(rule, project.github_repo)
        kept = rule.required_checks
    if not gaps:
        return ReadinessItem(
            key="branch_protection",
            state="ok",
            summary=f"{rule.branch} · 1 approval, CODEOWNERS, verify, dismiss-stale",
            checks=checks,
        )
    return ReadinessItem(
        key="branch_protection",
        state="warn",
        summary=(
            f"{rule.branch}: {gaps[0]}" if len(gaps) == 1 else f"{rule.branch}: {len(gaps)} gaps"
        ),
        detail=f"Branch protection on {rule.branch} does not meet the profile "
        f"({', '.join(gaps)})"
        + (", and this process requires it" if required else "")
        + ". The spec pull request is the only path to the default branch, and without "
        "protection a merge does not need a person's approval.",
        remedy=_protection_remedy(project.github_repo, rule.branch, host=host, checks=kept),
        checks=checks,
    )


def _protection_checks(rule: BranchProtection, repo: str) -> list[ReadinessCheck]:
    """The profile one setting at a time, each with a command that changes that setting alone.
    GitHub can switch review or status-check enforcement on only by replacing the whole rule, so
    while one is off its settings fall back to the whole-profile PUT, and say so."""
    branch = rule.branch
    whole = protection_command(repo, branch, rule.required_checks)
    if not rule.protected:
        return [
            ReadinessCheck(
                key="protected",
                title="Has a protection rule",
                description=f"Without a rule, anyone with write access can push to {branch} or "
                "merge without a review. The command applies the whole profile: one approval, a "
                "code owner's review, stale approvals dismissed, and `verify` required.",
                passed=False,
                remedy=whole,
            )
        ]
    api = f"repos/{repo}/branches/{branch}/protection"
    replaces = f" The command replaces the protection rule on {branch} with the whole profile."

    def review(field: str) -> str:
        return f"gh api -X PATCH {api}/required_pull_request_reviews -F {field}"

    reviews_off = (
        ""
        if rule.reviews_enabled
        else f" Pull request reviews are off on {branch}, and only a new rule can switch them on."
        + replaces
    )
    checks_off = (
        ""
        if rule.checks_enabled
        else f" Required status checks are off on {branch}, and only a new rule can switch them "
        "on." + replaces
    )
    return [
        ReadinessCheck(
            key="protected",
            title="Has a protection rule",
            description=f"Nobody can push to {branch} or merge into it except as the rule "
            "allows. The spec pull request is the only path to it.",
            passed=True,
        ),
        ReadinessCheck(
            key="approval",
            title="Requires an approving review",
            description="A pull request needs one approval before it merges. The App opens "
            "every pull request and cannot approve its own, so a person must." + reviews_off,
            passed=rule.required_approvals >= 1,
            remedy=review("required_approving_review_count=1") if rule.reviews_enabled else whole,
        ),
        ReadinessCheck(
            key="codeowners",
            title="Requires a code owner's review",
            description="A change to a path listed in CODEOWNERS needs approval from its owner."
            + reviews_off,
            passed=rule.requires_codeowners,
            remedy=review("require_code_owner_reviews=true") if rule.reviews_enabled else whole,
        ),
        ReadinessCheck(
            key="dismiss_stale",
            title="Dismisses stale approvals",
            description="A new push clears earlier approvals, so an approval always covers the "
            "code that merges." + reviews_off,
            passed=rule.dismisses_stale,
            remedy=review("dismiss_stale_reviews=true") if rule.reviews_enabled else whole,
        ),
        ReadinessCheck(
            key="verify",
            title="Requires the `verify` check",
            description=f"The `verify` status check must pass before a pull request merges into "
            f"{branch}." + checks_off,
            passed="verify" in rule.required_checks,
            remedy=f"gh api -X POST {api}/required_status_checks/contexts -f 'contexts[]=verify'"
            if rule.checks_enabled
            else whole,
        ),
    ]


async def ci_workflows_item(
    read_workflows: WorkflowsReader | None,
    project: Project,
    configs: Mapping[str, ProcessConfig],
    *,
    host: HostDisplay = GITHUB_DISPLAY,
) -> ReadinessItem | None:
    """Whether the repo defines any GitHub Actions workflows — shown only when the process
    gates on `ci_green`, whose verdict can never hold without check runs.

    This row exists because the runtime deliberately does NOT skip: zero check runs is not
    green (a validation stage must not pass vacuously), and from observed state "no CI
    defined" is indistinguishable from "CI did not trigger for this branch" — the exact shape
    of the sandbox's PR #23 base-filter miss. Any skip signal would also live in
    `.github/workflows/`, a path agents write to, which is the §7.5 trust inversion. So a
    no-CI repo makes every `ci_green` stage wait out its timeout and escalate — and this row
    says that at configuration time, before a ticket has to.

    Never `blocked`: a probe verdict must not gate activation, and the remedy is a config or
    repo edit the operator owns.
    """
    gating = sorted(key for key, config in configs.items() if _uses_ci_green(config))
    if not gating:
        return None
    if read_workflows is None:
        why = host.workflows_unconfigured
        return ReadinessItem(
            key="ci_workflows",
            state="unknown",
            summary="not checked",
            detail=(
                why + f"Process(es) {', '.join(gating)} gate on ci_green, which needs CI to exist."
            ),
        )
    try:
        names = await read_workflows(project.github_repo)
    except Exception as exc:  # a probe degrades; it never takes down the readiness read
        return ReadinessItem(
            key="ci_workflows",
            state="unknown",
            summary="not checked",
            detail=f"Workflows not checked ({_brief(exc)}). Process(es) {', '.join(gating)} "
            "gate on ci_green, which needs CI to exist.",
        )
    if names:
        shown = ", ".join(names[:3]) + (f" and {len(names) - 3} more" if len(names) > 3 else "")
        return ReadinessItem(key="ci_workflows", state="ok", summary=shown)
    return ReadinessItem(
        key="ci_workflows",
        state="warn",
        summary="no workflows, but a process gates on ci_green",
        detail=f"Process(es) {', '.join(gating)} reference ci_green and {project.github_repo} "
        + (
            host.no_ci
        )
        + "Zero check runs is never "
        "green, so every ci_green step will wait out its timeout and escalate to "
        "factorial:blocked on every ticket in those processes. Add a CI workflow, or remove the "
        "step from the process file and sync a new version.",
    )


def _permission_merge_gates(
    configs: Mapping[str, ProcessConfig], branch: str | None = None
) -> list[str]:
    """`lane.step` for each merging gate a repository role may decide — into `branch`, when
    given."""
    return sorted(
        f"{key}.{step.name}"
        for key, config in configs.items()
        for step in config.steps
        if isinstance(step, GateStep)
        and step.gate.approver_permission is not None
        and step.gate.on_approve.merge
        and (branch is None or matches_ref(step.gate.pr.base, branch))
    )


async def gate_approvers_item(
    read_protection: ProtectionReader | None,
    project: Project,
    configs: Mapping[str, ProcessConfig],
    *,
    host: HostDisplay = GITHUB_DISPLAY,
) -> ReadinessItem | None:
    """Whether a gate a repository role may decide merges into a branch that also requires a
    code-owner review (design.md §7.12).

    A writer who is not a code owner can then pass the gate, and GitHub refuses the merge. The
    gate already copes — it waits and comments with GitHub's reason — so this row only says so
    before the first ticket meets it. Never `blocked`; `unknown` when protection cannot be read,
    as `protection_item`.
    """
    if not _permission_merge_gates(configs):
        return None
    if read_protection is None:
        return ReadinessItem(
            key="gate_approvers",
            state="unknown",
            summary="not checked",
            detail=f"Gate approvers not checked. {_unconfigured(host)}",
        )
    try:
        rule = await read_protection(project.github_repo)
    except Exception as exc:  # a probe degrades; it never takes down the readiness read
        return ReadinessItem(
            key="gate_approvers",
            state="unknown",
            summary="not checked",
            detail="Gate approvers not checked. " + _protection_unread(exc, project.github_repo),
        )
    gates = _permission_merge_gates(configs, rule.branch)
    if not (rule.protected and rule.requires_codeowners and gates):
        return ReadinessItem(key="gate_approvers", state="ok", summary="no code-owner conflict")
    return ReadinessItem(
        key="gate_approvers",
        state="warn",
        summary=f"{rule.branch} also requires a code owner",
        detail=f"{', '.join(gates)} let anyone with the gate's approverPermission approve, and "
        f"{rule.branch} requires a code-owner review. A reviewer who is not a code owner can pass "
        "the gate. "
        + host.name
        + " then refuses the merge, and the gate waits and comments until a code "
        "owner approves. "
        + host.gate_approvers_fix,
    )


def shared_protection(read: ProtectionReader | None) -> ProtectionReader | None:
    """One protection read per request, for every row that needs it."""
    return None if read is None else AsyncKeyedMemo(read).get
