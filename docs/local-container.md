# How to run the factory in one container

**Status:** procedure · [Simplified Technical English](writing-rules.md)

One Docker image holds the whole application: Postgres, the API, the worker, the terminal
service, the assistant, tmux, `glab`, and the `claude` and `codex` CLIs. The API also serves the
dashboard. The worker
starts Claude and Codex agents inside the container. You build the image on your machine.

On your machine, `make up` starts the same container, and a second container, `database`, for
the database. Both containers come from the same image. `make dev` uses the `database`
container too.

This page gives the steps.

---

## Before you start

- Docker, started.
- A GitHub App or a GitHub token for each GitHub project. [`setup.md`](setup.md) makes the App.
- A checkout of this repository. The build reads it.

You do not need Python, Node, uv or pnpm on your machine. The image carries them.

---

## Step 1: write `.env`

```bash
cp .env.example .env
```

Set these two values. The container stops at start when one of them is empty. With only
these two values, the container starts. It then offers runtimes on the host only, because the
cloud sandbox (E2B) needs a deployment name. See "codex_cli in the container" below.

| Variable | What to write |
|---|---|
| `FACTORIAL_API_TOKEN` | A random string of 32 characters or more. The command is below. Scripts and the assistant send it to the API. It also signs the session cookies |
| `FACTORIAL_MASTER_KEY` | 32 random bytes as base64. The command is below |

For a GitLab project, also set `FACTORIAL_GITLAB__BASE_URL` (for example
`https://gitlab.example.com`) and `FACTORIAL_GITLAB__TOKEN`.


Make the API token with this command, then copy the output into `.env`:

```bash
openssl rand -base64 32
```

Make the master key with this command, then copy the output into `.env`:

```bash
python3 -c "import base64,os; print(base64.b64encode(os.urandom(32)).decode())"
```

Every other value in `.env.example` has a working default. The agents' model credentials
are not among them: a run reads its key from its Runtime row in the database, which
[`bootstrap.md`](bootstrap.md) creates.

### What the container sets for itself

The container exports the values below, and an exported value outranks `.env`. What you
write for them in `.env` has no effect inside the container. `FACTORIAL_CORS_ORIGINS` is the
one exception: the container adds its value to the origins in `.env`.

| Variable | Value in the container |
|---|---|
| `FACTORIAL_DATABASE_URL` | the Postgres that runs beside the API |
| `FACTORIAL_CORS_ORIGINS` | the origins in `.env`, and `http://localhost:8000`, the dashboard origin, for the terminal service on port 8001 |
| `FACTORIAL_RUNTIME__CLAUDE_CLI__LOCAL_ROOT` | `/data/runs/claude_cli`, on the volume |
| `FACTORIAL_RUNTIME__CODEX_CLI__LOCAL_ROOT` | `/data/runs/codex_cli`, on the volume |
| `FACTORIAL_RUNTIME__E2B__STATE_ROOT` | `/data/runs/e2b`, on the volume |
| `FACTORIAL_DASHBOARD_DIR` | `/app/ui/dist`, the dashboard the image built, which the API serves |
| `FACTORIAL_API_DOCS` | `false`. The API does not serve `/docs` in the container |
| `FACTORIAL_TERMINAL__BIND` | `0.0.0.0`, in the network of the container only |
| `FACTORIAL_ASSISTANT__WORK_ROOT` | `/data/assistant`, on the volume |

`docker/rootfs/etc/factorial/env.sh` holds the list. It sets `FACTORIAL_DATABASE_URL` and
`FACTORIAL_CORS_ORIGINS` only when the container runs its own Postgres. On your machine the
terminal service is on a different port from the dashboard, so it needs the CORS value. On AWS,
the task gives the URL of an RDS database. Then the container does not start Postgres, and CORS
stays empty, because the dashboard and the terminal share one origin behind the ALB.

### Previews on a local machine

A step preview URL must use `https`. A preview on a local machine usually uses `http`.
To accept `http` URLs, set this value in `.env`:

```bash
FACTORIAL_PREVIEW_ALLOW_HTTP=true
```

Do not set this value on a deployment. The preview must also use a different origin from
the dashboard. The dashboard uses `http://localhost:8000`, and `make dev` uses
`http://localhost:5173`, so use a different port for the preview.

### Do not export `.env`

Do not start the container with `--env-file .env`, and do not pass a setting with `-e`.
Every agent run inherits the environment of the worker. The master key must not reach an
agent. Mount the file instead, as Step 3 does.

Do not pass AWS credentials to the container: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` or
`AWS_SESSION_TOKEN`. The startup check stops the container when a secret setting or an AWS
credential is in the environment of a service. On AWS, the container removes the credential
variables of the ECS task (`AWS_CONTAINER_CREDENTIALS_*`, `AWS_CONTAINER_AUTHORIZATION_TOKEN*`)
and its endpoint variables (`ECS_CONTAINER_METADATA_URI*`, `ECS_AGENT_URI`) before the check.

### codex_cli in the container

The sandbox of `codex` needs user namespaces, and the default seccomp profile of Docker
refuses them. `make up` starts the container with `docker/seccomp.json`, a profile
that lets them. A codex Runtime with the `local_cli` isolation runs when the container
has this profile.

You can also start the image without compose. Then the container also runs its own Postgres.
Do not start it while `make up` runs, because both use the `factorial-data` volume. This is
the full command:

```bash
docker run -d --name factorial --stop-timeout 60 --security-opt "seccomp=$PWD/docker/seccomp.json" -p 127.0.0.1:8000:8000 -p 127.0.0.1:8001:8001 -v factorial-data:/data -v "$PWD/.env:/run/secrets/factorial.env:ro" factorial:local
```

In PowerShell, use `${PWD}`, and a backtick at the end of each line:

```powershell
docker run -d --name factorial --stop-timeout 60 `
  --security-opt "seccomp=${PWD}/docker/seccomp.json" `
  -p 127.0.0.1:8000:8000 -p 127.0.0.1:8001:8001 `
  -v factorial-data:/data `
  -v "${PWD}/.env:/run/secrets/factorial.env:ro" `
  factorial:local
```

To test the sandbox, run this command in a shell such as Bash (on Windows, Git Bash or WSL).
It starts a short-lived container from the image:

```bash
docker run --rm --user 10001:10001 --security-opt "seccomp=$PWD/docker/seccomp.json" --entrypoint sh factorial:local -c 'cd /home/factorial && mkdir -p p && cd p && CODEX_HOME=$(mktemp -d -p /home/factorial) /opt/codex/bin/codex sandbox -c sandbox_mode="\"workspace-write\"" -- sh -c "touch inside && echo SANDBOX_OK; touch /home/factorial/outside 2>/dev/null && echo ESCAPED || echo OUTSIDE_BLOCKED"'
```

The expected output is `SANDBOX_OK` and then `OUTSIDE_BLOCKED`. Without the
`--security-opt` flag, the command prints the `bwrap` error `No permissions to create a new
namespace` and exits with code 1.

If you start the container without the profile, for example with a plain `docker run`, the
container starts, but the orchestrator refuses each codex run. The ticket shows this message:

> codex's workspace-write sandbox cannot start on this machine: {detail}. A container needs
> the seccomp profile docker/seccomp.json (--security-opt seccomp=docker/seccomp.json);
> tech-stack decision 52 says why, and that Fargate cannot apply one

The sandbox runs each command of the agent in its own process namespace. A background
process of the agent (`&`, `nohup` or `setsid`) therefore ends when the command that started
it ends. On a macOS host, this is not the case.

Use Docker Desktop on macOS or Windows. A Linux host with AppArmor also needs an AppArmor
change, which this repository does not give. The profile gives every process in the
container the right to create user namespaces (decision 52). Fargate cannot apply the
profile, so `codex_cli` does not run in the ECS task.

A runtime on E2B runs either CLI from the container, because the agent runs in the E2B
sandbox. The image sets the state root. It also gives the deployment name, `factorial-` and eight
hex digits, once, and keeps it in `/data/e2b-deployment`. To use your own name, put
`FACTORIAL_RUNTIME__E2B__DEPLOYMENT` in `.env`. Use a name that no other copy of factorial
uses. Then create the runtime with the E2B
isolation. On the Runtime page, store the key of your E2B team as its `E2B_API_KEY`, then choose
its template from the list of that team's builds. The name of each build shows its CLI version.
If the list is empty, build the templates in that team:

```bash
E2B_API_KEY=<key> ANTHROPIC_API_KEY=<key> OPENAI_API_KEY=<key> make e2b-template
```

The command installs the latest version of each CLI. Then it tests each build: first the
preview firewall, then a short agent session, which bills the model keys. It shows a pin only
for a build that passes both tests. The worker runs agents only on such a build (decisions 25
and 43).
To build a known version, set `CLAUDE_CLI_VERSION` or `CODEX_CLI_VERSION`. The command refuses
claude before 2.1.304 and codex before 0.164.0, because those versions trust the compromised
eMudhra certificates. Each build ignores the E2B layer cache.
After a build, add its slug to the project's `runtime_allowlist` (decision 25). To remove an old
template, first set every runtime to a new build. Then delete the template in E2B.

---

## Step 2: know where the GitHub credentials go

The container holds no GitHub credential. After step 4, connect each GitHub project on its
**Ticket source** page, with an App or a token. The database keeps the key, the webhook
secret and the token encrypted. Never write a GitHub key or token into `.env`, and never
commit one.

Put extra CA PEMs in `.local/ca-bundle.pem` so the image can trust a private GitLab. Only
`scripts/run-local-container.sh` (step 3) mounts that file.

---

## Step 3: build the image and start the container

```bash
make image                # docker build --pull -t factorial:local .
make up                   # the app container and the database container
docker compose logs -f app
```

Each service writes its log as JSON, one object on each line (tech-stack decision 81). To
read the log as text, set `FACTORIAL_LOG_FORMAT: text` in the `environment:` of `app` in
`docker-compose.yml`. The `.env` file does not set it.

`make up` mounts `.env` read-only, applies the seccomp profile of codex
(`docker/seccomp.json`), maps ports 8000 and 8001 to `127.0.0.1` only, and attaches the
`factorial-data` volume. It also publishes the database on `127.0.0.1:5432`. Run it from the
root of the repository, because compose finds `.env` there. `make up` refuses to start while
a container from outside the stack uses `factorial-data`, because two Postgres servers on one
volume corrupt the database. If you ran the one container of an older version, remove it one
time with `docker stop factorial && docker rm factorial`.

`make up` cannot mount an extra CA or a Bedrock token. To use one, run `make down`, then
start one container with `scripts/run-local-container.sh` from the root of the repository.
The script runs the plain `docker run` of this page. It mounts `.env` read-only, and the extra
CA and the Bedrock token only when those files exist. Before you use `make up` again, remove that container with
`docker stop factorial && docker rm factorial`.

The containers and `make dev` use the same ports, so run `make down` before `make dev`.
`make dev` uses the same database: `make db` starts the `database` container alone.

Open the dashboard at `http://localhost:8000/factorial/`. The API answers at
`http://localhost:8000/factorial/api/`. The terminal service of decision 57 listens on port
8001. It serves the Terminal section of a run's Output drawer. Test that it started:

```bash
curl http://localhost:8001/factorial/terminal/healthz
```

The dashboard asks the API where the terminal service is. On your machine, the API gives
`http://localhost:8001`. When `FACTORIAL_AUTH__EXTERNAL_URL` names a host that is not
`localhost`, the API gives the origin of the dashboard. Then a proxy in front of the container
must send `/factorial/terminal/*` to port 8001, as the ALB does on AWS. To use a different
origin, set `FACTORIAL_TERMINAL__PUBLIC_URL` in `.env`, for example
`https://terminal.example.com`. An empty value means the origin of the dashboard.

The terminal service accepts each origin in `FACTORIAL_CORS_ORIGINS` as the dashboard. It
also accepts the origin of `FACTORIAL_AUTH__EXTERNAL_URL`. Thus any local development server
on one of those origins can open a terminal that types into a run.

### Behind a proxy

A proxy in front of the container sends each request from its own address. Then the API sees
one address for all clients, and one client can use the password tries of all clients and
lock their accounts. Write the address of the proxy in `.env`:

```bash
FORWARDED_ALLOW_IPS=172.16.0.0/12
```

Use the address or the network of your proxy. A proxy on a Docker network usually has an
address in `172.16.0.0/12`. Then the API reads the address of each client from the
`X-Forwarded-For` header that the proxy sends. Do not write `*`: then each client can send its
own `X-Forwarded-For` header and choose its address. When the API gets an `X-Forwarded-For`
header from an address that this value does not name, the log of the container shows a warning
one time. The warning starts with `A request from`. On AWS, the task sets this value to the
network of the VPC.

### Bedrock on a laptop

The image has no AWS role. A deployment mints the bearer from the task role.

On a laptop, unset a stale `AWS_BEARER_TOKEN_BEDROCK` first (`401` means it expired), then
either export a fresh `AWS_BEARER_TOKEN_BEDROCK` for the **host** worker, or start the
container with an explicit file:

```bash
unset AWS_BEARER_TOKEN_BEDROCK
export AWS_BEARER_TOKEN_BEDROCK=<a fresh short-term Bedrock API key>
# host processes:
make worker
# or the image, opt-in only:
BEDROCK_TOKEN_FILE=.local/bedrock.token ./scripts/run-local-container.sh
```

The script does not pass the host bearer. It mounts `.local/bedrock.token` only when you
set `BEDROCK_TOKEN_FILE`. Do not put `AWS_ACCESS_KEY_ID` /
`AWS_SECRET_ACCESS_KEY` on the container.

### The Terminal section

The Terminal section needs `FACTORIAL_TERMINAL__ENABLED=true` in `.env`. The default is `false`,
and then the service refuses each terminal.

The dashboard opens a terminal with your session cookie. The service lets you in only when you
are a member of the organization of the run. It records what you type with your email.

When you change `FACTORIAL_API_TOKEN`, start the container again. The terminal service reads
the token only when it starts. The change also signs out every person.

At start, the container does these steps in order:

1. It copies the two mounts to files that its own users can read. On AWS, it writes these files
   from the secrets of the task, and removes the secrets from the environment of each service.
2. It tests your configuration, and stops the container when something is missing.
3. It waits for the `database` container, which makes the database when the volume is new.
   On AWS, it waits for RDS. Without compose, it starts Postgres itself.
4. It applies the migrations.
5. It starts the API, the worker, the terminal service and the assistant.

---

## Step 4: bootstrap a project

A new volume gives you an empty database. Open the dashboard and make an account. The first
account on an empty database is the owner of `default`. The owner invites other people from the
Members page. [`bootstrap.md`](bootstrap.md) tells you how to
make a project, give it credentials, snapshot its config, and switch admission on. Use
`http://localhost:8000/factorial` as the API, and your `FACTORIAL_API_TOKEN` as the token.

---

## Step 5: connect an MCP client

The API also serves the MCP server of [`mcp.md`](mcp.md). Claude Code and Codex on your
machine can connect to it. The URL in the container is `http://localhost:8000/factorial/mcp/`.

Do the steps in [`mcp.md`](mcp.md), with these changes:

- The container maps port 8000 to `127.0.0.1` only, so only your machine can connect.
- Use `http://localhost:8000/factorial/mcp/` as the URL. Keep the slash at the end.

When `FACTORIAL_AUTH__EXTERNAL_URL` is empty, the **MCP access** page shows this URL. When you
set it, the page shows that origin with `/factorial/mcp/` at the end. Use port 8000 in each
case.

---

## The assistant

The `assistant` service runs the chat in the dashboard.
The service runs as its own user, `factorial-assistant`. That user cannot read `/app/.env`.

At start, the container copies these values from `.env` into a file that only the assistant
can read:

- `FACTORIAL_API_TOKEN`
- `FACTORIAL_AUTH__EXTERNAL_URL`
- `FACTORIAL_LOG_LEVEL`
- `FACTORIAL_TERMINAL__IDLE_PARK_AFTER`
- `FACTORIAL_DATABASE_URL`, when the database is not the Postgres of the container
- each `FACTORIAL_ASSISTANT__*` value

The assistant does not get `FACTORIAL_MASTER_KEY`. It cannot read a stored secret. For each
turn, it gets its credential from the API. If the assistant finds a master key at start, it
stops and writes `the assistant holds FACTORIAL_MASTER_KEY` to the log. Remove the key from
the environment of the assistant.

`make dev` runs the assistant as your user, with the `.env` of the repository. There it clears
the master key at start. With `make dev`, no other user separates the assistant from the key.

To turn on the assistant, open **Settings → Assistant settings** in the left navigation of the dashboard. Set its credential
there. Do not write the credential in `.env`.

| Variable | Default | Value |
|---|---|---|
| `FACTORIAL_ASSISTANT__CONCURRENCY` | 4 | The number of chat turns that run at the same time, from 1 to 32 |
| `FACTORIAL_ASSISTANT__RETAIN_DAYS` | 30 | The number of days that a conversation stays after its last message |
| `FACTORIAL_ASSISTANT__OFFERED` | `claude_sdk:api_key,codex_cli:api_key` | The sign-ins that a person can choose for the assistant, as `<backend>:<auth_mode>` entries. The other entries are `claude_sdk:subscription`, `codex_cli:subscription` and `codex_cli:bedrock` |

To make sure that the service runs:

```bash
docker compose exec app /command/s6-rc -a list | grep assistant
curl http://localhost:8000/readyz
```

The `assistant` field of `/readyz` shows if the assistant can start a chat, and when the
service did its last check. When it cannot start a chat, the field gives the reason, and the
API refuses each message until the service starts again without the problem. The field is
`null` until the service starts one time.

To restart only the assistant:

```bash
docker compose exec app /command/s6-svc -r /run/service/assistant
```

A restart stops each chat turn that runs. The conversation then tells you to send the message
again.

The assistant runs its tools in its own process. It asks the API for its read-only GitHub and
GitLab access at `http://127.0.0.1:8000/factorial`, inside the container. It reads a GitLab
project through the API, which adds the token of the project. Set `FACTORIAL_ASSISTANT__API_URL` only
when the API answers on another origin. Give the origin only, because the assistant adds
`/factorial`.

---

## Sessions in the container

A `claude_cli` or a `codex_cli` runtime runs each agent run as a session in the container. The
image has tmux, and the `claude` and `codex` versions that the adapters pin. A `codex_cli`
session also needs the seccomp profile (see "codex_cli in the container"). To watch or type into
a session, set `FACTORIAL_TERMINAL__ENABLED=true` in `.env`. This setting turns on the Terminal
section only. Each run is a session when the setting is on or off.

A session has a time limit. The limit starts when the session starts, and again each time a
person pushes Resume in the dashboard. Work that the agent does in the session does not change
the limit. At the limit, the worker stops the session, keeps its workspace, and puts
`factorial:interrupted` on the issue. To continue the session, push Resume in the Terminal section.
When you remove the label, the session does not continue.

The worker checks a parked session every 5 minutes. When the session stopped at its prompt and its
postconditions are now true, the worker accepts the run. The session does not continue, and the
ticket goes to the next step. The worker does not check a session that you stopped, a session that
stopped during a turn, or a session with a newer comment from a person.

| Variable | Default | Value |
|---|---|---|
| `FACTORIAL_TERMINAL__SESSION_CEILING` | 4 hours | The limit, as seconds or as an ISO 8601 duration, for example `PT4H` |
| `FACTORIAL_TERMINAL__PARKED_RECHECK` | 5 minutes | The time between two checks of a parked session, as seconds or as an ISO 8601 duration, for example `PT5M` |

Each session has its own tmux server, apart from the worker. A restart thus has these results:

- When only the worker restarts, the session continues. The next worker finds the session and
  watches it again. This command restarts only the worker:

  ```bash
  docker compose exec app /command/s6-svc -r /run/service/worker
  ```

- When the container stops, each session stops. `make down`, `docker compose restart app` and a new
  image all stop the container. On AWS, each deploy stops the task. The run fails with a reason that
  starts with `host_lost`, and the step starts again. The workspace of the run stays on the volume.
- When a finish or a stop of the run starts before the container stops, the run keeps that
  result. Thus a run that you interrupt can continue later.

---

## When the container stops at once

The startup check writes the reason to the log:

```bash
docker compose logs app
```

| Message | What to do |
|---|---|
| `is missing` | Run `make up` from the repository root, with `.env` in place |
| `FACTORIAL_API_TOKEN is empty` | Set it in `.env` |
| `FACTORIAL_API_TOKEN is shorter than 32 characters` or `is a published sample value` | Make a new token with `openssl rand -base64 32` and set it in `.env` |
| `FACTORIAL_MASTER_KEY is empty` | Set it in `.env` |
| `FACTORIAL_MASTER_KEY is not valid base64` or `must decode to 32 bytes` | If no secret is stored yet, make a new key with `openssl rand -base64 32`. Otherwise, set the key that the secrets were stored with |
| `settings do not load` | A value in `.env` has the wrong form. The message names it |
| `FACTORIAL_RUNTIME__E2B__DEPLOYMENT is empty` | `FACTORIAL_RUNTIME__OFFERED` has an `@e2b` entry. Set the deployment name in `.env` to a name that no other copy of factorial uses, or remove each `@e2b` entry |
| `is in the services' environment` | Remove that variable from the compose file or the `docker run` command. Put a `FACTORIAL_` setting in `.env`. Do not give an AWS credential to the container |

Correct `.env`, then start the containers again:

```bash
make down && make up
```

---

## How to stop it, and how to delete the data

```bash
make down                         # stop the containers; the data stays
make up                           # start them again on the same data
docker volume rm factorial-data   # after make down: delete the database and the agent runs
```

Use `make down`, not `docker rm -f`. Do not use `docker compose down -v`. The terminal service
closes each terminal before it stops, and this can take 45 seconds. Compose lets the app container
use 60 seconds to stop. When the service cannot close a terminal in an E2B sandbox, that terminal
stays open.

The `factorial-data` volume holds the database and the agent run folders. A container that
you build again from a new image keeps the projects and the tickets in that volume.
