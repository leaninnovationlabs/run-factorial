#!/bin/sh
# Usage: e2b-deployment.sh <env file> <state file>
# Gives an install the E2B deployment name it did not choose. The E2B reaper kills the sandboxes
# that carry this name and that no run wants, so the name must be unique per install (two copies
# on one E2B team would reap each other's live sandboxes) and stable across restarts and image
# upgrades (a new name would orphan the old sandboxes). The state file, on the /data volume,
# keeps it. A name the env file already holds is never replaced, however it is written: Settings
# itself reads the file, so "set" means what Settings reads. Prod's name arrives that way, and
# `init` calls this script only on the image's own database.
set -eu
env_file="$1"
state_file="$2"

# Exit 0 is a name, 3 is none; anything else, such as a missing interpreter or a bad value
# elsewhere in the file, stops the script rather than shadow a name it could not read.
status=0
python -I -c '
import sys
from orchestrator.settings import Settings
named = Settings(_env_file=sys.argv[1]).runtime.e2b.deployment
sys.exit(0 if named.strip() else 3)
' "$env_file" || status=$?
case "$status" in
  0) exit 0 ;;
  3) ;;
  *)
    echo "factorial: could not read the E2B deployment name from $env_file" >&2
    exit 1
    ;;
esac

if [ -e "$state_file" ]; then
  chmod 0600 "$state_file"
  name="$(head -n 1 "$state_file" | tr -d '\r\n')"
  case "$name" in
    "" | *[!A-Za-z0-9._-]*)
      echo "factorial: $state_file does not hold a deployment name: fix or remove it" >&2
      exit 1
      ;;
  esac
else
  name="factorial-$(od -An -N4 -tx1 /dev/urandom | tr -d ' \n')"
  (umask 077 && printf '%s\n' "$name" > "$state_file")
fi

# Appended in place: the file keeps the mode and owner init gave it.
if [ -s "$env_file" ] && [ -n "$(tail -c 1 "$env_file")" ]; then
  printf '\n' >> "$env_file"
fi
printf 'FACTORIAL_RUNTIME__E2B__DEPLOYMENT=%s\n' "$name" >> "$env_file"
