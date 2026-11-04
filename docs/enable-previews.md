# How to enable previews for a repository

**Status:** runbook

Use this guide to show a reviewer an agent step's output beside the step's run. Start with an
[onboarded project](bootstrap.md) and access to its dashboard settings and repository.
The [process field reference](process-yaml.md#preview-viewing-a-steps-output-reference)
defines each YAML field.

A step's `preview:` block shows a URL, the files that the step writes, or both. An agent step
can declare its files beside a URL or alone.

| Kind | Field | What the reviewer sees | Runtime | Procedure |
|---|---|---|---|---|
| App preview | `services` | The running app, served from the run's E2B sandbox | E2B only | Steps 1 to 6 |
| Outside URL | no `services` | A URL that a producer outside Factorial publishes | Any | [Field reference](process-yaml.md#preview-viewing-a-steps-output-reference) |
| Declared files | `files` | The files that the step writes on its own branch, such as a summary page and the spec files, one tab for each | Any | Step 7 |

For an app preview, the agent writes the app. The preview starts by itself when a reviewer opens it
on the latest attempt, during the agent's session or after it. The runtime starts the services; the
worker publishes their URLs after readiness checks pass. The agent does not need a deployment
command or a URL-publishing instruction.

For declared files, read [what declared files do](#what-declared-files-do-reference) first.
A summary page is a declared file: `preview.page`, which pushed a summary to a branch of its own,
is retired. Step 8 diagnoses failures of every kind.

---

## Step 1: choose what to serve [Procedure]

1. Read the app's package manifests, lockfiles, start scripts, and environment examples.
2. Choose a frontend-only preview or a preview with an API and database.
3. List the install commands, workspace builds, migrations, and seed data that the app needs.
4. Test the proposed commands in a clean development environment.

For a frontend-only preview, identify which features need an API. Do not treat a rendered
page as proof that those features work. For a full app, arrange a disposable database and
configure the API connection. Factorial does not provision these dependencies from `preview:`.
See the [full-stack examples](#full-stack-previews-reference) for the current runtime's limits.

Check browser-facing API URLs, CORS, and HTTPS. Browser code cannot reach a sandbox API through
`localhost`. Use a browser-reachable endpoint or an app proxy. Do not assume that YAML expands
service URLs into environment values.

---

## Step 2: select an E2B runtime [Procedure]

1. Open **Runtimes** in the dashboard.
2. Select a runtime whose isolation is **E2B** and whose family matches the agent manifests.
3. Check its credentials and template configuration.
4. Add its exact slug to the process's top-level `runtime` field.
5. Open the project's **Configuration**, then **Runtime**.
6. Under **Cloud sandbox runtimes**, enable that runtime for the project.

```yaml
# Example slug; use the value from your deployment's Runtimes page.
runtime: claude-api-on-e2b
```

The process selects a registered runtime; it does not create one. A host runtime cannot
keep an E2B sandbox for a preview. See the [runtime
reference](process-yaml.md#runtime-which-runtime-runs-the-agents-reference).

---

## Step 3: enable downloads before the build starts [Procedure]

1. Open the project's **Configuration**, then **Runtime**.
2. Under **Egress hosts**, add the hosts that installation and application startup need.
3. Save the settings **before the build sandbox starts**.

For npm, pnpm, or `npx`, include `registry.npmjs.org`. Include other hosts if dependencies
download binaries or the app contacts external services. Enter hostnames, without schemes or
paths. See [egress settings](bootstrap.md#runtime_allowlist-and-egress_allowlist--reference).

The sandbox keeps the egress list from its build run. A preview restart uses that same list.
If you change it later, use a new build run with a new sandbox.

---

## Step 4: configure the preview [Procedure]

Edit `.factorial/processes/process-<name>.yaml` on the repository's `factorial` branch.
Add `preview:` to the agent step whose output you review, alongside `workspace` and `onSuccess`.

This block assumes an npm app whose `dev` script accepts `--host` and `--port`.
Adapt the commands to the app; the block is not a complete process file.
The sample process and the editor's Simple template have this block without `env`,
commented out, on the build step.

```yaml
  preview:
    setup:
      - "npm ci"
    services:
      - name: web
        command: "npm run dev -- --host 0.0.0.0 --port $PORT"
        port: 3000
        ready:
          path: "/"
          timeout: 120s
    stopAfter: 30m
```

1. Use the repository's package manager and lockfile in `setup`.
2. Add builds for shared workspace packages before the service starts.
3. Give each service a unique name and a port from 1024 through 65535. Choose a port below 32768.
   Port 49983 is refused.
4. Keep service commands in the foreground and bind web servers to `0.0.0.0`.
5. Configure the app's host checks to accept its E2B hostname, where needed.
6. Choose a readiness path that answers without authentication.
7. Add required environment values under `preview.env`. Setup does not receive them.

For example, after creating a project secret named `PREVIEW_DATABASE_URL`:

```yaml
    env:
      DATABASE_URL: "{secrets.PREVIEW_DATABASE_URL}"
```

Use preview-only data and credentials: the sandbox is public while the preview runs.
Secret references must use names that start with `PREVIEW_`. Do not depend on the build
agent's credentials; the runtime removes them before it parks the sandbox.

### Execution rules [Reference]

- Setup commands run from the checkout as the agent's user, without `env`. Services run from the
  checkout as a separate `preview` user, with `env`, and with `HOME` set to `/home/preview`. Both
  users can write the checkout. Each setup command has its own shell;
  a `cd` or `export` does not carry into the next command.
- Only declared service ports are public. A server the agent starts on another port is not
  reachable from outside the sandbox.
- While a run's credential rule is live, services reach only loopback, and on loopback only the
  declared ports. A rule is live while the agent runs and while an open session is open. In that
  time, a local database or other server on a port that is not declared does not answer the
  services. Declare every port that the app uses. A database cannot be a declared service (see
  Full-stack previews), so connect to a local database through a Unix socket. When no rule is
  live, services have normal egress.
- Choose service ports below 32768. The switch to this mode cuts every connection on a declared
  port, and a port in the ephemeral range could be a local port of another connection.
- The checkout's root is sticky: a service cannot rename or delete an entry that the agent owns
  there, for example a `dist/` that setup built. Have setup and services write separate
  directories, or let the service own what it rebuilds.
- The sandbox runs with `fs.protected_regular=2`, so the agent cannot reopen for writing a regular
  file that the preview created in the checkout's root. Keep generated files in subdirectories.
- Setup runs until it succeeds, then later starts skip it in that sandbox. Make partial setup safe to retry.
- Setup can run while the agent's session runs in the same checkout, for example at the first
  automatic start. Make setup safe to run at that time.
- Services start concurrently after setup. A service must handle dependencies that are not ready yet.
- The runtime sets each service's `PORT`. Do not set it in `env`.
- Readiness checks use `127.0.0.1` and accept HTTP responses below 500, including 403.
  Test the public URL too; a passing check does not prove that browser access works.
- `stopAfter` defaults to `30m`; its range is `1m` to `55m`. It counts from a person's start or
  **Restart**, the run's end, or a **Keep running**, and never while the run is live. The
  dashboard warns five minutes before it closes the preview.

Without `services`, the preview waits for an external producer to publish a URL. That mode does
not offer **Start preview**. See the [field
reference](process-yaml.md#preview-viewing-a-steps-output-reference).

### Full-stack previews [Reference]

The E2B runtime can start a frontend and backend in one sandbox. It does not manage databases,
containers, dependency order, or migrations. The examples below need scripts in your app;
they are configuration patterns, not tested deployments.

Every declared service needs HTTP readiness. Do not declare PostgreSQL on port 5432 as a
service: the runtime cannot check the PostgreSQL protocol. All services must pass before the
worker publishes any URLs. The first service appears in the frame; the panel links to the others.

**External preview database.** Create a disposable database first. Store its connection string
as the project secret `PREVIEW_DATABASE_URL`. Test the actual database driver from E2B;
an egress hostname entry alone does not prove that the connection works. While a run's
credential rule is live (the agent runs, or an open session is open), services have no egress
and cannot reach this database. A preview that starts then fails its readiness check. Start it
again after the session parks.

```yaml
  preview:
    setup:
      - "npm ci"
      - "npm run build:shared"
    env:
      DATABASE_URL: "{secrets.PREVIEW_DATABASE_URL}"
      API_INTERNAL_URL: "http://127.0.0.1:3000"
    services:
      - name: web
        command: "bash scripts/preview-web.sh"
        port: 3001
        ready: { path: "/", timeout: 120s }
      - name: api
        command: "bash scripts/preview-api.sh"
        port: 3000
        ready: { path: "/health", timeout: 180s }
    stopAfter: 30m
```

The app must implement these contracts:

- `build:shared` builds any workspace packages; replace or omit it to match the app.
- `preview-web.sh` starts the frontend on `0.0.0.0:$PORT`. Its server proxies browser requests
  from `/api/*` to `API_INTERNAL_URL`. Browser code uses the frontend's public origin.
- `preview-api.sh` waits for the database, applies safe migrations, then starts the API on `$PORT`.
  Its `/health` returns 503 until the API and database are ready, then 200.
- Installation and database hosts must satisfy the sandbox's network policy before the build starts.
  Use separate data for concurrent previews; Factorial does not isolate an external database for each run.

`DATABASE_URL` and `API_INTERNAL_URL` are app-defined names. Factorial passes the same `env`
to every service, and passes no `env` to setup. It does not isolate database credentials to the
API process. It expands secret references, but does not inject public service URLs into `env`.

**Database inside the sandbox.** Use a template with the database binaries already installed.
Factorial's template builder does not explicitly install PostgreSQL. Services run as `preview`
and setup runs as `agent`, both without sudo. They cannot install system packages or manage a
root-owned database service.

Keep the two HTTP services above, remove the external `DATABASE_URL`, and replace the API command:

```yaml
      - name: api
        command: "bash scripts/preview-api-with-db.sh"
        port: 3000
        ready: { path: "/health", timeout: 180s }
```

That script must initialize database storage if absent, start the database, wait for it,
apply migrations, and start the API. It must set the API's local database connection details.
Keep data under a directory the `preview` user owns, such as `$HOME/preview-db` (a service's
`HOME` is `/home/preview`), outside the checkout. Use a directory the `preview` user owns for
database sockets too; a cold boot clears `/run`. Connect the API to the database through that
socket: while a run's credential rule is live, a TCP port that is not declared does not answer.

The wrapper must run on **every preview start**, supervise both processes, and handle shutdown.
Do not start the database only in `setup`: successful setup does not run again in that sandbox.
Stop sends SIGTERM to every process of the `preview` user, then SIGKILL after ten seconds if
needed. The agent's processes keep running.
The database must tolerate recovery after an abrupt stop. Files survive a successful park;
they are disposable when Factorial releases the sandbox. There is no database backup or volume lifecycle.

Public access applies to the sandbox, not to individual service entries. There is no `private`
service flag. Leaving a process out of `services` is not an access-control boundary.
Test the exposure of every listener and use only disposable preview data.

These rules come from the [launcher and stop scripts](../runtime/src/runtime/adapters/e2b/layout.py),
[preview adapter](../runtime/src/runtime/adapters/e2b/adapter.py),
[network policy](../runtime/src/runtime/adapters/e2b/client.py), and
[template builder](../runtime/src/runtime/adapters/e2b/template.py).

---

## Step 5: check the agent's branch instructions [Procedure]

Read both `system-prompt.md` and `instructions.md` for the build agent.
Compare their checkout and PR commands with the process gates and postconditions.

For a stacked process with spec approval set to `merge: false`, check these rules:

- The agent fetches `spec/<ticket>-<slug>` before it checks for spec files.
- It creates `impl/<ticket>-<slug>` from that spec branch and reuses the plan agent's slug.
- Its PR targets the spec branch, not `main`.
- Revisions reuse the implementation branch and handle both `pr_review_comments` and `change_request` inputs.

Keep `workspace.base` on a resolvable ref such as `main`; do not put `{slug}` there.
The agent switches to the spec branch after checkout. For a process that merges the spec
first, match the instructions to that different workflow.

---

## Step 6: sync and test a new ticket [Procedure]

1. Open a PR into `factorial` with the configuration and prompt changes.
2. Merge with a merge commit or squash, then press **Sync** in the dashboard.
3. Check that sync succeeded. In the editor, **Commit and sync** is the alternative route.
4. Admit a new ticket after sync, using the process's entry label and assignee rules.
5. Move it through the gates until the build run starts.
6. On the ticket page, open **Activity**, then the **Output** of the build run, during the session. Confirm that Terminal and
   Preview open side by side.
7. Let the preview reach READY. Test the page, API-dependent actions, and **Open in new tab**.
8. Send **Request changes** at the gate. Confirm that the drawer closes and **Activity** shows the
   change request. Open the **Output** of the rework. Confirm that the preview is still up.
9. Leave the preview idle. Confirm the five-minute warning and **Keep running**.
10. Press **Restart**, then **Stop preview**, and start it again.

The preview starts by itself when the pane opens on the latest attempt. The pane decides once,
on the first answer it gets, and starts the preview only if that answer is the latest attempt's
preview, not running and not renewing. A Stop or an idle close that you see later, while the pane
stays open, is left alone until you press **Start preview**. Opening the pane again on a stopped
latest attempt starts it again. An active preview shows **Stop preview** and **Restart**.
Repeated Start requests while it starts or serves return the current state; they do not extend
its deadline. **Restart** while it starts does nothing.

Test before the ticket is done: a done ticket stops its awake previews and serves none.
When a gate whose `onReject` reruns the build waits on a successful build, **Request changes**
starts rework through that `onReject`. A rework carries a ready preview over to its
run, unless less than 15 minutes of the sandbox's hour is left; then the loop starts it again on
the rework's run. A failed build can still have a preview if its sandbox remains available.

Configuration and prompts stay pinned to each ticket's admission snapshot. A retry of an
existing ticket does not adopt a later config sync. Inspect any uncommitted-changes warning;
the preview can show files outside the PR diff.

---

## What declared files do [Reference]

Declared files let a reviewer read the files that a step writes beside the step's run: for the
plan step, a summary page, then `spec.md`, `plan.md` and `tasks.md`.

- **The config names the files.** Each entry under `files` is a path on the step's own branch,
  or a pattern with `*` or `**`, with a label. A pattern shows each file that it matches in its
  own tab. The Preview section shows one tab for each file, in the order the block
  declares them, and is called **Files**.
- **The agent learns the paths and the rules.** The run's first turn has a `# Files` section. It
  gives each file's path and label, and the rules for each type: for example, that an HTML file
  must be self-contained and has no JavaScript. It does not say what to write in a file: the
  agent's instructions or a skill say that. The sample's `spec-page` skill says what goes on the
  summary page.
- **The files merge with the work.** The agent commits each file on its branch with the rest of
  its work, so the summary merges with the spec that it summarizes.
- **The worker records what the run changed.** When the run ends, the worker reads the branch on
  GitHub. It records each file that the step's attempts changed there, as a
  `run_outputs_observed` event. The record starts where the branch was when the run started. A
  step that works on the branch of another step does not get that step's files.
- **Only declared files show.** The step can write other files. Factorial records them, but it
  does not show them.
- **The API serves them inert.** A file runs no script and loads nothing from outside itself.
  Its extension sets how it shows: `.html` as written, `.md` rendered as Markdown, and any other
  as plain text. A file is at most 1 MB, and its link is signed for one run. When Google sign-in
  is on, a reader must also sign in with Google. Links in a file open in a new tab.
- **It blocks nothing.** A declared file that the step did not write shows as missing. To make the
  file necessary, add `file_exists` to the postconditions.

Do not type a credential into a file in the drawer. A link without `target="_blank"` in an HTML
file can move the frame to another site. Use **New tab** to get an address bar.

## Step 7: declare the files a step writes [Procedure]

1. Add `files` to the step's `preview:` block:

   ```yaml
   preview:
     files:
       - { path: "specs/{ticket}-{slug}/summary.html", label: "Summary" }
       - { path: "specs/{ticket}-{slug}/spec.md", label: "Spec" }
       - { path: "specs/{ticket}-{slug}/plan.md", label: "Plan" }
       - { path: "specs/{ticket}-{slug}/tasks.md", label: "Tasks" }
   ```

   The editor's Simple template already has this block on its plan step.
2. Write each path as the agent writes it. Use `{ticket}` and `{slug}`. When the agent chooses
   the file names, use a pattern: see **Choose a pattern** below. Make the extension match how
   the file must show: `.html`, `.md`, or any other for plain text.
3. Make sure that `workspace.branch` contains `{ticket}`.
4. For each file that the agent does not write yet, tell the agent what it holds, in its
   instructions or in a skill. Do not put that text in the process file. For the summary, make
   sure that `.factorial/skills/spec-page/SKILL.md` exists, and that `spec-page` is in
   `skills.shared` in the agent's `agent.yaml`. A first commit of the Simple template has both.
   The agent's own instructions must let it commit the file: the sample's plan agent commits its
   spec folder and every file that its `# Files` section lists.
   A repository set up before the summary was a declared file has older copies of both: a
   `SKILL.md` that mentions a `# Page` section, and a plan agent that writes and commits only
   three files. Replace the skill with the copy in `docs/samples/factorial-simple/skills/spec-page/`.
   Take the output contract and the commit rule from the sample's
   `agents/plan-agent/system-prompt.md` and `instructions.md`.
5. Open a PR into `factorial`, merge it, and press **Sync**.
6. Admit a new ticket, or press **Restart on latest** on an open ticket. Wait for the step's run
   to end.
7. On the ticket page, open **Activity**, then the **Output** of the plan run. Make sure that the Files section shows a tab
   for each file.
8. Open each tab. Make sure that the toolbar names the file and its commit, such as
   `specs/12-widget/summary.html at 1a2b3c4`.
9. Test **Reload**, **New tab** and **GitHub**. Click a link in the summary: it must open in a
   new tab.

A rework shows every file, and the toolbar says which files the attempt did not change. A change
request about the summary is answered like any other: the rework pushes a new commit to the pull
request.

Configuration, prompts and skills stay pinned to each ticket's admission snapshot. A ticket
admitted before the sync keeps its old `files` until you press **Restart on latest**.

### Choose a pattern [Procedure]

Use a pattern when the agent chooses the file names, for example one report for each check.

1. Find the folder where the agent's instructions put the files.
2. Keep that folder fixed in the pattern. Use `{ticket}` or `{slug}` where the folder has them.
3. Put `*` in the file name. Add `**/` only if the files are in subfolders.
4. Keep the extension, so that only files of that type match: `reports/{ticket}/*.html`.
5. Give the pattern a label, such as `Reports`. The label names the set of files.
6. Sync, then press **Restart on latest** on a ticket, and open its Files section.

Each file that matches gets a tab. The tab name is the path below the fixed folder, so
`reports/12/lint.html` gets `lint.html`. A pattern shows at most 50 files. A file that a path
without `*` already shows does not show again under a pattern.

## Step 8: diagnose failures [Reference]

| Symptom | Check and action |
|---|---|
| No **Preview** or **Files** section in **Output** | Check the step's enabled `preview:` in the ticket's pinned config. Sync corrections, then press **Restart on latest** or admit a new ticket. |
| **No preview published** | This toolbar text only means no URL is available. Press **Start preview** if present; read the status beneath it. |
| **Preview not available yet**, no Start button | Check for `services` in the pinned block. Without it, Factorial waits for an external URL producer. |
| **Renewing the sandbox** | E2B allows a sandbox an hour between pauses. The preview restarts by itself; wait for it. |
| **Stopped after it sat idle** | Nobody kept it for `stopAfter`. Start it again. |
| Runtime cannot serve a preview | Check the selected runtime's E2B isolation and project runtime access. Use a new run after correction. |
| `setup failed`, `ECONNRESET` at a package registry | Check egress permissions at sandbox creation. Add missing hosts and use a new sandbox. If already permitted, inspect network errors. |
| Missing command, package, or build output | Check setup, package-manager availability, workspace builds, and paths. Read the panel's error and log tail. |
| `setup failed`, `== step 2/3 exited 137` | The setup log names each step, and the exit code of the step that failed. Code 137 shows a kill signal. Usually the kernel stopped the step because it used too much memory. Make the step use less memory, or ask the operator for an E2B template with more memory. |
| `the preview's start script ended before setup finished` | Read the start log under the message. Its last line names the stage. `exit 1` and an error show a command before setup that failed. For example, a file can change during the regroup while the agent runs. Start the preview again after the run ends. `ended by a signal`, or no last line, shows that a process stopped the script. A `killed by the kernel` line names a process that used too much memory. |
| **port N is held by another user** | Another process holds a declared port, often a server the agent left running. Stop that process or start the preview after the run ends. |
| A service fails to rename or delete a build output | The agent owns that entry in the checkout's root, which is sticky. Write it in a separate directory, or let the service own it. |
| The agent cannot rewrite a file the preview made | The sandbox protects regular files in the checkout's root. Keep generated files in subdirectories. |
| Service did not answer before its timeout | Check the process log, port, readiness path, required environment, and dependencies. Increase the timeout only for a healthy but slow startup. |
| Ready, but browser shows 403 or API errors | Check allowed hosts, public API URLs, CORS, HTTPS, and database access. The local readiness check does not cover these. |
| Sandbox no longer kept | A sandbox is kept until its ticket exits or its retention passes, seven days by default. Use a newer run or a new ticket. |
| Ticket is done | Factorial serves no preview on a done ticket, and stops its awake previews when the ticket is done. Use a new ticket. |
| A later attempt continues in this workspace | A rework resumes in the previous attempt's sandbox, and the preview carries over to the newer run. Open the newer run's preview. |
| Three previews or open sessions already awake | Stop another preview or close an open session, then retry. A preview beside a live run is not counted. |
| The configuration page says `preview.page is retired` | The block shows nothing. Move the summary under `files` and give the agent the `spec-page` skill, as Step 7 says. |
| **The file comes when attempt N ends** does not change | The run has not ended, or it is interrupted. The worker records an interrupted run only when it ends, because its resume can still push. |
| **Getting the file from GitHub** does not change | The worker waits while the ticket is exited, and retries while GitHub fails. |
| **The file could not be looked up** | GitHub failed until the next attempt started, or the commit's tree was too large to list. |
| **The file is over 1 MB** | The file is over the 1 MB limit, so it is not framed. Make it smaller, or read it on GitHub. Draw diagrams as SVG, not as large raster `data:` images. |
| The frame shows `the page is not UTF-8 text` | The file is a Markdown or plain-text file that is not UTF-8 text, for example an image. Write it as UTF-8 text, or read it on GitHub. |
| The frame shows `the page renders too large to show` | The Markdown file makes too large a page, for example a wide table with many rows. Make the tables smaller, or read the file on GitHub. |
| A file shows as plain text | The path does not end in `.html` or `.md`. Change the path under `files` (Step 7). |
| **The file has no link** | `FACTORIAL_API_TOKEN` is empty, or the record has no file hash. Read the run's `run_outputs_observed` event, and read the file on GitHub. |
| A chart, image or font does not show | The file runs no script and loads nothing from outside itself. In an HTML file, use inline SVG, a `<style>` element and `data:` URIs. In a Markdown file, use `data:` images: raw HTML such as `<svg>` shows as text. |
| A link opens inside the drawer | The HTML file's link has no `target="_blank"`. Use **New tab**, and correct the skill or the file. A Markdown file's links always open a new tab. |
| Sync refuses the `files` block | The message names the rule. Correct the path as Step 7 says. |
| A file tab says **File not found** | The step did not change the file on its branch. The tab lists the files that the step changed: find the file there, and correct the path. Check the slug in the directory name: it must be the same as the branch's slug. For a file such as the summary, check that the agent's instructions or skill tell it to write the file (Step 7). |
| A pattern shows no file, or the wrong files | Open its tab. A pattern with no match lists the files that the step changed. Correct the pattern as **Choose a pattern** says, sync, then press **Restart on latest** on the ticket. A `*` does not match `/`: use `**/` for files in subfolders. |
| A pattern says that it matched more files than it shows | A pattern shows 50 files at most. Open the folder on GitHub from the notice, or make the pattern narrower. |
| The Summary tab says **File not found**, and the agent's `spec-page` skill mentions a `# Page` section | The repository has the skill and the plan agent from before the summary was a declared file. Update both as Step 7 says. |
| A file tab says **More than one file matches** | The run did not name one branch slug, and two directories match. Delete the stale directory or branch. |
| A file tab says the record was cut off | The step changed more than 300 files. A path without `*` still shows its file. A pattern shows only the files in the record. |

### Which changes need a new run? [Reference]

| Change | When it takes effect |
|---|---|
| Process YAML, agent manifests, prompts or skills | After merge and sync, for a newly admitted ticket. |
| Project egress permissions | At sandbox creation. Restarting an existing preview keeps its original list. |
| Successful setup | Once per sandbox. Restarting services does not rerun it. |
| New build run | Gets its own preview; the previous run's sandbox stays kept under the same rule. A rework continues in the previous sandbox and carries its preview over. |
| New run of a step with declared files | Records its own files. Each attempt's tabs show that attempt's files. |

Schema validation checks configuration structure and gate relationships. It does not test
downloads, application startup, file serving, or browser behavior. Complete Step 6 or Step 7
before calling the preview setup working.
