#!/usr/bin/env bash
# Local image only. A deploy does not use this script.
# The plain `docker run` of docs/local-container.md, for the files `make up` cannot leave out or
# add: an extra CA bundle and a laptop Bedrock token. The container runs its own Postgres on
# factorial-data, so `make down` first; `make up` refuses while this container exists.
set -euo pipefail

cmd=(
  docker run -d --name factorial --stop-timeout 60
  --security-opt "seccomp=${PWD}/docker/seccomp.json"
  -p 127.0.0.1:8000:8000 -p 127.0.0.1:8001:8001
  -v factorial-data:/data
  -v "${PWD}/.env:/run/secrets/factorial.env:ro"
)

if [[ -s "${PWD}/.local/ca-bundle.pem" ]]; then
  cmd+=(-v "${PWD}/.local/ca-bundle.pem:/run/secrets/gitlab-ca.pem:ro")
fi
# Opt-in laptop Bedrock stand-in. A deploy mints from the task role.
if [[ -n "${BEDROCK_TOKEN_FILE:-}" && -s "${BEDROCK_TOKEN_FILE}" ]]; then
  cmd+=(-v "${BEDROCK_TOKEN_FILE}:/run/secrets/bedrock.token:ro")
fi

cmd+=(factorial:local)
exec "${cmd[@]}"
