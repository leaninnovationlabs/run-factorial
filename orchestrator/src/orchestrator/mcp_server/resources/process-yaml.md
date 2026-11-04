# Writing a process file

**Status:** reference

This page is the field-by-field reference for a process file, and the rules that decide whether the file you wrote does what you meant.

---

## Three facts that explain every rule below [Reference]

**The orchestrator owns the state; this file is the desired behavior.** A worker tick observes
GitHub, evaluates this config against what it saw, acts, and projects the result back onto the
issue. Nothing here commands GitHub directly. A step advances because its postconditions hold in
observed state, never because an agent said it finished.

**Conditions are executable, not prose.** Every `when`, `preconditions` and `postconditions` is a
composition of a closed predicate library implemented in orchestrator code. Config may reference a
predicate; it may not define one. An unknown name fails at parse time with the whole allowed list
in the error.

**A ticket pins this file at admission.** The parsed config is snapshotted into
`process_version.content` as JSONB and governs that ticket for its whole life, and the COMMIT it
was read from is what identifies it. Editing the file in the repo and syncing creates a new
snapshot at the new commit. It never reaches a ticket already in flight. Every "fix it in
config" remedy therefore applies to the *next* ticket.

There is no `version:` field to maintain. It used to be an integer an author bumped by hand, and
it decided nothing a commit does not decide better: a sha is immutable, already recorded, and
already what the agent pins freeze against. A file that still carries `version:` parses. The key
is accepted and never read, and nothing shows it or acts on it.

---

## Where the file lives, and what its name means [Reference]

One process per file, at `.factorial/processes/process-<name>.yaml` on the repo's protected
`factorial` branch, not its default branch. A sync with no ref reads that branch. A config
change reaches it by the process builder's **Commit and sync**, or by a pull request (PR) that a
person merges. Sync refuses any other commit, unless a person adopts a direct push on the
dashboard (see [What guards the branch](https://www.runfactorial.com/docs/connect-a-repository/#branch-guard)):

```
.factorial/
  processes/
    process-standard.yaml
    process-hotfix.yaml
  agents/<name>/agent.yaml
  mcp/<name>.yaml
```

**The stem minus its `process-` prefix IS the process name**, and must equal the file's own
`name:`, the same rule that `agents/<name>/agent.yaml` follows. So `processes/process-hotfix.yaml`
declares `name: hotfix`, and an issue enters it by carrying the label `factorial:process:hotfix`.

The name is a slug: lowercase letters, digits and dashes, starting and ending with a letter or
digit. `default` is refused, because the default process is a property of the project (below) and
a file claiming that name would put a second, contradictory answer in the repo. Anything in
`processes/` that is not `process-*.yaml` is refused, not skipped. Otherwise a filename typo
would silently drop a lane that you think you added.

A project may have as many lanes as it likes. Each carries its own history of commits, and one
sync writes every lane at the same commit. So `(project, process, commit)` identifies a
snapshot, and a snapshot is addressed by its row ID wherever the API needs one value.

**The legacy layout still works.** A repo keeping one process at `.factorial/process.yaml` is read
forever, under whatever `name:` it carries. The slug rule above binds new files only.
What a repo may NOT have is both: a repo carrying the root file *and* a `processes/` folder is
refused at sync, naming the file to delete. The config editor and the process builder's commit write only
`processes/`, so they refuse **any** edit to a repo still on the root file, and tell you the one
`git mv` that fixes it.

---

## Which issues enter which process [Reference]

Selection happens **before** any predicate in this file runs:

| Issue labels | Outcome |
|---|---|
| no `factorial:process:` label | the project's `default_process` setting |
| `factorial:process:hotfix` | `hotfix`, overriding the default |
| `factorial:process:hotfix` + `factorial:process:docs` | not admitted; two labels select nothing |
| a label naming a process the repo does not offer | not admitted; **never** a fall back to the default |
| no label, and no default configured | not admitted; every issue must then name its lane |

Only then is the selected lane's `entry.when` evaluated. If it is false the issue waits for
readiness. It does not try another lane.

**`entry.when` is readiness, never lane selection.** Do not write predicates that try to keep two
lanes apart: identical entry conditions across lanes are valid and expected, because only the
selected lane's is ever consulted. Writing disambiguating predicates instead makes mutual
exclusivity your obligation, and it fails silently the day somebody adds a third lane.

The default lives in `project.settings.default_process`, set from the Process config page, and never
in this repo. `.factorial/` sits in the repo agents write to, so a default here
would let an agent redirect every future ticket by renaming a file.

A lane is selectable only while the last sync still found its file. Deleting it retires the lane;
tickets already in it are untouched, and putting the file back offers the lane again with every
commit it already had.

---

## The skeleton [Reference]

Seven top-level keys are required. Order does not matter. This file parses:

```yaml
specVersion: factorial/v1     # the schema version; `adlc/v1` also parses, for pinned configs
kind: Process                 # this document's type
name: my-process              # the lane; must equal the file stem minus `process-`

entry:                        # which issues are admitted, and where they start
  when: ticket_label("factorial:new")
  step: plan

steps:                        # at least one; an empty list is refused
  - name: plan
    kind: agent
    agent: plan-agent
    next: done
    workspace: { repo: "{repo}", base: main, branch: "spec/{ticket}-{slug}" }

terminal:                     # the virtual step a ticket ends on
  step: done
```

Five more are optional:

- `description`: free text.
- `roles`: named lists referenced by `assign` and `approvers`.
- `defaults`: timeout, retry and escalation for every agent step.
- `policy`: what this process expects of the repository (see below).
- `runtime`: which operator-defined Runtime the agents run on (see below).

**Unknown keys are fatal, everywhere.** Every model sets `extra="forbid"`, because a typo in a file that governs a ticket for its whole life must fail loudly at admission rather than silently do nothing. `on_success` where you meant `onSuccess` is a parse error, not a default.

**Keys are camelCase in this file.** `ticketLabel`, `maxAttempts`, `onSuccess`, `onFailure`, `onApprove`, `onReject`, `rerunStep`, `closeTicket`, `specVersion`. `agent.yaml` and `mcp/*.yaml` are snake_case, on purpose (a different base class). `specVersion` is the one key spelled the same way in all three.

---

## `entry`: admission [Reference]

```yaml
entry:
  when:
    all:
      - ticket_label("factorial:new")
      - ticket_has_assignee()
  step: plan
```

`when` is evaluated against each open issue that selected THIS process (see above). An issue that satisfies it is admitted, pins this process's current snapshot, and starts at `entry.step`.

Only three template values exist at admission (`{ticket}`, `{repo}` and `{ticket_owner}`), because no ticket row exists yet. `{ticket_owner}` is read off the issue's first assignee here and then recorded, which is what makes it stable for the rest of the ticket's life. There is no `{slug}` and no `{pr_url}` at admission, so do not write one into `entry.when`.

`entry.step` must name a step in `steps[]`. Admission is idempotent: a finished ticket is never re-admitted.

---

## `steps[]` [Reference]

Four kinds, discriminated by `kind`. Each has `name` (unique), `kind` and `next`. These are the only required keys.

**Step names become labels.** When no outcome fires on a tick, the orchestrator projects `factorial:<step name>` as the ticket's steady-state label. So a step name must be label-safe, and it is what a person reads on the issue. A gate whose `gate.waiting.ticketLabel` is set shows that instead while it waits. Every label this file can name must exist in the target repo (see [Create the labels](https://www.runfactorial.com/docs/connect-a-repository/#labels)).

### `kind: agent`

Spawns an agent on a runtime adapter and accepts its work against postconditions.

| Key | Required | Meaning |
|---|---|---|
| `name` | yes | Unique within the file. Also the `factorial:<name>` steady-state label. |
| `kind` | yes | `agent` |
| `description` | no | Free text. |
| `agent` | yes | A folder name under `.factorial/agents/`. The loader fails if no manifest matches. |
| `next` | yes | The step to promote to on success. May be `terminal.step`. |
| `workspace` | yes | `repo`, `base` and `branch`. All three are required. |
| `preconditions` | no | Must hold before an attempt spawns. Unmet means "wait", not "fail". |
| `inputs` | no | What the agent is given. |
| `onStart` | no | Outcome block applied when an attempt spawns (see below). |
| `postconditions` | no | The acceptance gate. Also derives what the runtime looks for. |
| `onSuccess` | no | Outcome block applied when the step completes. |
| `timeout`, `retry`, `onFailure` | no | Override `defaults` for this step. |
| `preview` | no | Show each run's output on the ticket page: a URL, or the files the step writes. See [`preview`](#preview-viewing-a-steps-output-reference). |

`onStart` fires at the moment the issue otherwise learns nothing about: preconditions held,
the attempt won its idempotency race, and the adapter accepted the spawn. It fires once per
attempt, so a retry announces itself. Use it for
a "the agent is working" comment; it carries `{step}` and `{attempt}` beyond the tick's own
values, and no `{pr_url}` (nothing is delivered yet).

`workspace.repo` must be `owner/name`, or a template such as `{repo}`. A literal without a slash is refused at parse time. It could never mint a scoped token, and the failure would otherwise surface deep inside a spawn as an unclassified error that retries invisibly.

`workspace.branch` is a directive: it is what the agent is told to push. It is also the template `{pr_url}` resolution matches against, so a step that pushes nothing still needs a `branch` that names the PR it cares about.

`workspace.base` is the ref the adapter checks the worktree out at. **Never put `{slug}` in `base`.** [The `agent` step](https://www.runfactorial.com/docs/processes/#step-agent) explains what that costs. The checkout at `base` does not hold the run's config: an agent that must read `.factorial/` reads the pinned commit with `git show "$FACTORIAL_CONFIG_SHA":.factorial/<path>`.

**Unmet preconditions are silent.** The orchestrator declines the attempt on every tick and writes no event. A ticket parked on a precondition that can never hold looks the same as one that is only early. Prefer preconditions that observe another step's finished work (`pr_merged(...)`) over guards against this step's own past attempts, because a self-guard blocks rework.

### `kind: human_gate`

Waits for a person to approve or reject a PR. All controls live under `gate:`.

```yaml
- name: spec_review_gate
  kind: human_gate
  description: Reviewer approves the spec pull request
  next: build
  gate:
    type: pr_approval                              # the only type
    pr: { head: "spec/{ticket}-*", base: "main" }  # both globbed
    approvers: ["{roles.spec_reviewers}"]          # optional; empty means any person's approval counts
    approverPermission: write                      # optional; also anyone with write, maintain or admin
    waiting:                                       # optional; fires while the gate is open
      ticketLabel: "factorial:spec-review"
    onApprove:                                     # required
      merge: true                                  # the one action a gate may take itself
      ticketLabel: "factorial:ready-for-build"
      assign: ["{ticket_owner}"]
    onReject:                                      # optional; without it a rejection has no edge
      rerunStep: plan
      inputs: [pr_review_comments]
      ticketLabel: "factorial:spec-rework"
```

`pr_approval` is the only gate type. Anything a person must approve is expressed as a gate on a PR, or it is not expressed at all. Ordering between gates comes from the graph, not from a condition.

`approvers` and `approverPermission` are a filter, not a requirement to collect: **one** qualifying approval advances the gate. A review qualifies when its author is named in `approvers`, or holds `approverPermission` on the repository or a stronger role (`write` < `maintain` < `admin`), as GitHub reports it on each poll. With neither set, any person qualifies. With `approverPermission` set and `approvers` empty, only people with that role qualify. A bot's review counts only when `approvers` names it, and the App's own review never counts, because agents act as the App. A review that does not qualify decides nothing in either direction. While the gate waits, the ticket gets one comment and a `gate_review_ignored` event for each such review, naming why it did not count. A qualifying reviewer's latest review requesting changes fires `onReject`. A PR closed without a merge does not: the gate holds, and the ticket gets one comment and a `gate_pr_closed` event. Reopen the PR to continue the review, or use Redo or **Close and exit** on the dashboard. A changes-requested review fires `onReject` one time only. GitHub keeps that review after the rework pushes, so after a rework the gate waits for a new review. Approval wins over an outstanding changes-requested review from someone else. `.factorial/` cannot change that.

`approvers` still decides nothing about who is asked: `waiting.assign` does. A lane that lets writers decide still parks the ticket with the people it assigns.

**An approval counts only on the commit that it approves.** The gate reads the PR's current head. A commit that someone pushes after an approval needs a new approval. This holds whether or not the base branch dismisses stale approvals. So two gates on one PR, with commits between them, each need their own approval. When `onApprove` asks for a merge, the gate merges the approved commit.

**A refused merge keeps the gate waiting.** GitHub can refuse the merge after the approval. For example, a required check failed, a CODEOWNERS review is missing, or the branch is out of date. Then the gate stays at its step, and the ticket gets one comment with GitHub's reason and a `gate_merge_refused` event. The gate tries the merge again on every poll, and it announces a refusal again only when the reason or the head changes. Fix the cause on GitHub; nothing on the ticket needs to change.

A gate step has no `timeout`, `retry` or `onFailure`, because it waits for a person and there is nothing to retry. `defaults` does not reach it.

`onReject.rerunStep` must name a real step, and it creates a graph edge (see reachability
below). `onReject.inputs` is the rework bundle the re-run attempt receives on top of its own
`inputs:`. `pr_review_comments` is what makes an agent's Revision section fire. The re-run attempt
resumes the previous attempt's session in its kept workspace, with the change request as the
turn. When it cannot, it starts a fresh session and records
`session_resume_skipped` with the reason: the previous attempt ran on another runtime
(`runtime_changed`) or CLI version (`cli_version_changed`), is near the end of its retention
(`past_retention`), left no session ID (`no_session`), or the substrate cannot continue it
(`not_kept`): its workspace, its branch on the remote or its session log is gone, its
checkout is on no branch or on another branch, or its sandbox is public where this step's is
private or the reverse. An automatic retry after a failure starts fresh. A person's **Retry**
continues the failed session (see `retry` below).

The re-run attempt, and any retry of it while the gate stays rejected, is accepted only when
its postconditions hold **and** the PR the gate rejected is no longer open at the
commit the gate rejected. Its branch and PR existed before it began, so its postconditions
alone would accept an attempt that pushed nothing. A push to the PR's branch counts as soon
as the branch moves. A change to a declared file, such as the summary, is a push like any
other. Unmet, the session waits at its prompt under `factorial:waiting` when the agent asked,
its turn failed, or a person worked with it; otherwise the attempt fails with
`postcondition_failed`. If that PR is closed or replaced, or its branch
is not the one the step's `workspace.branch` names, the step's own postconditions decide. To
accept a rework that needs no change, push a commit to the PR: the gate cannot be
approved until the rework is accepted.

**Request changes from the dashboard.** While a gate waits, and its `onReject.rerunStep` is an agent step whose latest run succeeded, the dashboard offers **Request changes**: on the ticket's "Waiting for a review" message, and in that run's Output drawer. Sending it records a `step_changes_requested` event. On its next pass the loop rejects the gate through its own `onReject`: the same label, assignees and rerun as a GitHub rejection. The new attempt receives the text as a `change_request` input, beside whatever `onReject.inputs` declares. The same pass posts the text exactly as written to the PR the gate rejected, with the email of the person who sent it. Approval stays on GitHub. A gate without `onReject`, or whose `rerunStep` is not an agent step, offers nothing. A request after the PR merged is refused with a reason and undoes nothing. So is a request for an attempt that is no longer the latest. Any person signed in to the dashboard may send one. A change request can only send the work back. It never approves, merges or advances a gate.

**Redo from the dashboard.** While a gate waits, the dashboard offers **Redo <step>** for the step the gate reviews: its `onReject.rerunStep`, or else the one agent step whose `workspace.branch` is the gate's `pr.head` (`{slug}` as `*`). The current step of a ticket that is not at a gate offers **Redo** for itself. A redo throws away that step and every step its `next` edges reach, never an `onReject` edge: their runs end and are set aside, their open PRs close and their branches are deleted, and the step runs again from a clean start on the same pinned config. A branch that a kept step's template also names stays, with its PRs. Merged work stays merged. The dashboard asks once, with an optional note for the fresh agent, and shows no preview: the timeline's **Redone** row and the issue comment say what the redo did.

### `kind: action`

Runs a registered orchestrator action. No agent, no container, no prompt.

```yaml
- name: fix_contract
  kind: action
  action: fix_the_contract     # from the closed registry
  next: build
  onSuccess:
    comment: "Contract frozen at {contract_sha}."
  retry: { maxAttempts: 2, backoff: 5m }
```

`action:` is a bare name, because actions take no arguments from config. The registry is closed
and holds one name, `fix_the_contract`. An unregistered name fails at parse time. An action step
honors `retry` and `onFailure` but has no `timeout` and no `workspace`.

### `kind: check`

Completes when its conditions hold on observed state. The step has no agent, no action and no
person, so its conditions are its acceptance gate.

```yaml
- name: validate_ci
  kind: check
  description: CI must be green on the implementation PR before review
  next: code_review_gate
  conditions:
    all:
      - ci_green("impl/{ticket}-*")
      - pr_open("impl/{ticket}-*", "spec/{ticket}-*")
  pr: { head: "impl/{ticket}-*", base: "spec/{ticket}-*" }   # optional; resolves {pr_url}
  timeout: 30m
  onSuccess:
    ticketLabel: "factorial:code-review"
    assign: ["{roles.code_reviewers}"]
    comment: "CI is green. Ready for review: {pr_url}"
```

| Key | Required | Meaning |
|---|---|---|
| `conditions` | yes | The same predicate compositions as `postconditions`; evaluated every tick. |
| `pr` | no | The PR this step is about, in the same globbed `{head, base}` shape as `gate.pr`. It only resolves `{pr_url}` for outcomes; a check step not about a PR omits it. |
| `timeout`, `retry`, `onFailure` | no | Override `defaults`, as on an agent step. |
| `onSuccess` | no | Outcome block applied when the conditions hold. |
| `preview` | no | As on an agent step. An action step takes it too. |

Waiting is attempt-free: while the conditions are false and the timeout has not passed, ticks
consume nothing. As with agent preconditions, the wait does not fail and writes no event.
The `timeout` is what bounds the wait, because a predicate like `ci_green` cannot tell "red"
from "still running" and only time can. At the deadline the attempt is recorded `timeout` with
the failing predicates as its reason, and the ordinary retry/escalate ladder takes over. So a
red check ends at `factorial:blocked` with a comment naming what never held, after
`maxAttempts` times `timeout`, plus backoffs. Set the timeout from how long the watched thing
takes, not from the agent-shaped `1h` in `defaults`.

Exists for work the orchestrator can only watch. The alternative spellings both misbehave:
`ci_green` in the *producing* step's postconditions is evaluated once, when that agent's run
ends, so CI that is slow fails the attempt and re-runs the whole agent; an agent step
that only watches spends a container run on waiting.

**Never point a gate's `onReject.rerunStep` at a check step.** Rework re-evaluates the same
observed state and completes immediately, the gate reopens, and the standing changes-requested
review re-rejects on the same tick. The result is a permanent automatic loop. The lint warns
(`check-rerun`); point the rerun at the agent step whose work the review is about.

---

## `preview`: viewing a step's output [Reference]

For setup and troubleshooting, follow [How to enable previews for a repository](https://www.runfactorial.com/docs/previews/).

Any step except a `human_gate` may declare `preview:`. Each run of that step then has a Preview
section in its **Output** drawer on the ticket page. It opens beside the run's Terminal section
and shows the URL for that run, or the files the step declares. For a step with no agent run,
the Transcript switch in the Terminal section shows the step's activity.

```yaml
- name: build
  kind: agent
  agent: build-agent
  next: test
  # workspace, inputs and the rest as usual
  preview:
    setup: ["npm ci"]                 # runs once per sandbox, as the agent's user, without env
    services:
      - name: web                     # [a-z][a-z0-9-]*, unique
        command: "npm run dev -- --port $PORT"
        port: 3000                    # 1024-65535, unique; below 32768, not 49983
        ready: { path: "/", timeout: 120s }
    env:
      ADMIN_PASSWORD: "{secrets.PREVIEW_ADMIN_PASSWORD}"   # preview-only values
    stopAfter: 30m                    # at most 55m
```

The same block works under any step name. Here a `docs` agent's preview is a URL a producer outside Factorial publishes, and its gate follows it directly:

```yaml
- name: docs
  kind: agent
  agent: docs-agent
  next: docs_review
  preview: { enabled: true }

- name: docs_review
  kind: human_gate
  next: done
  gate:
    type: pr_approval
    pr: { head: "docs/{ticket}-*", base: "main" }
    onApprove: { merge: true }
    onReject: { rerunStep: docs, inputs: [pr_review_comments] }
```

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | `false` keeps the block but turns the preview off, the same as no block. |
| `reviewGate` | none | Retired. Still accepted and checked, so older files and pinned tickets stay valid; it changes nothing. Every gate with `onReject` offers **Request changes** (see the gate section). |
| `services` | none | Agent steps only. The run's own cloud sandbox serves these, and the page offers **Start**. |
| `setup` | none | Commands run once per sandbox, before the services, as the agent's user and without `env`. Needs `services`. |
| `env` | none | Environment for the services only. May reference only secrets named `PREVIEW_*`, as `{secrets.PREVIEW_<NAME>}`. Needs `services`. |
| `stopAfter` | `30m` | How long a started preview waits with nobody keeping it before it closes: counted from a person's start, its run's end or a Keep running, never while the run is live. At most `55m`. Needs `services`. |
| `files` | none | Agent steps only. The files the step is expected to produce on its own branch, each `{path, label}` and each in its own tab; see below. |
| `page` | none | Retired. Still accepted, so older files and pinned tickets stay valid; it shows nothing. Declare a summary under `files`; see below. |

**The URL is informational.** A step with `preview:` runs, succeeds and fails as it
would without it. A preview that never serves blocks nothing.

**Who serves the preview.** With `services`, the run's own sandbox does, on a cloud sandbox
runtime. The sandbox is created public and, like every run's workspace,
kept after the run without a credential until the ticket exits or 7 days pass. Nothing
serves until the dashboard or a reviewer starts it, during the run's session or after it:
`setup` runs once, and each service starts with `PORT` set, as its own `preview` user.
Choose a service port below 32768; 49983, the sandbox's control port, is refused. A service
is ready when it answers its `ready.path` with a status below 500. The page frames the first
service and links the others. **Stop** ends the services, **Restart** relaunches them, and
**Keep running** restarts the `stopAfter` clock. A rework carries a ready preview over to its
run. A run on a host runtime keeps no sandbox, and the page says so.
Values in `env` reach code the agent wrote in plain text, so `env` may reference only
secrets whose names start with `PREVIEW_`: store a throwaway password or a preview-only
client under such a name, and never a production credential. The parser refuses any other
name, and every park deletes the values from the sandbox. Anyone with the
preview's URL can reach the ports that it declares.

**Without `services`, an outside producer publishes the URL.** A producer, such as a CI job
that serves the build, calls `PUT /api/step-runs/{id}/preview` with `{"preview_url": "<url>"}` (or `null` to
withdraw it). The call needs `FACTORIAL_API_TOKEN`, so nothing that runs in an agent's sandbox
may make it. The API refuses the call for a step whose pinned process has no
enabled `preview:`. Publishing the same URL twice records it once. The URL must be `https`,
and carry no username, no password, no query string and no fragment, because it is kept in the
event log forever. It must sit on a different origin from the dashboard, because the page
frames it with scripts allowed. Plain `http` is accepted only when
`FACTORIAL_PREVIEW_ALLOW_HTTP=true`, for local development. The producer owns everything
behind the URL: hosting, sign-in, readiness, lifetime and cleanup. The orchestrator never
fetches it. Until a URL is published the page says "Preview not available yet". The API
refuses `PUT` for a step that declares `services`.

**`files`: the files a step shows.** An agent step can name the files it is expected to write
on its own `workspace.branch`. The reviewer reads each one in its own tab of the run's Preview
section, in the order the block declares them. When a step declares files, the section is
called **Files**. A summary page is a file like any other. The sample's plan step declares its
summary first, then its spec files:

```yaml
- name: plan
  kind: agent
  agent: plan-agent
  workspace: { repo: "{repo}", base: main, branch: "spec/{ticket}-{slug}" }
  preview:
    files:
      - { path: "specs/{ticket}-{slug}/summary.html", label: "Summary" }
      - { path: "specs/{ticket}-{slug}/spec.md", label: "Spec" }
      - { path: "specs/{ticket}-{slug}/plan.md", label: "Plan" }
      - { path: "specs/{ticket}-{slug}/tasks.md", label: "Tasks" }
```

| Key | Default | Meaning |
|---|---|---|
| `path` | required | The file, as a path in the repository. It may use `{ticket}` and `{slug}`, and `*` and `**` to make it a pattern; see below. |
| `label` | the file name | The tab's name. At most 64 characters. A pattern must have a label. |

The file's extension sets how its tab shows it:

| Extension | Shown as |
|---|---|
| `.html` | The page, as the agent wrote it. |
| `.md` | Markdown, rendered to a page. Raw HTML in it shows as text. A file that renders too large is not shown. |
| Any other, or none | Plain text, exactly as written. The file must be UTF-8. |

The API serves each file with no script, and the file loads nothing from outside itself. A file
is at most 1 MB, and its link is signed for one run.

**Patterns: files that the agent names.** Use a pattern when the agent chooses the file names
at run time, such as one report for each check:

```yaml
    files:
      - { path: "specs/{ticket}-{slug}/summary.html", label: "Summary" }
      - { path: "reports/{ticket}/*.html", label: "Reports" }
```

- A `*` matches any characters in one folder. A `**` segment matches any number of folders, or
  none: `docs/**/*.md` matches `docs/a.md` and `docs/x/y/a.md`. A `*` in a predicate is
  different: it also matches `/`.
- A pattern must have a label. The label names the set. Labels can repeat.
- Each file that matches gets its own tab. The tab shows only the files that the step's attempts
  changed on the branch and that are still there at the end of the run. The tabs are in path
  order.
- A pattern shows at most 50 files. When more files match, a notice gives the count and a link
  to the folder on GitHub.
- A file shows in one tab only. A path without `*` gets its file first. Then each pattern, in
  order, gets the files that no tab before it shows. A pattern adds no tab when all of its files
  already show.
- The tab name is the part of the path below the pattern's fixed folder. The fixed folder is the
  part of the pattern before its first segment with a `*`. For `reports/{ticket}/*.html`, the
  file `reports/12/lint.html` gets the tab `lint.html`. Factorial creates and moves no file: the
  name comes from the place where the agent wrote the file.
- A pattern that matches no file shows as missing. A missing tab lists the files that the step
  changed, with a link to each on GitHub, so you can correct the path.
- Over GitHub's limit of 300 changed files, a pattern shows only the files in the record.

**What the agent is told.** The run's first turn has a `# Files` section. It gives the path and
the label of each declared file, and the rules for each type: for example, that an HTML file must
be self-contained and has no JavaScript. The section does not say what to write in a file. A
file that the agent's own instructions already produce, such as `spec.md`, needs nothing more.
For a new file, such as a summary, say what the file holds in the agent's instructions or in a
skill, never in the process file. The sample's `skills/spec-page/SKILL.md` tells the plan agent
how to write a good summary. The agent commits each file on its branch with the rest of its
work, so the file merges when its PR merges.

**What the worker records.** When a run ends, the worker reads the run's branch on GitHub and
records every file that the step's attempts changed there. The record
starts where the branch stood when the run started, so a step that works on another step's
branch is shown only its own files. A file the step wrote but did not declare is recorded and
not shown.

A declared file is informational. A file the step did not produce shows as missing, and the step
runs, succeeds and fails as it would without `files`. To require a file, add
`file_exists(path, branch)` to the postconditions.

The parser refuses `files`:

- on a step that is not an agent step;
- on a step whose `workspace.branch` has no `{ticket}`;
- whose paths repeat;
- whose path uses a placeholder other than `{ticket}` and `{slug}`, or holds a brace that is not
  part of one;
- whose path holds a character other than letters, digits, `.`, `_`, `-`, `/` and `*` outside a
  placeholder;
- whose path has `**` in a segment with other characters, such as `a**`;
- whose path is a pattern without a `label`;
- whose path has a segment that starts with `-`;
- whose path is absolute, or has a `..`, a `.` or an empty segment.

**`page` is retired.** `page` had the agent push one summary file to a branch of its own. A
summary is a declared file now: list it under `files`, as above. The parser still accepts `page`
with the rules it always had, so older files and pinned tickets stay valid. It shows nothing, and
the configuration page warns that it is retired.

A ticket keeps the `preview:` its snapshot pinned at admission. Adding or changing the block
changes new tickets only.

---

## Outcome blocks [Reference]

Seven places take the same three-key block: `onStart` (agent steps), `onSuccess`,
`onFailure.escalate`, `gate.waiting`, `gate.onApprove`, `gate.onReject`, and
`terminal.actions`.

| Key | Effect |
|---|---|
| `ticketLabel` | The `factorial:` label the ticket wears after this outcome. |
| `comment` | One comment posted on the issue. |
| `assign` | **Replaces** the issue's assignees. It does not add to them. |

`assign` replaces the assignees so that the ticket always sits with whoever must act next. Entries are literal logins, templates (`{ticket_owner}`), or a whole-entry role reference
(`{roles.spec_reviewers}`). A role reference must be the entire entry. `"{roles.x}-bot"` is a
`TemplateError`, because there is nothing sensible to splice a list into. Roles may reference
roles; a cycle is an error.

Two blocks carry extra template values beyond the tick's own:

- `onFailure.escalate` gets `{step}`, `{attempt}`, `{failure_summary}` and `{failure_reason}`. It
  does **not** get `{pr_url}`. `{failure_summary}` is one plain sentence for the people reading
  the issue, without its final period so that the template ends the sentence;
  `{failure_reason}` is the whole reason, rule code and raw output included.
- `onStart` gets `{step}` and `{attempt}`, and no `{pr_url}`, because nothing is delivered yet.
- `onSuccess`, `gate.waiting`, `gate.onApprove` and `gate.onReject` get `{pr_url}`, resolved from
  observed state.
- `terminal.actions` gets `{step}` only.
- An action step's `onSuccess` gets whatever its action returned. `fix_the_contract` supplies
  `{contract_sha}` and `{slug}`.

---

## `defaults`, `retry` and escalation [Reference]

```yaml
defaults:
  timeout: 1h
  retry:
    maxAttempts: 2
    backoff: 10m
  onFailure:
    escalate:
      ticketLabel: "factorial:blocked"
      assign: ["{ticket_owner}"]
      comment: "Factorial stopped at {step} after {attempt} attempt(s): {failure_summary}."
```

Durations are `<int><s|m|h>`, such as `30s`, `10m` or `1h`. Nothing else parses.

`defaults` applies to agent steps (all three keys) and action steps (`retry`, `onFailure`). A step
sets any of them to override; unset means inherit. With no `timeout` anywhere the worker uses
**1h**.

The step `timeout` counts only the time the session is working, not the time it waits at its
prompt. `max_duration` is accepted and ignored: the step timeout over
active time and the deployment's session ceiling are the limits. A session renews its token
every hour, and parks for a person at the deployment's `terminal.session_ceiling`. A
session that stops at its prompt with its postconditions unmet waits there for
`terminal.idle_park_after` (10 minutes). Then it parks for a person under `factorial:waiting`
when the agent asked a question, its turn failed, or a person typed into it or released it;
otherwise the attempt fails with `postcondition_failed` and `retry` applies, so a silent stop
costs an attempt. A silent stop also waits while a check run that a `ci_green` postcondition
reads is not complete, and then for `terminal.idle_park_after` after the last one completes, but
not past the step `timeout` counted from when the session last started working.

An agent's `limits.max_turns` in `agent.yaml` caps its turns: on Claude a turn is one model
request, on Codex one prompt round, and the count spans resumes within one attempt and restarts on
a retry.

A session that a person ends from its terminal (`/exit`, or Ctrl+C or Ctrl+D twice) before its
postconditions hold parks under `factorial:interrupted` when the dashboard's terminal recorded
their typing since the session last started working; remove the label to resume it. Before it
parks, it waits while such a check run is not complete, but not past the step `timeout`. If
all its postconditions then hold, the worker accepts the run instead. A quit that nobody typed
fails on its postconditions, and `retry` applies.

A failed attempt's retry starts once `backoff` has passed since the attempt ended. To start it
sooner, press **Retry** in the header of the failed, timed-out or canceled attempt in the ticket
page's activity, then confirm. Retry shows during the backoff and after escalation. During the
backoff it takes the booked attempt now. After escalation, or with no retry policy, it grants one
attempt, and a failure of that attempt escalates again. An agent step's attempt continues the
failed session and its folder, unpushed work included, where the runtime can. Otherwise it starts
fresh and the timeline says why. A timed-out attempt always starts fresh. Check and action steps
start fresh. MCP's `factorial_ticket_retry` is the other "retry now": it
starts the booked attempt fresh during the backoff.

When a step exhausts `maxAttempts`, the orchestrator applies `onFailure.escalate`, sets the ticket
BLOCKED, and falls back to `factorial:blocked` if the block named no label.

**Always configure a retry policy.** If neither `step.retry` nor `defaults.retry` is set, the file
still parses, but a failed step can never start another attempt. A person who removes
`factorial:blocked` to release the ticket gets an explicit refusal comment instead of a retry, because the pinned config
has nothing to retry under, and adding `retry:` to the repo only helps newly admitted tickets.
The dashboard's **Retry** on the failed attempt still grants one attempt with no policy.

---

## `policy`: what the process expects of the repo [Reference]

Distinct from `defaults`, which configures how steps run. `policy` describes the repository's
shape, which the orchestrator observes and reports but never imposes.

```yaml
policy:
  branchProtection: required      # required | advisory | ignored. Default: advisory
```

`policy` has one key, `branchProtection`. It changes what the configuration page's **Branch protection** row says about the
default branch, measured against the profile the orchestrator checks for: one required approval,
CODEOWNERS review, the `verify` check, and dismiss-stale. The `factorial` branch has its own
readiness row, which the page shows in the **Process config** section. This key does not change it.

| Value | A repo that meets the profile | One that does not | One that cannot be read |
|---|---|---|---|
| `required` | passes | reported in red | **reported in red** (see below) |
| `advisory` (default) | passes | reported as a warning | reported as "Not checked" |
| `ignored` | row is not shown at all | row is not shown | row is not shown |

`required` with an unreadable answer is the case this key exists for. Reading protection needs
the App's `administration: read` permission, which is not granted by default
([Set the permissions](https://www.runfactorial.com/docs/github-app/#permissions)). Declare `required` without it and protection can never be
proven, so the process claims a guarantee that nothing checks. The row says so, instead of a neutral
"Not checked".

**Write `ignored`, never `off`.** YAML reads a bare `off` as the boolean false, so it never
reaches the parser as a string. The parser detects the boolean and says so, but the value to
write is `ignored`.

### None of the three gate anything [Reference]

`required` reports; it does not block activation, does not block admission, and does not block a
merge. This follows from where this file lives.

`.factorial/` sits in the repo agents write to. Anything here is editable by an agent, which is the
inverse of the rule that governs MCP servers (there, a repo file *selects* and operator state
*permits*). Enforcement an agent can switch off is worse than no enforcement, because it
looks like a control while being a preference. A reader who sees `required` would stop looking
for the real control.

The real control is branch protection itself, plus the App holding no bypass and being the
author of every implementation PR. Neither is reachable from this file. Making `required` block
would move a real control into a place an agent can edit; it would not create one.

---

## `runtime`: which Runtime runs the agents [Reference]

```yaml
runtime: claude-api               # the slug of a Runtime from the Runtimes page
```

A Runtime is a substrate an operator defines on the dashboard's **Runtimes** page: a type
(`codex_api`, `codex_subscription`, `codex_bedrock`, `claude_api`, `claude_subscription`), a
name, and the credentials that the type needs (`codex_bedrock` uses the host's AWS role and
stores none). This file names one by its slug. A slug has lowercase letters, digits and dashes, starts
with a letter or digit, and has at most 63 characters. The parser refuses any other value.

Required. The Runtime's type says which CLI runs the agents. Sync refuses a process without
this key.

**The file selects; the operator defines.** `.factorial/` sits in the repo agents write to, so
this key can name a Runtime but cannot create one. It cannot reach the credentials behind the
name either: they live in the orchestrator's database, which no agent can reach. This is the
trust direction of MCP servers, and the opposite of `policy`, which only reports.

**The Runtime owns its vendor key.** A process that names a Runtime takes that Runtime's own
credential (`OPENAI_API_KEY` for `codex_api`, `ANTHROPIC_API_KEY` for `claude_api`, the login
for a subscription) from the Runtime alone. `codex_bedrock` stores none: the worker mints
`AWS_BEARER_TOKEN_BEDROCK` from the host's AWS role at each launch. A project secret of the
same name is never read by a process that names the Runtime. An agent manifest may still declare the key in its `secrets:`
block. The declaration is inert when the process names a Runtime, and pinned snapshots keep
parsing. Every other declared key, and every `{secrets.KEY}` an MCP server references, still
comes from the project's secrets. A process that names no Runtime reads every declared key from
the project, as before. This holds per ticket: a ticket pinned to a snapshot that names no Runtime keeps
reading the project copy after a later version names one.

**Every declared key from the project's secrets reaches the agent.** The Secrets page says how:
an environment variable with the real value, the default, or a header a cloud sandbox's egress
proxy sets on one host, where the agent's variable holds a placeholder. A host run has no proxy,
so it gets a header-routed key as an environment variable. A key an MCP server references
arrives as that server's header.

**The agent's `runtime:` block is ignored.** The Runtime's type says which CLI runs the agent, so
you can delete the block from `agent.yaml`. A manifest that keeps it still parses.

Sync needs the database for one check: the slug must exist in the project's organization. A slug
names a Runtime there and nowhere else. Sync refuses a process that names a Runtime nobody
created, and the message says which slug to create.

A Runtime deleted after sync, or a Runtime that holds no credential of its type, shows as a
readiness blocker on the project page. `codex_bedrock` is ready without a stored key.

---

## Conditions [Reference]

A condition is one predicate call, or a composition:

```yaml
postconditions:
  all:                                   # every child must hold
    - branch_exists("spec/{ticket}-*")
    - any:                               # at least one child must hold
        - pr_open("spec/{ticket}-*", "main")
        - pr_merged("spec/{ticket}-*", "main")
    - not:                               # negation of a single child
        ticket_label("factorial:blocked")
```

Calls are written `name("arg", "arg")` with quoted string literals only. A hand-written scanner
parses them (never `eval`), so anything else (a bare number, an unquoted word, trailing text) is
a parse error naming the malformed call.

### The closed library

| Predicate | Args | Holds when |
|---|---|---|
| `branch_exists(name)` | 1 | Some branch matches `name`. |
| `file_exists(path, ref)` | 2 | Some branch matching `ref` holds a path matching `path`. |
| `pr_open(head, base)` | 2 | A matching PR is open and not merged. |
| `pr_merged(head, base)` | 2 | A matching PR was merged. |
| `pr_approved(head, base, by?)` | 2 or 3 | A matching, still-**open** PR is approved at its current head, optionally by `by`. |
| `pr_changes_requested(head)` | 1 | A matching, still-open PR has a login whose latest review requested changes. |
| `ci_green(ref)` | 1 | Every branch matching `ref` has check runs, and all completed successfully. |
| `ticket_label(name)` | 1 | The issue carries that label, matched exactly. |
| `ticket_has_assignee()` | 0 | The issue has at least one assignee. |
| `issue_comment_contains(marker)` | 1 | The issue has a comment **by the App's own bot login** containing `marker`. |
| `pr_comment_contains(head, marker)` | 2 | A matching PR has such a comment. A change request that the orchestrator posts from the dashboard does not count. |

Notes:

- **Zero check runs is not green.** `ci_green` on a branch whose CI never ran reads false, on
  purpose.
- **`pr_approved` ignores merged and closed PRs.** A merged PR's approval is frozen evidence; a
  gate that accepted it would approve the past. For the same reason it ignores an approval of a
  commit that is no longer the head.
- **The comment predicates are author-fenced.** Only the App's own bot login counts, because the
  marker is delivery evidence and a third party could otherwise forge it. A marker must be a
  non-empty literal. An empty one is refused at parse time.
- **A new predicate is a code change**, in `orchestrator/predicates/library.py` plus the arity
  table in `config/conditions.py`. There is no per-attempt predicate and no comment-*content*
  predicate; `any:`/`not:` cannot manufacture either.

### Globs

Branch names, PR head/base refs and file paths accept `*`. Label names do not. The orchestrator
writes labels, so `ticket_label` compares exactly. Markers are literal substrings, never globs.

A pattern with no `*` is compared **exactly**. A pattern containing a `*` goes through `fnmatch`,
which also gives `?` and `[...]` a meaning. Do not use them. And `*` crosses `/`, so
`specs/12-*/spec.md` also matches `specs/12-a/nested/spec.md`; the predicate still requires a real
file at a real ref, so this is accepted imprecision rather than a bug.

`pr_open`'s base globs symmetrically with its head, so `pr_open("impl/{ticket}-*",
"spec/{ticket}-*")` matches whatever slug an earlier agent chose.

---

## Templates [Reference]

Only `{name}` and `{roles.<name>}` are recognized. Everything else is literal text, so a config
string may carry JSON-like braces safely.

| Placeholder | Available where | Value |
|---|---|---|
| `{ticket}` | everywhere | The issue number. |
| `{repo}` | everywhere | The target repo, `owner/name`. |
| `{ticket_owner}` | everywhere | The assignee **at admission**. It does not change after a later reassignment. |
| `{slug}` | directives only | The agent-chosen part of a branch name. |
| `{pr_url}` | `onSuccess`, `gate.*` | The step's PR, resolved from observed state. |
| `{step}`, `{attempt}`, `{failure_summary}`, `{failure_reason}` | `onFailure.escalate` | Which step failed, on which attempt, and why: `{failure_summary}` in one plain sentence, `{failure_reason}` in full. |
| `{step}` | `terminal.actions` | The terminal step's name. |
| `{contract_sha}`, `{slug}` | an action's `onSuccess` | Whatever the action returned. |
| `{roles.<name>}` | `assign`, `approvers` | Expands to that role's members, recursively. |

### `{slug}` is the one with rules

An agent chooses part of its branch name at runtime, so config cannot know it in advance. So
the two uses of `{slug}` differ:

- **Directives** (`workspace.branch` and predicate arguments) expand `{slug}` to the concrete
  observed name once exactly one candidate is on record, and leave it literal otherwise.
- **Matchers** (the orchestrator's own PR and gate lookups) always glob `{slug}` to `*`,
  resolved or not. A matcher may only widen against observed state.

Resolution counts candidates per branch template. Only the newest run that delivered anything for that
template speaks for it, so a rerun on a different slug supersedes the run it replaced. Two
templates that disagree (a build whose slug is not its plan's) stay ambiguous. So does one run
that reports two names under one template. Both leave `{slug}` literal.

Resolution is recomputed every tick and is **not** monotonic, so it can revert. Write predicates
that hold either way. That is what `branch_exists("spec/{ticket}-*")` is for. Keep the template
and its predicate glob consistent: a `branch: "spec/{ticket}-{slug}"` pairs with
`branch_exists("spec/{ticket}-*")`, never with a literal name.

---

## Postconditions do two jobs [Reference]

They are the acceptance gate: the orchestrator trusts them, never the agent's self-report. A
person's dashboard Force complete is the one recorded exception.

They also derive what the runtime is told to look for. Four predicates imply a delivery, and only
in **conjunctive positions**: the condition itself, or a child of `all:`.

| Predicate | Derived expectation |
|---|---|
| `branch_exists(glob)` | a BRANCH matching the glob |
| `pr_open(head, base)` | a PR whose head matches (the base is dropped) |
| `issue_comment_contains(marker)` | a COMMENT on the issue carrying the marker |
| `pr_comment_contains(head, marker)` | a COMMENT on a matching PR |

A call under `any:` is one alternative among several, and one under `not:` asserts the opposite.
Neither states "this must be delivered", so neither derives an expectation. The postcondition
still evaluates in full either way; only the delivery probe narrows. `file_exists`, `ci_green`,
`pr_merged`, `pr_approved`, `pr_changes_requested`, `ticket_label` and `ticket_has_assignee` never
derive anything: no adapter can verify a file, and the rest are a gate's business or not
artifacts at all.

So **put the predicate that names the step's delivery at the top level of
`all:`**, not nested inside `any:`.

---

## `inputs` [Reference]

```yaml
inputs:
  - ticket_body
  - ticket_comments
  - file: "specs/{ticket}-*/spec.md"
```

Bare names are a closed set of three: `ticket_body`, `ticket_comments`, `pr_review_comments`.
Anything else must be a `{file: <path>}` mapping, whose path globs like any other path argument.

**A file input passes the path, not the content.** Every run already holds a repo-scoped token, so
the agent reads the repo itself. The input tells it what to open.

**Comment inputs are filtered to the ticket owner, and that is a security boundary.** A comment
becomes agent input only when its author matches the owner at admission. An agent acts on its
inputs unsupervised, so admitting a third party's comment would open a prompt-injection channel.
A rejecting gate's `onReject.inputs` additionally trusts the reviewers that gate lets decide
it (named in `approvers`, or holding its `approverPermission`). This is the one place a second
identity is principled, because those people are what decided the PR's fate. A gate with neither trusts
no review text. A step's own `inputs:` stays owner-only.

From such a reviewer, `pr_review_comments` carries every review: its body and its inline comments.
A standalone inline comment ("Add single comment") is a review too. Each inline comment names
the file and line it is on.

Both digests are capped at 16,000 bytes, truncated from the **oldest** end so the newest comments
survive whole.

**The run gets the images that these inputs embed.** An image in `ticket_body`, or in a
comment or review that passed the filters above, reaches the agent's first turn as an image and
as a file whose path the prompt names. A comment that the filters drop, or that the digest cap
cuts, brings no image. The prompt names an image that the run cannot get as not delivered: one
from another repository, one that is not an image, or one over the limits of 20 images per run
and 5 MB each.

`pr_review_comments` belongs on `gate.onReject.inputs`, where it is what makes a rework attempt
read the review it is answering. On a step's own `inputs:` it resolves against the PR that step's
`workspace.branch` matches, and costs an extra PR lookup. Do not declare it where it is unused.

---

## `terminal` [Reference]

```yaml
terminal:
  step: done
  actions:
    closeTicket: true
    comment: "Factorial finished. Merged to main."
```

The terminal step is **virtual**: it must not also appear in `steps[]`, and the parser says so if
it does. `actions` is an outcome block plus `closeTicket`. If it names no `ticketLabel`, the
ticket gets `factorial:done`.

---

## What YAML the parser accepts [Reference]

The parser reads every file under `.factorial/` with the same rules: process files, agent
manifests and MCP server files. It refuses two things before it reads a field:

- **Anchors and aliases.** The parser refuses `&name`, `*name` and the merge key `<<: *name`.
  A small file of nested aliases can expand to gigabytes, so write each value out in full.
- **A file larger than 128 KiB.** The largest sample is less than 13 KiB.

---

## What the parser checks about the graph [Reference]

`ProcessConfig` validates the whole step graph after the fields parse, and reports every problem
it found in one error rather than the first:

1. Step names are unique.
2. The terminal step's name does not collide with a real step.
3. `entry.step` names a real step.
4. Every `next` resolves to a real step or to `terminal.step`.
5. Every `gate.onReject.rerunStep` resolves to a real step.
6. The terminal step is reachable from `entry.step`, following `next` **and** gate rework edges.
7. No step is unreachable from `entry.step`.

Rule 7 is the one that catches a half-finished edit: a step you renamed but forgot to point at is
an orphan, and the file will not load.

---

## Authoring checklist [Reference]

1. Read the target repo's CI workflows first. The process must agree with them, and the sample's
   graph is not automatically right for another repo: a PR that targets the wrong branch can
   still report green.
2. Give every agent step a `workspace.branch` whose `{slug}` is matched by a `*` glob in its own
   postconditions.
3. Put each step's delivery predicate at the top level of its `postconditions.all`.
4. Set `defaults.retry` and `defaults.onFailure.escalate`, so a failure is both retryable and
   visible.
5. Give every outcome an `assign`, so the ticket never sits with nobody.
6. Do not guard a step against its own earlier attempts, because that blocks rework.
7. Confirm every `agent:` name has a folder under `.factorial/agents/` with a matching `agent.yaml`.
8. Give the system prompt of every agent that opens a PR the section that keeps it on
   task when a person types into its terminal. See
   [Keep the agent on its task](https://www.runfactorial.com/docs/agents/#terminal-input).
9. Confirm the `runtime` slug exists on the Runtimes page.
10. Commit the change with the process builder's **Commit and sync**, or merge it into
   `factorial` by PR and then sync. In-flight tickets keep the commit they pinned; the
   next ticket gets the new one.

---

## Check that it parses, before you commit it [Procedure]

Parse the process file alone:

```bash
uv run python -c "
from pathlib import Path
from orchestrator.config import load_processes
for key, p in load_processes(Path('<repo>/.factorial')).items():
    print(key, p.step_names)
"
```

Parse the whole folder, which also resolves each `agent:` name, every declared prompt and skill
path, and each MCP server file:

```bash
uv run python -c "
from pathlib import Path
from orchestrator.config import load_config_folder
c = load_config_folder(Path('<repo>/.factorial'))
for key, p in c.processes.items():
    print(key, p.step_names)
print(sorted(c.agents))
"
```

A `ConfigError` names the file and the reason. This is the same loader the API uses, so it catches
everything `POST /process-versions/sync` would refuse, without a round trip to GitHub.

Then follow [Snapshot the config](https://www.runfactorial.com/docs/connect-a-repository/#sync) to snapshot the config at a commit.
