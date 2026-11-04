# Sourced by every service that runs orchestrator code. These name paths and addresses that exist
# only inside the image, and an exported variable outranks `.env`, so a `.env` written for
# `uv run` on the host cannot point a service at a file that is not here. Nothing below is a
# secret: an agent run inherits this environment (see the Dockerfile header).
. /etc/factorial/database.sh
export HOME=/home/factorial
# Both belong to a laptop only: the compose stack, or a plain `docker run`. An ECS task, the one
# external-database case, has its URL in /app/.env and its terminal on the dashboard's origin. On
# a laptop the API serves the dashboard and the terminal is another port, so it admits the API's.
# That origin is added to the ones `.env` names, read by the parser the settings use, never put
# in their place: a server's own address written there would be dropped without a word.
# Both loopback spellings, since a browser keeps a cookie per host and a page opened on either one
# must be admitted.
# A database URL `.env` names is left to `.env`, never exported, when it names the compose
# service FACTORIAL_IMAGE_DATABASE_HOST names, on 5432: the public compose's factorial.env, whose
# role the `database` service made its volume with (postgres-standalone), and whose password is a
# secret. Any other is replaced by `factory`'s: a `.env` written for `make dev` names localhost,
# which no server in the app's container answers.
if ! factorial_external_database; then
  FACTORIAL_DATABASE_URL="$(python -I -c '
import sys
from urllib.parse import urlsplit
from dotenv import dotenv_values
named = (dotenv_values(sys.argv[1]).get("FACTORIAL_DATABASE_URL") or "").strip()
service, host = sys.argv[2].lower(), sys.argv[3]
url = urlsplit(named)
try:
    port = url.port
except ValueError:
    port = -1
if not (service and url.hostname == service and port in (None, 5432)):
    print(f"postgresql+asyncpg://factory:factory@{host}:5432/factory")
' /app/.env "${FACTORIAL_IMAGE_DATABASE_HOST:-}" "$(factorial_own_database_host)")"
  if [ -n "$FACTORIAL_DATABASE_URL" ]; then
    export FACTORIAL_DATABASE_URL
  else
    unset FACTORIAL_DATABASE_URL
  fi
  FACTORIAL_CORS_ORIGINS="$(python -I -c '
import json, sys
from dotenv import dotenv_values
named = (dotenv_values(sys.argv[1]).get("FACTORIAL_CORS_ORIGINS") or "").strip()
own = ["http://localhost:8000", "http://127.0.0.1:8000"]
if named.startswith("["):
    print(json.dumps([*json.loads(named), *own]))
else:
    print(",".join([named, *own] if named else own))
' /app/.env)"
  export FACTORIAL_CORS_ORIGINS
fi
export FACTORIAL_RUNTIME__CLAUDE_CLI__LOCAL_ROOT=/data/runs/claude_cli
export FACTORIAL_RUNTIME__CODEX_CLI__LOCAL_ROOT=/data/runs/codex_cli
export FACTORIAL_RUNTIME__E2B__STATE_ROOT=/data/runs/e2b
export FACTORIAL_ASSISTANT__WORK_ROOT=/data/assistant
export FACTORIAL_DASHBOARD_DIR=/app/ui/dist
# The load balancer forwards everything under the prefix, so a `.env` saying true would publish
# every route's schema; only `make api` on the host turns them on.
export FACTORIAL_API_DOCS=false
# Inside the container's own network namespace: compose publishes it on 127.0.0.1 only, and on
# ECS the task's security group admits only the load balancer.
export FACTORIAL_TERMINAL__BIND=0.0.0.0
# Host path in .env is not in the image. A mounted extra CA (any private PKI) wins.
if [ -s /run/factorial/gitlab-ca.pem ]; then
  export FACTORIAL_GITLAB__CA_BUNDLE=/run/factorial/gitlab-ca.pem
fi
# Opt-in laptop Bedrock stand-in (`BEDROCK_TOKEN_FILE=`). A deployment uses the task role.
if [ -s /run/factorial/bedrock.token ]; then
  AWS_BEARER_TOKEN_BEDROCK="$(tr -d '\r\n' < /run/factorial/bedrock.token)"
  export AWS_BEARER_TOKEN_BEDROCK
fi
