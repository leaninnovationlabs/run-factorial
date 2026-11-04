# GitLab as a ticket source

A project whose `ticket_source.kind` is `gitlab` uses that project's GitLab URL and
`GITLAB_TOKEN` for observe, project, config, gates and run credentials. It never falls
back to the GitHub App. GitHub projects are unchanged.

## Configure the orchestrator

```bash
FACTORIAL_GITLAB__BASE_URL=https://gitlab.example.com
FACTORIAL_GITLAB__TOKEN=...          # Maintainer, so the worker can mint a Developer run token
FACTORIAL_GITLAB__WEBHOOK_SECRET=... # optional wake-up at POST /webhooks/gitlab
FACTORIAL_GITLAB__BOT_LOGIN=...      # the username approvals and delivery probes ignore
FACTORIAL_GITLAB__CA_BUNDLE=...      # optional path to extra CA PEMs (any private PKI)
```

On the project: set the ticket source to `kind: gitlab` and `external_ref` to
`group/project` or a nested path such as `group/sub/project`. Store `GITLAB_TOKEN` in the project's secret store if it is not the
process-wide token. `settings.gitlab.base_url` and `settings.gitlab.bot_login` override
the process-wide values per project.

### Rules for the GitLab URL and credentials of a project

An organization owner can write project settings. Thus the orchestrator examines
`settings.gitlab.base_url` when a person writes it, and again each time it makes a client:

- The origin of `FACTORIAL_GITLAB__BASE_URL` is always permitted.
- Any other URL must not have a user name, a password, port 0, a query or a fragment.
- The host must be ASCII. Write an internationalized name in punycode.
- The host must not be an address that is loopback, link-local, unspecified or multicast.
  The same rule applies to the addresses that a host name resolves to. The orchestrator keeps
  each DNS answer for two minutes. If a lookup takes more than two seconds or the resolver
  fails, the orchestrator does not make a client. It holds the tickets of the project and does
  not count a failure, and it does the lookup again on the next pass. The API answers 503.
- When `FACTORIAL_MULTI_ORG__ENABLED=true`, the URL must also use `https`, and the host must
  not be a private, shared (`100.64.0.0/10`) or reserved address. When multi-org is off, a
  project can use `http`.

The orchestrator uses `FACTORIAL_GITLAB__TOKEN` and `FACTORIAL_GITLAB__WEBHOOK_SECRET` for a
project only when these two conditions are true:

- The GitLab URL of the project has the same origin as `FACTORIAL_GITLAB__BASE_URL`.
- Multi-org is off.

In all other cases, the project must keep its own `GITLAB_TOKEN`. For webhooks, it must also
keep its own `GITLAB_WEBHOOK_SECRET`.

A host with a private CA (DoD, corporate, or otherwise) needs extra certificates. Set
`FACTORIAL_GITLAB__CA_BUNDLE` to a PEM file, or keep the PEM text as the project secret
`GITLAB_CA_BUNDLE`. When multi-org is off, a project can also name a PEM file on the host in
`settings.gitlab.ca_bundle`. When multi-org is on, a project cannot name a file path: the API
refuses a path in `settings.gitlab.ca_bundle`, and the orchestrator ignores a path that it
finds there.
Extra CAs are added to the system store; they do not replace it.
Each GitLab agent run gets that combined file as `SSL_CERT_FILE`, `CURL_CA_BUNDLE`
and `GIT_SSL_CAINFO`, so curl, git and Node on a laptop or a VM trust the same host.

For the image, put the same PEM at `.local/ca-bundle.pem` and start it with
`scripts/run-local-container.sh`, which mounts that file (or keep
`FACTORIAL_GITLAB__CA_BUNDLE` as a host path for `uv run`). The image copies it to
`/run/factorial/gitlab-ca.pem` and exports that path, so a laptop path in `.env` is not
required inside the container. Set `FACTORIAL_GITLAB__BASE_URL` and `FACTORIAL_GITLAB__TOKEN`
in `.env` when the image uses its own Postgres.


Webhook: point GitLab at `$FACTORIAL_AUTH__EXTERNAL_URL/factorial/webhooks/gitlab` with the same
secret. Webhooks only wake a poll; polling stays authoritative. Many projects can use the same
GitLab repository. Each of these projects compares `X-Gitlab-Token` with its own secret. A
delivery wakes only the projects whose secret it matches. If no secret matches, the API sends
401. The API refuses a body that is larger than 2 MB.

## What matches GitHub

- Issues, merge requests, approvals and pipelines map onto the same snapshot types
- **Request changes on a merge request** — use GitLab’s MR review flow (Start review →
  **Request changes** → Submit), the same idea as **Request changes** on a GitHub pull request
  review. Conversation notes on the MR or the issue alone do not send work back; the dashboard
  **Request changes** button still works on either forge
- Config read, commit and sync on the `factorial` branch, without the GitHub App
- Dashboard ticket source, gate MR links, labels, protection and CI-file readiness
- MCP `create_ticket`, pending-issue reads, ticket get of a not-yet-admitted issue, and ticket URLs
- `kind: action` contract mutations and embedded issue images
- Assistant/MCP config read and commit through the project token
- Unknown-head adopt (`compare` of the `factorial` head against the last known commit)
- Per-run Developer project token, revoked at park and terminal; `GH_TOKEN` is the git
  channel the adapters already honour, plus `GIT_HTTP_HOST`

## What is still GitHub-only

- The in-app assistant's read token is still a GitHub App installation token. GitLab
  projects are left out of that mint; assistant and MCP tools read those repos with the
  project's `GITLAB_TOKEN` instead.
- Empty `factorial` branch seed cannot orphan: GitLab has no orphan-commit API, so a
  README commit is recorded after the branch exists.

## Agent prompts

The sample at `docs/samples/factorial-simple/` talks to GitHub (`gh pr create`). For a
GitLab project, the editor's **Simple template (GitLab)** is the process files of
`docs/samples/factorial-gitlab/`: `primary` (no test step) and `simple`. Importing it
writes those lanes, and the agents of `docs/samples/factorial-gitlab/` with them. The
project's issue source selects the agents: a commit into a GitLab project writes GitLab
agents from either template. These agents open merge requests with `glab` and never call
`gh`.

An E2B Runtime needs a template that has `glab`. If yours has none, build a new template with
`make e2b-template`, and set the Runtime to the newest build of that name. On E2B, `glab` sends
a placeholder token, and the E2B proxy replaces it with the real token, as it does for `gh`. A
host Runtime needs `glab` on the host. An image built from this version or later has it.
