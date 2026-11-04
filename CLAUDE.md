# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

This repo is the **factorial** ADLC orchestrator (`orchestrator/` + `runtime/` + `ui/`):
GitHub Issues flow through the Agent Development Life Cycle, AI agents do the work of each
phase on runtime adapters (`claude_cli`, `codex_cli`, and both CLIs in an E2B sandbox:
`claude_e2b`, `codex_e2b`), and humans approve at defined gates.

The design of record:

- `docs/design.md` — the architecture and its numbered design decisions. Read this first; most
  questions about "why is it like that" are answered by §2.
- `docs/data-model.md` — Postgres model (prose/ER diagram). SQLAlchemy models are the source of
  truth, and Alembic revision 0001 is the base (tech-stack.md decision 2).
- `docs/samples/factorial-simple/` — the canonical shape of a `.factorial/` folder, copied into target
  repos. Its `processes/process-simple.yaml` is also the commented reference for the
  process-config format.
- `docs/process-yaml.md` — the `process.yaml` field reference: every field, the predicate
  library, the graph rules. The place to look before changing a config format.
- `docs/bootstrap.md` — point the orchestrator at a repository, the `.factorial/`
  authoring facts found the hard way, and how to harden agent prompts against a person typing
  into a run.
- `docs/design-system.md` — the dashboard's design system. Read it before any `ui/` change.
- `docs/setup.md` — the GitHub App, the credentials, the machine.
- `docs/local-container.md` — the env vars, the build and the run for the single local
  image, beside its database container under compose (tech-stack decisions 52 and 73).
- `orchestrator/`, `runtime/`, `ui/` — the code. The Makefile wraps the commands (`make help`).

## Architecture — the load-bearing ideas

**Reconciliation, not webhook-imperative.** The orchestrator runs an observe → evaluate → act →
project loop per active ticket. Desired behavior comes from the ADLC config; observed state comes
from *polling* GitHub. Webhooks are only a wake-up signal that triggers an immediate poll, so a
dropped or duplicated webhook can never corrupt state. Any proposal that makes a webhook the
authority on state contradicts the design.

**The orchestrator owns the state; GitHub is a projection.** The state machine lives in Postgres,
keyed `(ticket, processVersion, step, attempt)`. Issue labels (`factorial:<step>`, `factorial:blocked`,
`factorial:interrupted`, `factorial:waiting`, `factorial:exit`, `factorial:done`) and assignees
are *written back* for human visibility — human edits to a ticket are observed input, never
commands that bypass the machine. Three labels are released by REMOVAL rather than addition:
`factorial:interrupted` resumes the agent's own session in the worktree the interrupt kept
(decision 26), `factorial:waiting` does the same after an idle park (decisions 26, 56), and
`factorial:exit` grants a fresh attempt at the step the ticket left (decision 29). All three stay
observed input — the gesture the loop watches for, never a command it obeys. A session parked at
its wall-clock ceiling is the exception: any App token can remove a label, so only the
dashboard's Resume releases it (decision 56). A loop park, not a person's stop, can also end
without any release: when the session sat at its prompt and its postconditions come to hold,
the re-check accepts the run (decision 56).

**Steps advance via `next`, gated on executable postconditions.** Conditions are compositions
(`all` / `any` / `not`) of a closed predicate library implemented in orchestrator code
(`branch_exists`, `file_exists`, `pr_open`, `pr_merged`, `pr_approved`, `pr_changes_requested`,
`ci_green`, `ticket_label`, `ticket_has_assignee`, `issue_comment_contains`,
`pr_comment_contains` — design.md §6). Config may only *reference* predicates; new ones require a
code change. Postconditions double as the acceptance gate for agent output — the orchestrator
trusts predicates, never the agent's self-report. The one exception is a person's dashboard
Force complete, recorded as `step_run.forced_by` and never counted as a success (decision 78).

**Everything is an event.** The `event` table is append-only (no UPDATE/DELETE). The dashboard,
audit trail, and cost-per-issue reporting are read models derived from it, never separately
maintained state. Per-run token/cost figures live on `step_run`, not on artifacts. The factory board
on `/overview` is the same thing at factory scale: one row per ticket of one organization's
projects, from `GET /api/orgs/{org}/overview` (`events/read_models.py::factory_overview`,
tech-stack.md decision 27). The issue **title** is the one piece of issue text the orchestrator
stores (`ticket.title`, design.md §2.9) — refreshed from the observation each tick already makes,
so it costs no extra GitHub call. Everything else about an issue is still read live.

**Config is pinned by snapshot.** At admission a ticket stamps the current `process_version` (the
whole parsed config, as JSONB) and keeps it until done. Editing config in the repo creates a new
version row and never touches in-flight tickets.

**Concurrency:** one lease per ticket (`ticket.lease_owner` / `lease_expires_at`); agent spawns are
idempotent on `(ticket, step, attempt)` — the `step_run` unique constraint.

**Security containment is structural, never prompt-level** (design.md §7). Load-bearing rules: the
GitHub App is **never** granted `workflows: write`, because CI runs agent-authored code with repo
secrets before any human sees it; a gate's qualifying approval is what blocks a bad merge, and
protected `main` plus the App being the PR author (and so unable to approve it) is the second
control; the Postgres `secret` table is the only store
the orchestrator manages; and a manifest's `secrets:` block is what to mint, *not* an isolation
boundary — a substrate may put every stored secret in front of every run. There is **one
permission profile** shared by agents and orchestrator, so "agents never add labels" is a prompt
rule rather than an enforced one — a known, accepted cost (§7.1), not an oversight to re-derive.

## Division of labor — the rule most likely to be violated

Responsibilities are split three ways, and duplicating one layer's job in another is the failure
mode to watch for:

- **Orchestrator** — admission, labels, assignment, promotion between steps, gate approval + merge,
  announcing PRs on the ticket, retries/timeouts/escalation, verifying every postcondition.
- **Runtime wrapper** (`spawn/status/result/logs/cancel/interrupt/resume` and the session verbs
  `finish/session_state/terminal_target/spawn_refusal`; callback optional) — checkout, secret
  injection, result reporting. It exists so the orchestrator never depends on a vendor API; vendor
  specifics belong only in an adapter. The adapters wrap the `claude` and `codex` CLIs, by
  subprocess on the host or through the `e2b` SDK in a sandbox (every sandbox call goes through
  `adapters/e2b/client.py`; the template builders import the SDK too); keep `contract.py` free of CLI-shaped assumptions so an HTTP adapter can join
  without touching anything above.
- **Agent prompts** — do the work, push a branch, open a PR, report honestly. Nothing else.

Agent prompts are deliberately small, and two opposite things get confused here. A prompt must
not DUPLICATE the orchestrator's bookkeeping — announcing a promotion, restating which label a
step sets, describing the merge the PR will get: that is redundancy, it drifts out of sync with
the orchestrator, and it goes. The PROHIBITION is the other case and stays: agents never merge,
self-approve, add labels, or push to `main`. Merging is enforced, but the rest is prompt-level
(see the security note above), so that sentence in the prompt IS the control — delete it and
nothing replaces it.

## Config formats

All three use `specVersion: factorial/v1` and are read from a target repo's `.factorial/`
folder. `adlc/v1` still parses: a pinned snapshot says it for ever (issue #138).
`processes/process-<name>.yaml` (`kind: Process`), `agents/<name>/agent.yaml` (`kind: Agent`,
folder name == the `agent:` name; prompts are markdown files, never inlined YAML),
`mcp/<name>.yaml` (`kind: McpServer`, file stem == the tool prefix). The field reference is
[`docs/process-yaml.md`](docs/process-yaml.md); the commented sample is
`docs/samples/factorial-simple/`. Two rules a session gets wrong by default:

**`{slug}` is resolved, not literal — but only for directives.** `{slug}` is the part of a
branch name the agent chooses at runtime, so predicates match with `*` globs
(`branch_exists("spec/{ticket}-*")`) and templates must stay consistent with them. It resolves
to the concrete observed name once the orchestrator reads it back from a delivered artifact,
and reverts to literal if a later artifact makes the name ambiguous — recomputed every tick
(`worker/templates.py`'s `_resolve_slug`). Resolution feeds template DIRECTIVES only
(`workspace.branch`, predicate arguments); the orchestrator's own PR/gate lookups against
observed state (`_glob`) stay `*`-globbed on purpose — a matcher must only ever widen against
what actually happened.

**Declaring an MCP server does not grant it.** `.factorial/` lives in the repo agents write to, so
the file *selects* and `project.settings.mcp_allowlist` — operator state in Postgres, matched
exactly on name/transport/url/command/args/tools — *permits* (design.md §7.5). A proposal that
treats the repo file as the boundary has the trust direction backwards. `stdio` versions must
be pinned; `@latest` is refused at parse time.

## Editing conventions

- Scope discipline: this phase is **GitHub only** (Issues + PRs + Actions). App Runtime, Jira,
  deploy steps, multi-repo tickets, and Projects v2 are explicit non-goals (design.md §9).
- The scripts' header comments are load-bearing documentation. Keep them true when you change
  behavior — stale comments have cost this project full validation runs.

## Conventions that bind every session

- **Comments:** only where the code cannot say it itself — a non-obvious
  constraint, a cause someone would otherwise re-derive, a rule that looks
  wrong. Never restate what the line does. The scripts' header comments are
  load-bearing (above); that is the exception, not the standard. Prefer
  deleting a comment to updating one that no longer earns its place.
- **Commits:** plain sentences that explain why. No `Co-Authored-By` or agent
  trailers, in commits or PR bodies.
- **Pushes:** this repository is a read-only mirror of a private repository; changes land there.
- **CI:** runs on each push to `main`, that is, on each sync. `make check` runs the same checks
  locally.
- **Tests:** the pinned invocation is `uv run pytest -c pyproject.toml
  orchestrator/tests runtime/tests` (never a bare `uv run pytest`). A test must
  fail for the reason it names — prove a new test by mutating a copy, never the
  tracked file.
- **Docs:** `README.md` and most of `docs/` follow Simplified Technical English
  (`docs/writing-rules.md`). After editing `README.md`, run
  `./scripts/check-ste.sh README.md`; it must pass before commit.

## Security invariants — never weaken these

- **Every LLM stage runs on a per-run, one-hour, repo-scoped App token**, minted by the
  orchestrator with granted-equals-requested asserted at mint time. The one exception is a
  project its operator connected with a token, whose runs get that stored token (an accepted
  cost, tech-stack decision 82). A lane is one of two kinds. **Stacked:** the impl PR
  targets `spec/<slug>`, never `main`; the only path to `main` is the spec PR, which a human
  merges (gate 2); CI's acceptance-diff guard gates the impl PR. **Base-target** (every starter
  lane): both PRs target `main`, each merges only after a person approves it at a gate, and no
  acceptance-diff guard applies. Either way, the gate is the control: the orchestrator merges
  only a head a qualifying person approved. Branch protection is the second control: readiness
  reports the profile (1 approval, CODEOWNERS, `verify`, dismiss-stale) and does not enforce it,
  and on an App project the App holds no bypass. The dormant **Complex lane** (credential-less
  blind builder) keeps its re-entry seams in `runtime/`: `contract.py`'s
  `Workspace.isolation`/`.credential`,
  `workspace.py::assert_isolated` and `adapters/_cli/checkout.py::prepare_blind_clone`, which
  `runtime/tests/test_cli_blind_clone.py` runs directly. Every adapter refuses a blind
  workspace at spawn, so reviving the lane means building a blind session first.
  A blind workspace is proven host-side before any agent process exists; anything read from
  an agent workspace is data — compare it, log it, never execute or interpolate it.
  The assistant's tools read GitHub on a read-only token the API mints per App project, or
  through the API's GET-only proxy for a token project, and GitLab through the API's GET-only
  GitLab proxy; the process caches an entry for up to
  an hour, and the CLI child never holds one. The assistant's process holds no master key: the
  API hands it each turn's credential, and it refuses to start in the image with a key
  (design.md §7.18).
- **GitHub credentials are per project.** A project's App private key, webhook secret or
  token is its own row in the `secret` store, encrypted, written only by the connection route
  and never read back out. No deployment setting holds a GitHub credential: none is in `.env`,
  and `*.pem` stays gitignored.
- **Agents run in worktrees under the adapter's `local_root`**, never the operator's
  working copy, and the adapter refuses any resolved path that escapes that root.

