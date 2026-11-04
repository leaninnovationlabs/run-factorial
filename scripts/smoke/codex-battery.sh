#!/usr/bin/env bash
# Codex live battery driver — the operator side of tech-stack decision 34's done bar.
#
# Drives the bootstrap.md runbook for the codex config, one phase per invocation, so a
# human runs (and can inspect) each step. Values leave this script exactly once, into the API;
# nothing secret is ever echoed.
#
#   scripts/smoke/codex-battery.sh check    # binaries, .env key NAMES, API up, project found
#   scripts/smoke/codex-battery.sh secret   # PUT OPENAI_API_KEY on the codex Runtime (codex_api only)
#   scripts/smoke/codex-battery.sh sync     # snapshot .factorial/ from the codex-battery branch
#   scripts/smoke/codex-battery.sh ticket   # file the battery issue (factorial:new, assigned)
#   scripts/smoke/codex-battery.sh worker   # print the worker launch env — run it yourself
#   scripts/smoke/codex-battery.sh watch    # ticket + board state, once
#
# Order: check → secret → sync → ticket → worker (own terminal, next to `make api`) → watch.
# The config comes from the sandbox repo's `codex-battery` BRANCH via sync's `ref` — nothing
# needs merging to run the battery, and re-syncing the default branch reverts it. Its process
# names a codex Runtime (tech-stack decision 70): create it on the Runtimes page with the slug
# FACTORIAL_BATTERY_RUNTIME names (default `codex-battery`) before `sync`.
# Prereqs: `.env` at the repo root with FACTORIAL_API_TOKEN, OPENAI_API_KEY, and the App credentials
# the API already uses; `gh` authenticated; the API running (`make api`). The project is looked up
# in the organization FACTORIAL_BATTERY_ORG names (default `default`); FACTORIAL_BATTERY_REPO
# (owner/repo) is required.

set -euo pipefail

API="${FACTORIAL_BATTERY_API:-http://localhost:8000/factorial}"
# The organization the battery's project lives in (tech-stack decision 68).
ORG="${FACTORIAL_BATTERY_ORG:-default}"
REPO="${FACTORIAL_BATTERY_REPO:?set FACTORIAL_BATTERY_REPO to owner/repo}"
REF="${FACTORIAL_BATTERY_REF:-codex-battery}"
RUNTIME="${FACTORIAL_BATTERY_RUNTIME:-codex-battery}"
TITLE="${FACTORIAL_BATTERY_TITLE:-Add a /uptime endpoint that reports when the service started}"
BODY="${FACTORIAL_BATTERY_BODY:-Expose GET /uptime returning JSON with the process start time (ISO 8601) and seconds since start. Cover it with a test.}"

envval() { sed -n "s/^$1=//p" .env | head -1; }
need() { command -v "$1" >/dev/null || { echo "missing binary: $1"; exit 1; }; }
api() { curl -sS -H "Authorization: Bearer $(envval FACTORIAL_API_TOKEN)" "$@"; }

pid() {
  api "$API/api/orgs/$ORG/projects" | jq -r --arg repo "$REPO" \
    '.[] | select(.github_repo == $repo) | .id' | head -1
}

rid() {
  api "$API/api/orgs/$ORG/runtimes" | jq -r --arg slug "$RUNTIME" \
    '.[] | select(.slug == $slug) | .id' | head -1
}

case "${1:-}" in
check)
  need codex; need gh; need jq; need curl
  codex --version
  for key in FACTORIAL_API_TOKEN OPENAI_API_KEY; do
    [ -n "$(envval "$key")" ] && echo ".env $key: present" || echo ".env $key: MISSING"
  done
  curl -sS -o /dev/null -w "api /healthz: %{http_code}\n" "$API/healthz" \
    || echo "api unreachable at $API — run \`make api\`"
  PID=$(pid); [ -n "$PID" ] && echo "project id for $REPO: $PID" \
    || echo "no project for $REPO — run the bootstrap.md runbook first"
  ;;
secret)
  KEY=$(envval OPENAI_API_KEY); [ -n "$KEY" ] || { echo "no OPENAI_API_KEY in .env"; exit 1; }
  RID=$(rid); [ -n "$RID" ] || { echo "no runtime $RUNTIME in $ORG"; exit 1; }
  jq -n --arg v "$KEY" '{value: $v}' \
    | api -X PUT "$API/api/runtimes/$RID/credentials/OPENAI_API_KEY" \
        -H 'Content-Type: application/json' -d @- >/dev/null
  echo "stored OPENAI_API_KEY on $RUNTIME; names now:"
  api "$API/api/runtimes/$RID/credentials" | jq .
  ;;
sync)
  PID=$(pid); [ -n "$PID" ] || { echo "no project for $REPO"; exit 1; }
  api -X POST "$API/api/projects/$PID/process-versions/sync" \
    -H 'Content-Type: application/json' -d "{\"ref\": \"$REF\"}" | jq .
  ;;
ticket)
  gh issue create --repo "$REPO" --title "$TITLE" --body "$BODY" \
    --label factorial:new --assignee "@me"
  ;;
worker)
  cat <<'EOF'
Run the worker in its own terminal, next to `make api`:

  FACTORIAL_RUNTIME__CODEX_CLI__LOCAL_ROOT="$HOME/.factory/codex-runs" \
  make worker

The Runtime's type says how it pays. codex_subscription bills your own Codex subscription and
is connected from the Runtimes page; the config on the battery branch names gpt-5.6-terra to
match. codex_api bills API credits through the key `secret` stores, and needs the manifests on
gpt-5.x-codex -- a model from the wrong mode fails as a vendor 404/400 on the agent's first turn.
EOF
  ;;
watch)
  PID=$(pid); [ -n "$PID" ] || { echo "no project for $REPO"; exit 1; }
  api "$API/api/projects/$PID/tickets" | jq .
  ;;
*)
  sed -n '2,20p' "$0"
  exit 1
  ;;
esac
