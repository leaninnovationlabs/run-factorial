# Setting up the factory

**Status:** setup guide · the orchestrator era · **Replaces:** the personal access token
retired 2026-08-04

What a deployment of the orchestrator needs: a machine with the toolchain, the GitHub App
that mints every credential an agent ever holds, the private key handled correctly, and
Postgres plus `.env`.

---

## Machine prerequisites

- **Orchestrator + dashboard:** Python 3.13, [uv](https://docs.astral.sh/uv/), Node 22.12+,
  [pnpm](https://pnpm.io/), Docker. Postgres is the `database` container of the repo's
  `docker-compose.yml`, from the local image (`make image`, then `make db`). Setup commands are in
  the [README](../README.md).
- **Runs on a host runtime:** tmux 3.2 or later, for the agent's TUI and the terminal gateway
  that relays it, and the CLI at exactly the adapter's pin (`PINNED_VERSION` in
  `claude_cli/cli.py` or `codex_cli/cli.py`); for codex, the native binary, not npm's wrapper. A
  tmux that is missing or older, or a CLI of another version or npm's codex, makes the worker
  refuse the runtime's spawns: the ticket blocks with a comment that names the gap, the worker
  asks again every `terminal.parked_recheck`, and the ticket continues by itself once the gap is
  closed. The worker retries a spawn whose CLI is not on PATH (tech-stack decisions 43, 56 and
  57). Use tmux 3.3 or later
  where you can: only 3.3 and later stop an agent's escape sequences from passing through to the
  viewer's terminal.
- **Or one container:** Docker only. The image carries Postgres, the toolchain, tmux and the
  `claude` and `codex` CLIs; `codex_cli` runs there only with the seccomp profile
  `docker/seccomp.json`, so it does not run in the ECS task (tech-stack decision 52). `make up`
  runs it beside a database container, as ECS runs it beside RDS (decision 73).
  [`local-container.md`](local-container.md) has the procedure, and the README has the short
  form.
- **`.env`:** copy [`.env.example`](../.env.example) and fill it in. The file is
  sectioned: section 1 is the substrate credentials the runtime adapters read directly,
  section 2 is the orchestrator's `FACTORIAL_*` surface. [`bootstrap.md`](bootstrap.md)
  covers everything project-shaped: secrets, config sync, labels, policy, activation.

---


## Which runtimes run where

A runtime is three choices: a CLI, a way to sign in, and a sandbox. Not every deployment can run
every combination. The first three columns are the `local_cli` isolation, which the dashboard
calls "On the host".

| CLI · authentication (type) | Host machine | Local container | ECS task | Managed cloud sandbox (`e2b`) | Self-hosted sandbox | The runtime stores | Entry in `FACTORIAL_RUNTIME__OFFERED` |
|---|---|---|---|---|---|---|---|
| Claude Code · API key (`claude_api`) | Yes | Yes | Yes | Yes | Not yet | `ANTHROPIC_API_KEY` | `claude_api@local_cli`, `claude_api@e2b` |
| Claude Code · Subscription (`claude_subscription`) | Yes | Yes | Yes | Yes | Not yet | `CLAUDE_CODE_OAUTH_TOKEN` | `claude_subscription@local_cli`, `claude_subscription@e2b` |
| Codex · API key (`codex_api`) | Yes | With the seccomp profile | No | Yes | Not yet | `OPENAI_API_KEY` | `codex_api@local_cli`, `codex_api@e2b` |
| Codex · Subscription (`codex_subscription`) | Yes | With the seccomp profile | No | Yes | Not yet | `CODEX_AUTH_JSON` | `codex_subscription@local_cli`, `codex_subscription@e2b` |
| Codex · AWS Bedrock (Commercial or GovCloud) (`codex_bedrock`) | Yes | With the seccomp profile | No | No | Not yet | Nothing. Each run mints `AWS_BEARER_TOKEN_BEDROCK` from the host's AWS role | `codex_bedrock@local_cli` |
| Claude Code · AWS Bedrock (Commercial or GovCloud) | Not yet | Not yet | Not yet | Not yet | Not yet | — | — |

Each deployment lists what it offers in `FACTORIAL_RUNTIME__OFFERED`. If you do not set it, the
deployment offers the API key rows on the host. It also offers the API key `e2b` rows when
`FACTORIAL_RUNTIME__E2B__DEPLOYMENT` has a value. The public image sets that name for you when
it uses its own database. To offer a Subscription or Bedrock row, set the list. When `FACTORIAL_MULTI_ORG__ENABLED` is
`true`, the deployment offers only the `e2b` rows. Any person can sign up there. A runtime on
the host runs its agent beside the master key, and a Bedrock runtime uses the AWS role of the
host. To offer a host row under multi-org, set the list.
You cannot create a runtime, or move one to a sandbox, for a pair that is not in the list. The Create runtime page does not show that pair. A runtime that already exists
keeps running. An `@e2b` entry needs
`FACTORIAL_RUNTIME__E2B__DEPLOYMENT`. If you do not set it, the container does not start.

The assistant has a list of the same kind, `FACTORIAL_ASSISTANT__OFFERED`. Its entries are
`<backend>:<auth_mode>`, for example `claude_sdk:api_key`. If you do not set it, the assistant
offers the Claude API key and the Codex API key. An organization that uses another sign-in
keeps it, and its settings page marks it "No longer offered". When `FACTORIAL_MULTI_ORG__ENABLED`
is `true`, the assistant never offers a `bedrock` entry.

To run the assistant on a Bedrock model, such as Grok, put `bedrock_responses:bedrock` in the
list. That backend calls Bedrock itself. It shows the reply while the model writes it, and it
shows each tool call. `codex_cli:bedrock` runs the Codex CLI on Bedrock. The model can use the
assistant's tools with both entries.

The conditions:

- **Every runtime.** Make it on the Runtimes page and store its key there. A process names the
  runtime by its slug.
- **Host machine.** The CLI is on `PATH` at the adapter's pinned version, and
  `FACTORIAL_RUNTIME__CLAUDE_CLI__LOCAL_ROOT` or `FACTORIAL_RUNTIME__CODEX_CLI__LOCAL_ROOT` is
  set. The host also needs tmux ([Machine prerequisites](#machine-prerequisites)).
- **Local container.** The image has both CLIs and sets both run roots. The sandbox of `codex`
  needs user namespaces, so start the container with `docker/seccomp.json`
  ([`local-container.md`](local-container.md#codex_cli-in-the-container)). Without the profile,
  the worker refuses each codex run and the ticket shows why.
- **ECS task.** Fargate cannot apply a seccomp profile, so a codex runtime on the host does not
  run there (tech-stack decision 52). Use the managed cloud sandbox for codex.
- **Managed cloud sandbox.** The deployment sets `FACTORIAL_RUNTIME__E2B__DEPLOYMENT`. The
  runtime also stores the `E2B_API_KEY` of its E2B team and pins a template from that team. The
  project's `runtime_allowlist` must name the runtime, because the run sends the repository's
  code to a third party (tech-stack decision 25).
- **Self-hosted sandbox.** Not built yet.
- **AWS Bedrock.** Not on `main` yet. The first version is for Codex on the host only. Claude
  Code on Bedrock, and Bedrock in a sandbox, are later work.

---

## The procedure for the organisation owner

> This section uses [Simplified Technical English](writing-rules.md). A person outside the
> team follows it. The remainder of this document explains the reasons, and it uses full
> English.

**Time: about 10 minutes. You need organisation owner rights.**

### Step 1 — Make a GitHub App

Go to `https://github.com/organizations/<your-org>/settings/apps`.

Make the App at the organisation level. Do not make it on a personal account. A personal App
stops work when that person leaves.

| Field | Value |
|---|---|
| Name | a name you choose |
| Homepage URL | the URL of your repository or deployment |
| **Webhook → Active** | **Remove the tick** for now. Tick it once the deployment has a public URL. A project's **Ticket source** page shows the webhook URL after you save its connection |
| Where can this be installed | Only on this account |

### Step 2 — Set the repository permissions

Set these permissions. Do not set more. This list matches the live installation, read back
from the GitHub API on 2026-08-21 (see [`bootstrap.md`](bootstrap.md), Step 5).

| Permission | Level |
|---|---|
| Contents | Read & write |
| Issues | Read & write |
| Pull requests | Read & write |
| Webhooks | Read & write |
| Checks | Read-only |
| Artifact metadata | Read-only |
| Metadata | Read (GitHub adds this) |
| Administration | Read-only — optional, see below |
| **Workflows** | **Do not set this one.** See step 5 |

Checks Read-only is what lets the orchestrator read check runs for its `ci_green`
predicate. Write stays off: a forgeable check would let agent output vouch for itself.

Administration Read-only is optional and follows the same rule. It lets the configuration
page's branch-protection row report what protection the default branch actually has, instead
of "not checked". Leave it off and everything still works — the row degrades and says so.

Do not set Administration to Read & write. Branch protection is what stops agent-authored
code reaching the default branch without a human, and the App is the author of every
implementation PR. An App that can write protection can remove it. The dashboard therefore
offers no button for this: it prints a `gh` command for you to run under your own
credentials.

### Step 3 — Install the App

Select **Only select repositories**. Then select the repository being onboarded.

**Do not select "All repositories".**

Factorial reads the installations of the App when a person saves a project's GitHub
connection. You do not send the installation ID.

The dashboard verifies this afterwards. A project's **Git source** page, under **Settings**,
shows whether the installation includes the project's repository. If it does not, the page shows
an error under the repository URL and a link to the settings page above. This error does not stop
activation, but no run can work until you add the repository. Only an owner of the account can
open that settings page.

### Step 4 — Make a private key and send it

Make a private key. GitHub gives you the `.pem` file one time only.

Send the file only through a password manager. **Do not send it in a chat or an email.** A
person who has this file can operate as the App.

Then send these two items:

1. the **App ID**, from the General page of the App
2. the location of the `.pem` file

A person pastes both into the GitHub connection of each project, on its **Ticket source** page.

### Step 5 — Two requests to refuse

**"Do we set Workflows also?"** No. Agents write code, and CI examines that code with
repository secrets. Write access to `.github/workflows/` lets an agent change the CI that
examines it. The App does not have this permission. Because of this, no token can get it
later.

**"Is 'All repositories' more simple?"** No. The installation list is the limit of what the
factory can touch. "All repositories" makes that limit the full organisation, when the
factory needs exactly the repositories it manages today.

**A permission added later needs a second approval.** The App declares it; each
installation must accept it, on the organisation's **Settings → GitHub Apps → Configure**
page. Before that acceptance, a mint call that asks for the new permission fails.

### What the team does after this

The orchestrator mints a token for each agent run. Each token is valid for one hour. Each
token gives access to named repositories only, and to named permissions only.

The private key does not go into an agent container.
A person pastes it into each project's GitHub connection, and the orchestrator keeps it encrypted
in the `secret` table, under the project's data key. No deployment setting holds a GitHub
credential. Never put the key in the repo — `*.pem` is in `.gitignore` — and never in `.env`.

The environment uses `FACTORIAL_`-prefixed variables. The `SF_` spelling was retired on
2026-09-06 and is now refused at startup, naming the variables to rename — it is not ignored,
because an ignored setting takes its default and `database_url`'s default is a local
development database.

---

## Why an App rather than a personal token

A PAT ties every agent action to the person who owns it — the audit log cannot tell an agent
from a human, the token cannot be scoped per role, and it dies when the owner leaves. A GitHub
App fixes all three: its own identity in the audit log, a token minted per run and expiring in
an hour, and it is org-owned. The App has been live since 2026-08-04 and stays the default. A
project may connect with a token instead, and then every run of it holds that long-lived token
(tech-stack decision 82 lists what that gives up).

## How a mint works, and who mints

Two steps: a JWT signed with the private key proves *"I am this App"* (max 10 minutes); an
installation token obtained with that JWT proves *"I am this App, acting on these
repositories"* (valid 1 hour). The token is the only credential an agent ever sees — an
agent holding the private key could mint itself anything the installation has, which is why
the `.pem` never enters a container.

The **orchestrator** owns the agent's credential: `GitHubClient.mint_run_token` mints from
the agent manifest's ceiling-checked `permissions:` block and fails the mint unless GitHub
granted exactly what was requested — see [`design.md`](design.md) §7.1 items 5–6 for the
ceiling and the two-directional check. No adapter mints a token of its own.

## Rotation and revocation

| Event | Action |
|---|---|
| Key suspected leaked | App → General → **Revoke** the private key, generate a new one, update your stored copy, and paste it into the GitHub connection of each project on the App. The next call uses it |
| Someone leaves | Nothing — the App is org-owned. This is the point |
| Narrowing access | Change the *Installation* repository list, not the mint call |
| Shutting the factory down | Uninstall the App. Every outstanding token dies within the hour |

Uninstalling is the fastest global kill switch: it invalidates every token from that
installation immediately, without touching any repository or any running container.

## Verifying a minted token

```bash
# Mint one by hand only to verify — the orchestrator mints per run. This is its own
# mint, with the default profile and the granted-equals-requested check, on the GitHub
# connection of the project on that repo; it needs the database and the master key `.env` names.
TOKEN=$(uv run python -c '
import asyncio
from sqlalchemy import select
from orchestrator.db.models import Project
from orchestrator.db.session import session_scope
from orchestrator.github.client import APP_PERMISSIONS
from orchestrator.github.pool import pool
repo = "<owner>/<repo>"
async def mint():
    async with session_scope() as session:
        rows = await session.execute(select(Project).where(Project.github_repo == repo))
        client = await pool().for_project(session, rows.scalars().first())
    return await client.mint_run_token(repo, APP_PERMISSIONS)
print(asyncio.run(mint()).token.get_secret_value(), end="")')

curl -sS -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $TOKEN" \
  https://api.github.com/repos/<owner>/<repo>
# expect 200

# It cannot WRITE workflow files ("Resource not accessible by integration"):
curl -sS -o /dev/null -w '%{http_code}\n' -X PUT -H "Authorization: Bearer $TOKEN" \
  https://api.github.com/repos/<owner>/<repo>/contents/.github/workflows/zz.yml \
  -d '{"message":"perm probe","content":"eA==","branch":"main"}'
# expect 403
```

---

## The Complex lane — the dormant boundary, and its re-entry seams

The stricter, dormant pipeline's load-bearing property: **the build agent cannot read the
held-out acceptance tests judging its work.** Two independent mechanisms gave it that
property, both proven live on 2026-08-14: the build role got **no credential at all**
(purge the store, assert the purge, before the container starts), and its checkout was
wiped and re-cloned `--single-branch --no-tags` every run, then asserted blind host-side —
zero tags, no `acceptance/<slug>`, no reachable contract commit — before any agent process
existed. A check on state the checked party controls proves nothing, which is why the
assertion runs on the host, never in the container.

The seams live in `runtime/`:

- `runtime/src/runtime/contract.py` — `Workspace.isolation` / `Workspace.credential`;
- `runtime/src/runtime/workspace.py::assert_isolated` — the host-side blindness gate;
- `runtime/src/runtime/adapters/_cli/checkout.py::prepare_blind_clone` — the blind
  checkout arm (the wiped-and-blind-clone shape).

Every adapter refuses the lane at spawn. `runtime/tests/test_cli_blind_clone.py` runs the
blind clone and its gates directly, so the seam stays tested.

One repository-side control belongs to this lane too: a **tag ruleset** on the app
repository restricting `contract/*` tag creation, update, and deletion to the App alone
(Settings → Rules → Rulesets → New tag ruleset; target `contract/*`; bypass list: this App,
mode Always). It makes the tag trustworthy as CI's integrity anchor; the blind clone is what
keeps the agent from reading it.

Why the blind clone is the boundary, and why the check that proves it runs host-side, is
[`design.md`](design.md) §7.6.

---

## GitLab as a ticket source

The GitHub App above is for GitHub projects. A project whose ticket source is GitLab
does not use the App: it uses `FACTORIAL_GITLAB__*` and the project's `GITLAB_TOKEN`.
See `docs/gitlab.md`. The App settings stay as they are for every GitHub project.

## Accounts and organizations

Every deployment has accounts, members and roles (design.md §7.19). People sign in with a
password, or with Google or another OIDC issuer when you set the `FACTORIAL_AUTH__*` values. Nobody
signs in to the dashboard with the API token. Membership limits what each person can see.

`FACTORIAL_MULTI_ORG__ENABLED` selects one of two installs:

- **Off (the default): one organization.** `default` is the only organization. A person gets an
  account in one of three ways: the first account on an empty install, an invite, or an SSO
  sign-in with an email in `FACTORIAL_AUTH__ALLOWED_DOMAINS`. That SSO sign-in adds the person
  to `default` as a member. The list only lets new people in. To stop a member, remove them on
  the **Members** page.
- **On: many organizations.** Anyone can make an account and make organizations.
  `FACTORIAL_AUTH__ALLOWED_DOMAINS` does not limit who signs in.

The first account on an empty install is the owner of `default`. An install that already has
projects but no owner gets one from the command line:

```sh
uv run python -m orchestrator.admin grant-owner owner@example.com
```

When the email has an account, it becomes the owner. When it has no account, the command prints
an invite link. Send the link to that person. They make an account from it, and the invite makes
them the owner. The link works once, for that email only, for seven days.
The owner invites the other people from the Members page.

You can turn the flag off again. Off, people cannot sign up or make organizations, and members
keep their organizations.

## Related

- [`../README.md`](../README.md) — orchestrator setup and run commands
- [`bootstrap.md`](bootstrap.md) — point the orchestrator at a repository
- [`design.md`](design.md) §7 — the security model this App serves
