# Factorial — High-Level Design

**Status:** v0.17 · 2026-11-03
**Scope of this phase:** GitHub (Issues + PRs + Actions) and GitLab (issues, pull/merge requests and CI). App Runtime is out of scope.

## 1. Overview

The Software Factory automates the **Agent Development Life Cycle (ADLC)** — an SDLC where AI
agents do the work of each phase (refine, develop, …) and humans approve at defined gates. Forge
issues are the unit of work. Each ticket flows through a configuration-driven workflow;
each step either runs an agent or waits on a human gate. All artifacts (source, specs, config)
live in the project's GitHub or GitLab repository on ticket-specific branches.

```
GitHub or GitLab (issues / reviews / CI / webhooks)
        │  webhooks (wake-up) + polling (observed state)
        ▼
┌─────────────────────────────────────────────┐
│ ADLC Orchestrator                           │
│  · Reconciliation loop, state machine/ticket│──► Agent Runtime (wrapper)
│  · ADLC config (desired behavior)           │      · claude / codex CLIs
│  · Event log (append-only)                  │◄──   · callbacks: status, result, logs
│  · API + Dashboard UI                       │
└─────────────────────────────────────────────┘
        │
        ▼
GitHub Actions (CI, and stand-in for deploys — App Runtime later)
```

The orchestrator runs as one ECS Fargate task on AWS, behind an ALB, on RDS Postgres
(tech-stack decision 64). GitHub's webhooks reach it at `/factorial/webhooks/github/apps/<app_id>`
(an App) or `/factorial/webhooks/github/projects/<project_id>` (a token), and only wake it.

## 2. Core design decisions

1. **Reconciliation, not webhook-imperative.** The orchestrator runs a converge loop per active
   ticket. *Desired* behavior comes from the ADLC config; *observed* state comes from polling
   the project's selected forge (issue labels/assignees, branches, reviews, checks). Webhooks, and
   the dashboard's refresh
   of one ticket or of the fleet, are only wake-up signals that trigger an immediate poll — a
   dropped or duplicated one can never corrupt state. A delivery that names a ticket, by its
   issue or by a `<prefix>/<issue>-…` branch, wakes that ticket at once. A delivery about an
   issue that is no open ticket asks admission about that issue at once; the worker judges it
   on its own read of the issue, never on the payload. Any other wake is a fleet pass, at least
   `worker.min_pass_gap_seconds` apart, so a burst of them costs one pass. The poll interval
   stays the guarantee.
2. **Orchestrator owns the state.** The authoritative state machine per ticket lives in the
   orchestrator DB, keyed `(ticket, processVersion, step, attempt)`. Ticket labels/status are a
   *projection* of that state, written back for human visibility. Human edits to a ticket are
   observed input, not commands that bypass the machine. So is a request from the dashboard or
   from an MCP client (§7.16): exit, re-entry, restart, interrupt, resume, retry, un-block, redo,
   completion and step retry each append a request event that the loop answers on its next pass,
   and neither surface writes a label. The last three, #382's, are dashboard-only (tech-stack.md
   decisions 76-78). An un-block request is the label-free twin of removing `factorial:blocked`
   (§4).
3. **`next`-driven state machine.** Steps advance via each step's `next` pointer once its
   postconditions hold. A step's `entry.ticket` condition is a guard for admission, not the driver.
   Besides a gate's rework edge, a person's Redo is the only move back inside a generation: it is
   outside the declared graph and limited to tech-stack.md decision 76's two targets.
4. **Executable pre/postconditions.** Conditions are compositions of a predefined predicate
   library (§6) — machine-checkable, never prose. Postconditions double as the acceptance gate
   for agent output. A rework a gate sent back is also held until the rejected pull request has
   moved past the commit the gate rejected, because its postconditions held before it began. A
   change to a file the step declares, such as its summary, moves the pull request like any
   other push. One exception is named: a person's recorded Force complete. It is dashboard-only,
   judged on a full evaluation of every leaf, recorded as `step_run.forced_by`, announced on the
   issue, and never the agent's word (tech-stack.md decision 78).
5. **Human gates are first-class steps.** `kind: human_gate` steps map to required PR reviews on
   protected branches, so the forge enforces them. No agent-produced change reaches `main` without
   an approval gate.
6. **Failure is modeled, not implied.** Every agent step has timeout, retry, and an escalation
   path to a human-visible `factorial:blocked` state. (Cost is tracked in the event log for
   reporting; enforced per-step cost budgets are deferred to keep things simple.)
7. **Everything is an event.** Every transition, agent run, gate decision, and dollar spent is an
   append-only event. The dashboard, audit trail, and cost-per-issue reporting are read models
   derived from this log — never separately maintained state.
8. **Config is pinned by commit.** A ticket pins the snapshot it was admitted under and keeps it
   until done, or until an operator restarts it: a restart moves it to its lane's current snapshot
   and starts it fresh from the entry step (tech-stack.md decision 67). That snapshot is identified
   by the commit it was read from, and `(project, process, commit)` is unique. There is no integer
   `version` in the file any more: it was maintained by hand, it decided nothing a sha does not
   decide better, and it could disagree with the commit — editing a file without bumping it
   produced a refusal ("you forgot to bump `version`") that a sha makes impossible. Re-syncing a
   commit finds the row it already wrote, and needs no content comparison to be sure, because a sha
   is immutable. The field is still ACCEPTED on the model and never read, because a snapshot
   written when it was required re-parses on every tick and `extra="forbid"` would otherwise
   quarantine the ticket (tech-stack.md decision 31). "Current" for admission is the most recently
   synced snapshot for the lane, where `version DESC` used to answer. The pin covers the agents
   too: a synced `process_version` records, in `agent_pins`, the commit each agent it names was
   read from, and the ticket resolves its agents there. Before that column the snapshot held the
   process YAML alone, and every agent resolved through one project-wide `agent` row that each
   registration re-pinned — so syncing anything moved the manifests, prompts and skills under every
   ticket already in flight. What is frozen is what the agent DOES; what the operator PERMITS and
   where the work RUNS stay live and deliberately so, because a ticket that froze its own MCP
   allowlist, permission ceiling or `active` flag at admission could no longer be reined in (§7.5).
   Sync is the only way a snapshot is registered, so every one of them carries the commit its
   identity and its pins are built on — the JSON-body upload route that carried neither is gone.
   With no ref, sync reads the repo's protected `factorial` branch, not the default branch, and
   records `factorial` as the ref it read (tech-stack.md decision 53); a snapshot synced before
   that carries a NULL ref, which still means the default branch. The process builder's commit
   syncs exactly the sha it wrote, in the same request (§7.7).
9. **The issue title is the one piece of issue text the orchestrator stores.** GitHub owns the
   issue; the loop only observes it. The exception is `ticket.title`, refreshed from the
   `IssueState` every tick already reads to evaluate predicates — so it costs a column and no
   extra API call. This reverses the earlier "no stored issue text" position, which priced titles
   at a GitHub round trip per board row and left every cross-project list searchable by issue
   number alone. Body, comments, and every other field stay unstored and are read live where a
   single ticket needs them. The column is a cache, never the truth: it is nullable, a ticket that
   has not ticked since it shipped shows its number alone, and a `done` ticket keeps whatever was
   last observed rather than being re-fetched.
10. **A project runs several processes, and an issue picks one before any predicate runs.** Each
    lane is a file under `.factorial/processes/` (§4) with its own history of commits, so
    `(project, process, commit)` is what identifies a snapshot. Selection
    is a `factorial:process:<key>` label on the issue, or `project.settings.default_process` when
    it carries none; only then is the selected lane's `entry.when` evaluated, and a false one
    means the issue waits rather than trying another lane.

    `entry.when` deliberately does NOT decide the lane. It answers "is this issue ready to
    start?", and making one condition answer both questions would turn mutual exclusivity across
    lanes into an operator obligation that fails silently as lanes are added — the editor's
    template carries an identical `factorial:new` + assignee entry condition into every lane
    started from it, so any two such lanes would be born ambiguous.

    An unknown or retired process label is refused, never defaulted, because the two failure
    modes are not symmetrical: an operator who typos `factorial:process:hotfxi` and silently gets
    the default has their hotfix run the standard lane and finds out days later, at the gate. Two
    process labels are refused for the same reason. Each refusal appends one `admission_refused`
    event per `(project, issue, reason)`; the *current* answer is `GET /projects/{id}/pending`,
    which recomputes the pass, because an append-only log can say a refusal happened and never
    whether it still holds.

    The default lives in `project.settings`, never in the repo. `.factorial/` lives in the repo
    agents write to, on a `factorial` branch the App also pushes to (§7.7), so a repo-side default — a `process-default.yaml`, or a
    `default: true` key — would let an agent redirect every future ticket into a lane of its
    choosing by renaming one file. Same trust direction as §7.5: the repo selects, Postgres
    permits. Promoting a lane is therefore a settings write that renames nothing and retires
    nothing, which is the other reason a lane may not be NAMED `default`.

    Which lanes are selectable at all is decided by the last sync, not by the commit history: a
    key the commit no longer offers has `retired_at` stamped on its rows and stops being
    offerable, and reintroducing the file clears it and keeps every commit the lane had. The
    history is not a menu, or deleting a lane's file would leave it selectable for ever. A
    snapshot is selectable only while the repository it was read from (`source_repo`) is the
    project's own: changing a project's repository offers none of the old one's lanes, and the
    first sync of the new one retires them, even a lane of the same name. A ticket already in
    flight is untouched by any of it — it holds `process_version_id` and reads the row by id — so
    a sync that retires a lane cannot disturb work already in it. A sync that retires the project's
    own default MOVES the default to a lane the commit does offer, and reports both ends of the
    move: a project whose default names nothing admits no issue that does not label itself, and
    refusing the sync asked the operator for a settings edit the page cannot make, since the
    default may only name a lane the project still offers. The replacement is the first offered
    key in sort order, which is the only answer when a rename leaves one lane and a deterministic
    one when it does not.
11. **An organization is the tenant, and one rule decides what it owns.** State a tenant brings,
    or that a tenant's people act through, belongs to an organization: its projects (and through
    them every ticket, run, secret, GitHub connection and event), its runtimes and their vendor credentials, its MCP
    tokens, its assistant conversations, and the assistant's settings, credential and daily
    budget. State the deployment's operator configures once stays deployment-wide: the worker,
    the assistant process and its health, the shared API token and the Google client.

    A project never moves between organizations, so nothing a project owns carries the
    organization again: the join through `project_id` is exact. `event.project_id` stays NOT NULL
    for the same reason it always was, and an organization's own changes record `updated_by`, as
    `app_setting` does. Names are unique per organization, because two tenants will both want a
    project called `web`, and a `runtime: <slug>` in `process.yaml` therefore names a row in the
    project's own organization and no other: the repo selects, the tenant's operator defines
    (§7.5). The migration seeds one organization, `default`. Creating more, and who may act in
    each, is §7.19, behind a flag that is off by default; off, `default` is the only organization
    and everyone admitted reaches it.

## 3. Components

### 3.1 ADLC Orchestrator (core)

Responsibilities: admit tickets into a process; run the reconciliation loop; spawn agents via the
Agent Runtime; evaluate predicates against GitHub; write status projections back to the ticket;
append events; serve the API/UI.

The loop, per active ticket:

```
observe   →  poll GitHub: issue (labels, assignees), branches, PRs, reviews, check runs
evaluate  →  current step from state DB; check step postconditions / gate outcome
act       →  postconditions hold → advance to `next` (event: step_completed, step_entered)
             agent step entering → check preconditions, acquire ticket lease, spawn agent
             gate step → project "waiting for review" to ticket; do nothing until observed approval
             failure / timeout → retry or escalate per config
project   →  write ticket labels + assignees + comment if state changed
```

Concurrency safety: one lease per ticket; agent spawns are idempotent on
`(ticket, step, attempt)`. Redelivered webhooks and concurrent loop runs are harmless.

**Ticket-status projection:** issue labels `factorial:<step>` plus `factorial:blocked`,
`factorial:interrupted`, `factorial:waiting`, `factorial:exit`, `factorial:done` (tech-stack.md
decisions 26, 29 and 56). GitHub remains the default ticket source; an explicit GitLab source
routes every forge operation through that project's GitLab client and never falls back to the
GitHub App (tech-stack decision 60).

### 3.2 Agent Runtime (wrapper)

A thin contract over an agent substrate (the `claude` and `codex` CLIs, on the host or in an E2B
sandbox per run — tech-stack.md decision 25; Claude managed agents later), so the orchestrator
never depends on a vendor API:

```
spawn(runSpec)  → runId          runSpec: {agent, ticket, workspace(repo, branch, base),
                                           inputs, attachments(images, as bytes — §7.11),
                                           mcp(servers, credential-free — §7.5),
                                           secrets(scoped, short-lived), timeout,
                                           resumeFrom, public}
status(runId)   → queued | running | idle | succeeded | failed | timeout | cancelled | interrupted
result(runId)   → structured: {outcome, summary, artifacts: [branch, pr, files], costUsd}
logs(runId)     → stream/fetch
cancel(runId)
interrupt(runId, reason)         stop, but keep the session — a human means to redirect this
                                 run, not to end it (tech-stack.md decision 26). `cancelled` and
                                 `interrupted` are separate states because the orchestrator does
                                 opposite things with them: a cancelled step is retried, an
                                 interrupted one parks its ticket until a human releases it,
                                 or, when the loop parked it at its prompt, until its
                                 postconditions hold.
                                 `reason` is `user`, `idle`, `rollover` or `ceiling`
                                 (decision 26). A session that ends with nobody asking is
                                 reported `interrupted` with `park_reason` `exited`.
resume(runId, turn?, attachments)
                                 continue an interrupted run's conversation, `turn` being what a
                                 human said while it was parked, or nothing. The SAME run — same
                                 id, same workspace, same attempt — not a new one: the agent keeps
                                 its context instead of starting over from the spec.
                                 `attachments` are the images the steering comments embed.
finish(runId)                    end an idle session gracefully: when its
                                 postconditions hold, or when it stopped with nothing
                                 asked and nobody engaged, after which the worker fails
                                 the run on them; idempotent. At its plain prompt (a
                                 turn's end, a failure, or a question asked in words) the CLI is
                                 told to exit; on a question tool's dialog it is hung up;
                                 while a turn runs nothing is sent. The run reads
                                 `idle` until the finish settles, then `succeeded`, however its
                                 last turn went, unless a declared artifact is missing.
session_state(runId)             why and since when a run is idle, its question if
                                 it asked one and when the CLI would answer its question itself,
                                 its turn count, its tokens so far, its session
                                 deadline (none on a host), its session-log offset and when a
                                 writer last typed.
                                 Not when its token was minted: an adapter never mints (§7.1),
                                 so the orchestrator reads that from its own events.
terminal_target(runId)           an opaque handle (substrate, socket, session, sandbox) that the
                                 terminal gateway attaches to (tech-stack.md decision 57).
spawn_refusal()                  why the substrate as configured now must not run a session, or
                                 nothing: on E2B, a build without its interactive verdict
                                 (tech-stack.md decision 25); on a host, a CLI other than the
                                 pin (for codex, also npm's wrapper), no tmux 3.2, or a
                                 `local_root` that cannot hold a tmux socket or holds whitespace
                                 (decision 43). Asked before every spawn, never by `spawn`,
                                 which the smoke that gives the verdict uses.
inspect_start / inspect_status / inspect_stop
                                 optional (`InspectHost`), beside the preview verbs: the run's own
                                 agent session, reopened read-only in its kept workspace, which
                                 holds no credential on either substrate (tech-stack.md decision 54). `inspect_status`
                                 never wakes a parked workspace.
preview_start / preview_status / preview_stop
                                 optional (`PreviewHost`): serve a run's workspace to a browser,
                                 beside its live session or an open session as well as once its
                                 agent has stopped (tech-stack.md decision 54). `preview_status`
                                 takes the keep-until the worker computes. A preview is
                                 informational and changes no run status.
callback        → runtime POSTs terminal status to the orchestrator (preferred over polling;
                  polling remains as fallback — it's just reconciliation again)
```

**Every run is a session**, the CLI's own TUI in tmux (tech-stack.md decision 56). `idle` says
the session is alive at its prompt, waiting on a turn, and NOT terminal — the worker decides
from the postconditions whether to `finish` it or to wait for a human, and the step deadline
does not run while a run is idle. Every run's workspace is kept after it ends (decision 54);
`resumeFrom` (`{runId, sessionId}`) names the previous attempt's kept workspace and session for
a rework, and `public` asks for a sandbox a preview can serve.

Every verb above is required except `callback` and the two optional protocols, `PreviewHost`
and `InspectHost`. `callback` is optional per adapter. The CLI adapters have no inbound channel,
so they run on `status()` polling alone — the fallback path, by design. MCP support is optional
in the same way (`supports_mcp`), and an adapter that lacks it must refuse a spec that
declares servers rather than drop them (§7.5).

Agents must end by producing verifiable artifacts (branch, PR, files) — the orchestrator trusts
postcondition predicates, not the agent's self-report.

### 3.3 App Runtime

Out of scope. The `develop` step's CI runs on GitHub Actions; deploy steps will be added when the
App Runtime (a deploy wrapper) exists. Hosting the orchestrator itself on AWS (tech-stack
decision 64) is not an App Runtime. A step preview (§4) is not a deployment: it is a URL
for one run's output, and publishing one is not a deploy step.

### 3.4 The assistant

A chat in the dashboard's Drawer that reads the factory, explains it, proposes writes a person
approves, draws metrics and helps shape process configs (tech-stack.md decision 63). It adds no
authority: the model reaches the factory only through its turn's tools, which run in the
assistant's process and can read and propose but never write (§7.18).

A message is a row, not a request held open:

```
Drawer      POST …/messages  →  a user_text item and a queued chat_turn, in one transaction; 202
assistant   claim the turn (SKIP LOCKED, a lease)  →  refresh the read-only GitHub token  →  backend.turn(spec, tools)
process       each event  →  a chat_item row (a reply at most every 200 ms) + NOTIFY
              Usage  →  the turn's tokens and cost;  the end  →  its status
claude      a tool call  →  the toolbox, in the assistant's process  →  the dashboard's own services; a write tool records a proposal
API         GET …/stream (SSE)  ←  one LISTEN, fanned out; each stream re-reads past its last rev
```

- **A fourth process** (tech-stack.md decision 3), because a turn holds a `claude` child for
  minutes and a stuck child must not degrade the API, and because the process that runs the model
  should hold no GitHub credential: its tools run there, and read GitHub through entries the API
  hands it (§7.18).
- **A vendor-neutral backend.** `runtime/assistant/contract.py` is the wall: a `ChatTurnSpec`
  goes in, a stream of `ChatEvent`s comes out. The orchestrator never imports a vendor module, as
  §3.2 keeps it from a runtime's.
- **One turn per conversation at a time.** The backend gives a conversation one working
  directory, so the claim skips a conversation whose turn runs; a second send answers 409, and an
  approval's continuation waits behind the running turn.
- **Settings are read when a turn starts**, so a change reaches the next turn and never the one
  running.
- **A restart interrupts.** A turn whose lease runs out ends `interrupted`, and
  the conversation says to send the message again. Nothing resumes a half-run turn.
- **Conversations are private and short-lived:** each is its owner's, and it is deleted 30 days
  after its last item.

## 4. ADLC process configuration

YAML, one file per process, stored under `.factorial/processes/` on each target repo's protected
`factorial` branch (§7.7, tech-stack.md decision 53); samples in this repo under `docs/samples/`. Schema summary — full sample in
[`samples/factorial-simple/processes/process-simple.yaml`](samples/factorial-simple/processes/process-simple.yaml):

```
.factorial/
  processes/
    process-standard.yaml     # the layout from here on
    process-hotfix.yaml
  agents/<name>/agent.yaml
  mcp/<name>.yaml
```

The stem **minus its `process-` prefix** is the process name, enforced against the file's own
`name:` — the rule `agents/<name>/agent.yaml` already follows, and the shape `mcp/<name>.yaml`
already uses. So `processes/process-hotfix.yaml` declares `name: hotfix` and answers to
`factorial:process:hotfix`. The prefix is redundant with the folder and is there anyway: these
files are read far more often in an editor tab than in a directory listing, and
`process-hotfix.yaml` says what it is where `hotfix.yaml` does not.

The file carries no `version:`; decision 8 says why. The name is a slug
(`^[a-z0-9]([a-z0-9-]*[a-z0-9])?$`) because it becomes a path segment and a label, and `default`
is reserved (decision 10). Both rules are checked at the LOADER boundary and
never on the parsed model: the model re-parses pinned snapshots every tick, so a pattern on it
would fail an old non-slug snapshot into quarantine (tech-stack.md decision 31). A file in
`processes/` that is not `process-*.yaml` is refused rather than ignored, the same
"unknown keys are fatal" posture as everything else in the folder — a lane an operator believes
they added, silently skipped over a filename typo, is exactly what the strictness prevents.

The legacy single-process layout, `.factorial/process.yaml`, is **read for ever and never written
again** (tech-stack.md decision 37). It keeps whatever `name:` it carries: the identity rules
above guard a NEW lane, and a repo that predates the folder is not asked to rename its process —
there is nothing to migrate a pinned snapshot to. A repo carrying BOTH layouts is refused at sync,
naming the file to delete; silently preferring one would leave an operator editing a file nothing
reads. Because the authoring surface writes only the folder, it refuses ANY edit to a repo still
on the root file, not just one that adds a second lane — writing beside it would create exactly
that mixed layout. Migration is one `git mv` an operator makes deliberately.

- `defaults` — timeout, retry, escalation applied to every agent step. An escalation parks the
  ticket under `factorial:blocked` until a person releases it, by removing the label or by an
  un-block request from the dashboard or an MCP client (`unblock_requested`). Both are observed
  input (§2.2), and both take the one grant path, so a step whose pinned config has no retry
  policy refuses either in the same way. A request is also refused, with `unblock_refused`, when
  the ticket is parked rather than escalated, or has escalated again since the request. The
  dashboard's Retry on a ticket that a failed run's escalation blocked is the exception: it grants
  one attempt that continues the failed session, even with no retry policy (tech-stack.md
  decision 77).
- `policy` — what the process expects of the *repository*, as opposed to how its steps run.
  Today one key, `branchProtection: required | advisory | ignored` (default `advisory`), which
  sets how the configuration page reports the default branch's protection against the profile in
  decision 7.4's threat model: one approval, CODEOWNERS, `verify`, dismiss-stale. The `factorial`
  branch's rules are a separate readiness row that this key does not touch (§7.7).

  **None of the three enforce anything, and that is deliberate.** `.factorial/` lives in the repo
  agents write to, so a value here is agent-editable — the inverse of §7.5's rule that a repo
  file selects and operator state permits. Enforcement an agent can switch off is worse than
  none: it reads as a control while being a preference, and it stops the reader looking for the
  thing that actually enforces. What enforces is branch protection itself plus the App holding no
  bypass while authoring every implementation PR, neither of which this file can reach. Making
  `required` blocking would relocate a real control into an agent-writable file rather than
  create one. This is an accepted cost of putting the toggle where the process is described, in
  the same class as §7.1's "agents never add labels" being a prompt rule.

  `required` also escalates the *unreadable* case rather than the negative one: reading protection
  needs `administration: read`, which the App does not hold by default, so a process can declare
  `required` and have nothing verify it. That combination is reported, because a guarantee nobody
  checks is the failure this key is most likely to produce.
- `runtime` — required; the slug of an operator-defined Runtime (a type, a name, and the
  credentials the type needs — a `runtime` row in Postgres, managed on the dashboard's Runtime
  page) that this process's agents run on. Its type says which CLI runs them (tech-stack.md
  decision 70). The trust direction is §7.5's: the repo file *selects* a Runtime by slug and can
  neither create one nor reach its credentials; the registry *defines*. Sync refuses a process
  without the key, and a slug that does not exist.
  Replaces the never-read `project.settings.agent_runtime` slot.
- `roles` — named user groups referenced wherever a username list is accepted (`assign`,
  `approvers`).
- `entry` — which tickets are admitted and at which step.
- `steps[]` — each has `name`, `kind: agent | human_gate | action | check`, `next`, and:
  - **agent steps:** `agent`, `workspace` (branch template, base), `preconditions`,
    `postconditions`, `onStart` / `onSuccess` / `onFailure` projections — `onStart` fires when
    an attempt actually spawns, the one moment the issue otherwise learns nothing about.
  - **human_gate steps:** a single `gate` attribute holds ALL gate controls — what is gated
    (`type`, `pr`, `approvers`, `approverPermission`) and what happens (`waiting` projection, `onApprove` — merge +
    advance, `onReject` — loop back to a rework step with review comments as input). A gate whose
    `onReject.rerunStep` is an agent step also offers the dashboard's Request changes (§7.8),
    which sends work back exactly as a GitHub rejection does. A pull request closed without a
    merge is not a rejection: the gate holds, says so on the ticket, and waits for the pull
    request to reopen, a Redo, or an exit (tech-stack.md decision 79).
  - **action steps:** an `action` name from the closed action registry (`orchestrator.actions`),
    dispatched outside the runtime adapter seam rather than to a substrate — no `agent`, no
    `workspace`, no prompt. `onSuccess` / `onFailure` projections work the same as an agent
    step's. Exists for work that has no LLM, no container, and no prompt (gate 1's contract-fix
    step, eventually `deploy`) but still needs sequencing, retries, and an acceptance-style
    result — forcing it through the agent seam would mean lying in `RunSpec`'s required fields.
  - **check steps:** `conditions` (the same predicate compositions as postconditions), an
    optional `pr` matcher for `{pr_url}`, `onSuccess` / `onFailure` projections. No agent, no
    action, no human — the step IS its acceptance gate, and completes when its conditions hold
    on observed state. Exists for work the orchestrator can only watch (CI finishing on a pull
    request): waiting is attempt-free, and only `timeout` consumes an attempt and feeds the
    retry/escalate ladder, because a predicate like `ci_green` cannot tell "red" from "still
    running" and only time can.
  - **previews:** any step except a gate may declare `preview:` — `enabled` and,
    for an agent step, the `services` its own sandbox serves (with `setup`,
    `env` and `stopAfter`), and the `files` it is expected to produce on its own branch, as paths
    or glob patterns. It
    binds to each run of the step a URL — served by the run's sandbox (tech-stack decision 54) or
    published by a producer outside the orchestrator — and each declared file: the worker records
    what each run changed on its branch, and the API serves the declared files inert, a summary
    page as much as a spec (tech-stack decisions 58 and 71). The ticket page shows them beside
    that run's terminal, a tab per entry. The URL or file is metadata about the run and never a
    condition: its absence blocks nothing and says nothing about success, which only
    postconditions decide (§2.4). A file may still carry
    `reviewGate`, which pinned snapshots hold; it is checked as before and changes nothing. So
    may a retired `page`, a summary on a branch of its own: it shows nothing, and the config page
    warns that the summary is a declared file now.
    A runtime-served preview may run beside the run's live session or an open session. It
    closes after `stopAfter` with nobody keeping it, counted from a person's start, the run's
    end or a Keep running, and never while the run is live.
  - **outcome blocks** (`onSuccess`, `onFailure.escalate`, `gate.onApprove`, `gate.onReject`,
    `gate.waiting`, `terminal.actions`) may set `ticketLabel`, `comment`, and `assign` — an optional list that
    *replaces* the issue assignees when that outcome fires, so the ticket always sits with
    whoever must act next. `{ticket_owner}` always refers to the assignee at admission
    (recorded in the state DB), so it stays stable across reassignment.
- `terminal` — final projection (`factorial:done`, close issue).

## 5. Event log

Append-only table/stream; one JSON event per occurrence:

```json
{ "ts": "...", "ticket": 123, "process": "factorial-github", "processVersion": 1,
  "step": "develop", "attempt": 1, "type": "agent_completed",
  "runId": "...", "costUsd": 4.12, "details": { } }
```

Event types (initial): `ticket_admitted`, `step_entered`, `step_started`, `agent_spawned`,
`agent_completed`, `agent_failed`, `postcondition_failed`, `gate_waiting`, `gate_approved`,
`gate_rejected`, `step_completed`, `retry_scheduled`, `escalated`, `ticket_done`.

`step_entered` and `step_started` mark different moments. The worker appends `step_entered`
wherever it assigns `ticket.current_step`, so the log moves when the state machine moves;
`step_started` means the worker created a `step_run` row. A step can enter and never start —
unmet preconditions decline silently, and an infrastructure spawn refusal rolls its own row back —
so a reader that treats the two as one counts too many runs. `step_entered.attempt` gives the
number the next run will carry, predicted before the row exists; an entry onto the terminal step
gives none.

Eleven types serve step previews (§4). `preview_url_updated` records a run's URLs, one per
service, or their withdrawal, and the latest one per run IS that run's preview — there is no
preview table, because the event's `step_run_id` is exactly the identity a URL must stay bound
to. `preview_requested` and `preview_stop_requested` are a reviewer's Start and Stop, recorded
like `interrupt_requested`; `preview_started`, `preview_failed` and `preview_stopped` are the
loop's answers. `step_changes_requested` is a dashboard change request, recorded against the
waiting gate's run; the gate's `gate_rejected` answers it by `change_request_id`, or
`step_changes_refused` does with a reason. `preview_page_observed` is no longer appended: it
recorded where a run's page landed on a branch of its own, before the summary became a declared
file, and the log keeps those (tech-stack decision 71). `run_outputs_observed` records, once per
run of a step that declares `preview.files`, every file the step's attempts changed on the run's
branch; the API selects the declared files from the latest one per run (tech-stack decision 71). `preview_kept` is a person's
Keep running; it moves only when the
preview closes for idle. `preview_stopped` carries a `reason` (`idle`, `renewal` or `continued`),
`preview_requested` can carry `restart` or `renewal`, and a `preview_started` or
`preview_stop_requested` that a rework inherits carries `carried_from`.

`gate_merge_refused` records a merge GitHub declined after a gate's approval: branch protection, a
conflict, or a head that moved. It is a state of the pull request, not a failure, so the gate
keeps waiting, retries the merge every tick, and records each distinct refusal once. A gate
counts an approval only on the commit the pull request's head is at, so an approval never carries
over to commits pushed after it, whether or not the base branch dismisses stale approvals.

`gate_pr_closed{pr_number, head_sha}` records that a waiting gate's pull request closed without a
merge. The gate holds instead of rejecting, and records each closed pull request once per gate
run, with one comment on the ticket (tech-stack.md decision 79).

Sessions (tech-stack.md decision 56) add fifteen types. `agent_idle{reason, idle_since,
question?, unmet?}` marks a session that stopped at its prompt with its postconditions unmet, once
per idle spell, with the leaf reasons in `unmet`; `agent_active{active_at}` marks its next turn,
and re-bases the step deadline;
`agent_finishing{idle_since, sent_at, retry?, silent?, unmet?}` precedes the `finish()` the
postconditions earned, or a silent one (`silent`, with the leaf reasons still `unmet`) for a stop
with nothing asked and nobody engaged, and its one re-send; an earned finish that still has not
ended `terminal.idle_park_after` after that parks idle, and a silent one is cancelled and fails with
`finish_unsettled`. `agent_parked{reason, at_prompt}` is the loop's own park, `idle`, `rollover`
(with `mint_age_seconds`), `ceiling` (with `live_seconds`, the time since the spawn or the latest
dashboard Resume, which `terminal.session_ceiling` bounds) or `exited` (with `unmet`, a session a
person ended from its terminal before its postconditions held), and `at_prompt` says the session
sat at its prompt, always for `idle`; a human's stop stays
`agent_interrupted`, which the pending-request count pairs with `interrupt_requested`.
`retry_requested{requested_by}` is the "retry now" of MCP's `factorial_ticket_retry`, its only
writer, recorded like `interrupt_requested` against the failed run whose
backoff the ticket waits out; the loop counts that run's retry as due, and the next attempt's
`step_started` answers it. The dashboard's Retry writes `step_retry_requested` instead (below).
`agent_resumed{via}` says what released a park — `label`, `dashboard`, `mcp` or `rollover`, and
an older event without `via` is a label release — and a rollover's never re-bases the deadline.
`session_resume_requested{by, has_answer, answer?}` is the dashboard's
Resume, recorded like `interrupt_requested`; an `agent_resumed{request_id}` or
`session_resume_refused{request_id, reason}` answers it. `session_resume_skipped{reason,
previous_step_run_id}` says why a rework or a person's Retry started a fresh session instead of
resuming the previous attempt's: `runtime_changed`, `cli_version_changed`, `past_retention`,
`no_session`, `not_kept` or `no_branch`; an event from before every run was a session may say
`not_interactive`. A stop that a park overtook is answered on release by
`interrupt_overtaken{reason: parked}`. `inspect_requested`, `inspect_started{terminal,
started_at}` and `inspect_parked{reason, failed?}` are an open session's request and the answers
of the inspect pass, which is deployment-wide because no ticket's pass reaches a done ticket;
`inspect_close_requested` is a person's Close session, answered by the park; a
refused request is answered by `inspect_parked{failed: true}`, and no sandbox's own words reach
either answer. `terminal_attached{via}` and `terminal_detached` record viewers, `via` naming the
credential (`cookie` or `ticket`), `terminal_seat_taken{by, from, forced}` a viewer taking the run's
one writer seat, and `terminal_input{by, bytes}` one burst of typing — who and how much, never the
content (§7.14); the latest one holds off an IDLE session's finish and idle park, and any one
makes the run's unmet stop park for a person rather than end and retry. A turn a person
typed is no event: the translated transcript marks it `factorial_origin: human`. A session log that
shrank under the mirror is a stream warning on the run's result. `agent_spawned` records
`minted_at`, and `agent_resumed` records `resumed_at`; a rollover is timed from the later of them,
and a non-rollover `resumed_at` restarts the step deadline. `agent_spawned` also records
`workspace_run_id`, the workspace the keep rule keys on, `cli_pin`, which a rework compares, the
session-log offset the attempt starts at (`log_offset`), in bytes of the CLI's raw log, where on
E2B the run's own transcript already starts, and on a rework that adopts the previous attempt's
workspace, `resumed_from`. `agent_spawned` and every `agent_resumed` record `terminal`, where the
gateway attaches: it reads the latest from the log and
builds no adapter.
`agent_completed` and `agent_failed` carry the run's `session_id` when it has one; a rework
continues the one on `agent_completed`. An `agent_completed` that the parked re-check wrote
carries `while_parked`, the park's reason: the run was accepted from its park, with no resume. A
host session whose tmux server is gone with no finish or stop in flight ends in `agent_failed`
with a `failure_reason` that begins `host_lost`, and the step retries; it is a reason, not an
event type of its own.

`gate_review_ignored` records a review a waiting gate weighed and set aside — an approval of the
head or an unanswered change request whose author is not named, holds too weak a role, or is a
bot — with the reason. The ticket gets one comment beside it. Each review is announced once for
the ticket's life, and never in a poll where the gate decides.

A request names its surface. Each request event records who asked (`requested_by`, or `by` on
`session_resume_requested`) and `requested_via`: `dashboard`, `mcp` or `chat`. From an MCP client
it also records the `mcp_token_id` it came through (§7.16). From the assistant it records the
`chat_proposal_id` its conversation's owner approved, and who asked is that person, never the
model (§7.18). An event without `requested_via` came from the dashboard. `ticket_requeued`,
`config_committed` and `config_head_adopted` record the same fields. `unblock_requested{step,
attempt}` is the label-free twin of removing `factorial:blocked`: `unblocked{via, request_id}`
answers it, or `unblock_refused{request_id, reason}` with `parked`, `stale` or `no_retry_policy`.
`restart_requested{target_process_version_id, target_sha, allow_current_pin, allow_merged}` asks
for a fresh start on the lane's current snapshot: `ticket_restarted{request_id, from_sha, to_sha,
generation, cancelled, abandoned, closed_prs, deleted_branches}` answers it, written before the
`step_entered` it opens, or `restart_refused{request_id, reason}` with `issue_closed`,
`lane_retired`, `merged_outside`, `cancel_failed` or `exited`. A `ticket_restarted` also answers
every re-entry, un-block and resume request asked before it.

The three step controls (tech-stack.md decisions 76-78) are dashboard-only requests of the same
shape. `redo_requested{target_step, has_note, note?, step, attempt, generation}` is a Redo, and
the only place its note is kept: `step_redone{request_id, target_step, from_step, generation,
discarded_steps, superseded_runs, cancelled, ended_parked, closed_prs, deleted_branches,
kept_branches, merged_prs_left, has_note}` answers it, written before the `step_entered` it opens,
or `redo_refused{request_id, target_step, reason}` with `issue_closed`, `exited`, `stale` or
`cancel_failed`. A `step_redone` also answers every Resume, un-block and step retry request asked
before it; a pending completion gets `completion_refused{reason: redone}` and a change request on
a gate it ends `step_changes_refused{reason: redone}`. `step_retry_requested{step, attempt,
next_attempt}` is the dashboard's Retry: the next attempt's `step_started` answers it, or
`step_retry_refused{request_id, reason}` with `stale`, `gate`, `not_escalated`, `off_the_floor`,
`preconditions_unmet` (with `unmet`), `spawn_refused` (the runtime refused; the ticket is blocked)
or `spawn_failed` (the substrate failed, with `detail`; the ticket is as it was); a redo, a
restart or an exit answers it too. `completion_requested{step, attempt, allow_unmet,
seen_failures}` is Force complete: `step_accepted_on_request{request_id, from_status}` answers it
when the postconditions hold, `step_forced{request_id, failures, from_status}` when a person
continued past failures they read, or `completion_refused{request_id, reason, detail, failures,
overridable}` with `stale`, `issue_closed`, `not_forceable`, `mid_turn`, `release_pending`,
`unmet`, `changed`, `unmoved`, `unavailable`, `exited` or `redone`; only `unmet` and `changed` are
overridable. An IDLE or INTERRUPTED run accepted or forced this way also gets an
`agent_completed` carrying `accepted_on_request` or `forced_by`. `next_step_waiting{step,
from_step, forced_by, unmet}` says, once per entry, that the step after a forced one waits on
unmet preconditions or finds no pull request.
`issue_create_requested` and `issue_created{request_id, repo, number, recovered}` bracket an
issue opened through MCP. Neither stores the title or the body (§2.9).

Read models derived from it: **dashboard** (tickets by step, in-flight agents, throughput),
**audit trail** (full per-ticket timeline), **cost per issue** (sum of `costUsd` by ticket/step;
a run still going counts its figure so far, a list-price estimate), **project
dashboard** (spend by outcome, by ticket and by step with the rework rule, the no-merged-change
buckets, the delivery metrics and the callouts — one answer per project, tech-stack decision 61).

## 6. Predicate library

Small, closed set of machine-checkable predicates; conditions compose them with `all` / `any` /
`not`. Templates like `{ticket}` are expanded before evaluation. Branch, PR-head, and file-path
arguments accept `*` globs — needed because agents choose part of the branch name (a slug) at
runtime, e.g. `branch_exists("spec/{ticket}-*")`. Workspace templates use `{slug}` to mark the
agent-chosen part.

| Predicate | Checks |
|---|---|
| `branch_exists(name)` | branch present in repo |
| `file_exists(path, ref)` | file present at ref |
| `pr_open(head, base)` / `pr_merged(head, base)` | PR state |
| `pr_approved(head, base, by?)` | a matching, still-open PR is approved at its current head (optionally by role) — a merged or closed PR's approval is frozen evidence and never satisfies this, and neither does an approval of an earlier commit |
| `pr_changes_requested(head)` | a matching, still-open PR has a login whose latest review requested changes |
| `ci_green(ref)` | all required check runs succeeded |
| `ticket_label(name)` / `ticket_has_assignee()` | issue fields |
| `issue_comment_contains(marker)` | the ticket carries a comment, authored by the App's own bot login, containing `marker` |
| `pr_comment_contains(head, marker)` | a matching PR carries a comment, authored by the App's own bot login, containing `marker`; a dashboard change request the orchestrator posted (§7.8) is a person's words and never counts |

The library is code in the orchestrator; config can only reference it. New predicates are added
by code change, keeping conditions auditable. One acceptance check sits beside the library rather
than in it, because no config can name it: a rework is not accepted while the pull request its
gate rejected is open at the commit `gate_rejected{head_sha}` records (§2.4).

## 7. Security model

Ticket bodies, PR comments, and file contents are untrusted input to agents — the prompt-injection
surface. Containment is **structural**: an agent that is talked into misbehaving must not be *able*
to do damage. The prompt-level rules in the project constitution are defense in depth, never the
control, because an injection defeats prompt rules by definition.

### 7.1 Credentials

1. **Postgres is the only secret store the orchestrator manages.** The `secret` table holds
   ciphertext; values are injected into a run at spawn time and never logged. An agent substrate's
   own store holds only that substrate's bootstrap credential — no GitHub tokens, no model keys —
   with one exception: E2B's egress rules hold a run's model credential, its GitHub token and its
   MCP servers' headers while its sandbox runs, so that the agent never can (tech-stack.md
   decision 25). The E2B key never enters a sandbox.
2. **One GitHub App, one permission profile** (deliberately simplified for now — see the accepted
   cost below): `contents: write`, `pull_requests: write`, `issues: write`, `metadata: read`,
   `checks: read`. The `checks` scope exists for read-only CI observation (`ci_green`); write
   stays forbidden — a forgeable check would let agent output vouch for itself.
   **`workflows: write` is never granted** — see §7.2.
3. **Tokens are minted per run**, scoped to the target repo, and live one hour (GitHub's cap, not
   ours). A session whose token nears its hour parks and resumes with a fresh one, and parks for
   a human at `terminal.session_ceiling`, which nothing the session reports can move and only an
   operator's dashboard Resume restarts (tech-stack.md decision 56).
4. **No deploy-class permissions** until the App Runtime phase.

**Accepted cost of the single profile:** the agent's token can also write labels, assignees, and
comments. So "agents never add labels; the orchestrator owns promotion" is a prompt rule rather than
an enforced one, and a misbehaving agent could move its own ticket's labels. Merging stays
structurally blocked (§7.3), which is the property that actually matters. Splitting into separate
agent and orchestrator profiles is the fix when this stops being acceptable — nothing else changes.
(Reversed by item 5 below, at the validation layer and now at the mint: the profile named here is
the *default* an agent gets when its manifest asks for nothing, not the only profile on offer.)

5. **Amendment (decision S7, 2026-08-20): per-agent permission ceilings.** Item 2's single shared
   profile is deliberately reversed at the config/validation layer: `agent.yaml` may declare
   `permissions:`, a closed scope → `read`|`write` request (`contents`, `issues`,
   `pull_requests`, `metadata`, `checks`, `actions`), and a project may set
   `project.settings.permission_ceiling` to bound what any agent manifest in it may request —
   the same shape and the same trust direction as the MCP allowlist (§7.5 item 1): the manifest
   lives in the repo agents write to, so it only *requests*, and the operator-owned ceiling in
   Postgres is what *permits*. Enforcement lives in `orchestrator.factorial.permission_policy`,
   re-checked on every agent resolution alongside the MCP allowlist, and refuses — never
   clamps — a manifest asking for more than its project's ceiling grants, naming every
   over-request at once. `workflows: write` stays ungrantable at any level (item 2 above; §7.2):
   neither the ceiling nor the manifest can even declare the key, enforced in each pydantic
   model itself, independent of this amendment.

   Back-compatible by construction: an unset `permission_ceiling` permits requests only up to
   the item-2 default profile, and a manifest with no `permissions:` key still requests exactly
   that default — so every manifest and every project configured before this existed keeps
   behaving the same way.

   The mint threads it: `GitHubClient.mint_run_token(repo, permissions)` sends the checked request
   to GitHub, and the worker resolves manifest → request through the *same* function the ceiling
   check used, so the scope that was validated and the scope that is minted cannot diverge. One
   consequence is worth stating plainly, because it decides where a second minter may exist: the
   orchestrator owns the agent's token, and a runtime adapter reads it from
   `RunSpec.secrets["GH_TOKEN"]` rather than minting its own. An adapter may still mint for its
   own mechanics, but never the credential the agent runs with. Two minters would be two answers to "what may this agent
   do", and only one of them would have been checked against the ceiling.

6. **Token facts outrank config intent** (decision S7, 2026-08-20). GitHub narrows a token request
   silently: an installation that was never granted a scope returns `201` with a usable token that
   simply lacks it. So `mint_run_token` reads the `permissions` block off its own reply and
   **fails the mint** unless the granted set *equals* the requested set, naming every difference.
   This is the credential-layer form of the rule that already governs step acceptance (§2.4): what
   the system believes comes from what it can observe, never from what a configuration file, or an
   agent, says should be true.

   **The check is two-directional, and the wider direction is the one item 5 exists for.** Too
   narrow is an availability failure — the run dies on an unattributable 403 deep inside a
   container, an hour after the mistake was made. Too wide is a *security* failure: a project
   whose ceiling is `contents: read`, and a manifest that duly requests `contents: read`, must not
   end up holding a `contents: write` token because the App's installation happened to have one.
   Nothing downstream would notice, and the ceiling would have bought nothing. So a requested
   scope granted above the level asked for, and any granted scope name that was never requested,
   are refusals exactly like a missing one. Note that this is the opposite reading from the
   ceiling check, deliberately: a ceiling *permitting* more than an agent asks for is the normal
   case, a token *carrying* more is the thing being prevented.

   **One carve-out, and exactly one:** `metadata: read`, which GitHub attaches to every
   installation token whether or not it was requested. `metadata: write` is not implicit and is
   refused. If a live mint reveals another scope GitHub adds on its own, it is added to the
   carve-out here and in `check_granted` as a deliberate, dated amendment — never
   absorbed by relaxing the comparison to "extra grants are fine".

7. **Each project brings its own GitHub connection; the deployment holds none** (tech-stack.md
   decision 82). The key, the webhook secret and the token are the project's encrypted `secret`
   rows; the App ID, the bot login and the installations are `project.settings["github"]`. A
   project with no connection is not ready, and the worker holds its tickets without failing
   them. An App connection keeps items 2–6 unchanged. A token connection gives every run of the
   project the stored token as `GH_TOKEN`: it does not expire, its reach is whatever the token
   was made with, item 6's check cannot apply, and "never `workflows: write`" (§7.2) and the
   author-cannot-approve rule (§7.3) hold only if the token was made that way. The person who
   chooses it accepts that, and the page says so beside the choice.

### 7.2 Why `workflows: write` is withheld

Agents push branches to the *same* repository, and `ci_green` is a postcondition — so CI runs on
agent-authored code before any human sees it, and same-repo `pull_request` runs receive repository
secrets. If an agent could write `.github/workflows/`, an injected ticket body would reach arbitrary
code execution in CI, upstream of the human gate that every other guarantee depends on. A GitHub App
cannot modify workflow files without this permission, so withholding it closes the path outright.

### 7.3 What actually prevents a bad merge

- `main` is protected and requires a human approval.
- The App authors every agent PR, and GitHub does not permit approving your own PR — so the App's
  own token can never satisfy that review requirement on a PR it opened. It *can* approve a PR a
  person opened, because agents hold `pull_requests: write`. So a gate never counts the App's own
  review, even when `approvers` names it, and counts another bot's review only when `approvers`
  names that bot (`worker/reviewers.py`).

That pairing, not agent obedience, is why no agent-produced change reaches `main` unreviewed.

### 7.4 Declared secrets are a manifest, not a boundary

An agent manifest's `secrets:` block lists what must be minted for that agent. It is **not** an
isolation mechanism: a substrate may inject every stored secret into every run regardless of what
the manifest declares.
The real limits are what exists in the store at all, and what each minted token is scoped to.

Every declared project secret reaches its run, and the operator chooses how on the Secrets page
(tech-stack decision 75). As an environment variable, the default, the agent reads the real value
and can leak it. As a header, a cloud sandbox's egress proxy sets it on one host and the agent
sees a placeholder.

### 7.5 MCP servers: the repo selects, the operator permits

Agents may be given MCP servers. Each one is a runtime-neutral manifest at `.factorial/mcp/<name>.yaml`
(or `agents/<agent>/mcp/<name>.yaml` for one agent), referenced by name from `agent.yaml`'s `mcp:`
block — the same local/shared split, and the same resolve-at-the-pinned-sha treatment, as `skills:`.

1. **`.factorial/` is not a trust boundary for tools.** It lives in the repo agents open PRs against, so
   an agent can propose granting itself a server. Pinning bounds that (an in-flight ticket keeps its
   sha) but decides no policy. The boundary is `project.settings.mcp_allowlist` — operator-owned
   state in Postgres that no agent can reach. A repo *selects* from it; it never grants. **An empty
   or absent allowlist permits nothing**, which is what every project configured before this
   existed already means.
2. **Matching is exact** — name, transport, URL, and for stdio the command *and* args. A permitted
   name pointing at a different URL, or the same `npx` invocation with different arguments, is a
   different server. Requested tool patterns must be covered by the entry's patterns.
3. **This is L1.** Definitions live in the repo, the operator says which may run. The end state
   (L2) is the shape the predicate library already has — definitions in operator-owned storage,
   config allowed only to reference them — and getting there changes where an allowlist entry comes
   from, not what it means.
4. **Credentials are references, never values.** A header or env value may contain `{secrets.KEY}`
   and nothing else; the key must be declared in the server file's `secrets:` block. The
   orchestrator resolves those keys at spawn *alongside* the agent's own, and the value is rendered
   by the runtime adapter at injection time — so no credential ever enters a `RunSpec`, an event, or
   a log, and there is still exactly one place that unwraps a secret. An unresolvable reference
   fails the spawn; an unrendered `Authorization: Bearer {secrets.X}` must never go out.
5. **`stdio` is the supply-chain hole.** It runs a command inside the container beside a live
   `GH_TOKEN` and model key — and a substrate may inject *every* stored secret there regardless of
   declarations (§7.4). Floating versions (`@latest`, `@main`) are refused at parse time: a floating
   tag changes the code that runs without changing any pinned config. Prefer `http`.
6. **Containment is server-level.** Which servers exist for a run is structural — the adapter writes
   exactly the declared set and tells the substrate to ignore every other source. Narrowing *within*
   a server (`tools.allow`) is emitted where the substrate accepts it and is best-effort: under the
   `claude_cli` adapter's `bypassPermissions` mode it is not a hard gate. Do not describe it as one
   without re-verifying, and let the operator allowlist carry the weight. A codex session never
   loads the checkout's `.codex/` config, because the adapter seeds the checkout `untrusted`
   (tech-stack decision 56), so a repo cannot grant itself an MCP server there.
7. **A server must not hold authority the orchestrator owns.** No postcondition verifies a write to
   an external system, and none of §7.3's protections apply to it. Allowlist read-only tools; a
   server that can merge, label, or deploy is a second, unverified control path.
8. **Failure is loud, both ways.** A `required: true` server that will not connect fails the run
   with a reason, rather than leaving an agent to be blamed for missing tools it was promised. An
   adapter that cannot wire MCP up at all (`supports_mcp = False`) refuses the spawn instead of
   dropping the block — silently discarded config is the failure mode this codebase has already met
   twice.
9. **Factorial's own MCP server is never an agent's MCP server.** `/mcp/` (§7.16) holds the
   operator's requests — exit, restart, un-block, retry, resume, a config commit — so an agent
   that reached it could release its own escalation, retry its own failed run, restart its own
   ticket, or commit config. An
   agent holds no MCP token, but an allowlist entry plus a token stored as a secret would hand it
   one. So the spawn check refuses any `http` or `sse` server whose host and port are one of the
   orchestrator's own, those of the MCP URL and of `auth.external_url` (one host since tech-stack
   decision 74), even when the allowlist names it exactly. It matches host and port, not the
   URL, because a path or case variant defeats item 2's exact URL match. It also refuses a URL
   whose host two parsers could read differently (a percent escape, a backslash, a non-ASCII
   character, a numeric last label), because a client may then reach the host that the check did
   not see. It cannot recognise
   another name or address for the same server (§7.16 cost 9). There is no write-time check and
   no attempt to recognise a token among stored secrets: a shape test is bypassed by wrapping
   the value, and it would read as a boundary it is not.

### 7.6 The blind workspace: a dormant capability, never an imposed process

A repository may ask for an agent that cannot read the tests it is judged against. factorial
provides the seam; it never requires the shape. What steps exist, and whether a held-out test
stage is one of them, comes from the repo's own process file under `.factorial/processes/`.

1. **The reason is correlated error, not distrust.** If one system writes the tests and the code,
   the tests stop being a check and become a description of whatever the code does. Agents sharing
   a model family are closer to one opinion sampled three times than to three opinions, and what
   reaches the reviewer is self-consistent: a spec, matching code, a green check. Measured: 28.6%
   of adjudicated "solved" SWE-bench issues were incorrect, half of those regressive; iterating
   against agent-written tests raises overfitting rather than lowering it. A blind workspace is
   why no automatic fix loop belongs in a process built on this seam.
2. **Blindness is provided by the clone, not by instruction.** `git clone --single-branch --no-tags
   --no-local` means no tag and no branch but its own ever enters the checkout. An instruction not
   to look is a string, one edit from defeat; there must be nothing in the workspace to read.
3. **Where the check runs is the security property.** `assert_isolated` runs host-side, the instant
   the clone finishes and before any agent process exists. An earlier design checked from inside
   the container after the run, and a reviewer showed live that the agent could delete the seeded
   tag and ref with two unprivileged commands and turn a leak into a clean read. A probe from
   inside proves nothing. This is why the adapter calls the gate inside `prepare()` rather than
   exposing it as a second call, and why its test re-runs it on the clone's path instead of
   trusting the function.
4. **File immutability is not execution immutability.** An implementer that cannot touch a test can
   still make it vacuous through fixtures, imports, or an `__eq__` that always returns `True`. That
   is CI's problem, not the workspace's. And a detector that matches a **string** is one edit from
   defeat, a **number** is tunable, a **relationship** ("row count does not grow when data grows
   10×") is not — prefer relationships, and treat string checks as raising the cost of cheating,
   never as proof of honesty.

The seam in code: `Workspace.isolation` / `.hidden` / `.credential` and `supports_blind_workspace`
in `runtime/src/runtime/contract.py`; `assert_isolated` and `assert_no_retained_credential` in
`runtime/src/runtime/workspace.py`; the CLI adapters' blind arm, `prepare_blind_clone` in
`adapters/_cli/checkout.py`; `runtime/tests/test_cli_blind_clone.py`, which runs that clone and
its gates directly, because every adapter refuses `isolation="blind"` at spawn. Proven live
2026-08-14 on the Scion arm and 2026-08-22 on `claude_cli`'s.
The Complex lane's full historical account is in git history at `6a8fbaf`.

### 7.7 The App commits config only on an operator's Commit, from the dashboard or an MCP client

Config lives on a branch named `factorial` in each target repo, not on the default branch
(tech-stack.md decision 53). `POST /projects/{id}/config-branch` seeds it: an orphan commit whose
tree holds only the config folder as it is at the default branch head, or only a README for an
operator starting from a template, recorded as a `config_branch_created` event. An operator protects it with a ruleset — no force-push, no
deletion, no rule that refuses the App's direct commits, and a bypass for the admin role only —
since the App never holds `administration: write`.

`POST /projects/{id}/config-commits` lets the App commit the operator's process file — plus a folder
under `.factorial/agents/` for every agent that config names and the `factorial` branch does not
already have, and the shared skills and MCP servers those agents read, plus any file the `factorial`
branch already has that the operator edited — as one commit directly on `factorial`, and then sync
exactly that commit. The operator's **Commit and sync** click is the approval: the human gate for
config is that dashboard action, not a GitHub merge. An MCP client's `factorial_config_commit` runs
the same path with a model's tool call in place of the click, an accepted cost with its own guard
(§7.17). The commit's parent is the `factorial` head the request read, and the ref moves only as
a fast-forward, so a branch that moved since the page read it answers 409 and nothing changes. A
runtime binding the lane cannot use is a 422 before anything is written.
`POST /projects/{id}/config-preview` answers what such a request would create, reading the
`factorial` branch and writing nothing (both answer 409 until the branch exists); the operator
inspects, selects and may edit any candidate file it returns before it is ever submitted. Six
structural bounds:

1. **Every picked file must be a member of the proposal the server recomputes, in the same request,
   from the parsed config and the repo's own contents.** The client sends the config text and the
   set of files it kept, each with whatever body the operator left it after editing; the server
   parses the text itself, computes the proposal (`config/scaffold/propose.py`) from that parse and
   the observed repo, and refuses any picked path absent from that recomputation or already
   `present_in_repo` (`config/scaffold/assemble.py::assemble`) — the catalog never writes over a
   file the repo has. This replaces an earlier, stronger-looking bound: the writer's signature used
   to take no path argument at all, which stopped holding the moment the operator needed to edit a
   proposed file's body — a writer with no path in its signature cannot express "keep this one, drop
   that one, and here is what I changed" for an arbitrary subset. The bound is exactly as hard as
   before, because the legal set is still derived wholly server-side and never from anything the
   request supplies — a client cannot invent a path into legality by asking for it twice. The
   process file itself is excluded from the submitted set on principle: it is always committed from
   the request's own `content`, never from a body in `files`, so the config that was parsed and used
   to compute the legal set can never diverge from what gets written. Its PATH is derived
   server-side from that same parse — the config's own `name:`, written to
   `.factorial/processes/process-<name>.yaml` — which is this bound applied to one more segment
   rather than a weaker one. That path and an agent's own manifest path
   (`.factorial/agents/<name>/agent.yaml`) are the two places the membership rule does not reach
   either way, since both are built from a name in the parsed config rather than looked up in the
   proposal — `assemble()` validates each name is a single safe path segment before building it,
   closing the seam a config could otherwise use to walk a write outside `.factorial/` (naming an
   agent `../../.github/workflows`, say).
2. **The catalog never rewrites a file the repo already has; only the operator's own edit does.**
   A proposal for a path the `factorial` branch already holds is shown and never written from the
   catalog, so re-running the builder cannot overwrite a prompt a human edited, or a constitution
   they tuned. The operator may still change any file the branch holds, as an *edit*: its body
   starts from the branch's own copy, and the request carries the git blob it started from. The
   server admits an edited path only when it is under `.factorial/` and a member of the branch's
   own listing at the head the request read — membership in a set it recomputes itself, the same
   rule as bound 1 — and only while the branch still holds that blob, so an edit never overwrites
   a change it did not see (409). The lane's own process file is never an edit. A commit carrying
   edits is validated as the whole folder it will produce, not as the bundle alone. This is what
   makes one endpoint safe for onboarding, later config edits and multi-file changes, and why
   there is no second endpoint: a gap opens whenever a step names a new agent, which is an
   edit-time event, not an onboarding one.
3. **Prompts are substituted from the config, or the variant is withheld — never guessed.** The
   factory's prompts name concrete branches, so the lane facts they use are read back out of the
   parsed config — lane-wide for a shared component, per-agent for an agent's own files, since a
   lane whose pull requests all target the base branch reads no prefix at all from a bare
   `pr_open` — and any variant whose markers do not all read is left out of the proposal instead.
   Ambiguity is absence, not a default: a prompt that contradicts its own config produces a pull
   request satisfying no postcondition *while CI reports green*, which is the one failure with no
   mechanical signal.
4. **Selection is per file, not per agent, and there is no `use_templates` permission anymore.**
   The preview proposes every candidate file; the operator ticks which to submit and may edit a
   body first. Untick every file a catalog agent offered and that agent still resolves the way an
   untemplated one always has — the comments-only stub — because the server writes one for any
   referenced agent left with neither a submitted manifest nor one already in the repo. A variant
   contributes candidate files at all only when its lane matches the config's own topology. An
   offered variant whose lane does not match contributes nothing and is reported nowhere: no
   reason is produced (a reason exists only when markers are missing), and the explorer's
   withheld list is built by filtering on `offered`, which this variant still is — so it earns no
   tree node and no row explaining what was skipped. The operator learns such an agent needs
   attention only from the `agent_warnings` of the sync that follows the commit, by which point
   the comments-only stub has already been written; a step whose agent is still that stub fails
   at spawn, so it must be filled in before activating. `test-agent`, which ships only a
   `base-target` variant, hits exactly this path on any stacked lane.
   Selection still cannot conjure a template: an agent name with no catalog entry, whose markers
   do not all read from the config, or whose only matching variant is withheld this way, is
   stubbed regardless of what the operator ticks.
5. **A shared component is offered only because some agent's lane-matching variant declares it —
   but shipping it no longer implies shipping that agent, or the reverse.** `.factorial/skills/<name>/
   SKILL.md` and `.factorial/mcp/<name>.yaml` are proposed at all only when some agent's matching
   variant names them in `skills.shared`/`mcp.shared`, read back out of that manifest rather than
   listed separately — the manifest is what the loader validates and what the runtime resolves at
   the pin, so a name in one and not the other is a 404 an hour into a run. Declaring policy for
   an agent we know nothing about is still the guess bound 3 forbids, so nothing is ever offered
   that no variant declares — except a component the catalog marks `offered_with` an agent,
   offered undeclared wherever the config names that agent: the `product-docs` MCP server as the
   inert `mcp/product-docs.yaml.example` (a live server needs an endpoint, a secret and an
   allowlist entry). What ships, though, is decided by selection alone: the shared
   component and the declaring agent are separate files with independent ticks in the explorer, so
   an operator can select the skill or MCP server while leaving every one of that agent's own
   files unticked — the commit then carries the real, branch-substituted component beside
   that agent's comments-only stub. The skills' own markers join the gate in bound 3 for the same
   reason the prompts' do — the constitution carries the naming table every stage reuses.
6. **The App commits only on an operator's click, and sync takes only commits it can account
   for.** The App writes to `factorial` only from `POST /config-branch`, which seeds it once, and
   `POST /config-commits`, which a signed-in operator's **Commit and sync** calls, with only the
   paths bounds 1 and 2 allow. The ruleset alone
   cannot hold this: agents share the App's identity, so they could push to `factorial` too — the
   ruleset holds no pull-request rule, or the builder could not commit — and an agent chooses its
   own git author, so authorship proves nothing. The **sync guard** (`factorial/registry.py`)
   therefore accepts only a commit the orchestrator recorded (a `config_branch_created` or
   `config_committed` event, written before it moved the ref, or a `config_head_adopted` event,
   written before its sync, for this project), or the merge result of
   a pull request into `factorial` that a human merged onto a commit the guard also accepts —
   otherwise `ConfigCommitRefused`, a 422. The base must be known, or a human merge on top of an
   agent's push would carry the push; a rebase merge of several commits is refused for the same
   reason, so merge or squash.
   A direct push to `factorial`, by an agent or an admin past the ruleset, never syncs unless a
   person adopts it, after reading what it changed. The orchestrator cannot tell a person's push
   from an agent's, so the adoption is a person vouching for exactly the head they read — the
   same principle as the Commit and sync click. The dashboard shows the commits and files
   between the newest accepted commit on the head's first-parent chain and the head (one GitHub
   compare call). `POST /config-branch/adoptions {sha}` then records `config_head_adopted`. It
   needs a Google session and an admitted `Origin`, the shared bearer alone answers 403, and the
   service refuses it over MCP: a model could have read injected text. The `sha` must be the
   head the server reads again, else 409. The event is committed before the sync, and the sync
   runs in a savepoint, so a head that does not parse stays adopted and the builder can commit a
   fix on top. A later direct push needs its own adoption. With no accepted commit beneath the
   head, the preview links the head's commit and a person may vouch for it, when the first-parent
   walk reached the chain's root: a GitLab branch can predate the factory, and another install's
   seed is known only to that install's database (issue #422). Re-seeding instead would lock that
   install out in turn. A walk that stops at its 50-commit limit leaves the
   base unknown, not absent, and that head is never adopted this way. The admin reset below stays the way to
   discard a push. The guard checks every ref and every re-sync of a stored sha the same way, so
   another ref syncs only such a commit. The builder applies the same test to the branch head before it commits, and refuses
   with 409 on a head it does not accept: a commit on top of an agent's push would carry the push
   into a commit the recorded event vouches for. Readiness warns on such a head until a person adopts it or an admin,
   through the ruleset's admin-role bypass, resets the branch to the newest accepted commit below
   it, or deletes and re-seeds it when there is none. Main's protection and gate 2 do not change. The bundle is proven with the production loader first
   (`config/scaffold/validate.py` writes it to a temp directory and calls `load_config_folder`), so
   the App never commits config the orchestrator would reject at sync. Stubs are exempt by
   design — they deliberately do not parse, so validation stands a synthetic manifest in for them
   and never reads the stub itself. A shared skill the repo already has is stood in for the same
   way, and named explicitly by the bundle, so a manifest declaring a skill that exists *nowhere*
   still fails here rather than at spawn.

The stub's cost is paid in §2.8's neighbourhood rather than hidden: `sync` reports an
`agent_warnings` list, and the configuration page's file explorer hangs the same verdict on the
manifest it is about, both treating "absent" and "present but unparseable" as one verdict.
Registering a snapshot pins agent rows *by convention* without reading a manifest, so
before those two, a committed-and-unfilled stub activated green and failed at the first spawn.

The explorer is where that verdict belongs because the process-config section owns the whole
`.factorial/` folder, not the process file alone: `GET /process-versions/{id}/files` lists the
folder at the snapshot's own commit, annotating each file with what it is, whether the `factorial` branch head has
moved it since, and — for a manifest the process names — why it does not resolve. A checklist row
could only say "two agents are broken"; the file itself can say which two. The row it replaced
never blocked an activation, and neither does this: `readiness.blockers` still reads only
`core_items`, so no GitHub outage can refuse one.

Selecting a node on the same graph lists that step's configuration below it, and for an agent
step its manifest too, read at the snapshot's commit by `GET /process-versions/{id}/agents`.

### 7.8 A dashboard change request: a rejection path, not a new authority

Any human gate whose `onReject.rerunStep` is an agent step lets a signed-in dashboard user ask
for changes while it waits on that step's latest run (§4). The API records the request; the worker, under the ticket's
lease, re-checks it and calls the gate's ordinary rejection, so the ticket follows the gate's own
`onReject` exactly as it would for a GitHub review. Three properties bound what this adds:

1. **It can only reject.** Nothing in the path approves, merges, or advances. A request that
   arrives after the pull request merged is refused with a reason, not honoured — a completed
   transition is never undone — and a pending request is handled before an approval landing in
   the same poll, so an approval cannot race past it.
2. **It widens who may reject, not who may approve.** A gate's `approvers` are GitHub logins, and
   a dashboard session carries a Google email with no mapping to one (tech-stack decision 12), so
   the approver filter cannot apply. Any user the dashboard admits may therefore send a gate's
   work back — the same authority Interrupt and Exit already carry. The event records who: the
   session's email, or `api-token` for the shared bearer token, which names no one. The cost of
   misuse is a wasted rework attempt, not an unreviewed merge; §7.3's protected `main` is
   untouched. It used to be offered only where a step's `preview.reviewGate` named the gate;
   every gate with an `onReject` now offers it. That widens where, not what: it still only
   rejects.
3. **Its text reaches an agent unfiltered.** The feedback becomes the rework's `change_request`
   input verbatim. That is a second trusted channel beside the owner-filtered digest
   (`worker/inputs.py`): its author is an operator the dashboard authenticated, not a stranger
   commenting on an issue, which is the population the owner filter exists to exclude. The text
   is stored in the append-only event, so it must never carry a credential.

The rejection that honours a request also posts its text verbatim to the rejected pull request,
under the App's login, with the requester's email, so the pull request keeps what sent its work
back for review and audit. Anyone who can read the repository reads both, which is one more
reason the text must never carry a credential. Posted under the App's login, it would read as
delivery evidence to `pr_comment_contains` (§6); the comment opens with
`CHANGE_REQUEST_MARKER` and that predicate skips it. It never reaches an agent twice: the
owner-filtered digest refuses the App's own comments.

One unanswered request per gate run, and a client-chosen idempotency key, mean no number of
submissions starts more than one attempt.

### 7.9 A framed preview: agent-written pages beside the dashboard

The ticket page frames a run's preview next to its terminal. The frame is safe only because
the page behind it is on another origin: the API refuses a URL on the dashboard's own origin
(`events/previews.py`), so agent-written JavaScript never runs with the dashboard's cookies. The
frame allows scripts, same-origin and forms, and nothing else: no top navigation, no popups, no
modal dialogs.

Framing has two costs, accepted for the reviewer's convenience:

1. **Trusted chrome.** An agent-written page is drawn inside the dashboard's panel. A
   prompt-injected agent can draw a fake sign-in form there, and the reviewer has no address bar
   to check. Never type a credential into a preview.
2. **A login inside the frame can fail.** The preview is cross-site to the dashboard, so a browser
   that blocks third-party cookies can break the app's own sign-in inside the frame. Open in new
   tab still works.

### 7.10 Accepted cost: a preview's sandbox is public for its whole life

E2B decides public traffic when it creates a sandbox, and a browser cannot send its traffic
token, so a run whose step declares preview `services` gets a sandbox whose every listening port
is public from the start (tech-stack decision 54). The URL's unguessable part is the sandbox id.
The sandbox's firewall narrows that to the ports the preview declares, from the spawn: an
agent's own server on any other port is closed to the public. Port 49983 is the sandbox's
control port, and a preview may not declare it.

A preview may run beside a credential rule. It runs beside the run's live session, beside an
open session, and beside the end of a run. E2B's egress proxy adds the run's model key and
GitHub token to the requests the agent makes (§7.1). The agent cannot read them, so a public
port cannot disclose them. It can serve a relay, though. A prompt-injected agent writes a
server that forwards requests to the model API or GitHub. Then anyone with the link spends the
key, or acts as the run's token, until the run ends. Four rules contain the preview's own
processes, so that they never reach the proxy that adds the credentials.

- **The preview's services run as a uid of their own.** The agent cannot read that uid's
  environment, and that uid cannot read the agent's.
- **A root firewall table binds that uid first.** The table keeps the uid to loopback, and there
  to the declared ports. The uid cannot start a connection off loopback, cannot reach a port
  that is not declared, and can send nothing but TCP, though its servers still answer. At the
  spawn the sandbox is created with its credential rules, and the table loads before the
  agent's CLI or any preview process starts; a failed load ends the spawn. At every resume,
  rework and session view the table loads before any credential header rule is written, and a
  failed load stops the write. The load cuts every connection on a declared port, so a browser
  request in flight is dropped once and the browser retries.
- **The table opens only when no credential is left.** It opens after the last header rule is
  dropped and the credential files are scrubbed. A run that ends beside a serving preview
  keeps the sandbox awake for it: the end scrubs the credentials, drops the rules and then
  opens the firewall. While no credential rule is live, the uid has its normal egress back, so
  an app with an outside database works.
- **No `.git` is writable by the preview uid.** The uid can read the checkout, `.git` included.
  It cannot write, replace or rename any `.git`: the regrouping leaves every `.git` out of the
  shared group, and the checkout root is sticky. So it cannot plant a hook or a config that
  the agent's git would run, and the runtime's own git calls as the agent run with hooks and
  fsmonitor off. No credential is stored in `.git`, because the egress proxy injects the
  header. So read access shows the uid nothing it could relay.

`setup` runs as the agent, without `preview.env`. So preview values reach only the services'
uid. `preview.env` may reference only secrets named `PREVIEW_*`, and every park deletes it from
the sandbox. A service counts as ready only when its own uid listens on the port. A server the
agent left on a declared port therefore fails the preview instead of being published. A codex
ChatGPT login is a file in the sandbox until the park. Once the run is parked, the sandbox
holds no credential rule, so a preview at the gate has nothing to relay.

**Remaining accepted cost:** a live, prompt-injected agent can relay its own credentials to
anyone holding the preview link through a channel it shares with the preview, the checkout.
That needs an agent that actively relays, and it ends at the session's park. It is narrower
than the cost this section accepted before the firewall, when the agent could serve a relay on
any public port.

The public sandbox is accepted for steps that declare `services`, and only for them, and only
on their declared ports: every other sandbox is created closed. Revisit if E2B makes public
access per port, or changeable after creation.

Two residuals stay, and neither reaches past what the agent's own uid already holds. A filter
the agent writes into its own `.git` config still runs as the agent when root reads the
checkout's git status, as the rework script's header accepts; hooks and fsmonitor are off there,
as in an open session (§7.14). And the services share the
checkout with the agent, so preview code can edit files the agent's next turn runs (scripts,
tests, `package.json`); a shared checkout cannot prevent that.

### 7.11 Embedded images: fetched by the orchestrator, delivered as bytes

An image in an input's text is part of what the agent must do, so the run gets the image, not
only its link (tech-stack decision 55). Four rules keep the fetch from widening what an agent can
reach.

- **Trust follows the text.** An image counts only when its link is in the final text of an input
  the agent receives, and in an item that passed that input's fences: the owner filter, the
  approver anchor, the bot fence and the digest cap. A third party's image never reaches a
  spawned run. A resumed run gets the images in its steering comments, which pass the bot fence
  only (tech-stack decision 26), the same as their text.
- **Only GitHub's own hosts.** Downloads go to the hosts that GitHub wrote into `body_html`:
  `private-user-images`, `user-images` and `camo.githubusercontent.com`. Redirects are not
  followed. A URL that an author typed is a lookup key and is never fetched.
- **The signed URL is a credential.** It lives 300 s and needs no header, so it never goes into a
  `RunSpec`, a prompt, an event, a log line or an exception message. Downloads send no
  Authorization header, because S3 echoes a forwarded one back in its error body.
- **GitHub keeps the repository boundary.** GitHub signs an asset only when the item it is in
  belongs to the asset's own repository, even for the installation-wide token. An image pasted
  from another private repository stays unsigned, and the prompt names it as not accessible.

Image bytes never go into Postgres: `agent_spawned` and `agent_resumed` record each image's origin,
size, type and sha256. A host adapter deletes a run's `attachments/` folder at the run's end, and
an interrupted run's when the reaper removes its workspace.

**Accepted cost: the CLI session store keeps a copy.** The CLI that a host adapter runs records
its session in the run's own config home, `runs/<run_id>/config/` under the adapter's
`local_root`, and that record holds every image the run was given. Only the reaper removes it,
with the rest of the kept workspace (tech-stack decision 54), so a screenshot from a ticket stays
on the orchestrator's host for as long as the run is kept (in `/data/runs` in the local image,
decision 52). Revisit if a ticket's images need retention rules of their own, or if
the run area is kept, backed up or shared beyond that host.

### 7.12 Who may decide a gate

A gate counts a review only from a reviewer it qualifies: one named in `approvers`, or one that
GitHub reports as holding the gate's `approverPermission` or a stronger role. One resolver
(`worker/reviewers.py`) decides this for the gate's approvals, its change requests and the review
text its rework receives, so the three cannot disagree.

- **The role is GitHub's answer, read on every poll.** The orchestrator asks
  `GET /repos/{owner}/{repo}/collaborators/{login}/permission` for each reviewer whose review
  matters (an approval of the head, or an unanswered change request) and who is not named, and
  keeps the answer for that poll only. Removing someone's access stops their reviews counting on
  the next poll. The call needs only `metadata: read`. A 404 is no access; any other failure
  leaves the gate waiting and never reads as "no access".
- **An empty `approvers` means anyone only when `approverPermission` is unset.** With the key set
  and nobody named, a review counts only from someone who holds the role.
- **Bots never decide on their own standing** (§7.3). The App's review never counts, even when
  named. Another bot counts only when `approvers` names it: never through an empty list or
  through a role.
- **A review that does not count is said out loud.** While the gate waits, each review it set
  aside gets one `gate_review_ignored` event and one comment on the ticket naming the reason, so
  a reviewer who is not allowed to decide learns it from the ticket rather than from silence.
- **An agent can grant neither.** The key lives on the guarded `factorial` branch (§7.7) and is
  pinned at admission. Who holds the role is GitHub's answer, not config.
- **Review text widens with the rule.** A rejecting gate's `onReject.inputs` carries review text
  from every reviewer it qualifies, named or by role, and none from an open gate. Writers can
  already push code the agent reads and CI runs, so this grants them nothing new, but the group
  whose words reach an agent is larger.
- **Accepted cost: approving your own push.** A writer can push to a pull request and then approve
  it. Owner-only gates had the same gap. GitHub's "require approval of the most recent reviewable
  push" covers protected branches; a gate could mirror it later.
- **A code-owner rule can still refuse the merge.** A writer who is not a code owner can pass a
  gate whose merge GitHub then refuses; the gate waits and comments (§4). The configuration page's
  `gate_approvers` row warns about this combination before a ticket meets it.

### 7.13 Host git never honours repository config an agent can write

The CLI adapters keep one clone per repo under `local_root/repos/` and one worktree per run under
`local_root/runs/`. Every `.git` there is agent input: codex's sandbox leaves each clone's `.git`
writable, because a commit writes there (tech-stack.md decision 34), and `claude_cli`'s agent runs
unsandboxed (decision 16). That is the shared clone's config, hooks and `info/attributes`, and
each sibling run's `.git/worktrees/<id>/`. The worker's own git runs outside any sandbox, with
the run's token in its environment, and git runs whatever a repository's config names — a
credential helper, an fsmonitor, a hook, a filter driver. `runtime/hostgit.py` is where the rule
lives, and every host-side git goes through it (`Cli.git`, the blindness gates).

1. **Command scope switches off what has one switch.** Every host git runs with the credential
   helper list emptied before the adapter's own token helper is added, `core.fsmonitor=false`,
   `core.hooksPath=/dev/null`, no askpass program, and no system or global config. https is the
   only transport, and every other one is refused by name as well as by `protocol.allow`,
   because a repository's own `protocol.<name>.allow` beats `protocol.allow` from any scope.
   Command scope applies after every config file, so a planted key cannot win. Host git that
   never leaves the machine (`rev-parse`, `worktree add`, a spawn's deletion of stale branches,
   a park's HEAD detach, a rework's stash and checkout) runs without the token at all.
   `core.hooksPath` does not reach a
   config-defined hook (`hook.<name>.*`, in git 2.54 but not in the image's 2.47.3): item 2
   refuses those.
2. **A content check refuses what has no switch.** A filter or diff driver, or a config-defined
   hook, can take any name, and an include, an `http.*` key (a proxy with TLS checks off is
   handed the token) or a `url.*` rewrite has no override. Before host git uses a clone, its
   config, every `config.worktree`, `info/attributes` and `objects/info/alternates` are read as
   data against an allowlist: what `git clone` and an agent's ordinary git and hook managers
   write, keys item 1 pins whatever the file says (so `husky`'s `core.hooksPath` is harmless),
   and keys that name no program for a host git that never commits. `.git/hooks/` is not
   checked: `core.hooksPath=/dev/null` disables it with no window. The check runs again right
   before every git call that checks files out or carries the token into an existing
   repository — including the fetch right after a replacement, since the agent that forced the
   replacement can put a repository of its own at that path meanwhile — and before any host git
   on a kept worktree (a park's HEAD detach, a rework's fetch, stash and checkout) or that
   deletes a branch of the shared clone, since a config-defined hook runs even on git that
   never leaves the machine. A refused shared clone
   is renamed aside and cloned again with `--no-checkout`, because a checkout at the end of a
   long transfer would read config written during it; the old one is deleted only after the
   new clone or fetch. Runs still working in the old clone lose it, which is right, because
   their own git reads the same config. A refused run repository makes that run's probes ask
   GitHub alone, as on E2B.
3. **The remote is a URL, never a name.** Fetches and `ls-remote` address
   `https://github.com/<repo>.git`, built from `workspace.repo`. `origin` resolves through config
   the agent writes, and the token goes wherever it points.
4. **A path read back from the run area leads into the run's own clone, or is not followed.** A
   worktree's `.git` pointer, its `commondir` and the state file's `git_dir` must resolve into
   `repos/<repo>/.git/worktrees/`. git takes a linked worktree's common directory, and with it
   the config, from that directory's `commondir`, so item 2's check refuses a clone with an
   entry there that is not a directory or whose `commondir` leads anywhere but its `.git`, and
   every host git that names an administrative directory with `--git-dir` pins
   `GIT_COMMON_DIR` to the clone's `.git`, which git honours over the file: a redirect written
   after the check is not followed either. Removal runs no git: it deletes the run's own
   `runs/<id>/worktree` and the administrative directory the clone records for it, checked
   again to be one of the clone's entries right before the delete, never
   through `worktree remove`, which deletes whatever path a worktree's `gitdir` file names, nor
   `worktree prune`, which takes every kept worktree without a `.git` pointer for orphaned
   (tech-stack.md decision 54). A sibling's tampered `HEAD` or `gitdir` is data to host git: it
   can fail a fetch, or make a removal miss or take that sibling's administrative directory,
   and nothing more.

**Residual, accepted:** item 2 is a check, then a git call. An agent in a concurrent run can write
between the two; item 1 holds through that window, item 2 does not. A fresh `git clone` has no
check before it at all, because there is nothing yet to check: for the milliseconds between git
writing the new `.git/config` and its transport reading `http.*`, a key written there applies. So a filter driver, a
config-defined hook, an `http.*` key or a `url.*` rewrite written in that window can still win,
and each git release that adds a config key naming a program widens what only item 2 covers —
re-read this list when the image's git is upgraded. The shared clone is also shared state that
one run's agent can leave refs and objects in for the next run to start from — integrity rather
than execution, and outside this rule. A clone per run that the agent cannot write fixes both;
this is the mitigation until then. `claude_cli` gets the same hardening, though its agent
already runs on the host unsandboxed (tech-stack.md decision 16).

### 7.14 The terminal: a relay to a session, and a shell on a host runtime

Every agent run is a session (tech-stack decision 56): a person can watch and type into the
agent's own TUI, and any kept run, on either substrate, can be reopened as a read-only session
(decision 57). The deployment switch `FACTORIAL_TERMINAL__ENABLED` gates both. The terminal
gateway relays bytes and changes no run state; the worker still owns the run's life, and the
predicates still decide its outcome.

**Accepted cost: write access on a host runtime is a shell as `factorial`.** Any member of the
run's organization whose role holds `tickets:terminal` (§7.19), or the API token, can take the
terminal's writer seat, and the
TUI's `!` mode runs a shell command as the user that runs the worker, claude's and codex's alike
(codex runs it outside its own sandbox), which can reach the database credentials and the master
key (tech-stack decision 52). The role decides who can hold this shell. An
open session is no such shell: it is the agent's own CLI, read-only at the
gateway, with the model credential alone. Because tmux and the agent share that uid, the agent
can read its own session and every other live host run's, and type into them, past the gateway's
audit (the next cost). The dashboard's own authority — Interrupt, Exit, Request changes — never
reached that far. Two things bound it, and neither is containment:

- **Every viewer attaches read-only**, on a read-only tmux client, so a stray key never reaches
  the agent. A run has one writer seat: a viewer takes it on purpose, takes it from its holder
  only by forcing, and it frees after two minutes without typing. Only the holder's client is
  re-attached writable.
- **Every input burst is audited.** `terminal_input{by, bytes}` records the session's email, or
  `api-token` for a terminal ticket the token minted, and a byte count, never the content, which
  can hold a secret. `terminal_seat_taken{by, from, forced}` records who took the seat, and from
  whom; a forced taking records the old holder's pending burst under it first. **Accepted gap:** a
  claim cut short by its own viewer's teardown between taking the seat and recording it leaves
  that displacement unrecorded; no key of that viewer's ever passed.

**Accepted cost: on a host runtime the agent's user reaches every session.** The agent, a
person's `!` command and an open session all run as `factorial`, so each can:

- **Drive any live session.** tmux on any run's socket under `<local_root>/t` types into its TUI,
  reads or copies its scrollback, or kills its server. No `terminal_input` records it, and a
  forged prompt hook or keystroke moves the session's last input, which holds off a finish or an
  idle park; a forged `last_input_at` never engages the session, so its silent stop still ends
  and retries, and the predicates still decide the outcome.
- **Read every run's keys.** Another run's environment through `/proc` holds its GitHub token and
  model key; `/app/.env`, which the worker reads as the same user, holds the master key; and
  every run's config dir is readable.
- **Keep a run from ending.** Holding a run's `supervise.lock`, or putting a link in its place,
  keeps every supervisor from reading the session. Killing a server and starting a look-alike
  launch with the run's session id, or for codex its turn file, passes the check a worker makes
  when it takes a session back.
- **Race the run's files.** The adapter replaces the last component of each path it writes, never
  writing through a link there, but follows links in the directories above it: `turns/`,
  `<local_root>/bin`, `<local_root>/t`, a `config/` swapped for a link, and the state file's
  temporary copy. A `config/` that resolves outside `local_root` keeps its credential files past
  the run's end, the one property lost. The translated mirror's appends follow a link, and the
  launch env file can be rewritten between its write and the pane's read.
- **Outlive the run.** At each end the adapter ends every process of that user whose environment
  holds the run's `CLAUDE_CONFIG_DIR` or `CODEX_HOME` (tech-stack decisions 16 and 34). A process
  that scrubs its environment escapes, and so does an Apple binary's job on a macOS host, whose
  environment macOS hides.
- **Run code in the adapter's git, in a window.** Host git honours no repository config an
  agent can write (§7.13), but a key written between its check and the git call still wins,
  §7.13's accepted residual.

None of it reaches past what a host agent already holds as `factorial` (decision 16); tmux only
makes typing into another run easy. Containing it needs the TUI to run as a user apart from the
worker's, the revisit trigger below.

**Accepted cost: the bearer token is operator-equivalent here too.** Sessions and terminal tickets
are signed with keys derived from `FACTORIAL_API_TOKEN`, so whoever holds the token can mint an
`sf_session` for any email as well as a ticket. A ticket the token mints is recorded as
`api-token`, which names no one: refusing the token would buy no attribution, since it can mint a
session under any name, and an honest `api-token` beats a forgeable email. So `terminal_input.by`
is only as trustworthy as the token's secrecy. Rotating the token logs every session out and voids
every ticket, and the gateway is a process of its own, so it must be restarted with the API to stop
honouring credentials signed by the old one.

On E2B the session is the agent's uid in a microVM that holds no key (§7.1), but for one: a
`codex_e2b` run on a ChatGPT subscription holds its login as a file there until the park (§7.10).
**Accepted cost:** a person who types into that session can have the agent print the login, which
the agent can already read, and a login printed into the TUI lands in the session log's mirror
and the dashboard's transcript, which outlast the park: the same exposure as a prompt-injected
run's (§7.10). An open session opens only after the park deletes it. Revisit
if the dashboard admits people who should not hold a host shell, or when a host runtime can run its
TUI as a user apart from the worker's.

**What holds the rest:**

- **The handshake is checked by hand.** Starlette's CORS middleware skips WebSocket scopes, so the
  route compares `Origin` with the dashboard's origins (`FACTORIAL_AUTH__EXTERNAL_URL` and
  `FACTORIAL_CORS_ORIGINS`) before it accepts, which closes cross-site WebSocket hijacking; a
  missing `Origin` is refused too, a ticket's as much as a cookie's. It then authenticates by the
  `sf_session` cookie, which names the person each input burst is charged to, or else by a
  terminal ticket offered as `Sec-WebSocket-Protocol: factorial.terminal, <ticket>`, never in the
  URL, which access logs keep. A ticket is signed with a key of its own, so a session cookie is no
  ticket and a ticket no cookie; it admits one viewer of one run for 30 seconds,
  once, its nonce spent after every other check. A bare bearer token is refused. A page on another
  origin can mint none it can read, because CORS keeps the answer from it, and can use none,
  because the Origin is checked first. The run must be an agent run that is running or idle, or hold an
  open session, and its ticket must not be quarantined. The gateway checks all of it again
  every 5 seconds, and closes a viewer whose run parked or whose session expired, the viewer's own
  or the one that minted its ticket.
- **On a host, another user cannot reach a session.** Its socket directory, `<local_root>/t`, is
  `factorial`'s and 0700, so another uid (Postgres's in the image) cannot connect. Every server
  loads the same lockdown conf and starts with only PATH, LANG and HOME. The secrets reach the CLI
  through 0600 files, never argv, so `ps` shows none: the env file, which `launch.sh` deletes
  before it execs the CLI, and the MCP config with rendered values (claude's `mcp.json`, codex's
  `config.toml`); every end deletes both, and claude's `.claude.json` or codex's `auth.json`.
- **On E2B the agent cannot reach its own terminal.** The tmux server is root's, its socket
  `/opt/factorial/tmux/s` sits in a root-owned directory of mode 0700, and the agent runs as its
  own uid, so it can neither read its scrollback nor type into it. The server loads only a
  root-owned lockdown conf (`/opt/factorial/tmux.conf`, mode 0644) that binds no key, so a
  writer's keys reach the pane and never tmux's own commands, which would run as root. The
  hooks' signals file is the agent's (0600) inside root's run directory, so the agent can write
  it but not swap it for a link; the launch's argv and environment files are root's (0600).
- **A forged signal cannot keep a session alive.** On either substrate the agent can write the
  hook lines that end a turn and submit a prompt. Each pair restarts the active-time step
  deadline and holds off a finish and an idle park, and the rollover mints a fresh token every
  hour. The wall-clock ceiling bounds that: `terminal.session_ceiling` after the spawn or the
  latest dashboard Resume, counted from the loop's own log, the session parks for a human and is
  minted no token past it. Only a dashboard Resume releases that park: any App token in the
  repository can remove a label, so a label release is refused, and the MCP server's resume
  refuses it too, because a model holding an operator's token could press it at every park
  (tech-stack.md decision 56, §7.16). The
  ceiling bounds a session, not a ticket: such a token can also exit and re-enter a ticket, or
  unblock it, which starts a fresh attempt with a fresh window and mint; that is §7.1's accepted
  label cost.
- **A kept workspace holds no credential, on either substrate.** Every run's end deletes its
  credential files — `_park` on E2B, which also drops the header rules, and the host adapter's
  scrub on a host runtime (decision 54). An open session gets the model
  credential alone, no GitHub token and no MCP server, so it can neither push nor mint. A rework
  that resumes in a kept workspace ends any open session before it writes fresh credentials; an
  awake preview keeps serving, contained by the firewall first (§7.10).
- **An open session is read-only and leaves the session as it found it.** The gateway attaches every
  viewer read-only and nothing is typed into it but the Enter that takes a CLI's consent screen. Its
  end cuts the session log back to its length at the launch, so what the view's CLI wrote there is
  never read as the session's. It loads the user's settings alone, so a hook the checkout ships
  never runs in it beside the model key. Its git runs with hooks and fsmonitor off, set in its
  environment above every config file, so no hook or fsmonitor the agent wrote into the
  checkout's git config runs there. A filter driver the agent wrote can still run when that git
  reads a file the driver filters. This is an accepted residual: an agent must have planted it
  while it ran. It may open beside an awake preview. A preview's services
  run as a uid of their own, and the firewall binds that uid before the session's credential is
  written (§7.10), so a session cannot read their environment and they cannot reach the proxy.
- **A viewer never wakes a sandbox.** The gateway never calls E2B's connect, which resumes a paused
  sandbox and sets its timeout. It reads the sandbox with `GET /sandboxes/{id}`, attaches only when
  that says running, and builds its envd handle from the same read, so a pty to a sandbox paused
  since fails. Only the worker wakes one.
- **Nothing on screen is stored.** No raw pty log is written, so a secret a person types or the
  agent prints reaches no file.
- **Turn caps and cost are forgeable on every run.** They come from the CLI's session
  log, which the agent can write, so they are accepted as reported; a log that shrinks under the
  mirror is flagged as a stream warning on the run's result. The figure shown while the session
  runs is an estimate from that log: a forged line moves only it, never below zero, and never
  past what the row's columns hold.

### 7.15 A served page: inert, opaque-origin, behind a signed link

A file a step declares (`preview.files`, tech-stack decisions 58 and 71), a summary page as much
as a spec, is one an agent wrote and committed on the step's branch: HTML served as it is,
Markdown the API renders to HTML, or any other file shown as plain text. The route serves a blob
only when the worker recorded it for that run and the pinned config selects it, so a signed link
cannot read another file of the repository. The API serves the files from its own origin, so the
response itself must make them inert, whatever their type:

- **No script.** The CSP `sandbox` directive omits `allow-scripts`, and so does the frame. The
  page also gets an opaque origin, in the frame and in a new tab, so it holds no dashboard
  cookie or storage.
- **No network.** `default-src 'none'`, with images and fonts only as `data:` URIs, means the
  page loads nothing. `form-action 'none'` means it submits nothing. With no script, nothing
  can read what a reader types, so a fake sign-in form goes nowhere.
- **No execution.** HTML and plain text are the blob's exact bytes. Markdown is the one
  transform: a parser turns its text into escaped HTML inside a fixed shell, and nothing the
  agent wrote runs, on the server or in the frame.
- **The type is the server's.** The media type comes from the renderer table, and is
  `text/plain` for any type without a renderer. It is never guessed from the extension or taken
  from GitHub, so a file cannot choose the document it becomes.

Three costs are accepted:

1. **Links open in a new tab.** The frame and the CSP allow popups, a departure from §7.9's
   "no popups". A summary must link to its pull request and its files. A new tab has an
   address bar, and without script every link is fixed in the markup, like a link in a pull
   request description.
2. **A link the reader clicks can navigate the frame.** This applies to an HTML page: the
   Markdown renderer gives every link `target="_blank"`. The CSP has no navigation control, so a
   link without `target="_blank"` moves the frame to another site, and that site then shows
   inside the dashboard's panel. The page cannot move the frame by itself: without
   `allow-scripts`, the sandbox also blocks a `<meta http-equiv="refresh">`. This is the
   trusted-chrome cost that §7.9 accepts for a preview, and it has the same rule: never type a
   credential into a page. The reviewer has no address bar there, and New tab gives
   one. The frame's own sandbox still applies to the site it lands on, so that site runs no
   script and submits no form.
3. **A copied link reads nothing without a session.** A frame cannot send a bearer token, so the
   guarded API signs a link for one run and one blob, good for at most two hours. The route also
   requires the session cookie, which the frame's request and a new tab do send; the opaque
   origin keeps it from the page itself. Every dashboard reader has a session, because nobody
   signs in with the API token (§7.19). A browser that opens a copied link without one is sent
   to the dashboard's sign-in screen, which returns it to the link. At every open the route also
   reads the reader's membership of the run's organization, because a link travels and outlives
   a removal: a person who is not a member gets 404. The page sends no referrer and loads no
   subresource, so it does not leak the link itself.

Revisit if pages need script: that needs a separate content origin, not a looser CSP. Revisit
too before a renderer emits a type that can carry active content other than HTML, such as SVG,
XML or PDF: this envelope was argued for HTML and plain text, and an image such as a PNG holds
none.

### 7.16 The operator MCP server and its token

`/mcp/` (tech-stack decision 60) gives an operator's Claude Code or Codex session the dashboard's
reads and a subset of its requests. It adds no authority. A write tool appends the event that the
dashboard's button would append, with `requested_via: "mcp"` and the token's id beside who asked
(§5). The worker still makes every change to a ticket, with the same tick, the same projection
and the same predicates. The only new GitHub write is opening an issue as the App
(`factorial_ticket_create`). The config commit already existed (§7.7), and §7.17 records what it
costs to call it from a model.

1. **The row is the authority.** A JWT's signature only finds its `mcp_token` row. The row
   decides the principal, the scopes, the expiry and revocation on every request, with no cache,
   so a revocation takes effect on the next call. The token carries `iss`, `aud`, `jti`, `iat` and
   `exp` and nothing else (no `sub`, no `scope`), so no reader can be tempted to trust a claim.
2. **The jti is stored as a sha256.** It is shown once, inside the token, and never listed or
   logged. So a database read plus the API token cannot re-sign anyone's token.
3. **Two scopes.** `factorial:read` is required for every request, resources and prompts
   included. `factorial:write` adds the write tools, which a read token's `tools/list` does not
   show, and each write tool checks the scope again itself, one line kept as a second lock. A row
   with `chat` access is a history of the assistant's former mount: `/mcp/` refuses it.
4. **Minting needs a recent sign-in.** A session must have signed in within the last 24 hours.
   When a request carries both the session cookie and the API bearer, the session wins, so
   sending the bearer as well cannot skip the freshness check or the membership check. A token
   minted with the shared API token is recorded as `api-token`. Mint and revoke refuse a missing
   or foreign `Origin` and answer `no-store`.
5. **Revocation.** A person lists and revokes the tokens they minted. Seeing and revoking
   anyone's needs `mcp_tokens:manage` (§7.19); another person's token answers 404. Rotating `FACTORIAL_API_TOKEN` voids every token and every session,
   because the signing key and the audience both come from it. Removing a member stops
   their tokens at the next call. Sign out everywhere and setting a password revoke every token
   the account's sessions minted (§7.19 item 14). Removing a domain from `auth.allowed_domains` does not: the
   list only admits new people (§7.19).
6. **Never a tool:** gate approval or merge (§7.3, §7.12), secrets (§7.4), permits and
   allowlists (§7.5), runtimes and their credentials, the terminal (§7.14), labels (§2.2),
   minting, adopting a direct push to `factorial` (§7.7), and Redo, a continuing Retry and Force
   complete (tech-stack.md decisions 76-78).
7. **One organization per token.** The row names the organization the token was minted in, and
   every tool call resolves within it: a project, ticket or run elsewhere is not found, and a
   list draws from that organization alone (§2.11). An assistant turn's tools resolve within
   the conversation's organization the same way.
8. **Never given to an agent** (§7.5 item 9). The dashboard's setup snippets keep the token out
   of every file and out of shell history, in a `FACTORIAL_`-prefixed variable. The host
   adapters strip that prefix from an agent's environment (`runtime/hostgit.py`). A host-runtime
   agent has a `HOME` of its run's own, but it runs as the operator's user and can read the
   operator's files by absolute path, so a machine that runs host adapters must not hold a token
   at rest.

Nine costs are accepted:

1. **The API token can sign MCP tokens offline**, because the key is derived from it. The row
   lookup is what refuses a forgery, so it runs on every request and is never an optimisation
   to skip. A holder of the API token can already do everything, so forging gains them only
   false attribution.
2. **A token lives a year and outlives its session and its holder's employment.** Revocation is
   manual. This breaks decision 57's pattern, where a terminal ticket lapses with its session.
   The server is on the internet (item 7), so a leaked token works from anywhere until someone
   revokes it.
3. **Untrusted text reaches a model that holds write tools.** Issue text, agent output and repo
   config come back from read tools. The server marks them as data, and it annotates every write
   tool as not read-only, so a client asks before it calls one. The client's approval setting is
   the only human step, and the server cannot see it.
4. **An issue opened through MCP is authored by the App.** The footer, which names the
   requester in a code span, and the `issue_create_requested` event are the record. The footer
   also publishes the requester's email on a public repo. The named assignee becomes
   `owner_at_admission`, a gate approver whose comments are trusted input. The issue body is
   agent input with no author filter (`worker/inputs.py`).
5. **A ceiling park stays a person's to lift.** `factorial_ticket_resume` refuses a session
   parked at its wall-clock ceiling. An MCP token is an operator credential, but a model under
   prompt injection could press Resume at every park, and then the ceiling would no longer bound
   the session's spend (decision 56). `_live_since` still counts only a dashboard Resume, so an
   MCP resume that got past the service would park again on the next tick.
6. **An XSS in the dashboard can mint a persistent credential.** The `Origin` check stops a
   cross-site request, not script running on the dashboard's own origin, so `cors_origins` must
   stay dashboard-only. `/factorial/mcp/` shares that origin now, and fastmcp's Origin guard,
   given no `allowed_origins`, admits a same-origin request, so the dashboard's own page passes
   it where it used to get 403. The bearer is still required there: the session cookie and the
   API bearer get 401 at MCP (`test_mcp_mount.py`), so such script needs the token it can mint,
   and a minted token already worked from anywhere; the admission adds nothing. A served page
   (§7.15) has an opaque origin, `Origin: null`, which the guard refuses.
7. **Exposure is the internet.** The server answers at `/factorial/mcp/` on the app's host,
   beside the dashboard and `/api` (tech-stack decisions 60 and 74). The VM's dashboard faced
   the tailnet only, so MCP on the internet once took a host of its own, `mcp.runfactorial.com`;
   on ECS the app's host faces the internet as well, so a second host closes no path, and item
   1's bearer check is what refuses a caller.
8. **A new lane may carry gates with approver fields unset** (§7.17). Any non-App reviewer may
   approve such a gate. Protected `main` still bounds what a merge can do.
9. **The own-server refusal matches host and port** (§7.5 item 9). It cannot recognise another
   name or address for the same server: `127.0.0.1` for `localhost`, the ALB's own DNS name, a
   CNAME to the app's host. It knows that one host, which serves MCP and the dashboard alike.
   Https-only manifests and TLS name checks mitigate this, and the ALB answers 404 to a Host it
   has no rule for.

### 7.17 Accepted cost: an MCP client can press Commit

`factorial_config_commit` calls the path behind **Commit and sync** (§7.7), so a model's tool call
now stands in for the operator's click. That weakens §7.7, where the click is the human gate for
config.

Everything the server checks still holds:

- validation first, which refuses a config the orchestrator would reject at sync;
- the membership set recomputed in the same request, and the rule that never overwrites a file
  the repo has;
- the fast-forward-only ref;
- the sync guard, which accepts only a commit the orchestrator recorded or a person adopted;
- gate 2, protected `main` (§7.3);
- the pinned snapshot of every ticket already in flight.

What is lost is a person reading the diff before it lands. `edits` can rewrite any file already
on `factorial`. Two kinds of file there carry controls that nothing else enforces:
`agents/*/prompts/*`, where "agents never merge, self-approve, add labels, or push to `main`" is
the only control (§7.1), and the gate-1 `approvers` / `approverPermission` settings (§7.12). A
model that has read injected issue text could delete that sentence or widen who approves. A
shared skill under `skills/` loads into an agent's context the same way a prompt does, so it
could carry the contrary instruction.

**Guard.** On the MCP and chat surfaces, every surface but the dashboard, the config service
refuses any file the commit would write or edit under `.factorial/agents/` or
`.factorial/skills/`, and leaves those paths out of `offered`. It also refuses any approver change, compared parsed against the base version of the
process file. On a lane that exists on the base, adding, removing or renaming a human gate counts
as an approver change, as does a change to a gate's `approvers` or `approverPermission`. On a
brand-new lane, gates may be added only with `approvers` and `approverPermission` both unset.
Each refusal is an `Invalid` that names the change as dashboard-only. The dashboard's Commit is
unchanged. This keeps the prompt-level controls, the text that loads beside them, and the
approvers of an existing lane behind a person. Everything else in the folder stays reachable from
a model.

Two risks remain, both accepted:

- A new lane committed through MCP may carry gates with the approver fields unset, which means any
  non-App reviewer may approve. Branch protection on `main` still bounds what a merge can do.
- On an existing lane, MCP can still change a gate's `pr:` ref, its `onApprove.merge` or its
  `next` edges. These are not approver fields, and the graph validator rejects an orphaned step.

The other mitigation is attribution after the fact. `config_committed` records
`requested_via: "mcp"`, the token's principal and its id. The commit message names the requester:
"Update the {name} process through the Factorial MCP server", then "Requested by …". To revert a
bad config, commit the previous one. The pin keeps a bad config away from tickets already in
flight. `config_head_adopted` records the adopter and `requested_via: "dashboard"` the same way,
and the version list names the adopter.

**The chat surface.** The assistant's `factorial_config_commit` is a proposal twin (§7.18). It
checks the draft on the chat surface first, so a draft the guard refuses gets no card; such a
change goes to the process editor through `factorial_editor_open`, and a person commits it with
Commit and sync. A draft the guard accepts becomes a card that shows the files it writes and the
diff against the branch head, and the proposal keeps that head. Approve then runs `commit_config`
as the person who clicked, and the service picks the surface from `actor.via`, so the guard holds
again at the commit. The commit also checks its base: when the branch moved after the card was
drawn, the proposal ends `refused` and nothing is written, so the person never approves one diff
and gets another. The model reads the refusal, reads the branch again and proposes again.
`config_committed` records `requested_via: "chat"`, that person and the proposal's id, and the
commit message says "through the Factorial assistant". Here a person does read the diff, but
the model wrote it, and a rubber-stamped card commits what it shows (§7.18, accepted cost 1).

### 7.18 The assistant: a model that reads and proposes

The assistant (§3.4, tech-stack.md decision 63) puts a model in front of a person's factory. The
model's credential is the Anthropic key; its only authority over the factory is its turn's
tools, which read and propose.

1. **The model's tools cannot write.** Every write is a proposal. Each write tool of `/mcp/` has
   a twin among the assistant's tools with its name, title and input. The twin checks that the action is
   possible now and records a proposal and its card; it writes nothing to the factory. A
   person's click runs it, with that person's session, through the write tool's own body and so
   the same service the dashboard uses. Its events carry `requested_via: "chat"`, that person as
   the requester, and `chat_proposal_id` (§5).
2. **The CLI child has no tools but its turn's.** They come from the SDK's in-process server
   `factorial` and run in the assistant's process. `tools=[]`, `setting_sources=[]`,
   `strict_mcp_config`, and `dontAsk` with only `mcp__factorial__*` allowed. The init check proves
   this every turn and fails the turn closed.
3. **The child's environment is built, not inherited.** It comes from an allowlist through the
   launcher: no `FACTORIAL_*` value, no GitHub credential, and no API key in a subscription turn.
4. **The assistant process holds no GitHub credential and no master key.** It runs as its own
   OS user, `factorial-assistant`, in the image (on a laptop or on AWS), and cannot read
   `/app/.env`. It holds the database URL and the API token, so every stored secret is opaque to
   it. The API hands it each turn's model credential, only for the API bearer and only the one
   stored for the backend and auth mode the turn runs. A master key that reaches it in the image
   stops it at start. `make dev` runs it as the developer from the API's `.env`, where it blanks
   the key for itself; a laptop has no OS-user boundary. Its tools read GitHub and GitLab
   through entries the API hands it,
   per organization and per project. For an App project the entry is a
   read-only token the API mints with that project's App key, for the project's repo and intake
   repo, with `contents`, `issues`, `pull_requests`, `checks` and `metadata` at `read`, and
   asserts that GitHub granted exactly that. A token project's entry holds no token: its base URL
   is the API's GET-only proxy for that project, which adds the stored token server-side. A
   GitLab project's entry is the same, on the API's GitLab read proxy, and names the project's
   bot login. That proxy reads only the project's own GitLab repo and intake repo, and under
   each only the project itself and its `repository/`, `issues/`, `merge_requests/` and
   `members/` reads, which are what the chat tools read. The stored token is a Maintainer's, so
   the rest (`variables`, `jobs`, `pipelines`, `hooks` and the project's tokens) could hand out
   a CI secret. It drops every `*_token` key, such as `runners_token`, from the project's own
   answer. It reads the repo from the raw path, so an encoded `/` cannot split it. It
   refuses a dot or empty segment and a `%` left after decoding, drops a query parameter that
   GitLab reads as a credential, and follows no redirect. It returns the status, the body with
   the token redacted, the content type and GitLab's paging headers. The
   process asks with the API token, on loopback, and keeps an entry until it has less than a
   turn's wall clock plus five minutes to live. A turn reads with its conversation's
   organization's entries, so its GitHub reads never reach another organization's repos.
   Nothing in the process can write to GitHub or GitLab: its tools have no writer, and the
   proxies answer only GET.
5. **A tool call belongs to its turn.** The toolbox carries the turn. A tool that draws in the
   conversation checks that the turn still runs and that it is its owner's. The CLI child holds no
   token of any kind.
6. **Approval is the owner's.** Only the conversation's owner can approve, from the dashboard's
   origin. The stored arguments run exactly as stored, once. Approve and decline are dashboard
   routes behind the API's gate, not tools, so the model cannot reach them, and each needs an
   `Origin` that names the dashboard. Approve locks the conversation, then the proposal, and
   commits `approved` before anything runs: a second click answers 409, and a run that crashes
   leaves the proposal approved, never pending again, with the failure on its card. A service
   refusal ends it `refused` with the service's words. Every outcome adds a notice in the
   person's words and queues one continuation turn, which two approvals in a row share; when
   the person's or the organization's daily budget is spent, the write still runs, the notice
   says so, and no turn follows. A decline queues nothing, and a proposal still pending after 24 hours expires
   (Approve then answers 410): the next turn's message carries either to the model. At a
   session ceiling a chat resume is refused, as an MCP one is (decision 56).
7. **Config keeps §7.17's guard.** Chat commits refuse `agents/`, `skills/` and approver changes:
   the config service guards every surface but the dashboard. The commit twin checks first, so
   such a draft gets no card. Those go to the editor and a person's Commit and sync.
8. **Untrusted text stays marked.** Tool outputs keep the MCP server's `Untrusted` marking. The
   system prompt carries rule 8 of the MCP instructions. The model cannot navigate without a
   click.
9. **Never in the chat:** gate approval or merge, secrets, permits and allowlists, runtimes,
   labels, tokens, the terminal, and Redo, a continuing Retry and Force complete — the list of
   §7.16 item 6. The Settings credential is never readable back.

Seven costs are accepted:

1. **Untrusted text can shape what the model proposes.** The card is the human step, and a
   rubber-stamped card does what the person clicked. Configs keep §7.17's guard.
2. **Chart numbers are copied by the model.** The prompt rule and the source label reduce this;
   they do not prevent it. A later version lets the server fill the numbers from a tool result.
3. **One subscription token serves the whole deployment.** Every conversation counts against its
   owner's plan and limits, as with subscription runtimes.
4. **Privacy is by convention.** No role hides a conversation, and the API token can read every
   conversation.
5. **The read-only token outlives a turn.** The process keeps it for up to an hour, across turns,
   for every project repo. A compromised assistant process can read those repos for that hour.
   It cannot open a stored secret itself, but with the API token it can ask the API for any
   organization's model credential and read-only GitHub and GitLab access. On GitHub that is
   any GET under a project's repos. On GitLab it is only a project's metadata (its tokens
   removed) and its repository, issue, merge-request and member reads: no CI variable, job log,
   artifact, hook or token.
6. **Costs are estimates.** The SDK estimates cost on the client, and both daily budgets use
   those estimates. On a subscription the estimate is at API prices and does not track the
   plan's own limits. Codex reports no cost, so no budget limits a Codex conversation.
7. **Conversations store quoted issue text** until the person deletes the conversation, and for
   at most 30 days after its last activity.

### 7.19 Accounts, organizations and who may act in one

Every deployment has accounts, members, roles and invites (tech-stack.md decision 80). People
sign in with a password, Google or OIDC, and never with the shared API token: the token is for
machines (decision 12). `FACTORIAL_MULTI_ORG__ENABLED` chooses between two shapes of install, and
the self-hosted build ships with it off:

- **Off: one organization.** `default` is the only one, and nobody creates another. An account
  comes from the first owner on an empty install (item 11), from an invite, or from a verified
  SSO sign-in whose email is in `auth.allowed_domains`, which joins `default` as a `member`
  unless the person was removed from it or left it (item 13).
  Nobody signs up without one of these. An invite admits its email whatever the domain: the
  owner who sent it already decided. A password signup must send the invite's token, so a
  person who only knows the invited address cannot take the account first.
- **On: many organizations.** Anyone signs up, and any account creates organizations.
  `auth.allowed_domains` decides nothing, and an SSO sign-in joins nothing by itself. Signup is
  open, so the deployment must send mail to verify it (item 17).

1. **An account is an email.** `app_user` keeps one row for each lowercase email. A session
   carries the email, whether the account's email was verified when the session began,
   and the account's session epoch (item 14) (tech-stack.md decision 12), so changing the flag
   signs nobody out.
   An account signs in with a password, Google or OIDC. The first SSO sign-in creates it.
2. **A password does not prove the email.** Only the mailbox or the identity provider does:
   `email_verified_at` is set by a verified SSO sign-in, a verification link or a password reset
   (item 16). A verified SSO sign-in that reaches an account with an unverified email removes
   its password, and a reset replaces it, because the person who set it may not own the
   mailbox. Both end every session that began before. Without this, that person keeps a session
   and sets the password again. A reset also revokes the MCP tokens the account's sessions
   minted; an SSO sign-in does not (accepted cost 2).
3. **Membership is the tenant boundary.** A person reaches an organization only through an
   `organization_member` row. A person who is not a member gets 404, never 403, so a slug does
   not tell a stranger that the organization exists. This holds with the flag off too: a person
   who is not a member of `default` reaches nothing.
4. **A role is a set of permissions, and a permission is code.** Permissions are a closed set of
   `module:action` keys, as the predicates are (§6): a row can only name one, and a new one needs
   code that enforces it. Each route names the one permission it needs. The built-in roles
   `owner`, `admin` and `member` are rows that a revision seeds, and only a revision changes them.
   The owner role holds every permission, and a test keeps it so. The admin holds all but
   `members:assign_roles`. The member works tickets: it reads the organization, its projects,
   tickets, runs and costs, operates tickets, opens their terminals, and reads each project's
   configuration (revision 0038). It changes no setting.
5. **Settings are read as they are changed, except a project's.** A setting's read needs the
   permission its write needs: a runtime `runtimes:manage`, the assistant's settings
   `assistant:configure`, the organization's own `org:manage`, the members list
   `members:invite`. A project's configuration (secret names, ticket source, processes, agents,
   labels, readiness, the config branch) needs `projects:read`, which every built-in role
   holds, because the people who work a ticket read the process and prompts that run it; its
   writes need `projects:manage`. A role without `projects:read` gets a project with its
   `settings` cut to the GitLab host and no secret names or readiness rows, and every caller
   gets the organization with `settings` null unless it holds `org:manage`. The MCP and
   assistant tools that read config need `projects:read` (`GATED_READ_TOOLS`); opening the
   editor needs `projects:manage`. A run's terminal needs `tickets:terminal`, apart
   from `tickets:operate`, because the session holds the run's secrets: the gateway checks it at
   the handshake and again every few seconds while the terminal is open, so a removal or a
   demotion closes it. The route that mints a terminal ticket or opens a session checks it too.
   What the terminal offers stays readable to every member, so a parked run still offers Resume.
6. **Some rules are code, whatever a role holds.** Nobody grants a role that holds a permission
   they do not hold. An admin removes members only. The last owner cannot leave, be removed or
   be demoted.
7. **One function decides, and every way in calls it.** Routes under `/orgs/{org}` find the
   organization in the path. Item routes (`/projects/{id}`, `/tickets/{id}`, `/step-runs/{id}`,
   `/runtimes/{id}`) find it through the row. An MCP token (§7.16) is checked on every request
   against its own organization and the permission each tool needs, so removing a member stops
   that member's tokens on the next call. An assistant turn (§7.18) runs only while its owner may
   use the assistant there. The gateway checks an open terminal again every few seconds, and the
   page route checks a page link at every open, not only when a guarded route mints them. The
   shared API token holds every permission in every organization: it is the operator's, for
   machines and the operator's own API calls, and no dashboard sign-in accepts it.
8. **An invite is a link for one email.** The token is 32 random bytes, and the row keeps only
   its SHA-256. An invite is used once, lasts seven days, can be revoked, and is the only pending
   one for its organization and email. Only a session whose email equals the invite's can accept
   it, so a forwarded link is useless to anyone else. The link is shared by hand until invites
   are sent by email.
9. **An organization role never approves a merge.** A gate counts GitHub reviews, as §7.12 says.
10. **The flag can go back off.** Membership decides in both shapes, so turning it off only stops
   signup and the creation of organizations. Members of an organization other than `default`
   keep reaching it.
11. **The first account on an empty install owns `default`.** Empty means no row that only an
    operator makes: no membership, invite, project, runtime, secret, organization credential or MCP
    token. Revisions seed the settings rows, so they do not count. The account that a signup or an
    SSO sign-in creates there becomes the owner of `default` and skips onboarding, so a fresh
    install needs no token to start. One transaction locks the `default` row and checks again, so
    two people who sign up at once cannot both become owner. An install that is not empty never
    does this: a stranger who reaches a deployment's signup page first must not take its projects.
    An install that a stranger can reach before its owner signs up sets
    `FACTORIAL_AUTH__BOOTSTRAP_TOKEN`: then only a password signup that sends it owns the empty
    install, and an SSO sign-in there is refused until it does, because a member joined first
    would leave data and no owner. With the token set, the install stays empty for its holder
    until `default` has an owner, whatever else exists, and nobody creates an organization
    before then: with multi-org on, a stranger who signed up first and made an organization
    would otherwise have left the holder no `default` to claim. Unset, the API logs a warning
    at start, with multi-org on or off.
12. **An install with data but no owner gets one from the command line.** An existing install,
    which signed people in with Google and no memberships before accounts, runs
    `python -m orchestrator.admin grant-owner <email>` once (inside the running container, for example with `docker exec` or your platform's exec command). An
    account becomes the owner at once; an email with no account gets an owner's invite, from
    which the person signs up. The operator's API call with the token can invite the owner too.
13. **A person who was removed, or who left, needs a new invite.** Removing a member deletes the
    `organization_member` row, revokes the person's pending invites to that organization, and
    writes an `organization_removal` row for the organization and the email, with the reason
    `removed` or `left`. While that row exists, the allowed-domain SSO sign-in does not join the
    person to `default`: they still sign in, and reach nothing. Accepting a new invite, or
    `grant-owner`, deletes the row. Only a join that happens by itself reads the row. The
    membership check never does, so a missed check keeps a person out and never lets one in.
14. **A session can be ended from outside the browser that holds it.** The cookie is signed,
    not stored, so it names `app_user.session_epoch` as it was at sign-in, and the guard, the
    page route, `/auth/me` and the terminal gateway refuse a cookie whose epoch is not the
    account's. Sign out everywhere (`POST /auth/logout/everywhere`) and setting a password move
    the epoch; setting one gives the browser that set it a new cookie. Both also revoke each MCP
    token a session of the account minted, since a token outlives the session that minted it.
    A terminal ticket carries its session's epoch, so an open terminal closes at the next
    recheck. An account with no
    password sets its first one only within ten minutes of a sign-in: a copied cookie alone
    would buy a password that outlives it. Removing a member does not move the epoch, because
    membership is read on every request and recheck already, and the person's other
    organizations stay theirs.
15. **A change made by cookie comes from the dashboard.** `SameSite=Lax` still sends the cookie
    with a form POST from another host of the same site, so the guard refuses a cookie request
    with any method but GET, HEAD or OPTIONS unless its `Origin` names the dashboard
    (`dashboard_origins`) or it sends `Sec-Fetch-Site: same-origin`. It answers 403, not 401, so
    the dashboard does not sign the person out. The bearer token is exempt: a browser never
    sends it by itself.
16. **An email with a link proves the mailbox.** With mail configured (`FACTORIAL_MAIL__*`, one
    SMTP server), the API sends two emails, each a link with a token: a verification after a
    password signup, at a password sign-in of an unverified account with no live link, or on
    request, and a password reset on request. The token is 32 random
    bytes. The `email_verification` row keeps only its SHA-256, its purpose (`verify` or
    `reset`), the address it went to, and its expiry: a day for `verify`, an hour for `reset`.
    A token works once, and a new one for the same account and purpose replaces it. Every link
    starts with `auth.external_url`, never the request's `Host`, so a forged header cannot send
    a token to another host. Each link opens a dashboard page that sends the token by POST, so
    a mail scanner that opens the link uses nothing.
    - **A verification link verifies only the account that is signed in.** The page asks the
      person to sign in, then sends the token with that session, and the API refuses a token
      whose email is not the session's. Without this, the owner of a mailbox who opens a link
      they did not ask for would verify the account of a stranger who set its password. The
      browser that sends the token gets a new session. Every other session of the account that
      began unverified ends, as after an SSO sign-in.
    - **A reset needs no session.** It sets the new password and `email_verified_at`, and ends
      every session and every session-minted MCP token (item 14). This is how the owner of an
      address takes back an account that a stranger registered with it. The reset request
      answers the same whether the email has an account or not, and it looks the email up
      after the answer, so its time does not tell either.
    - **Sending is limited.** One email a minute and five a day for each account and purpose,
      and the request routes count against each client address too. Open signup lets anyone
      make the deployment write to any address, and a provider suspends a sender whose mail
      bounces or is reported.
17. **With mail, an unverified session reaches only its verification.** While mail is
    configured, a session whose email is not verified may read `/auth/me`, ask for a new
    verification email, send a token, and sign out. The guard, the page route and the terminal
    gateway refuse it everything else, with the 403 `email_unverified`, and an MCP token that a
    session of an unverified account minted is refused (item 7), also after the account is
    verified: it may be a stranger's. An account that SSO created is
    verified already. Without mail there is nothing to verify with, so the gate is off. With
    multi-org on the API refuses to start without mail, because open signup without
    verification lets a stranger take an invite to an address they registered, and the image's
    startup check names the missing mail.
    `python -m orchestrator.admin verify-email <email>` verifies an account whose mail does not
    arrive.

This design accepts five costs:

1. **Without mail, a password account stays unverified.** An install with multi-org off and no
   mail cannot verify a password signup. There a signup needs an invite's token or the empty
   install, so a stranger cannot take an invited address first, but `email_verified_at` stays
   empty and the members list marks each such account unverified. With multi-org on the API
   does not start without mail (item 17).
2. **An SSO sign-in keeps the MCP tokens of the account it verifies.** A stranger who
   registered the address first and minted a token keeps that token until it expires or someone
   revokes it. Revoking them all would also break the tokens of each honest person who signed
   up with a password and then signs in by SSO. With mail configured an unverified account
   cannot mint a token, and one it minted before is refused (item 17), so this cost is an
   install's without mail. A reset revokes them.
3. **Signup and sign-in say whether an email has an account.** A stranger can learn whether an
   address is registered. A reset request answers the same for every email, but signup still
   tells.
4. **Two organizations' projects may use one GitHub App.** Each project stores its own copy of
   the key, and a delivery to the App's URL wakes only the projects whose webhook secret
   verifies it (§7.1 item 7).
5. **Membership changes are rows, not events.** An organization is not a project, and
   `event.project_id` is NOT NULL (§2.11), so each row records who made it.

## 8. Repo layout

```
docs/                 design docs and operator guides
docs/samples/         sample .factorial folders (factorial-simple is the canonical layout)
orchestrator/         reconciliation loop, state DB, event log, predicates, GitHub client, API,
                      terminal gateway, the assistant's process
runtime/              agent-runtime wrapper (contract + claude_cli, codex_cli, E2B adapters)
ui/                   dashboard (reads event-log read models)
scripts/              repo-level operational scripts (tech-stack decision 21)
.factorial/                process config for this repo itself (eventually)
```

The API, the reconciliation loop, the terminal gateway and the assistant's process are four
entrypoints over one `orchestrator` package, deployed as separate processes (tech-stack decision
3).

## 9. MVP slice & non-goals

**MVP:** one process (`refine → spec gate → develop → code gate → done`), GitHub or GitLab
issues/reviews/CI, one agent substrate adapter, event log + minimal dashboard. Snapshot types
and the Tracker / Forge protocols live in `orchestrator.adapters`; a composite `StateSource`
can pair one of each. The worker still binds both from the ticket source.

**Non-goals (this phase):** Jira, deploy/monitoring steps, App Runtime, multi-repo tickets,
carrying an in-flight ticket's progress onto a new config (a restart starts it fresh instead,
tech-stack.md decision 67), redo of a step the ticket passed beyond the reviewed one, rolling a
shared branch back (tech-stack.md decision 76), Projects v2 integration, custom roles, joining an organization by email domain,
sending invites and notifications by email (§7.19). A step preview is not a deploy step
(§3.3).
