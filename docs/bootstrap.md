# Bootstrapping a project on the orchestrator

**Status:** runbook

This page tells an operator how to make the orchestrator (`orchestrator/`) act on a repository:
create the project, point it at a ticket source, give its agents their credentials, create its `factorial` config branch,
snapshot its `.factorial/` config,
set its policy, create the labels, and switch admission on.

Two styles, kept apart as `docs/writing-rules.md` permits. **[Procedure]** sections are steps to
follow. **[Reference]** sections give the reasons and the facts behind them; read those to decide,
not to act.

---

## What this does not do — [Reference]

Merging a `.factorial/` folder into a repository changes nothing. The folder is inert until a project
exists that names that repository and is active. The bootstrap below is the deliberate act that
turns it on, and every step of it is reversible: `PUT /active {"active": false}` stops admission
again, and tickets already in flight keep reconciling.

---

## Before you start — [Procedure]

1. The orchestrator API must run and answer.
2. You must have `FACTORIAL_API_TOKEN`. One shared secret gates every `/api` route
   (`api/auth.py`). An unset token makes the API refuse everything with a 503 — that is
   deliberate, not a fault.
3. You need a GitHub App installed on the target repository's owner, or a token that can reach
   the repository. Step 1 connects the project with one of them.
4. The repository must hold at least one process file — `.factorial/processes/process-<name>.yaml`
   — **on its `factorial` branch**. Step 1a creates that branch from the default branch's config
   folder. A repository still keeping one at `.factorial/process.yaml` is read as a single lane
   under that file's own `name:`, and keeps working; what it may not have is both.

Set three shell variables. Every command below uses them.

```bash
export SF=https://app.runfactorial.com/factorial   # or http://localhost:8000/factorial on your own machine
export TOK=<FACTORIAL_API_TOKEN>
export ORG=default                        # the organization the project belongs to
```

A deployment has one organization, `default`. A later phase adds more. A project, a
runtime and its key belong to one organization. A runtime slug in `process.yaml` names a runtime
in the project's own organization.

### Why the `factorial` branch — [Reference]

`POST /process-versions/sync` with no `ref` resolves the branch named `factorial`
(`factorial/source.py`'s `CONFIG_BRANCH`) to a concrete sha and reads the config at that sha. The
config preview reads the same branch, and the process builder commits to it. A `.factorial/`
folder that sits only on the default branch or a feature branch cannot be resolved by an unpinned
sync. You can pass an explicit `ref` (a branch, a tag, or a sha) to read from somewhere else.

Config used to live on the default branch. A config change merged there waited behind CI,
CODEOWNERS and the spec pull requests, none of which says anything about config
(`docs/tech-stack.md` decision 53). The code, CI, the spec pull requests and gate 2 stay on the
default branch; only the config moved.

---

## Step 1 — create the project — [Procedure]

```bash
curl -sS -X POST "$SF/api/orgs/$ORG/projects" \
  -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
  -d '{
        "name": "<repo>",
        "github_repo": "<owner>/<repo>",
        "description": "The ADLC target repository",
        "settings": {"mcp_allowlist": []}
      }'
```

The response carries the new `id`. Keep it:

```bash
export PID=<id from the response>
```

The project starts **inactive**. A `name` that already exists in the organization answers 409.

### Connect the project to GitHub

Every GitHub read and write of the project, and every run token, uses the project's own
connection. Without one the project is not ready, and the worker holds its tickets. Open
**Project settings → Ticket source** and connect with an App (App ID, private key, webhook
secret) or a token, or call the route:

```bash
curl -sS -X PUT "$SF/api/projects/$PID/github-connection" \
  -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
  -d @connection.json   # {"mode": "app", "app_id": "…", "private_key": "…", "webhook_secret": "…"}
```

Saving reads the App's installations and its bot login from GitHub. The secrets are stored
encrypted and never come back out. The response's `webhook_url` is where GitHub must deliver.
Set it as the URL of the App's **General → Webhook** (for a token project, the repository's
**Settings → Webhooks**), with the connection's webhook secret and content type
`application/json`, and tick **Active**. GitHub does not follow a redirect, so keep `/factorial`
in the URL. Subscribe to Issues, Issue comment, Pull request, Pull request review, Pull request
review comment, Push and Check suite (on an App, under **Permissions & events**).

---

## Step 1a — create the config branch — [Procedure]

Open the project's **Process config** page, under **Project settings**. If the repository has no
`factorial` branch, the page shows a gallery of starting points:

- **From `<default branch>`** shows when the default branch has a config folder. Press
  **Import `<default branch>`'s config**. It copies that folder unchanged and syncs it.
- **A template**, for example **Simple template**. A template names no runtime. Select one in
  **Runtime** beside **Import process template**, then press that button. The import makes the
  branch and commits each process of the template with that runtime. If the project does
  not allow the runtime yet, the button says `Permit <slug> and import`, and the import allows
  it after the commits. If the project has no synced lane, the branch is empty. If it has synced
  lanes, the branch is a copy of the default branch's config folder, so that those lanes stay at
  the next sync.

Select a card to see its processes, their agents and their skills. After the import, open a
process in the editor to change it. Or call the route:

```bash
curl -sS -X POST "$SF/api/projects/$PID/config-branch" -H "Authorization: Bearer $TOK"
curl -sS -X POST "$SF/api/projects/$PID/config-branch" -H "Authorization: Bearer $TOK" \
  -H "Content-Type: application/json" -d '{"from": "empty"}'
```

A 201 returns `{"branch": "factorial", "sha": "<sha>"}`. The commit has no parent. Without a
body, its tree holds only the config folder as it is at the default branch head. If the default
branch has no config folder, the tree holds only `.factorial/README.md`. With
`{"from": "empty"}`, the tree holds only `.factorial/README.md`, and the process builder commits
the first process to it. A 409 means the branch exists already, or a `factorial/...` branch
blocks it: delete those branches first. A 503 means the project has no GitHub connection.

Do not make the `factorial` branch by hand. Sync refuses a commit that the orchestrator did not
write and that no human merged through a pull request. If you made the branch by hand, delete it
in GitHub. Then use the gallery.

Then protect the branch. The App cannot do this, because it never holds `administration: write`.
Run this as a repository admin:

```bash
gh api -X POST repos/<owner>/<repo>/rulesets --input - <<'JSON'
{"name":"factorial-config","target":"branch","enforcement":"active","conditions":{"ref_name":{"include":["refs/heads/factorial"],"exclude":[]}},"rules":[{"type":"deletion"},{"type":"non_fast_forward"}],"bypass_actors":[{"actor_id":5,"actor_type":"RepositoryRole","bypass_mode":"always"}]}
JSON
```

The bypass is for the repository admin role (`actor_id` 5), never for the App. It lets an admin
reset the branch when its head is unknown.

Do not add a `pull_request`, `update`, `required_status_checks`, `required_deployments`,
`merge_queue` or `required_signatures` rule. The process builder commits to the branch directly,
and each of these rules stops its commits. The readiness row reports them as a gap. It shows the
same command when force-push or deletion protection is missing.

### What guards the config branch — [Reference]

The ruleset forbids force-push and deletion. It has no pull-request rule, because the App
commits to the branch. The App commits only when a signed-in operator presses **Commit and sync**
on the dashboard, and only the paths the server calculates again. That click is the approval.

Agents push as the App too, so an agent can also push to `factorial`. The commit author proves
nothing, because an agent sets it. So sync accepts a commit only if one of these holds:

- The orchestrator wrote it: a `config_branch_created` or `config_committed` event for this
  project names the sha. The orchestrator records the event before it moves the branch.
- A signed-in person adopted it: a `config_head_adopted` event names the sha (see below).
- It is the merge result of a pull request into `factorial`, the user who merged it is not a
  bot, and the first parent of the merge result is also a commit that sync accepts. A merge on
  top of an agent's push does not make that push safe. A pull request that you open by hand
  still syncs. Use a merge or a squash merge: a rebase merge of more than one commit leaves
  commits that no merge made, so sync refuses it.

Anything else answers 422 and names the sha. A direct push to `factorial` does not sync, from an
agent or from an admin, unless a person adopts it. The guard checks every ref the same way, and a re-sync of a stored sha
too. So a sync of another branch works only for a commit that one of these rules accepts.

The builder applies the same rules to the head of `factorial` before it commits. A commit on top
of an agent's push would carry that push into a recorded commit, so the builder refuses an
unknown head with 409. Readiness shows `unknown head`.

To adopt the push, press **Review and adopt** on the Process config page. The dialog lists the
commits and files between the newest known commit and the head. The commit authors are claims: an
agent sets its own. Tick that you read it, then press **Adopt and sync**. The adoption covers
exactly that head. A later direct push needs its own adoption. An MCP client cannot adopt.

To discard the push, use the command that resets the branch to the newest known commit below the
head. Only an admin with the ruleset bypass can run it. If no known commit is below the head,
there is nothing to adopt: an admin deletes the `factorial` branch and creates it again from the
configuration page.

The protection of the default branch and gate 2 do not change.

Two readiness rows report the branch. Neither is ever `blocked`, because admission reads stored
snapshots, not the branch:

- `config_branch` is `warn` when the branch is missing or its head is unknown. The page shows
  this row in the **Process config** section, beside the fixes for it.
- `config_branch_rules` is `warn` when the ruleset falls short. The page shows this row on the
  **Git source** page, under **Branch protection**, after the default branch. It is `unknown`
  until the branch exists.

Each of the two branch protection rows (`branch_protection` and `config_branch_rules`) carries a
`checks` list: one entry for each rule, with a `title`, a `description` and `passed`. A failed
check has a `remedy`, which is a command that changes only that rule, or a `link` when no command
can make the change safely.

## Step 2 — set the ticket source — [Procedure]

```bash
curl -sS -X PUT "$SF/api/projects/$PID/ticket-source" \
  -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
  -d '{"kind": "github", "external_ref": "<owner>/<repo>"}'
```

This is an upsert. One project has one ticket source.

For GitLab, send `"kind": "gitlab"` and put the `group/project` path (nested groups allowed) in `external_ref`.
The procedure is otherwise the same; see `docs/gitlab.md`.

### Ticket source against `github_repo` — [Reference]

They are separate fields because intake and artifacts can differ: the issues can live in one
repository while branches and pull requests go to another. For this project they are the same
repository.

`webhook_secret_ref` is **inert — do not rely on it.** It holds the *name* of a secret row,
never a value, and it is written by `PUT /ticket-source`, stored on the row, and surfaced in the
dashboard — and read by nothing. The webhook handler verifies against the webhook secret of the
project's GitHub connection (`api/routes/webhooks.py`). Leave it unset.

Webhooks are only a wake-up signal in any case, so getting this wrong costs latency, not
correctness — a dropped or duplicated delivery cannot corrupt state, because polling is the
authority.

---

## Step 3 — give the agents their credentials — [Procedure]

Two stores, one rule. The vendor API key belongs to the **Runtime** the process names:
`OPENAI_API_KEY` for a `codex_api` runtime, `ANTHROPIC_API_KEY` for a `claude_api` runtime (a
subscription runtime holds a login instead). A `codex_bedrock` runtime stores none — the worker
mints `AWS_BEARER_TOKEN_BEDROCK` from the host's AWS role. Every other key the manifests
declare belongs to the project.

1. Create the runtime on the dashboard's **Runtime** page (or `POST /api/orgs/$ORG/runtimes`), with the
   type the agents' manifests are written for.
2. Store its key on the Runtime page:

   ```bash
   curl -sS -X PUT "$SF/api/runtimes/$RID/credentials/ANTHROPIC_API_KEY" \
     -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
     -d '{"value": "<key>"}'
   ```

   A subscription runtime connects by device login on the same page.
3. Name the runtime in `process.yaml` (`runtime: <slug>`) and sync it (Step 4).
4. Store the project's own keys — whatever the manifests or MCP servers declare that the runtime
   does not provide, such as a `PRODUCT_DOCS_TOKEN` an MCP server references:

   ```bash
   curl -sS -X PUT "$SF/api/projects/$PID/secrets/PRODUCT_DOCS_TOKEN" \
     -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
     -d '{"value": "<token>"}'
   ```

5. Activate (Step 7).

Names only come back from either list (`GET /api/runtimes/$RID/credentials`,
`GET /api/projects/$PID/secrets`). There is no read-back endpoint: a value leaves the store
exactly once, into an agent run at spawn time.

Every process names a runtime (tech-stack decision 70), and takes the vendor key from the
runtime **alone**. A project secret of the same name is never read, and the manifest's
`secrets:` entry for it does nothing — keep or drop it. A ticket pinned to a snapshot from before
its process named a runtime cannot run: restart it on the current snapshot. To move a fleet's per-project keys onto one runtime, run
`scripts/migrate-vendor-keys.py` (dry run by default; its header says the order). It keeps a
copy that open tickets still read and says which — rerun once they close.

### How activation checks this store — [Reference]

**Skipping this step now stops you — when the check can see.** `blockers` in
`api/readiness.py` derives the declared keys from the latest process version's pinned agent
manifests (read from the repository at the pinned sha — manifests are not in the database),
discards `GH_TOKEN` and the named runtime's own key, and reports every remaining name it can
*prove* has no stored value as a blocker: `PUT /active` answers 409 naming the missing keys, and
Step 7's `blockers` list names the same ones, because both run the same probe on the same inputs.
The runtime row blocks separately when the runtime holds no key of its type. Two honest limits.
The check is best-effort, per agent: a manifest the probe cannot read — no GitHub App configured,
GitHub unreachable, agent not yet pinned — degrades to a `warnings` entry naming that agent, while
the keys from manifests that did read still count, and activation proceeds on what was proved. A
blocker is only asserted on positive evidence, never on an outage. And a stored value is not
verified to be a *working* credential; a wrong value still fails per run, at spawn. Confirm the
values by hand.

`GH_TOKEN` is deliberately absent from both stores: the orchestrator mints it per run, scoped to
the repository and to the agent's own permission request. The manifests declare it so an operator
can see what a run receives, not so anyone stores it.

---

## Filling `.factorial/` when the repo has none yet — [Procedure]

Prerequisite 4 in "Before you start" assumes a process file already sits on the `factorial` branch. The dashboard's process builder is how it gets there without anyone touching git by hand.
It reads and writes through the project (Step 1) and the config branch (Step 1a) — it needs neither
the ticket source nor the secrets above. Without the branch, the preview and the commit answer
409 and ask you to create it first. The page has no **New lane** button. To add a lane beside the
lanes that run, ask the assistant to draft it, or merge a pull request that adds its process file
to `factorial` and press **Sync config**.

1. **Draft.** On the project's **Process config** page, press **Edit config**. The editor replaces
   the page, and **Back** returns to the lanes. The editor opens on the whole
   `.factorial/` folder: the explorer on the left lists what the pinned commit holds, and the pane
   on the right opens the process file in a tab. Each file you open from the explorer gets a tab
   of its own, and closing a tab keeps its edits. **New file** and **New folder** over the
   explorer add a path under `.factorial/` that the branch does not have; a folder is written
   once a file is in it. Right-click a folder, or press the menu button on its row, to add what
   belongs in it. The menu lists it under **Add new**: on `.factorial/` that is **Agent**,
   **MCP server**, **Process** and **Skill**, then **File** and **Folder**, and inside one
   agent's folder it is a skill or an MCP server of that agent's own. The explorer asks for a
   name in a small prompt beside the place you pressed, then creates every folder and file the
   construct needs, each filled from a commented shell. An agent shell and a skill shell parse as they are. An MCP
   server shell and a process shell do not, because neither has a safe default: uncomment and
   edit the example each one carries, or the commit is refused and names the file. The same menu
   has **Delete folder**, and each file row has **Delete**. A deletion is staged like an edit,
   the row stays in the explorer struck through with **Restore**, and nothing leaves the branch
   until the commit. Deleting a process file retires that process at the sync, after a
   confirmation, and the last process cannot be deleted. **Validate** runs the commit's own
   checks on every edited, new and deleted file and lists each problem under the file it is in.
   Drag the file panel's right border to make it wider, up to two fifths of the editor; a name
   the panel cuts short shows in full when the pointer is over it.
   Choose **Simple template** under **Import** at the top of the editor, or paste an existing
   process file. On an existing lane, the template keeps the name of that lane, so the commit
   replaces it. The template is the `primary` lane of `docs/samples/factorial-simple/`, with its
   comments. The server takes the exact text, comments included. On a repo with no config yet, a
   commit of the template with every file ticked writes the sample folder, without the sample's
   `simple` lane and the `test-agent` that only that lane names. The template needs nothing set
   up outside the repository. A Runtime,
   the previews and the MCP server are examples in comments, and the MCP server file is
   `mcp/product-docs.yaml.example`, which the orchestrator does not read.
2. **Preview.** Each time the text validates, the page calls `POST /projects/{id}/config-preview`
   for you and the explorer fills in with what a commit would add. It reads
   the repo and writes nothing, and returns a real file body only for the one lane-matching
   variant of each agent the catalog actually carries a prompt set for, plus the shared skills and
   MCP servers that variant declares. **This is an honest limitation, not a feature, and it comes
   in more than one shape.** An agent whose name the catalog does not know at all contributes no
   file and no entry here at all — nothing to inspect, nothing to tick, nothing in the explorer.
   An agent the catalog does know fares no better when its only matching variant cannot be
   offered: if every variant's branch facts fail to read from your config, at least the withheld
   variant names a reason; if a variant's facts all read but its lane does not match your
   config's topology (`test-agent`, which ships only a `base-target` variant, on a stacked lane
   is a real instance of this), it counts as "offered" all the same, so it carries no reason,
   produces no row, and is exactly as invisible as the uncatalogued case. Either way, the
   comments-only `agent.yaml` such an agent will actually get is created only when the commit
   itself is assembled — step 4, "Commit and sync" — and it is never visible in this explorer.
   You find out it happened from the sync's `agent_warnings` in the answer, and you must fill
   each one in before activating the project: a step whose agent is still that comments-only
   stub fails at spawn.
3. **Inspect, select, edit.** The explorer draws both populations at once: the files the pinned
   commit already has, and the files a commit would add. Every candidate file is readable in
   full before you consent to it, ticked on by default unless the repo already owns it — a file
   the repo owns carries no tick, because the catalog never writes over it. You can still change it:
   open it and edit its own copy, and the commit rewrites it. A file that changed on the `factorial`
   branch since the pinned version was synced stays read-only until you sync. Any candidate body is
   edited in the right-hand pane; what you submit is whatever the selection and the edits say at
   the moment you press the button. Editing the process text clears the ticks, because
   consent belongs to the exact bytes it was given about. Your edits to other files stay.
4. **Commit and sync.** Press **Commit and sync**. `POST /projects/{id}/config-commits` takes the
   config text, the files still ticked with whatever bodies you left them holding, and every repo
   file you edited, and commits all of it — as one commit directly on
   `factorial`. Then it syncs that commit, the same as Step 4 below. Untick every file for an
   agent, or take none to begin with, and that agent still gets its comments-only stub, so an
   agent the config names is never simply missing from the folder.
5. **Done.** The answer gives the commit `sha`, its `url` and the `sync` result. There is no pull
   request to merge and no separate sync. The sync guard accepts the commit because a
   `config_committed` event names it.

Six answers need an action:

- **409, the branch moved.** Someone changed `factorial` after the page read it. Nothing was
  committed. Reload the page and try again.
- **409, a file you edited changed on the branch.** The answer names the file. Nothing was
  committed. Reload the page, then keep or revert the flagged edit and try again.
- **422, a runtime the lane cannot use.** Nothing was committed. Correct the `runtime` binding and
  try again.
- **422, an edit the server refuses.** The answer names the file: it is not a file on the
  `factorial` branch, it was sent twice, or it is the lane's own process file, which comes only
  from the process text. Nothing was committed. Revert that edit and try again.
- **409, `unknown head`.** Someone pushed to `factorial` directly. Nothing was committed. Press
  **Review and adopt** on the Process config page, or reset the branch.
- **201 with `sync: null`.** The commit is on `factorial`, but the sync failed; `sync_error` says
  why. Correct the cause and press **Sync**.

### A stale edit blocks the commit — [Reference]

Editing the process text after previewing does not throw away the rest of your work: a file
you never touched is silently refreshed against the new preview, because there was nothing in it
to lose. A file you *did* edit is kept regardless — never discarded, never silently trusted as
still correct — and is flagged **stale** only when a fact that file itself used has since changed:
moving the branch prefix, say, after already tuning a prompt that names it by hand. A stale file
still ticked for submission blocks "Commit and sync" until you either revert it or confirm it with
"Use anyway"; a stale file you have unticked is not this commit's problem, though the badge
stays if you tick it again later.

The gate exists because a hand-edited prompt that still names `spec/` after the config moved the
prefix to `release/` still parses as YAML and still looks finished in a diff. Left alone, it would
produce a pull request that satisfies no postcondition while CI reports green — the one failure
with no mechanical signal, because the orchestrator's loader reads the file as prose, not as
config, and has no way to notice it contradicts the lane it ships beside (design.md §7.7).

---

## Step 4 — snapshot the config — [Procedure]

```bash
curl -sS -X POST "$SF/api/projects/$PID/process-versions/sync" \
  -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
  -d '{}'
```

The response carries `sha` and one row per lane in `results`, each with its `process`, its
`outcome` (`created`, `unchanged` or `retired`) and, for the first two, the `version_id` the
snapshot routes address. Record the sha.

`unchanged` is the ordinary answer for every lane whose file that commit did not touch: this
project already holds that lane at that commit, so nothing about it moved. There is no outcome to
act on — a config edit produces a new commit, and a new commit is simply a new snapshot.

With `{}` the sync reads the `factorial` branch, and the stored `source_ref` says `factorial`.

If the answer is 422, read the detail. It names the file and the reason: a missing `.factorial/`, a
ref that does not exist (a missing `factorial` branch says so), invalid config, a repository
carrying both layouts, or a commit that the orchestrator did not write and no human merged into
`factorial` (Step 1a's reference). A 409 means a
concurrent sync; try again.

A commit that retires the lane `default_process` names does not fail: the default moves to a lane
the commit offers, and the response's `default_process_moved` names both ends of it. This is what
a rename does — the old lane is gone from the commit that renamed it — so check the default is the
lane you meant and promote another one if it is not.

### What sync does, and why it is not an upsert — [Reference]

`sync` is the only admin endpoint that talks to GitHub. It resolves the ref to one concrete sha,
reads **every** process the repo offers at that sha, validates each, stores each as a JSONB
snapshot, and pins **every agent those processes reference** at the same sha
(`factorial/registry.py`'s `register_process_versions` → `upsert_agent_rows`). One sha across all
lanes: a process and its agents move together or not at all.

A snapshot is never rewritten. A ticket pins its `process_version` at admission and must still
read the same one tomorrow; rewriting one would change the process under every ticket that pinned
it. Editing config in the repository is therefore: **Commit and sync** in the process builder,
or merge a pull request into `factorial` and then sync. The new commit is
a new snapshot, and in-flight tickets keep the one they came in on.

The same call decides which lanes are **selectable**. A key the commit no longer offers is retired
and stops being offerable; putting the file back clears that and keeps every commit it had.
Version history is not a menu, or deleting a lane's file would leave it selectable for ever.

`POST /process-versions` (no `/sync`) used to take parsed config in a request body. It is gone: a
snapshot is identified by the commit it was read from, and that route carried none — nor did it
pin any agents. Sync is the only way config reaches the orchestrator.

**A 201 here still does not mean the agents are sound — but it now says so.** `sync` validates
every process file and registers an `agent` row for each agent the process names, pinning each by
*convention* without reading the manifest. A broken `agent.yaml` — a size alias where a concrete
model id belongs, a `prompts:` path that does not exist at that sha, a declared skill with no
`SKILL.md`, an `mcp:` block against an MCP-incapable runtime — is therefore not what fails this
call.

What the response now carries is `agent_warnings`: one line per referenced agent whose manifest
does not resolve at the synced sha. The configuration page's file explorer reports the same probe,
hung on the manifest each verdict is about. Both treat **absent** and **present but unparseable**
as one verdict, which is what an unfilled scaffolded stub is — and the likelier of the two
failures, because a file that exists looks finished.

Read what that does and does not buy you. The probe proves the manifest parses and every path it
declares exists at that sha. It says nothing about whether the prompts are any good, and — being
a GitHub read — it degrades to silence rather than blocking, so it can never refuse an
activation. Warnings here do not stop the 201, and a warned manifest does not stop `PUT /active`.
Same shape of trap as the secret store above, one step less silent than it was: read the
warnings, and validate the folder on disk first with the loader command at the end of this page —
it is the same code path and it costs nothing.

---

## Step 5 — set the policy in `project.settings` — [Procedure]

Two keys are boundaries rather than knobs. Decide both explicitly.

```bash
curl -sS -X PATCH "$SF/api/projects/$PID" \
  -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
  -d '{"settings": {"mcp_allowlist": []}}'
```

`PATCH` replaces the whole `settings` object. Send every key you want to keep.

For a project with no extra needs, both decisions are "the default":

| Key | Value | Effect |
|---|---|---|
| `mcp_allowlist` | `[]` | No MCP server may run. Correct here — no manifest declares one |
| `permission_ceiling` | absent | Requests are permitted up to the shared default App profile |

### `runtime_allowlist` and `egress_allowlist` — [Reference]

A runtime whose isolation is `e2b` sends the repository's code to E2B (tech-stack decision 25),
so a project runs on one only when this operator-owned list names its slug. Host runtimes are not
gated by it, so listing an E2B runtime never refuses the project's host lanes. `egress_allowlist` adds hosts its E2B runs may reach
beyond GitHub and the model API — a package registry such as `registry.npmjs.org`, or
`*.pypi.org`. Hosts only: no scheme, no path. Both are refused, not ignored, when malformed.

```bash
curl -sS -X PATCH "$API/projects/$PROJECT_ID" -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"settings": {"mcp_allowlist": [], "runtime_allowlist": ["claude-e2b"],
                    "egress_allowlist": ["registry.npmjs.org"]}}'
```

### `mcp_allowlist` — [Reference]

An empty list, or an absent key, permits **nothing** (`factorial/mcp_policy.py`). That is the only safe
reading of silence: `.factorial/` lives in the repository that agents write to, so declaring a server
there is a *request*. This operator-owned list is the grant.

Matching is strict but not uniformly "exact" — the shades matter when you write an entry. Name and
transport must match exactly. A `stdio` entry's `command` and `args` must match exactly, because a
stdio server is arbitrary code running beside a live `GH_TOKEN`. An `http`/`sse` entry's `url` is
compared **normalised** — a trailing slash and the case of scheme and host are ignored, everything
else is not. `tools` is a **coverage** check, not equality: every pattern the manifest requests
must be covered by some pattern the allowlist permits, and an entry naming no tools permits all of
them. So the repository cannot vary what runs, but an allowlist may legitimately be broader than
what a manifest asks for.

An empty allowlist costs nothing here, because no manifest in such a project declares an
MCP server. Both CLI adapters can wire one up (`supports_mcp`), so adding a server needs only its
allowlist entry.

### `permission_ceiling` — [Reference]

Left absent on purpose. Absent means "the shared default App profile is the ceiling"
(`factorial/permission_policy.py`) — never "anything goes" and never "nothing". A manifest with no
`permissions:` key requests exactly that same default profile, so absent-on-both-sides is the
back-compatible position and is what every manifest gets by default.

Set a ceiling when a manifest starts asking for more than the default, and you want a written,
operator-owned limit on how much more. A malformed value raises rather than degrading to "no
ceiling": an operator typo must never read as permission.

`workflows` is not grantable at any level, and cannot even be written: both the manifest model and
the ceiling model are closed, `extra="forbid"` models that do not name it. CI runs agent-authored
code with repository secrets before any human sees it, so the App must never hold
`workflows: write`. The App's granted permissions are `contents:write`, `issues:write`,
`pull_requests:write`, `repository_hooks:write`, `metadata:read`, `checks:read` (for `ci_green`) and
`artifact_metadata:read` — no `workflows`. This is why a `.github/workflows/`
change and a `.factorial/` change cannot both be pushed by the factory, and why a human merge is what
lands a paired change.

---

## Step 6 — create the labels — [Procedure]

The orchestrator writes labels onto tickets, and admission reads one. Create them in the target
repository before you activate. A repository set up earlier may carry most of them, with
colours chosen by hand — create only what `gh label list --repo "$R" --search factorial` says is
missing, and do not `--force` over a label that already exists.

There are three families. The second is easy to miss because no process file names it, and the
third only exists once a project runs more than one lane.

**Named in a process file** — every `ticketLabel:` in an outcome or a gate's `waiting:`, plus the
one `entry.when` reads. Unioned across every lane the project offers: a label one lane projects is
a label the repository has to define, whichever lane a given ticket happens to run.

```bash
R=<owner>/<repo>
gh label create factorial:new             --repo "$R" --color 0e8a16 --description "Admit this issue to Factorial"
gh label create factorial:spec-review     --repo "$R" --color fbca04 --description "Waiting: gate 1, spec review"
gh label create factorial:spec-rework     --repo "$R" --color fbca04 --description "Spec rejected; plan agent revising"
gh label create factorial:ready-for-build --repo "$R" --color 1d76db --description "Spec approved; build may start"
gh label create factorial:testing         --repo "$R" --color 1d76db --description "Implementation open; test agent running"
gh label create factorial:code-review     --repo "$R" --color fbca04 --description "Waiting: code review"
gh label create factorial:build-rework    --repo "$R" --color fbca04 --description "Implementation rejected; build agent revising"
gh label create factorial:spec-merge      --repo "$R" --color fbca04 --description "Waiting: gate 2, merge spec and implementation to main"
gh label create factorial:done            --repo "$R" --color 5319e7 --description "Factorial finished"
gh label create factorial:blocked         --repo "$R" --color b60205 --description "Stopped on a failure. Fix it, then remove this label to retry."
gh label create factorial:interrupted     --repo "$R" --color d93f0b --description "A person stopped the run. Remove this label to resume it."
gh label create factorial:waiting         --repo "$R" --color d93f0b --description "Agent waiting for an answer. Remove this label or press Resume to continue."
gh label create factorial:exit            --repo "$R" --color 6e7781 --description "Left Factorial. Remove this label to bring it back."
```

Three labels do something on REMOVAL. `factorial:interrupted`: the orchestrator parks a run a human
stopped from the dashboard, and removing the label resumes that agent's own session in the
workspace the interrupt kept (tech-stack.md decision 26). Comments left on the ticket in the
meantime become the resumed agent's next instruction. `factorial:waiting`: a
session that sat idle and unanswered parks with it, and removing it — or the dashboard's Resume —
resumes the session the same way (decision 56). While the run is still idle and not yet parked,
removing it does nothing: the projector writes it back. A session that reached its wall-clock
ceiling also parks under `factorial:interrupted`, but only the dashboard's Resume releases it:
removing the label there gets one comment and the label back (decision 56). `factorial:exit`: the orchestrator parks a
ticket a human sent away — from the dashboard, by adding this label, or by closing the issue
— and removing it, with the issue open, grants a fresh attempt at the step it left (decision
29). Create all three even if you never expect to press stop or exit — without them the projector
has nothing to write, and a parked ticket cannot be released.

**Derived from step names, and never written in config** — `factorial:<step>`, spelled exactly as the
step is spelled, underscores included:

```bash
# Steady-state: what a ticket wears while an agent or check step runs and no outcome has
# fired.
gh label create factorial:plan              --repo "$R" --color 1d76db --description "Plan agent running"
gh label create factorial:build             --repo "$R" --color 1d76db --description "Build agent running"
gh label create factorial:test              --repo "$R" --color 1d76db --description "Test agent running"
gh label create factorial:validate_ci       --repo "$R" --color 1d76db --description "Waiting for CI on the impl PR"
gh label create factorial:validate_merge_ci --repo "$R" --color 1d76db --description "Waiting for CI on the merged branch"

# Blocked-at: projected ALONGSIDE factorial:blocked, naming the step the ticket is stuck on.
gh label create factorial:spec_review_gate --repo "$R" --color b60205 --description "Blocked at spec review"
gh label create factorial:code_review_gate --repo "$R" --color b60205 --description "Blocked at code review"
gh label create factorial:pr_merge_gate    --repo "$R" --color b60205 --description "Blocked at the final merge"
```

The dashboard does this too: the project's **Tickets** settings page reports which of
these the repo lacks, and **Create missing labels** creates exactly those. It is additive —
a label the repo already defines is never touched, so a colour you chose deliberately survives.
Colours and descriptions are derived from each label's role in the pinned process files, which
is why the `factorial:<step>` family comes out right for any process, not just this one.

Add `--force` to overwrite an existing label rather than fail. Verify:

```bash
gh label list --repo "$R" --search factorial
```

### Where the second family comes from — [Reference]

`worker/projection._desired_labels` computes the whole label set every tick and `set_labels`
replaces the issue's labels with it. Two of its branches produce names no `ticketLabel:` ever
mentions:

- `_steady_label` falls through to `step_label(step.name)` when a step is running and no outcome
  fired this tick — so `factorial:plan`, `factorial:build`, `factorial:test`. Gate steps do not appear here as
  long as every gate has a `waiting.ticketLabel`, which is why the hyphenated `factorial:spec-review`
  and not `factorial:spec_review_gate` is what a ticket wears while waiting.
- The blocked branch adds `step_label(_status_step(tick))` beside `factorial:blocked` — and
  `_status_step` returns `ticket.current_step`, the **raw step name**. That is where the
  underscores come from. A gate-2 rejection therefore projects `factorial:pr_merge_gate` +
  `factorial:blocked`, which is exactly the path this lane's no-`onReject` gate 2 produces.

Names are mechanical, so a step renamed in a process file silently needs a new label.

### The third family: choosing a lane — [Reference]

A project reads one more label per lane, `factorial:process:<name>`, and an issue carrying it
enters that lane instead of the project's default. One is expected for every process the project
offers, so the **Tickets** settings page lists them and **Create missing labels** creates them alongside
the rest:

```bash
gh label create factorial:process:hotfix --repo "$R" --color d4c5f9 --description "Run this issue through the hotfix process"
```

This is the one family the orchestrator does not project — a human puts it on an issue and the
loop reads it. It is still expected in the repo, because GitHub's label picker offers only labels
that already exist: a lane whose selection label is missing cannot be chosen on an issue at all.
Once read they are spent input, swept off the issue on the first tick after admission, exactly as
`factorial:new` is.

An issue carrying an unknown process label, or two of them, is not admitted and does not fall back
to the default. The Tickets page lists every declined issue with its reason.

### Do they have to exist first? — [Reference]

**Unverified, and the runbook does not depend on the answer.** GitHub's set-labels endpoint is
widely believed to create a label that does not yet exist, with a default colour and no
description. Confirming either way means writing a label onto a live issue for a documentation
footnote; it has not been done.

So create all twenty anyway, as hygiene rather than as a fix for a known failure. Two reasons
that hold whichever way the endpoint behaves: you get the colours and descriptions above instead
of whatever a default would be, and the dashboard is legible from the first ticket rather than
after one has already passed through. If auto-creation turns out not to happen, this step is also
what stops the projection failing — but do not treat that as the established reason.

Two labels genuinely are load-bearing regardless. **`factorial:new` is the admission trigger** —
`entry.when` requires it plus an assignee, and a ticket cannot be labelled with it by a human who
cannot find it, so without it the pipeline looks dead rather than broken. **`factorial:blocked` is the
escalation surface**, and removing it by hand is the documented un-block gesture.

The colours are a convention, not a control: green to admit, blue while an agent is working,
yellow while a human is the bottleneck, red for blocked or stuck, purple for done.

---

## Step 7 — check readiness, then activate — [Procedure]

Read the project back first:

```bash
curl -sS "$SF/api/projects/$PID" -H "Authorization: Bearer $TOK"
```

Look at `blockers` — an empty list means activation will succeed — and read `warnings` too: it
carries the advisory findings that do not gate activation but do predict later friction. A
manifest the secrets probe could not open is one of them.

`readiness` carries the same facts as one row per requirement, including the requirements that
pass, which is what the dashboard renders as a checklist. This payload answers from the database
alone. Its keys are `ticket_source`, `process_config`, `secrets` and `runtime`.

The rows that read the repository live answer on their own route. Each one costs a GitHub round
trip, and none of them can gate an activation. So the dashboard draws the page first and fills
them in:

```bash
curl -sS "$SF/api/projects/$PID/readiness" -H "Authorization: Bearer $TOK"
```

It carries the same two fields for the keys `app_access`, `config_branch`,
`config_branch_rules`, `labels`, `branch_protection`, `ci_workflows` and `gate_approvers`.
`branch_protection` is about the default branch; `gate_approvers` appears when a merging gate sets
`approverPermission`, and warns when the branch it merges into also requires a code-owner review;
`config_branch` reports the `factorial` branch, and `config_branch_rules` its ruleset (Step 1a). Every row on both routes has a `state`:

| State | Meaning |
|---|---|
| `ok` | The requirement is met. |
| `blocked` | On the database route, activation fails until you fix this, and `blockers` is exactly these rows. On the live route, only `app_access` can be `blocked`: no run can work, but activation is not refused. |
| `warn` | A real defect that provably does not stop admission. |
| `unknown` | Nobody could check it — the App is unconfigured, unreachable, or lacks the permission. |

`unknown` is not a quiet `blocked`. Every repo-side probe degrades rather than failing, so a
GitHub outage lands there. Treating it as a defect would send you to fix working configuration.

`app_access` comes first because the other repo-side rows depend on it. It reads the App's
installation and then tests whether this project's repository is inside it, so "the App is not
installed" and "the App is installed and this repo was not selected" stop looking identical.
When the repository is outside the installation, the row is `blocked`, and each other repo-side
row is `unknown` with the summary "not checked: the App cannot see `<repo>`". The dashboard
shows the error on the project's Project details page.

To confirm a permission change landed, read the row it applies to rather than this one.
GitHub queues an added permission as a request the account owner must accept, and until they do
the App keeps running on the old set with no error anywhere — so `branch_protection` staying at
"not checked" after you grant `administration: read` means the request has not been accepted
yet.

```bash
curl -sS -X PUT "$SF/api/projects/$PID/active" \
  -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
  -d '{"active": true}'
```

A 409 lists exactly what is missing. Fix it and repeat.

To stop admission again:

```bash
curl -sS -X PUT "$SF/api/projects/$PID/active" \
  -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
  -d '{"active": false}'
```

### Why `blockers` and the activation check are one function — [Reference]

Both read `blockers` in `api/readiness.py`. A page that offers a button the server then
refuses is worse than no button, so the readiness shown and the check enforced agree whenever
they see the same world — the same probe runs on the same inputs in both places, and only
availability changing between two requests can make them differ. Each entry mirrors something
the worker genuinely needs: admission joins on a ticket source, admitting a project gives up
with a warning when there is no process version, and every spawn resolves the declared secrets
— the Step 3 check.

Note what `blockers` still does **not** cover. Label existence (step 6) is advisory only — a
`warnings` entry on `GET /projects/$PID/readiness`, never a blocker, because whether a missing
label breaks anything is unverified (step 6's own reference), and an unproven failure must not
refuse an activation. Sound agent manifests (step 4) get no check of their own; a manifest the
secrets probe cannot load surfaces only as a "declared secrets not checked" warning naming that
agent. And whatever part of a check could not run — no GitHub App, GitHub unreachable, an agent
unpinned — degrades to a warning rather than a blocker, so an outage never bricks readiness, at
the price that activation then rests only on the manifests the probe could still read. An empty
`blockers` list still means "the worker will read this project", not "this project will work".
Walk the steps rather than trusting the empty list.

Activating a project that is not fully configured is a 409 rather than a silent no-op, because
`active = true` that admits nothing is the most confusing state the system can be in.
Deactivating is always allowed and stops admission only — dropping a ticket mid-process would
strand its branch and its pull request.

---

## Step 8 — run the worker — [Procedure]

The worker acts live. It spawns agents, mints a `GH_TOKEN` for each run, and writes labels,
comments and merges on the repository. The observer modes that ran without writes are retired
(`docs/tech-stack.md` decision 39).

### Prerequisites

- Docker running, and the local image built (`make image`), for `make db`.
- The project's GitHub connection from Step 1. `.env` holds no GitHub setting.
- The `.factorial/` lane already on the target repo's `factorial` branch, and synced.
- The labels from Step 6, created in the target repo. Admission reads `factorial:new`
  (`entry.when`), so the worker does not admit a ticket without it.

### The sequence

```bash
# 1. Postgres, from the repo root.
make db                    # the compose `database` container alone

# 2. Migrate. On the host this is a manual step; the single container (tech-stack decision 52)
#    runs it at every start instead.
uv run alembic upgrade head

# 3. Bootstrap the project: Steps 1–7 above.

# 4. Start the worker.
uv run python -m orchestrator.worker

# 5. Optional: the API too, so the events feed is queryable.
uv run uvicorn orchestrator.api.serve:app --reload
```

`FACTORIAL_DATABASE_URL`'s own default (`postgresql+asyncpg://factory:factory@localhost:5432/factory`)
already matches the `database` container's published `127.0.0.1:5432` — nothing to override when
the worker runs on the host rather than inside compose. `make up` uses the same database, so run
`make down` first.

### The `idle_in_transaction` requirement — [Reference]

`WorkerSettings.lease_seconds` (`settings.py:78-84`) documents why: a tick holds an **open**
Postgres transaction across the entire blocking spawn window, so a database-side
`idle_in_transaction_session_timeout` set below `FACTORIAL_WORKER__LEASE_SECONDS` (3600 by default) will
kill a tick mid-spawn, roll back its `RUNNING` `step_run` row, and leave the next tick spawning a
second run against a container that is still going. **`docker-compose.yml`'s `database` needs no change**: Postgres ships that
timeout disabled (`0`) by default, and nothing in this repo's compose file sets it, so the
requirement is already satisfied for the run described here. A managed Postgres (for
example RDS) must set it to `0` in its parameter group, as must any other self-managed Postgres.

### Poll-only on a host, on purpose — [Reference]

There is no webhook forwarding step here, and none is needed. `worker/loop.py`'s docstring: *"The
**interval tick is the guarantee**; the `LISTEN` connection only shortens latency."* A wake only
says where to look, and the pass it starts is one the timer would have run anyway (tech-stack
decision 4). The dashboard's refresh button is the same wake for one ticket. A worker that never
receives a single webhook still converges, on `FACTORIAL_WORKER__POLL_INTERVAL_SECONDS`'s own
interval (30s by default) — it is only slower to notice a change than the deployment, whose
webhooks wake it. No local tunnel is required for this. GitHub delivers to
`/factorial/webhooks/github/apps/<app_id>` (App) or `/factorial/webhooks/github/projects/<project_id>`
(token) — mounted outside `/api` and its auth, since GitHub, not this operator, is the caller —
and a delivery no project's webhook secret verifies is refused (`api/routes/webhooks.py`). A
refused webhook costs nothing, because nothing here depends on one arriving.

### Stopping it — [Reference]

`Ctrl-C` (SIGINT) is enough: `run_forever` (`worker/loop.py`) finishes whatever tick is in flight
— which releases its own leases — before it stops. To stop **admission** without stopping a worker
process, `PUT /api/projects/$PID/active {"active": false}` (Step 7, above) works exactly as it does
for a live project: it is always allowed and stops new tickets entering, but "tickets already in
flight keep reconciling" — the worker keeps driving the tickets it already admitted until they
finish or the process itself stops.

---

## Two org-side facts CI depends on — [Reference]

CI is the orchestrator's acceptance oracle: the `verify` workflow must run, or every lane wedges
at its first CI-gated postcondition. Two things outside this repository decide whether it can,
and both were found the hard way on a live lane.

- **Actions billing must be healthy.** A billing-frozen organisation still produces a check run:
  `conclusion: failure`, zero steps run, and a billing annotation ("job was not started … payments
  failed / spending limit"). So `ci_green` reads **false**, not absent, and the red check looks
  like a bad implementation until someone reads the annotation. The fix is in the organisation's
  Billing & plans, then a re-run of the check.
- **The App holds Checks: Read-only, and the installation has accepted it.** Without the
  permission, the check-run read behind `ci_green` answers **403**, so the predicate can never
  pass however green the run actually is. Granting is two acts — the App owner adds the
  permission, the installation accepts it — and both halves must have happened. Read-only is the
  ceiling on purpose: write would let an agent's token forge check runs and defeat the predicate.

---

## Live facts — [Reference]

Some facts the code depends on only a live system can prove: the CLI event streams and GitHub's
reply shapes. Re-verify them after a CLI upgrade or a GitHub change by re-running
`scripts/smoke/claude-cli-smoke.py` and `scripts/smoke/codex-cli-smoke.py`.

## `.factorial/` facts worth knowing before you author one — [Reference]

Found while writing a first `.factorial/` folder. Each cost a rewrite or nearly did.

**The process must agree with the target repo's CI, not with the sample.**
`docs/samples/factorial-simple/` merges the spec pull request to `main` at gate 1 and then targets
implementation pull requests at `main`. A target repository's `verify.yml` resolves which
checks to run from `case "$base" in spec/*)`.

Trace what a mis-targeted implementation pull request actually does there, because the intuitive
reading is wrong in the direction that matters. The workflow's `branches:` filter applies to the
**base**, and `main` is in the list, so the run triggers. The base is not `spec/*`, and the head
is not `spec/*` either, so it resolves to `mode=spec`. Install, the repository suite and the
reward-hack detectors run; the acceptance-diff guard, the spec-has-tests check and the acceptance
suite are skipped. One step does notice something — "Test files changed" flags edits to test
infrastructure — but it only emits a warning and gates nothing. **The check reports green**, and
`ci_green` accepts it.

So on a stacked lane, CI is not the defence. The process config is: `build`'s postcondition pins
the base to `spec/{ticket}-*`, and the code gate's `pr` block matches the same base, so a pull
request opened against `main` satisfies neither and the step never completes. CI's `spec/*` arm
makes the guards *run* once the base is right. Read the target's workflows before copying the
sample's graph, and read them for which checks are *skipped*, not only for which fail.

**On this lane the spec pull request itself runs in `mode=spec`.** Its base is `main` and its head
is `spec/*`, so — absent a `contract/<slug>` tag, which this lane never produces — the
acceptance-diff guard does not apply to it. That guard covers the implementation pull request
only. Nothing mechanically stops a plan agent from writing into `acceptance/`; that prohibition
stays prompt-level, in all three prompts and the shared constitution.

**A stacked lane forces `gate.onApprove.merge: false` at gate 1.** If the implementation pull
request's base is the spec branch, merging that branch at gate 1 deletes the base the next step
must target. The spec pull request instead stays open for the whole ticket, the implementation
merges into it at the code gate, and a second `pr_approval` gate carries both halves to `main`.

**A base-target lane has no acceptance-diff guard.** Every starter lane is base-target: both pull
requests target `main`. The defence is the gate: each pull request merges only after a qualifying
person approves it there. Branch protection is a second control only where the repo has it.
Readiness reports it and does not enforce it. On an App project, the App holds no bypass.

**`pr_approval` is the only gate type.** Any merge a human must approve is expressed as a gate on
a pull request, or it is not expressed. Ordering between gates is expressed by the graph, not by
a condition: "the merged implementation is in the spec pull request before gate 2" holds because
the code gate's merge is the only edge into gate 2, and there is no predicate that could state
it.

**A step that names `request_approval()` asks and waits.** Its run parks under
`factorial:waiting` whenever the agent stops with its postconditions unmet, so the prompt need not
make the agent ask a question: it tells the agent to push its work, say it is ready for review,
and stop, then to answer a person's change request in the same session and push again. The
approval accepts the run and merges nothing; a gate still carries the work to `main`
(process-yaml.md, "Waiting for a person's approval"; design.md §7.20).

**Predicate arguments glob on both sides.** `pr_open`'s base is globbed symmetrically with its
head (`predicates/library.py`), so `pr_open("impl/{ticket}-*", "spec/{ticket}-*")` matches
whatever slug the plan agent chose. Artifact derivation drops the base, so a wrong-base pull
request is caught one tick later by the postcondition rather than at delivery.

**Do not put `{slug}` in `workspace.base`.** `{slug}` resolves from observed artifacts and
deliberately reverts to literal when two distinct slugs are on record. The CLI adapters check
`base` out as the worktree start point, so a reverted `{slug}` makes `base` a ref that does not
exist. The adapter proves that before it builds the worktree — neither `origin/<base>` nor
`<base>` resolves to a commit after a fetch — and raises `SpawnError(reason=WORKSPACE)`, which
the orchestrator escalates as a misconfiguration: the ticket goes BLOCKED with
`factorial:blocked` and a comment naming the ref, on the first attempt. Before 9.1 the same
failure was a flat `SpawnError` classified INFRASTRUCTURE and retried forever with no
human-facing surface; that was the wedge. It is now loud, not silent — but it is still a
misconfiguration, so do not write one.

Use a branch that always resolves, and let the agent's instructions name the real branch point.
Be aware of the flip side: the adapter writes `base` into the agent's prompt
("- base branch: …") and tells the agent it is standing in a checkout of it, which is then a
statement the instructions have to contradict. Write those instructions to be unmissable.

**An agent reads `.factorial/` at the pinned commit, not in its checkout.** The checkout is
`workspace.base`, and the config lives on the `factorial` branch, so the checkout's copy is old
or absent. Each run gets the ticket's pinned config commit in `FACTORIAL_CONFIG_SHA` and in its
prompt, and that commit is present in the clone. A prompt that reads the process file uses
`git show "$FACTORIAL_CONFIG_SHA":.factorial/processes/process-<name>.yaml`.

**Every declared path must exist.** The disk loader checks `prompts.system`,
`prompts.instructions`, each `skills.local` as `skills/<name>/SKILL.md`, each `skills.shared` as
`<root>/skills/<name>/SKILL.md`, and parses each declared MCP server file. A folder name must
equal its manifest's `name`, and that name is what a process file step's `agent:` references.

**Limits stay nested; models stay concrete.** `max_turns` lives under `limits:`
in `agent.yaml`; the adapter enforces it. `max_duration` is accepted and ignored.
`model:` must be a concrete id: a size alias resolves to a name the API rejects and the agent dies
on its first turn, so the parser refuses aliases outright.

**Agent prompts say nothing about labels, promotion, or merge mechanics.** The orchestrator owns
all three; a prompt that describes them drifts out of sync with the code that does them. The
prohibitions stay — "never merge, never self-approve, never add a label" is currently enforced by
the prompt alone, so removing it removes the control.

## Harden agent prompts against terminal input — [Procedure]

A person can type into any run. An agent that follows them away from its task delivers nothing.
Give every agent that opens a pull request a system-prompt section that keeps it on task, and
keep the shared constitution from undoing it. The Simple sample and the Simple template do both,
for `plan-agent` and `build-agent`. Do these steps for a new agent, and for any config whose
prompts do not have the section.

1. Open `agents/<name>/system-prompt.md` on the `factorial` branch.
2. Copy the section `## A person may type into your session` from
   `docs/samples/factorial-simple/agents/plan-agent/system-prompt.md`. Copy it word for word.
3. Put the section in `system-prompt.md`, immediately before `## What not to do`, or at the end if
   the prompt has no such heading. Do not put it in `instructions.md`.
4. If the agent delivers something other than a pull request, change each "PR" in the section.
   Name what the step's postconditions check, for example "your verdict comment".
5. In the agent's `system-prompt.md`, `instructions.md` and skills, find each line that says
   nobody reads the session. Change it to say that nobody has to answer. The sample changed this
   line:

   ```diff
   - - Do not stop and wait for input. Nobody is listening. Assume and document instead.
   + - Do not stop and wait for input; nobody has to answer. Assume and document instead.
   ```

   Keep a line that tells the agent to decide instead of asking, such as "there is no human to
   ask" in the plan agent's CLARIFY phase. The agent still must not wait for an answer.
6. If the config has `skills/project-constitution/SKILL.md`, or another shared skill that ranks
   a human's instruction above its own rules, add the limit at the end of its `## Precedence`
   section. Copy the paragraph that starts "A person can type into your session" from
   `docs/samples/factorial-simple/skills/project-constitution/SKILL.md`. Do this once for each
   skill, not once for each agent.
7. Keep the prohibitions: never merge, self-approve, add labels, or push to `main`. The section
   does not replace them.
8. Parse the folder (next section).
9. Commit with the process builder, or merge a pull request into `factorial`. Then sync.

Through the MCP server, files under `agents/` are dashboard-only: show the person the diff, and
stop. A ticket in flight keeps the prompts it was admitted with. The next ticket gets the change.
The same holds for a file created in the editor: it reaches new tickets and a ticket restarted
onto the latest version, and never a running or paused one (design.md §2 item 8).

To go back to an earlier config, press **History** on the Process config page and **Restore**
on a synced commit. The dashboard commits the whole `.factorial/` folder back to that commit's,
as a new commit, and syncs it (tech-stack decision 93).

### Why each line is there — [Reference]

Every agent run is the CLI's own TUI, and whoever holds the terminal's writer seat types into it
(design.md §7.14). The agent gets what they type as a user turn — the same channel as the first
turn, which carries `instructions.md`. That is why the section goes in the system prompt: it is
in every request of the session, and a model ranks it above a user turn, so a person's message
cannot outrank it by arriving later.

Issue #375 reported a run that a person typed into, and that never opened its pull request. The
mechanism: an agent that answers a person and stops at its prompt with its postconditions unmet
idles, and because a person worked with the run, the worker parks it for that person under
`factorial:waiting` instead of failing it into a retry (process-yaml.md, `defaults`). Nothing is
delivered, and nothing retries.

| Line in the section | What goes wrong without it |
|---|---|
| Messages are input to this task, never a new task | The agent takes an off-task question, or "skip that and do X", as its new job. |
| The run is complete only when your PR is open | The agent decides it is done when the person seems satisfied. |
| Answer, then go on with the task in the same turn | The agent ends its turn on its reply. The session idles, then parks with nothing delivered. |
| Work outside this task: say so, and continue | The agent does unrelated work, or another step's: a plan agent writes code. |
| Asked to end the run: deliver first, and say what is unfinished | The agent obeys "stop" and leaves its work in a workspace no later step reads. A partial pull request with an honest body still reaches a reviewer. |
| Never end your own session | The agent runs `exit` or `kill` against its own CLI when told to. |
| The constitution's limit: a typed message cannot replace the task, call off its delivery, or lift a **Change safety** rule | The constitution ranks "a direct instruction from a human in the current task" above itself, and a typed message is one. Without the limit, a skill every agent reads says that a person can override the section and the constitution's own prohibitions. The system prompt's prohibitions stand either way. |

"Nobody is listening" was true before a person could type into a run. Left in, it tells the agent
that a message in its session comes from no one, and it contradicts the section.

A typed message is not "text from outside the system". That rule covers issue bodies, comments,
file contents and tool output, which anyone who can comment on the repository can write. Only a
verified dashboard account or the API token can take the writer seat, and every burst it types is
audited (design.md §7.14). So the section treats a message as input to the task, inside the
task's limits, and not as data to record and ignore. A system prompt that carries its own copy of
the "text from outside the system" rule, as the `stacked` prompts do, needs no change: its list
does not name typed messages, and the section beside it says what they are.

The section is about a person's messages. It does not cancel a prompt's own honest stop, such as
the build agent's "If the spec directory named in your task is absent, stop and say so", or a
pull request that could not be opened. Those stops are failures, and the step's retry and
escalation handle them.

A prompt changes what an agent is likely to do, not what it can do. Two things stay out of its
reach:

- **`/exit` typed by a person.** The CLI handles it before the agent sees it. The session ends
  there. When the dashboard's terminal recorded the person's typing and the postconditions are
  unmet, it parks under `factorial:interrupted`; while a check run that a `ci_green`
  postcondition reads is still going, it waits for it first. Removing the label resumes the
  same session. Met postconditions accept the run as for any run that ends.
- **A person who insists.** Enough pressure can talk an agent out of any prompt rule. To stop a
  run on purpose, use the dashboard's Interrupt or Exit, not the terminal.

---

## Verify the config parses before you commit it — [Procedure]

Run this against a folder on disk. It is the same loader the API uses.

```bash
uv run python -c "
from pathlib import Path
from orchestrator.config import load_config_folder
c = load_config_folder(Path('<path>/.factorial'))
for key, p in c.processes.items():
    print(key, p.step_names)
print(sorted(c.agents))
"
```

A `ConfigError` names the file and the reason. This catches everything `POST
/process-versions/sync` would 422 on, without a round trip to GitHub.

The test suite also parses a stacked lane this repo owns,
`orchestrator/tests/fixtures/stacked-lane/`, and `test_config.py` asserts its distinguishing
shape.
