"""The fixed layout inside every sandbox, and the root scripts that run there.

Root owns `RUN_DIR`: the exit code and the launch files. The agent runs as its own uid
(`AGENT_USER`, no sudo) and owns only its home: the checkout and the CLI's config dir. So the
agent cannot forge the exit code (decision 25). Everything lives on disk: a filesystem-only
pause cold-boots, and wipes `/run`.

The scripts are constants, installed at every spawn so they always match this adapter. They
take every value through the environment or a NUL-delimited file, never interpolated. A kept
run's preview has its own scripts and files under `PREVIEW_DIR`, its services run as
`PREVIEW_USER`, and `FIREWALL` keeps them off the egress proxy while a credential rule is live
(decision 54).

A run (decision 56) runs the CLI's TUI in a pane of a root tmux server. Its socket is in
`TMUX_DIR`, root's and 0700, so the agent cannot connect to its own terminal; the server loads
`TMUX_CONF`, which leaves a viewer typing into the pane no key into tmux's own commands.
The CLI's hooks run `SIGNAL` as the agent, appending to `SIGNALS`: the agent's file, in root's
`RUN_DIR`, so the agent can write it but never replace it with a link. Root reads the session
log, which is in the agent's config dir, only as the agent. Codex names its log, a rollout, only
once it runs, so a spawn finds it (`ROLLOUT_FIND`); and every codex launch first puts up a
folder-consent screen, which `CONSENT` takes.

A kept session's view (decision 57) is another session of that root server, opened only once
the agent has stopped: the run's own CLI resumed on its session for a person to read, launched
from files of its own (`VIEW_ARGV`, `VIEW_ENV`), never the session's.
"""

from __future__ import annotations

import base64
import binascii
import posixpath
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from runtime.adapters._cli.launch import SIGNAL_SH
from runtime.adapters.claude_cli.cli import EXIT_COMMAND, INPUT_MARK, VALUE_PROJECT_DIR
from runtime.adapters.codex_cli import cli as codex
from runtime.contract import CONTROL_PLANE_PORT

AGENT_USER: Final = "agent"
AGENT_HOME: Final = f"/home/{AGENT_USER}"
WORKSPACE: Final = f"{AGENT_HOME}/workspace"
#: The preview's services run as their own uid (design.md §7.10): what lets the firewall keep them
#: from the egress proxy while a credential rule is live, a stop end them alone, and the agent
#: not read their environment.
PREVIEW_USER: Final = "preview"
PREVIEW_HOME: Final = f"/home/{PREVIEW_USER}"
#: The group the agent and the preview share the checkout through.
WORKSPACE_GROUP: Final = "ws"
#: envd's port, which E2B's control plane reaches every sandbox on: the inbound rule leaves it open.
ENVD_PORT: Final = CONTROL_PLANE_PORT
#: The CLI's per-run config dir: the agent's to write, since the CLI keeps its session there.
CONFIG_DIR: Final = f"{AGENT_HOME}/.factorial"
#: A run's images (`RunSpec.attachments`): the agent's own, beside the CLI config and outside the
#: checkout it commits from.
ATTACHMENTS_DIR: Final = f"{CONFIG_DIR}/attachments"

ROOT_DIR: Final = "/opt/factorial"
BIN_DIR: Final = f"{ROOT_DIR}/bin"
RUN_DIR: Final = f"{ROOT_DIR}/run"
STDERR: Final = f"{RUN_DIR}/stderr.log"
EXIT_CODE: Final = f"{RUN_DIR}/exit_code"
ARGV: Final = f"{RUN_DIR}/argv"
ENV: Final = f"{RUN_DIR}/env"
#: NUL-delimited mode and path pairs, applied by one `xargs -0 -n 2 chmod`.
MODES: Final = f"{RUN_DIR}/modes"

#: A kept run's preview (decision 54): root's files, on disk, so a cold boot keeps them.
PREVIEW_DIR: Final = f"{RUN_DIR}/preview"
PREVIEW_ENV: Final = f"{PREVIEW_DIR}/env"
PREVIEW_SETUP: Final = f"{PREVIEW_DIR}/setup"
#: NUL-delimited quadruples: name, port, ready path, command.
PREVIEW_SERVICES: Final = f"{PREVIEW_DIR}/services"
PREVIEW_LOG: Final = f"{PREVIEW_DIR}/setup.log"
#: The start script's own output, and a timestamped line as each stage of a start begins.
PREVIEW_START_LOG: Final = f"{PREVIEW_DIR}/start.log"
SETUP_DONE: Final = f"{PREVIEW_DIR}/setup.done"
SETUP_FAILED: Final = f"{PREVIEW_DIR}/setup.failed"
#: Setup's environment: the sandbox's own and the agent's home, never a preview value.
PREVIEW_SETUP_ENV: Final = f"{PREVIEW_DIR}/setup-env"
#: Written once the checkout is the shared group's, so later starts skip the walk.
GROUP_DONE: Final = f"{PREVIEW_DIR}/group.done"


def service_log(name: str) -> str:
    """Where a preview service's output goes. `name` is `[a-z][a-z0-9-]*` (contract)."""
    return f"{PREVIEW_DIR}/{name}.log"


#: A run's tmux server. The agent has no search permission on the dir, so it cannot
#: `connect()` to the socket. A socket path must fit `sun_path` (about 100 bytes).
TMUX_DIR: Final = f"{ROOT_DIR}/tmux"
TMUX_SOCKET: Final = f"{TMUX_DIR}/s"
TMUX_SESSION: Final = "run"
#: A kept session's view (decision 57): a session of the same root server, which never holds the
#: run's own session at the same time, since a view opens only once the agent has stopped.
INSPECT_SESSION: Final = "inspect"
#: The view's launch, as `ARGV` and `ENV` are the session's: root's, 0600, and its own, so a view
#: and a resume never read each other's.
VIEW_ARGV: Final = f"{RUN_DIR}/view-argv"
VIEW_ENV: Final = f"{RUN_DIR}/view-env"
VIEW_STDERR: Final = f"{RUN_DIR}/view-stderr.log"
#: Where the view's hooks append: nowhere, so the run's signals stay its session's.
VIEW_SIGNALS: Final = "/dev/null"
#: Root's, 0644: the only config the server loads (`-f` replaces the system and user files).
TMUX_CONF: Final = f"{ROOT_DIR}/tmux.conf"
#: The hooks' output, one JSON line per hook call (`_cli.launch.SIGNAL_SH`). The agent's, 0600.
SIGNALS: Final = f"{RUN_DIR}/signals.jsonl"
#: A segment's turn text, `turns/<n>.md`, which the pointer prompt `@`-attaches.
TURNS_DIR: Final = f"{CONFIG_DIR}/turns"
#: The CLI's session logs: `CLAUDE_CODE_PROJECT_DIR_NAME` names the dir, the session id the file.
SESSION_LOG_DIR: Final = f"{CONFIG_DIR}/projects/{VALUE_PROJECT_DIR}"


#: Codex's session logs, its rollouts, which it names only once it runs.
ROLLOUT_DIR: Final = f"{CONFIG_DIR}/{codex.ROLLOUT_DIRNAME}"
#: The native binary the codex template's npm package vendors, which a codex session runs: npm's
#: `codex` is a node script, and a SIGKILL to it would orphan the agent (decision 56).
CODEX_NATIVE: Final = "/usr/local/bin/codex-native"


def turn_file(n: int) -> str:
    return f"{TURNS_DIR}/{n}.md"


def session_log(session_id: str) -> str:
    return f"{SESSION_LOG_DIR}/{session_id}.jsonl"


SETUP: Final = f"{BIN_DIR}/setup.sh"
STOP: Final = f"{BIN_DIR}/stop.sh"
SCRUB: Final = f"{BIN_DIR}/scrub.sh"
PREVIEW: Final = f"{BIN_DIR}/preview.sh"
PREVIEW_STATUS: Final = f"{BIN_DIR}/preview-status.sh"
FIREWALL: Final = f"{BIN_DIR}/firewall.sh"
PREVIEW_STOP: Final = f"{BIN_DIR}/preview-stop.sh"
SESSION: Final = f"{BIN_DIR}/session.sh"
TTY_RUNNER: Final = f"{BIN_DIR}/tty-runner.sh"
SIGNAL: Final = f"{BIN_DIR}/signal.sh"
FINISH: Final = f"{BIN_DIR}/finish.sh"
CODEX_FINISH: Final = f"{BIN_DIR}/finish-codex.sh"
CONSENT: Final = f"{BIN_DIR}/consent.sh"
ROLLOUT_FIND: Final = f"{BIN_DIR}/rollout-find.sh"
SESSION_POLL: Final = f"{BIN_DIR}/session-poll.sh"
REWORK: Final = f"{BIN_DIR}/rework.sh"
CAPTURE: Final = f"{BIN_DIR}/capture.sh"
INSPECT: Final = f"{BIN_DIR}/inspect.sh"
VIEW_RUNNER: Final = f"{BIN_DIR}/view-runner.sh"
INSPECT_STATUS: Final = f"{BIN_DIR}/inspect-status.sh"
INSPECT_STOP: Final = f"{BIN_DIR}/inspect-stop.sh"
#: The checkout's and the config dir's resolved paths, a line each: the CLI's trust seeds name
#: them exactly, and both dirs are in the agent's home, which it can relink.
REALPATHS: Final = f"readlink -f {WORKSPACE} {CONFIG_DIR}"

#: Seconds a stopped agent gets before SIGKILL, as the host adapters give it.
GRACE_SECONDS: Final = 10

#: SETUP_SH's exit code when the pinned config commit could not be fetched.
SETUP_CONFIG_FETCH_FAILED: Final = 3

SETUP_SH: Final = f"""#!/bin/bash
# Prepares a fresh sandbox: ensures the agent's uid and its membership of the group it shares the
# checkout with the preview through, the root run dir, and the checkout at $BASE.
# $REPO and $BASE arrive in the environment; $BASE may be a branch or a sha. $CONFIG_SHA, when
# set, is the pinned config commit: its objects are fetched after the checkout (a second fetch
# would move FETCH_HEAD), skipped when the clone already holds it.
set -euo pipefail
id {AGENT_USER} >/dev/null 2>&1 || useradd --create-home --shell /bin/bash {AGENT_USER}
getent group {WORKSPACE_GROUP} >/dev/null || groupadd {WORKSPACE_GROUP}
usermod -aG {WORKSPACE_GROUP} {AGENT_USER}
install -d -o root -g root -m 755 {RUN_DIR}
install -d -o {AGENT_USER} -g {AGENT_USER} -m 700 {CONFIG_DIR}
cd {AGENT_HOME}
as_agent() {{ runuser -u {AGENT_USER} -- "$@"; }}
as_agent git clone --quiet --filter=blob:none --no-checkout "https://github.com/$REPO.git" \\
  {WORKSPACE}
cd {WORKSPACE}
as_agent git fetch --quiet --filter=blob:none origin "$BASE"
as_agent git checkout --quiet --detach FETCH_HEAD
if [ -n "${{CONFIG_SHA:-}}" ] \\
  && ! as_agent git cat-file -e "$CONFIG_SHA^{{commit}}" 2>/dev/null; then
  as_agent git fetch --quiet --filter=blob:none --no-write-fetch-head origin "$CONFIG_SHA" \\
    || exit {SETUP_CONFIG_FETCH_FAILED}
fi
"""

STOP_SH: Final = f"""#!/bin/bash
# Stops every process of the agent's uid: SIGTERM, then SIGKILL after the grace period.
pkill -TERM -u {AGENT_USER} || exit 0
for _ in $(seq 1 {GRACE_SECONDS * 2}); do
  pgrep -u {AGENT_USER} >/dev/null || exit 0
  sleep 0.5
done
pkill -KILL -u {AGENT_USER} || true
"""

#: Every tmux call: the lockdown conf, UTF-8 output, and only ever root's socket.
_TMUX: Final = f"tmux -u -S {TMUX_SOCKET} -f {TMUX_CONF}"
#: Every call but SESSION_SH's start: none of them may start a server of its own.
_TMUX_CLIENT: Final = f"{_TMUX} -N"
_SESSION_TARGET: Final = f"={TMUX_SESSION}"
#: A pane: the session's current window's active pane.
_PANE_TARGET: Final = f"={TMUX_SESSION}:"
_INSPECT_TARGET: Final = f"={INSPECT_SESSION}"

SCRUB_SH: Final = f"""#!/bin/bash
# Before a pause, before a sandbox is left awake to its preview alone, and before a rework writes
# its credentials: nothing of the agent's may keep running, and no credential file its arguments
# name may stay. Killing a run's tmux server hangs its CLI up, which writes its cost
# on the way out.
set -eu
{_TMUX_CLIENT} kill-server 2>/dev/null || true
{STOP}
rm -f -- "$@"
"""

FIREWALL_SH: Final = f"""#!/bin/bash
# Root's firewall in a public run's sandbox (design.md §7.10), one nft table loaded whole and
# atomically. Inbound TCP from anywhere but loopback reaches only $PORTS and envd's port. With
# MODE=contain, {PREVIEW_USER} cannot originate a connection off loopback, nor on loopback to a
# port that is not its own, nor send anything but TCP; its servers still answer. The rules match
# a packet's own fields (a SYN, a source port), never connection state, so a connection it opened
# before the switch is cut: every connection on a declared port, established or mid-handshake, is
# destroyed once the table loads. Loaded before any credential rule is written and lifted
# (MODE=open) only once the last is dropped, so no preview process reaches the egress proxy that
# adds them. A pause clears it; every wake loads it again. $PORTS is the preview's declared ports,
# comma-separated.
set -eu
case "${{MODE:-}}" in
  contain | open) ;;
  *) echo "MODE must be contain or open" >&2; exit 2 ;;
esac
if ! [[ ${{PORTS:-}} =~ ^[1-9][0-9]{{0,4}}(,[1-9][0-9]{{0,4}})*$ ]]; then
  echo "PORTS must be comma-separated port numbers" >&2
  exit 2
fi
IFS=, read -ra listed <<< "$PORTS"
for port in "${{listed[@]}}"; do
  if (( port > 65535 )); then
    echo "PORTS must be comma-separated port numbers" >&2
    exit 2
  fi
done
id {PREVIEW_USER} >/dev/null 2>&1 || useradd --create-home --shell /bin/bash {PREVIEW_USER}
contain=""
if [ "$MODE" = contain ]; then
  owner="meta skuid {PREVIEW_USER}"
  contain="$owner oifname != \\"lo\\" meta l4proto != tcp reject
    $owner oifname != \\"lo\\" tcp sport != {{ $PORTS }} reject
    $owner oifname != \\"lo\\" tcp flags & (syn | ack) == syn reject
    $owner oifname \\"lo\\" meta l4proto != tcp reject
    $owner oifname \\"lo\\" tcp flags & (syn | ack) == syn tcp dport != {{ $PORTS }} reject"
fi
inbound="iifname != \\"lo\\" tcp dport != {{ $PORTS, {ENVD_PORT} }}"
nft -f - <<EOF
table inet factorial {{}}
delete table inet factorial
table inet factorial {{
  chain input {{
    type filter hook input priority 0; policy accept;
    $inbound tcp flags & (syn | ack) == syn reject with tcp reset
  }}
  chain output {{
    type filter hook output priority 0; policy accept;
    $contain
  }}
}}
EOF
if [ "$MODE" = contain ]; then
  declared=""
  for port in "${{listed[@]}}"; do
    declared="${{declared:+$declared or }}sport = :$port"
  done
  ss -K -tn state connected "( $declared )" >/dev/null
fi
"""

PREVIEW_STOP_SH: Final = f"""#!/bin/bash
# Stops every process of the preview's uid: SIGTERM, then SIGKILL every half second for as long
# again as the grace period. Exits 1 if any process of that uid is still left. The agent's and an
# open session's processes are another uid's, and keep running.
pkill -TERM -u {PREVIEW_USER} || exit 0
for _ in $(seq 1 {GRACE_SECONDS * 2}); do
  pgrep -u {PREVIEW_USER} >/dev/null || exit 0
  sleep 0.5
done
for _ in $(seq 1 {GRACE_SECONDS * 2}); do
  pkill -KILL -u {PREVIEW_USER} || exit 0
  sleep 0.5
done
pgrep -u {PREVIEW_USER} >/dev/null && exit 1
exit 0
"""

#: The checkout's regrouping, run as the agent (the checkout is the agent's path, so root never
#: walks it): the agent's own entries become group-writable, except every `.git` (a directory or
#: a gitfile, at any depth) with what is below it. The root is sticky, so the preview cannot rename
#: or delete an entry it does not own: `.git` cannot be swapped for a directory it filled.
_REGROUP: Final = (
    f'find "$1" -name .git -prune -o -user {AGENT_USER} ! -type l '
    f"-exec chgrp {WORKSPACE_GROUP} {{}} + -exec chmod g+rwX {{}} + "
    f'&& find "$1" -name .git -prune -o -user {AGENT_USER} -type d -exec chmod g+s {{}} + '
    '&& chmod +t "$1"'
)

PREVIEW_SH: Final = f"""#!/bin/bash
# Serves a kept run's preview: setup once, as {AGENT_USER} with no preview value, then every service
# as {PREVIEW_USER} with the preview's environment and PORT set (design.md §7.10). The checkout, but
# for every `.git`, is shared through group {WORKSPACE_GROUP}: regrouped once per sandbox as
# {AGENT_USER} (root never walks a path the agent controls), and both write with umask 002.
# Values arrive in NUL-delimited files, never interpolated. Stays alive while any service runs, so
# its pid says whether the preview can still serve.
# Its own output goes to {PREVIEW_START_LOG}, with a timestamped line as each stage begins and one
# at its end: `exit <code> during <stage>`, or `ended by a signal during <stage>`. A start with
# neither was killed. Setup's output goes to {PREVIEW_LOG}, each step under a numbered header, and
# with its exit code when it fails.
set -uo pipefail
exec >>{PREVIEW_START_LOG} 2>&1
# Its own file each time: a trap that fires inside a redirected call writes where that call does.
log() {{
  echo "$(date -u +%FT%TZ) $*" >>{PREVIEW_START_LOG}
}}
mark() {{
  stage=$1
  log "$1"
}}
quit() {{
  trap - EXIT
  log "exit $1 during $stage"
  exit "$1"
}}
# Every exit of its own goes through `quit`: a signal that ends it mid-command leaves `$?` at 0
# here, so this trap cannot report a code.
trap 'log "ended by a signal during $stage"' EXIT
mark starting
id {PREVIEW_USER} >/dev/null 2>&1 || useradd --create-home --shell /bin/bash {PREVIEW_USER}
getent group {WORKSPACE_GROUP} >/dev/null || groupadd {WORKSPACE_GROUP}
usermod -aG {WORKSPACE_GROUP} {AGENT_USER} && usermod -aG {WORKSPACE_GROUP} {PREVIEW_USER} || quit 1
# The preview passes through the agent's home to the checkout; its config dir stays 700.
chmod 711 {AGENT_HOME} || quit 1
# E2B starts this in root's home, which the agent's find cannot run its -exec from.
cd {WORKSPACE}
if [ ! -f {GROUP_DONE} ]; then
  mark "regrouping the checkout"
  setpriv --reuid={AGENT_USER} --regid={AGENT_USER} --init-groups --reset-env \\
    bash -c '{_REGROUP}' regroup {WORKSPACE} && touch {GROUP_DONE} || quit 1
fi
mapfile -d '' -t SETUP_ENVV < {PREVIEW_SETUP_ENV}
mapfile -d '' -t ENVV < {PREVIEW_ENV}
as_agent() {{
  setpriv --reuid={AGENT_USER} --regid={AGENT_USER} --init-groups --reset-env \\
    env -i "${{SETUP_ENVV[@]}}" "$@"
}}
as_preview() {{
  setpriv --reuid={PREVIEW_USER} --regid={PREVIEW_USER} --init-groups --reset-env \\
    env -i "${{ENVV[@]}}" "$@"
}}
umask 002
rm -f {SETUP_FAILED}
if [ ! -f {SETUP_DONE} ]; then
  mapfile -d '' -t STEPS < {PREVIEW_SETUP}
  n=0
  for step in "${{STEPS[@]}}"; do
    n=$((n + 1))
    mark "setup step $n/${{#STEPS[@]}}"
    echo "== step $n/${{#STEPS[@]}}: $step" >>{PREVIEW_LOG}
    as_agent bash -c "$step" >>{PREVIEW_LOG} 2>&1
    code=$?
    if [ "$code" -ne 0 ]; then
      echo "== step $n/${{#STEPS[@]}} exited $code" >>{PREVIEW_LOG}
      touch {SETUP_FAILED}
      quit 1
    fi
  done
  touch {SETUP_DONE}
  mark "setup done"
fi
mapfile -d '' -t S < {PREVIEW_SERVICES}
for ((i = 0; i < ${{#S[@]}}; i += 4)); do
  mark "starting service ${{S[i]}} on port ${{S[i+1]}}"
  as_preview PORT="${{S[i+1]}}" bash -c "${{S[i+3]}}" >>"{PREVIEW_DIR}/${{S[i]}}.log" 2>&1 &
done
mark serving
wait
quit 0
"""

#: Read once a preview's start script is gone before its setup finished: the end of its own log and
#: of setup's, and any process the kernel killed for want of memory, which neither log records.
PREVIEW_ENDED: Final = (
    f"tail -c 2000 {PREVIEW_START_LOG} 2>/dev/null\n"
    f'if [ -s {PREVIEW_LOG} ]; then echo "-- setup log --"; tail -c 2000 {PREVIEW_LOG}; fi\n'
    "killed=$(dmesg 2>/dev/null | grep -i 'killed process' | tail -n 3)\n"
    'if [ -n "$killed" ]; then echo "-- killed by the kernel --"; echo "$killed"; fi\n'
    "exit 0\n"
)

#: Node is in every template (the CLIs install through npm), so it is the ready check's client.
_READY_JS: Final = (
    'fetch("http://127.0.0.1:" + process.env.PORT + process.env.READY_PATH, '
    "{signal: AbortSignal.timeout(2000)})"
    ".then((r) => process.exit(r.status < 500 ? 0 : 1), () => process.exit(1))"
)

PREVIEW_STATUS_SH: Final = f"""#!/bin/bash
# Reports a kept run's preview, one fact per line, which the adapter reads as data: `setup
# failed` and the setup log's tail; or `setup running`; or `serving`, then `head <sha>`, `dirty`
# when the checkout holds uncommitted changes, and `ready <name>` for each service that answers
# its ready path on 127.0.0.1 with a status below 500 while a listener of {PREVIEW_USER}'s holds its
# port, or `held <name>` when any listener of another uid does (each line's first uid field;
# root's lines carry none). Git runs as {AGENT_USER}, with the checkout's hooks and fsmonitor off
# and its submodules skipped, which the preview could otherwise set.
set -u
if [ -f {SETUP_FAILED} ]; then
  echo "setup failed"
  tail -c 2000 {PREVIEW_LOG}
  exit 0
fi
if [ ! -f {SETUP_DONE} ]; then
  echo "setup running"
  exit 0
fi
echo serving
cd {WORKSPACE}
agit() {{
  runuser -u {AGENT_USER} -- git -c core.hooksPath=/dev/null -c core.fsmonitor=false "$@"
}}
echo "head $(agit rev-parse HEAD 2>/dev/null)"
if [ -n "$(agit status --porcelain --ignore-submodules=all 2>/dev/null | head -c 1)" ]; then
  echo dirty
fi
READY_JS='{_READY_JS}'
PUID=$(id -u {PREVIEW_USER})
mapfile -d '' -t S < {PREVIEW_SERVICES}
for ((i = 0; i < ${{#S[@]}}; i += 4)); do
  listeners=$(ss -Htlne "sport = :${{S[i+1]}}")
  owners=$(printf '%s\\n' "$listeners" | awk -v p="$PUID" '{{
    for (n = 1; n <= NF; n++) if ($n ~ /^uid:[0-9]+$/) {{
      print ($n == "uid:" p) ? "mine" : "other"; next
    }}
  }}')
  if printf '%s\\n' "$owners" | grep -qx other; then
    echo "held ${{S[i]}}"
  elif printf '%s\\n' "$owners" | grep -qx mine \\
    && PORT="${{S[i+1]}}" READY_PATH="${{S[i+2]}}" node -e "$READY_JS"; then
    echo "ready ${{S[i]}}"
  fi
done
exit 0
"""

#: SESSION_SH's exit code for a runner that never wrote one: its CLI was hung up on.
HANGUP_EXIT_CODE: Final = 255

SESSION_SH: Final = f"""#!/bin/bash
# Starts a session (decision 56): a root tmux server on the lockdown conf, its socket in
# a dir closed to the agent, whose one pane runs {TTY_RUNNER}. Creates the signals file as the
# agent's, kept across segments. Stays alive while the session does, so its pid is the run's
# liveness; then writes exit code {HANGUP_EXIT_CODE} if the runner never wrote one, as a hangup
# leaves it. tmux's own error, when it cannot start, goes to {STDERR} with the CLI's.
set -uo pipefail
export LANG=C.UTF-8
install -d -o root -g root -m 700 {TMUX_DIR}
[ -f {SIGNALS} ] || install -o {AGENT_USER} -g {AGENT_USER} -m 600 /dev/null {SIGNALS}
{_TMUX_CLIENT} kill-server 2>/dev/null
rm -f {EXIT_CODE} {TMUX_SOCKET}
{_TMUX} new-session -d -s {TMUX_SESSION} -x 200 -y 50 -- /bin/bash {TTY_RUNNER} 2>>{STDERR} \\
  || exit 1
while {_TMUX_CLIENT} has-session -t {_SESSION_TARGET} 2>/dev/null; do
  sleep 1
done
if [ ! -f {EXIT_CODE} ]; then
  echo {HANGUP_EXIT_CODE} > {EXIT_CODE}.tmp && mv {EXIT_CODE}.tmp {EXIT_CODE}
fi
"""

TTY_RUNNER_SH: Final = f"""#!/bin/bash
# The pane's process, as root: runs the CLI as {AGENT_USER} on the pane's terminal, then writes
# its exit code, which the agent therefore cannot forge. Never traps SIGHUP: a hangup must end
# this shell with the CLI, or an idle CLI lingers half a minute before it exits. With umask 002,
# so the preview's user can change what it writes in the shared checkout.
set -uo pipefail
mapfile -d '' -t ARGV < {ARGV}
mapfile -d '' -t ENVV < {ENV}
umask 002
cd {WORKSPACE} && setpriv --reuid={AGENT_USER} --regid={AGENT_USER} --init-groups --reset-env \\
  env -i "${{ENVV[@]}}" "${{ARGV[@]}}" 2>>{STDERR}
echo "$?" > {EXIT_CODE}.tmp && mv {EXIT_CODE}.tmp {EXIT_CODE}
"""

_ESC: Final = "\x1b"
_QUIT: Final = codex.QUIT_COMMAND
#: How long a TUI whose clear key quits it gets to leave before the quit command is typed: codex
#: 0.159.0 on Linux takes about 5 s to exit after its Ctrl-C.
CLEAR_QUIT_SECONDS: Final = 10


def finish_sh(
    header: str,
    *,
    quit_command: str,
    input_mark: str,
    clear_keys: Sequence[str],
    clear_quits: bool,
    process: str,
) -> str:
    """A TUI's finish script, under its `header`. MODE=exit pastes `clear_keys` apart, then
    `quit_command`, and Enter only once the input line reads `input_mark quit_command` alone.
    `clear_quits` when the last clear key alone quits a TUI idle at an empty input line: the
    script then gives it `CLEAR_QUIT_SECONDS` to leave before it types. Then the hangup, and
    `process`, the CLI's name, gets the grace period to leave before STOP."""
    keys = "".join(f"  type_ $'\\x{ord(key):02x}'\n  sleep 0.3\n" for key in clear_keys)
    if _ESC in clear_keys[:-1]:
        keys = "  # Apart, or the Esc is read as Meta+Ctrl-C.\n" + keys
    typing = f"""  type_ {quit_command} -p
  sleep 0.5
  if typed; then
    type_ $'\\r'
    for _ in $(seq 1 20); do
      tmux_ has-session -t {_SESSION_TARGET} 2>/dev/null || break
      sleep 0.5
    done
  fi
"""
    if clear_quits:
        keys += (
            f"  for _ in $(seq 1 {CLEAR_QUIT_SECONDS * 2}); do\n"
            f"    tmux_ has-session -t {_SESSION_TARGET} 2>/dev/null || break\n"
            "    sleep 0.5\n"
            "  done\n"
        )
        indented = "".join(f"  {line}" for line in typing.splitlines(keepends=True))
        typing = f"  if tmux_ has-session -t {_SESSION_TARGET} 2>/dev/null; then\n{indented}  fi\n"
    return f"""#!/bin/bash
{header}set -uo pipefail
export LANG=C.UTF-8
case "${{MODE:-}}" in
  exit | hangup) ;;
  *)
    echo "MODE must be exit or hangup" >&2
    exit 2
    ;;
esac
tmux_() {{ {_TMUX_CLIENT} "$@"; }}
type_() {{
  tmux_ set-buffer -b factorial "$1" \\
    && tmux_ paste-buffer -d "${{@:2}}" -b factorial -t {_PANE_TARGET}
}}
typed() {{
  pane=$(tmux_ capture-pane -p -t {_PANE_TARGET}) || return 1
  printf '%s\\n' "${{pane//$'\\xc2\\xa0'/ }}" | grep -qx '{input_mark} {quit_command} *'
}}
if [ "$MODE" = exit ] && tmux_ has-session -t {_SESSION_TARGET} 2>/dev/null; then
{keys}{typing}fi
tmux_ kill-server 2>/dev/null
for _ in $(seq 1 {GRACE_SECONDS * 2}); do
  pgrep -u {AGENT_USER} -x {process} >/dev/null || break
  sleep 0.5
done
{STOP}
"""


FINISH_SH: Final = finish_sh(
    f"""\
# Ends an interactive run. MODE=exit types `/exit` and waits for the CLI to leave; MODE=hangup,
# for a session on a question (where typing would answer it), skips that. Either way the tmux
# server is then killed, which hangs the CLI up: it writes its cost on the way out, and gets
# the grace period to, before {STOP}. Types with paste-buffer, since send-keys is refused while
# the only client attached is a read-only viewer. Esc first closes any dialog a human left open
# (`/model`, a rewind), where the Enter would apply the highlighted choice; Ctrl-C then empties
# a draft `/exit` would join. Enter is pressed only once the input line reads `/exit` alone;
# otherwise the hangup ends the session instead.
""",
    quit_command=EXIT_COMMAND,
    input_mark=INPUT_MARK,
    clear_keys=(_ESC, "\x03"),
    clear_quits=False,
    process="claude",
)

CODEX_FINISH_SH: Final = finish_sh(
    f"""\
# Ends a codex session. MODE=exit quits the TUI and waits for it to leave; MODE=hangup
# skips that. Either way the tmux server is then killed, which hangs the CLI up, and it gets the
# grace period to leave before {STOP}. Types with paste-buffer, since send-keys is refused while
# the only client attached is a read-only viewer. Ctrl-C, and never Esc, which at an idle prompt
# arms editing the previous message: at an empty input line Ctrl-C quits outright, taking about
# 5 s to exit, and on a draft it only empties it, so `{_QUIT}` is typed if the TUI is still
# there {CLEAR_QUIT_SECONDS} s later. Its Enter is a paste of its own after a gap, since codex
# reads an Enter inside a paste burst as a newline, and is pressed only once the input line reads
# `{_QUIT}` alone; otherwise the hangup ends the session instead.
""",
    quit_command=codex.QUIT_COMMAND,
    input_mark=codex.INPUT_MARK,
    clear_keys=codex.CLEAR_KEYS,
    clear_quits=True,
    process=posixpath.basename(CODEX_NATIVE),
)

#: CONSENT_SH's exit codes: no consent screen came, or it never closed. It prints the screen.
CONSENT_NOT_ASKED: Final = 3
CONSENT_NOT_TAKEN: Final = 4

CONSENT_SH: Final = f"""#!/bin/bash
# Takes the codex TUI's folder-consent screen, which every launch and resume puts up first for a
# checkout not seeded trusted, in the tmux session $TMUX_TARGET names: `run`, the default, or
# `{INSPECT_SESSION}`, a session view's. For the untrusted seed the choice already selected is
# "{codex.CONSENT_RESTRICTED}", which keeps the trust as seeded; one Enter, pasted alone, takes
# it. Exits 0 once the last input-mark line on screen is not a choice: the input line is the
# bottom one, below a human's turns, which the history also draws behind the mark, and a
# dialog holding the bottom pane, such as "Hooks need review" for a hook the agent added, draws
# its selected choice there. Otherwise prints the screen, for the caller to name what is on
# it, and exits {CONSENT_NOT_ASKED} when no consent screen came, {CONSENT_NOT_TAKEN} when no
# prompt followed it; 1 when the TUI is gone. E2B's start returns before session.sh's server
# is up, and until then a resumed sandbox holds the last segment's dead socket, or none: a look
# that fails before any has succeeded is retried quietly, and only the last one's error shown.
set -uo pipefail
export LANG=C.UTF-8
case "${{TMUX_TARGET:={TMUX_SESSION}}}" in
  {TMUX_SESSION} | {INSPECT_SESSION}) ;;
  *)
    echo "TMUX_TARGET must be {TMUX_SESSION} or {INSPECT_SESSION}" >&2
    exit 2
    ;;
esac
pane="=$TMUX_TARGET:"
tmux_() {{ {_TMUX_CLIENT} "$@"; }}
shown=
up=
look() {{
  shown=$(tmux_ capture-pane -p -t "$pane") || return 1
  shown=${{shown//$'\\xc2\\xa0'/ }}
  up=1
}}
asked() {{ [[ $shown == *"{codex.CONSENT_RESTRICTED}"* ]]; }}
for _ in $(seq 1 40); do
  if [ -n "$up" ]; then
    look || exit 1
  else
    look 2>/dev/null
  fi
  asked && break
  sleep 0.5
done
[ -n "$up" ] || look || exit 1
if ! asked; then
  printf '%s\\n' "$shown"
  exit {CONSENT_NOT_ASKED}
fi
sleep 0.3
tmux_ set-buffer -b factorial $'\\r' \\
  && tmux_ paste-buffer -d -b factorial -t "$pane" || exit 1
for _ in $(seq 1 20); do
  look || exit 1
  last=$(grep '^{codex.INPUT_MARK} ' <<<"$shown" | tail -n 1)
  if [ -n "$last" ] && ! grep -Eq '^{codex.INPUT_MARK} [0-9]+\\. ' <<<"$last"; then
    exit 0
  fi
  sleep 0.5
done
printf '%s\\n' "$shown"
exit {CONSENT_NOT_TAKEN}
"""

ROLLOUT_FIND_SH: Final = f"""#!/bin/bash
# Prints each rollout codex began since this launch's {ARGV} was written, one path per line: the
# plain `rollout-*.jsonl` files at `<YYYY>/<MM>/<DD>/` under {ROLLOUT_DIR}, newer than that
# file. A compressed one, or one an earlier launch began, is not this launch's. The dir is the
# agent's, so it is listed only as {AGENT_USER}.
set -uo pipefail
as_agent() {{
  timeout 10 setpriv --reuid={AGENT_USER} --regid={AGENT_USER} --init-groups --reset-env "$@"
}}
as_agent find {ROLLOUT_DIR} -mindepth 4 -maxdepth 4 -type f -name 'rollout-*.jsonl' \\
  -newer {ARGV} 2>/dev/null | sort
exit 0
"""

SESSION_POLL_SH: Final = f"""#!/bin/bash
# One supervisor poll of a session, in one round trip. Prints `<log size> <signals
# size>` (the log's -1 when it is absent), then base64 of {SIGNALS} from byte $SIG_FROM, then
# base64 of the session log $LOG from byte $LOG_FROM, each slice at most $LIMIT bytes and on
# one line. The sizes are read first, so a slice may run past them. The log is the agent's, as
# is the dir it is in, so it is read only as {AGENT_USER}: as root a link there could reach any
# file in the sandbox.
set -uo pipefail
for n in "${{SIG_FROM:-}}" "${{LOG_FROM:-}}" "${{LIMIT:-}}"; do
  if ! [[ $n =~ ^(0|[1-9][0-9]*)$ ]]; then
    echo "SIG_FROM, LOG_FROM and LIMIT must be decimal numbers" >&2
    exit 2
  fi
done
: "${{LOG:?the session log}}"
# Bounded: the agent can swap a FIFO in for the log once it has been checked.
as_agent() {{
  timeout 10 setpriv --reuid={AGENT_USER} --regid={AGENT_USER} --init-groups --reset-env "$@"
}}
signals_size=$(wc -c < {SIGNALS} 2>/dev/null) || signals_size=0
log_size=-1
if as_agent test -f "$LOG"; then
  log_size=$(as_agent sh -c 'wc -c < "$1"' sh "$LOG") || log_size=-1
fi
printf '%s %s\\n' "${{log_size//[[:space:]]/}}" "${{signals_size//[[:space:]]/}}"
tail -c "+$((SIG_FROM + 1))" {SIGNALS} 2>/dev/null | head -c "$LIMIT" | base64 | tr -d '\\n'
echo
if [ "$log_size" != -1 ]; then
  as_agent tail -c "+$((LOG_FROM + 1))" -- "$LOG" | head -c "$LIMIT" | base64 | tr -d '\\n'
fi
echo
"""

#: REWORK_SH's exit code when the checkout is on a branch outside $BRANCH_GLOB.
REWORK_NOT_THE_BRANCH: Final = 3
#: REWORK_SH's exit code when origin no longer has the checkout's branch.
REWORK_BRANCH_GONE: Final = 4
#: REWORK_SH's exit code when the checkout is on no branch.
REWORK_NO_BRANCH: Final = 5

REWORK_SH: Final = f"""#!/bin/bash
# Readies a kept checkout for a continued session (decision 56). $MODE=rework, the default, moves
# it to its pull request's head; $MODE=retry leaves it as the failed attempt left it, on its
# branch, its commits, pushed or not, and its uncommitted files kept, and fetches nothing. Every
# git command runs as {AGENT_USER}: the checkout's config and hooks are the agent's to write, so
# root must never run git there. Hooks and fsmonitor are off besides, so neither runs or writes
# into this report; attribute filters still run, as the agent, as they would in its own turn.
# The branch is the agent's to name too; it is matched and quoted, never run. Prints `<branch>
# <head sha>`. A rework's uncommitted work is stashed, not discarded: it then prints `stash
# <sha>` when a stash was made, then `lost <sha>` when the old HEAD held commits the PR head
# does not. $BRANCH_GLOB is the step's branch pattern; $RUN, the run doing the rework, names the
# stash.
set -uo pipefail
# Root's environment never reaches the programs git starts; HOME comes from passwd, so the
# agent's own git config still applies. LC_ALL=C keeps git's messages the ones matched below.
as_agent() {{
  setpriv --reuid={AGENT_USER} --regid={AGENT_USER} --init-groups --reset-env \\
    env LC_ALL=C GIT_TERMINAL_PROMPT=0 "$@"
}}
agit() {{ as_agent git -c core.hooksPath=/dev/null -c core.fsmonitor=false "$@"; }}
: "${{BRANCH_GLOB:?the branch pattern}}"
cd {WORKSPACE} || exit 1
if ! branch=$(agit symbolic-ref --quiet --short HEAD); then
  echo "the checkout is on no branch" >&2
  exit {REWORK_NO_BRANCH}
fi
case "$branch" in
  $BRANCH_GLOB) ;;
  *)
    echo "the checkout's branch does not match $BRANCH_GLOB" >&2
    exit {REWORK_NOT_THE_BRANCH}
    ;;
esac
if [ "${{MODE:-rework}}" = retry ]; then
  head=$(agit rev-parse --quiet --verify HEAD) || exit 1
  printf '%s %s\\n' "$branch" "$head"
  exit 0
fi
if ! fetched=$(agit fetch --quiet --no-tags origin "refs/heads/$branch" 2>&1); then
  printf '%s\\n' "$fetched" >&2
  case "$fetched" in *"couldn't find remote ref"*) exit {REWORK_BRANCH_GONE} ;; esac
  exit 1
fi
old=$(agit rev-parse --quiet --verify HEAD) || exit 1
lost=
agit merge-base --is-ancestor "$old" FETCH_HEAD 2>/dev/null || lost=$old
before=$(agit rev-parse --quiet --verify refs/stash) || before=
agit -c user.name=factorial -c user.email=factorial@localhost stash push --quiet \\
  --include-untracked -m "factorial: before the rework by run ${{RUN:-}}" >/dev/null || exit 1
after=$(agit rev-parse --quiet --verify refs/stash) || after=
agit reset --quiet --hard FETCH_HEAD >/dev/null || exit 1
head=$(agit rev-parse HEAD) || exit 1
printf '%s %s\\n' "$branch" "$head"
if [ "$after" != "$before" ]; then
  printf 'stash %s\\n' "$after"
fi
if [ -n "$lost" ]; then
  printf 'lost %s\\n' "$lost"
fi
"""

CAPTURE_SH: Final = f"""#!/bin/bash
# Prints the TUI's screen as text, for the smoke's dialog check. tmux draws a no-break space
# where the TUI does, so a reader matching text replaces U+00A0 first.
set -u
export LANG=C.UTF-8
{_TMUX_CLIENT} capture-pane -p -t {_PANE_TARGET}
"""

INSPECT_SH: Final = f"""#!/bin/bash
# Opens a kept session's view (decision 57): a session of the root tmux server on the lockdown
# conf, its socket in a dir closed to the agent, whose one pane runs {VIEW_RUNNER}. Opened only
# once the run's agent has stopped, so the server holds no other session. Stays alive while the
# view does, so its pid is the view's liveness. tmux's own error, when it cannot start, goes to
# {VIEW_STDERR} with the CLI's.
set -uo pipefail
export LANG=C.UTF-8
install -d -o root -g root -m 700 {TMUX_DIR}
{_TMUX_CLIENT} kill-session -t {_INSPECT_TARGET} 2>/dev/null
rm -f {VIEW_STDERR}
{_TMUX} new-session -d -s {INSPECT_SESSION} -x 200 -y 50 -- /bin/bash {VIEW_RUNNER} \\
  2>>{VIEW_STDERR} || exit 1
while {_TMUX_CLIENT} has-session -t {_INSPECT_TARGET} 2>/dev/null; do
  sleep 1
done
"""

VIEW_RUNNER_SH: Final = f"""#!/bin/bash
# The view's pane process, as root: runs the CLI as {AGENT_USER} on the pane's terminal from the
# view's own launch files. Writes no exit code: the run's is its session's. Never traps SIGHUP,
# as {TTY_RUNNER} does not. Reads its files without `mapfile`, which the bash of a Mac running the
# tests lacks. With umask 002, so the preview's user can change what it writes in the shared
# checkout.
set -uo pipefail
ARGV=()
while IFS= read -r -d '' word; do
  ARGV+=("$word")
done < {VIEW_ARGV}
ENVV=()
while IFS= read -r -d '' entry; do
  ENVV+=("$entry")
done < {VIEW_ENV}
umask 002
cd {WORKSPACE} && setpriv --reuid={AGENT_USER} --regid={AGENT_USER} --init-groups --reset-env \\
  env -i "${{ENVV[@]}}" "${{ARGV[@]}}" 2>>{VIEW_STDERR}
"""

INSPECT_STATUS_SH: Final = f"""#!/bin/bash
# Reports the session view in one line, which the adapter reads as data: `open <clients
# attached> <last activity> <created>`, the times in epoch seconds, or `closed`. For a target
# it cannot find, display-message prints blanks and succeeds.
set -u
export LANG=C.UTF-8
shown=$({_TMUX_CLIENT} display-message -p -t {_INSPECT_TARGET}: \\
  '#{{session_attached}} #{{session_activity}} #{{session_created}}' 2>/dev/null)
case "$shown" in
  *[0-9]*) echo "open $shown" ;;
  *) echo closed ;;
esac
"""

INSPECT_STOP_SH: Final = f"""#!/bin/bash
# Ends the session view: kills its tmux session, which hangs its CLI up, and gives every process
# of {AGENT_USER}'s the grace period to leave before {STOP}; nothing else of the agent's runs
# once its run has stopped. Only then, with nothing left to write them, cuts the session log
# $LOG back to $SIZE bytes, unless it is shorter or gone, and deletes the files named as
# arguments: the view's env file and the credential files. A view takes no turn, so what its CLI
# appended to the log, claude's cost on its way out, is not the session's. The log is the
# agent's, so it is cut only as {AGENT_USER}; an empty $LOG cuts nothing.
set -u
if ! [[ ${{SIZE:-}} =~ ^(0|[1-9][0-9]*)$ ]]; then
  echo "SIZE must be a decimal number" >&2
  exit 2
fi
# Bounded: the agent can swap a FIFO in for the log.
as_agent() {{
  timeout 10 setpriv --reuid={AGENT_USER} --regid={AGENT_USER} --init-groups --reset-env "$@"
}}
CUT='n=$(wc -c < "$1" | tr -d " ") && [ "$n" -gt "$2" ] && truncate -c -s "$2" -- "$1"'
{_TMUX_CLIENT} kill-session -t {_INSPECT_TARGET} 2>/dev/null
for _ in $(seq 1 {GRACE_SECONDS * 2}); do
  pgrep -u {AGENT_USER} >/dev/null || break
  sleep 0.5
done
{STOP}
if [ -n "${{LOG:-}}" ]; then
  as_agent sh -c "$CUT" sh "$LOG" "$SIZE"
fi
rm -f -- "$@"
exit 0
"""

#: What a resume runs before it writes a credential, whatever the state file says of a view:
#: the view's session's end, then every process of the agent's, the view's CLI among them.
#: A command, not a script, so a sandbox older than the view's scripts runs it too.
END_SHELL: Final = f"{_TMUX_CLIENT} kill-session -t {_INSPECT_TARGET} 2>/dev/null; {STOP}"

SCRIPTS: Final = {
    SETUP: SETUP_SH,
    STOP: STOP_SH,
    SCRUB: SCRUB_SH,
    PREVIEW: PREVIEW_SH,
    PREVIEW_STATUS: PREVIEW_STATUS_SH,
    FIREWALL: FIREWALL_SH,
    PREVIEW_STOP: PREVIEW_STOP_SH,
    SESSION: SESSION_SH,
    TTY_RUNNER: TTY_RUNNER_SH,
    SIGNAL: SIGNAL_SH,
    FINISH: FINISH_SH,
    CODEX_FINISH: CODEX_FINISH_SH,
    CONSENT: CONSENT_SH,
    ROLLOUT_FIND: ROLLOUT_FIND_SH,
    SESSION_POLL: SESSION_POLL_SH,
    REWORK: REWORK_SH,
    CAPTURE: CAPTURE_SH,
    INSPECT: INSPECT_SH,
    VIEW_RUNNER: VIEW_RUNNER_SH,
    INSPECT_STATUS: INSPECT_STATUS_SH,
    INSPECT_STOP: INSPECT_STOP_SH,
}

#: The values each script takes from its environment; everything else it reads from a file, or
#: SCRUB_SH and INSPECT_STOP_SH from their arguments. `SIGNAL` runs as the agent, under the
#: agent's environment.
SCRIPT_ENV: Final[dict[str, frozenset[str]]] = {
    SETUP: frozenset({"REPO", "BASE", "CONFIG_SHA"}),
    STOP: frozenset(),
    SCRUB: frozenset(),
    PREVIEW: frozenset(),
    PREVIEW_STATUS: frozenset(),
    FIREWALL: frozenset({"MODE", "PORTS"}),
    PREVIEW_STOP: frozenset(),
    SESSION: frozenset(),
    TTY_RUNNER: frozenset(),
    SIGNAL: frozenset({"FACTORIAL_SIGNALS"}),
    FINISH: frozenset({"MODE"}),
    CODEX_FINISH: frozenset({"MODE"}),
    CONSENT: frozenset({"TMUX_TARGET"}),
    ROLLOUT_FIND: frozenset(),
    SESSION_POLL: frozenset({"LOG", "LOG_FROM", "SIG_FROM", "LIMIT"}),
    REWORK: frozenset({"BRANCH_GLOB", "RUN", "MODE"}),
    CAPTURE: frozenset(),
    INSPECT: frozenset(),
    VIEW_RUNNER: frozenset(),
    INSPECT_STATUS: frozenset(),
    INSPECT_STOP: frozenset({"LOG", "SIZE"}),
}


@dataclass(frozen=True, slots=True)
class SessionPoll:
    """What one `SESSION_POLL` printed. `log_size` is None when the log does not exist."""

    log_size: int | None
    signals_size: int
    signals: bytes
    log: bytes


def read_session_poll(stdout: str) -> SessionPoll:
    """`SESSION_POLL`'s output, or ValueError when it is not that script's."""
    lines = stdout.split("\n")
    if len(lines) < 3:
        raise ValueError(f"a session poll printed {len(lines)} lines, not 3")
    sizes = lines[0].split(" ")
    if len(sizes) != 2:
        raise ValueError(f"a session poll's sizes line is {lines[0][:80]!r}")
    log_size, signals_size = (int(size) for size in sizes)
    if log_size < -1 or signals_size < 0:
        raise ValueError(f"a session poll's sizes line is {lines[0][:80]!r}")
    try:
        signals = base64.b64decode(lines[1], validate=True)
        log = base64.b64decode(lines[2], validate=True)
    except binascii.Error as exc:
        raise ValueError(f"a session poll's slice is not base64: {exc}") from exc
    return SessionPoll(None if log_size == -1 else log_size, signals_size, signals, log)
