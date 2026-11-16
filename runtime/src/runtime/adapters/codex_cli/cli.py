"""The pinned `codex` CLI surface, and the shared subprocess seam bound to its binary."""

from __future__ import annotations

import re
from typing import Final

from runtime.adapters._cli.process import GIT_BIN, Cli

# ------------------------------------------------------------------ PINNED CLI SURFACE
#
# Pinned against `codex --version` == `codex-cli 0.164.0`; every name below was observed (help
# text, and the event shapes from real `--json` runs), not assumed. Re-pin the block
# (not the parsers) when the installed CLI changes. The dated notes below cite the version a
# thing was first seen on:
# - 0.146.0, the first pin.
# - 0.153.2, re-verified 2026-09-06/07. Every flag survived, `exec resume` still had no `-s`, and
#   every event shape below was seen again: the error shapes on a keyless run, then
#   `turn.completed`'s `usage` and an `agent_message` item on a green subscription smoke.
#   `input_tokens` was still GROSS: 15373 including 12160 cached, on one observed turn.
# - 0.159.0, re-verified 2026-09-29 by the E2B template builder's smoke run (api-key auth). The
#   TUI names (decision 56) were observed on it by issue #243's probe and L1,
#   and read in its source; the hook hash reproduces the three that probe's codex trusted.
# - 0.164.0, re-verified 2026-10-24 by L1; the request has no top-level `instructions`.

CODEX_BIN: Final = "codex"

#: The version the parsers were last hand-checked against: a host run is refused on any other
#: (`spawn_refusal`). An E2B template installs the CLI's latest instead, and its builder's smoke
#: run checks the parse. The assistant's Bedrock relay knows this version's `namespace` tool and
#: call (`runtime/assistant/adapters/codex_cli/relay.py`): a re-pin runs a tool call through it.
PINNED_VERSION: Final = "0.164.0"
FLAG_VERSION: Final = "--version"
#: What `--version` prints before the version, on its first line.
VERSION_NAME: Final = "codex-cli"

#: `resume <thread>` brings the TUI back on a conversation, with the context the agent had.
SUBCMD_RESUME: Final = "resume"
#: `codex sandbox -c sandbox_mode="workspace-write" -- true` runs `true` under the sandbox a run
#: gets. Observed 0.159.0: exit 0 where that sandbox starts (bubblewrap in the image under
#: `docker/seccomp.json`, Seatbelt on macOS), and exit 1, with bwrap's "No permissions to create
#: a new namespace" on stderr, under Docker's default seccomp profile. The `-c` override is one
#: argv element, `key="value"`.
SUBCMD_SANDBOX: Final = "sandbox"
FLAG_CONFIG: Final = "-c"

FLAG_MODEL: Final = "-m"
#: The agent's working root. Passed alongside `cwd=` (they agree) so the choice is explicit in
#: the recorded argv rather than implicit in process state.
FLAG_CD: Final = "-C"
#: `-s workspace-write`: the agent may write its own workspace and reach the network, and
#: nothing else. `danger-full-access` was v1's posture and is kept only to assert its absence —
#: on the factory host it would mean every project's checkouts and the operator's own home.
FLAG_SANDBOX: Final = "-s"
VALUE_SANDBOX_WORKSPACE: Final = "workspace-write"
VALUE_SANDBOX_FULL: Final = "danger-full-access"

#: The sandbox as config too, the policy `-s` passes at every TUI launch. Every key
#: verified against 0.153.2 with `--strict-config`, which hard-errors on an unknown field, and
#: `codex doctor` reports the resolved policy back ("restricted fs + enabled network").
CONFIG_SANDBOX_MODE: Final = "sandbox_mode"
CONFIG_APPROVAL_POLICY: Final = "approval_policy"
#: Bedrock Grok `/openai/v1` rejects the `web_search` tool; Codex 0.159 enables it by default.
CONFIG_WEB_SEARCH: Final = "web_search"
VALUE_WEB_SEARCH_DISABLED: Final = "disabled"
#: `never`: nobody watches a TUI to approve a command, so an approval request would stall it.
VALUE_APPROVAL_NEVER: Final = "never"
CONFIG_WORKSPACE_TABLE: Final = "sandbox_workspace_write"
#: Without it the agent cannot push a branch or open a pull request — the whole job.
CONFIG_NETWORK_ACCESS: Final = "network_access"
#: The run area, not the worktree. A worktree's `.git` is a FILE pointing into the shared
#: clone's `.git/worktrees/`, so a run confined to its worktree alone could not even commit.
CONFIG_WRITABLE_ROOTS: Final = "writable_roots"
#: `workspace-write` leaves $TMPDIR and /tmp WRITABLE unless these say otherwise — observed
#: 0.153.2, where a run wrote into a mkdtemp under $TMPDIR and the sandbox allowed it. On a host
#: that runs every project's tickets that is a shared scratch every run can reach into, so both
#: are excluded. A run needing scratch space has its own worktree.
CONFIG_EXCLUDE_TMPDIR: Final = "exclude_tmpdir_env_var"
CONFIG_EXCLUDE_SLASH_TMP: Final = "exclude_slash_tmp"
#: `exec` trusts a cwd that has a `.codex/` folder and no trust entry: it appends
#: `[projects."<cwd>"] trust_level = "trusted"` to this file, then loads the folder's config, its
#: MCP servers included (observed 0.159.0; app-server `thread_processor.rs`). A repo file would
#: then grant a server outside the allowlist (design.md §7.5), started outside the sandbox. So the
#: checkout is seeded `untrusted` (here and in `e2b/codex.py`), which keeps `.codex/` unloaded and
#: this file unrewritten. It also stops codex loading AGENTS.md by itself; the prompts tell the
#: agent to read it.
CONFIG_PROJECTS_TABLE: Final = "projects"
CONFIG_TRUST_LEVEL: Final = "trust_level"
VALUE_TRUST_UNTRUSTED: Final = "untrusted"
#: Attaches an image to the first user turn: codex reads the file itself and sends its bytes, so
#: the model sees it with no tool call (observed 0.153.2: the rollout's first message carries an
#: `input_image`). Takes several values, so a bare `--image a.png` swallows the prompt after it
#: as a second image: always the `=` form, one flag per image, before the prompt. A path that
#: does not exist is not an error — codex tells the model it "could not read" it — so every file
#: is written before the launch. `resume` takes it too.
FLAG_IMAGE: Final = "--image"

#: Per-run config root. Points `codex` at a private directory, so the operator's own config,
#: skills and browser login cannot change agent behaviour (with a HOME of the run's own:
#: `HOME_DIRNAME`), and the manifest's skills can be laid down without writing anything into the
#: target repo's worktree. Auth (`auth.json`) and the run's `config.toml` both live under it.
ENV_CODEX_HOME: Final = "CODEX_HOME"
#: The api_key-mode credential, an ordinary run secret (decision 34, mirroring decision 16).
#: Under the config-home isolation above the operator's own login is invisible to a run;
#: subscription mode writes a stored `auth.json` instead (auth.py).
ENV_API_KEY: Final = "OPENAI_API_KEY"

#: Files this adapter writes under the per-run `CODEX_HOME`.
CONFIG_FILE: Final = "config.toml"
SKILLS_DIRNAME: Final = "skills"
#: The login file, read from `CODEX_HOME`. A browser login writes a refreshable token bundle,
#: which is why a subscription cannot be handed to a flag. Observed 2026-09-06 on 0.153.2: a
#: per-run home seeded with a COPY of that bundle authenticates, and the copy refreshes ITSELF —
#: the operator's own file is not rewritten. EITHER mode leaves a credential here — `api_key`
#: writes `auth_mode = "apikey"` with the key itself (`tui.py`) — so the adapter deletes it at
#: every end and every launch writes it again (decision 54).
AUTH_FILE: Final = "auth.json"

#: Stream event shapes, observed from real runs. `thread.started` carries the resume handle;
#: `turn.started` is the turn-count signal (no `--max-turns` flag exists — the trap of
#: decision 16, so the supervisor enforces the cap); `item.completed` with an `agent_message`
#: item carries the summary text; `turn.completed` carries per-turn `usage`.
EVENT_TYPE: Final = "type"
EVENT_THREAD_STARTED: Final = "thread.started"
EVENT_THREAD_ID: Final = "thread_id"
EVENT_TURN_STARTED: Final = "turn.started"
EVENT_TURN_COMPLETED: Final = "turn.completed"
EVENT_ITEM_STARTED: Final = "item.started"
EVENT_ITEM_COMPLETED: Final = "item.completed"
EVENT_ITEM: Final = "item"
ITEM_ID: Final = "id"
ITEM_TYPE: Final = "type"
ITEM_AGENT_MESSAGE: Final = "agent_message"
ITEM_TEXT: Final = "text"
#: Item types observed on 0.153.2 (VM runs 157-plan-agent-ef368a05, which opened its PR, and
#: 157-plan-agent-5701ab70, which could not commit; 2026-09-09). `agent_message` and `error`
#: arrive as `item.completed` only. `command_execution` arrives TWICE under one item `id`:
#: `item.started` with `status: in_progress`, an empty `aggregated_output` and a null
#: `exit_code`, then `item.completed` with `status: completed`, the output and the code. The
#: CLI's other item types — `reasoning`, `file_change`, `mcp_tool_call`, `web_search`,
#: `todo_list` — were NOT observed and nothing here names their fields; `events.py` types them
#: as unknown, name only, and never fails on one.
ITEM_COMMAND_EXECUTION: Final = "command_execution"
ITEM_COMMAND: Final = "command"
ITEM_AGGREGATED_OUTPUT: Final = "aggregated_output"
ITEM_EXIT_CODE: Final = "exit_code"
ITEM_STATUS: Final = "status"
EVENT_USAGE: Final = "usage"
#: Error shapes, observed from a real failed run (2026-08-29 live smoke): the diagnosis rides
#: stdout, as a top-level `error` event, a `turn.failed` event carrying an `error` object,
#: and/or an `item.completed` whose item type is `error`. All three spell the detail under a
#: `message` key. 0.146.0 left stderr EMPTY on such an exit; 0.153.2 writes `codex_api` tracing
#: there instead, one line per retry — which is why the stream's last error, not stderr, is the
#: reason reported (`stream.py`'s `last_error`). Prefer-stream was already the rule; the version
#: that made stderr useless only raised the stakes.
EVENT_ERROR: Final = "error"
EVENT_TURN_FAILED: Final = "turn.failed"
ITEM_ERROR: Final = "error"
KEY_MESSAGE: Final = "message"
#: `input_tokens` is GROSS — it already includes `cached_input_tokens` (observed: 17004 input,
#: 11008 cached on one turn), the opposite of Claude's net-of-cache reporting. Never sum them.
#: All five `usage` keys observed on 0.153.2. `output_tokens` already includes
#: `reasoning_output_tokens`, so the latter is pinned only to say it is NOT read: carrying it
#: would invite the first reader to add it in twice.
USAGE_INPUT: Final = "input_tokens"
USAGE_CACHED_INPUT: Final = "cached_input_tokens"
USAGE_CACHE_WRITE_INPUT: Final = "cache_write_input_tokens"
USAGE_OUTPUT: Final = "output_tokens"
USAGE_REASONING_OUTPUT: Final = "reasoning_output_tokens"

#: No cost field exists anywhere in the stream (observed) — codex reports tokens only, so
#: `RunResult.cost_usd` stays None: the orchestrator prices the run from the token split at
#: list price (`worker/pricing.py`) and labels the figure an estimate. The activity view
#: (`orchestrator/api/activity.py`) reads the same stream back off `stdout.jsonl`, so its own
#: `cost_usd` is None too.
NO_COST_REPORTED: Final = True

# ---- The TUI (decision 56): bare `codex`, and `codex resume <thread>`.

#: On every launch and resume. The release binary refuses to start without it ("no complete
#: local package"), and a shared daemon would outlive the session as a channel between runs.
FLAG_NO_DAEMON: Final = "--no-daemon"
FLAG_NO_ALT_SCREEN: Final = "--no-alt-screen"
#: The TUI's `-a`, the policy `approval_policy` also sets in config.
FLAG_APPROVAL: Final = "-a"
#: What the TUI's input line opens with (U+203A).
INPUT_MARK: Final = "\u203a"
#: Typed at the input line, it ends the session; `/exit` is its alias.
QUIT_COMMAND: Final = "/quit"
#: Empties a draft, and at an idle, empty input line quits outright, exit 0. No Esc: at an idle
#: prompt it arms "edit the previous message", and a second one opens the backtrack overlay.
CLEAR_KEYS: Final = ("\x03",)
#: Every launch and every `resume` in a folder not seeded `trusted` asks for the folder first.
#: For an `untrusted` one this choice is already selected, keeps the trust as seeded, and one
#: Enter takes it; the positional prompt waits behind it.
CONSENT_RESTRICTED: Final = "Open restricted"
#: What codex shows in place of its session when the login it was handed is refused: a stored
#: ChatGPT login that has expired brings up its onboarding, never a 401.
SIGN_IN_SCREENS: Final = ("Welcome to Codex", "Sign in with ChatGPT")
ENV_TERM: Final = "TERM"
VALUE_TERM: Final = "xterm-256color"

#: Off, or a newer release's notice is a dialog in front of the first turn.
CONFIG_CHECK_FOR_UPDATE: Final = "check_for_update_on_startup"
#: `auth.json` in `CODEX_HOME`, never an OS keyring a run cannot be handed.
CONFIG_AUTH_STORE: Final = "cli_auth_credentials_store"
VALUE_AUTH_STORE_FILE: Final = "file"
#: The system prompt, which the TUI takes as config rather than as part of the turn.
CONFIG_DEVELOPER_INSTRUCTIONS: Final = "developer_instructions"
#: Set at all, it also turns on the TUI's own text log, `TUI_LOG_FILE` in that dir, which holds
#: the error a launch that dies after its startup logs. One that dies sooner says it on stderr.
CONFIG_LOG_DIR: Final = "log_dir"
LOG_DIRNAME: Final = "log"
TUI_LOG_FILE: Final = "codex-tui.log"
#: `auth.json` in api-key mode, written directly: it reaches the prompt with no login dialog.
AUTH_KEY_MODE: Final = "auth_mode"
VALUE_AUTH_APIKEY: Final = "apikey"

#: `[[hooks.<Event>]]` / `[[hooks.<Event>.hooks]]` with `type` and `command`. A hook runs only
#: when `[hooks.state."<config.toml's real path>:<label>:0:0"] trusted_hash` matches its hash
#: (`tui.hook_hash`); an unmatched one brings "Hooks need review" up instead. It runs as
#: `$SHELL -lc <command>` in a new session, with codex's own environment snapshot, not the TUI's.
CONFIG_HOOKS: Final = "hooks"
CONFIG_HOOK_HANDLERS: Final = "hooks"
CONFIG_HOOK_STATE: Final = "state"
CONFIG_TRUSTED_HASH: Final = "trusted_hash"
CONFIG_HOOK_TYPE: Final = "type"
VALUE_HOOK_COMMAND: Final = "command"
CONFIG_HOOK_COMMAND: Final = "command"
#: Part of the hashed identity: what codex fills in for a hook that names no timeout.
HOOK_DEFAULT_TIMEOUT_SEC: Final = 600
#: The hooks installed, by event, with the label their trust key and hash use. A `SessionEnd` or
#: `Interrupt` hook would have its timeout normalised to 1-3 s, which changes its hash.
HOOK_LABELS: Final = {
    "SessionStart": "session_start",
    "UserPromptSubmit": "user_prompt_submit",
    "Stop": "stop",
}
#: A hook's stdin JSON, as `_cli/session_signals.py` reads it; a test holds the two equal.
HOOK_EVENT_NAME: Final = "hook_event_name"
HOOK_SESSION_ID: Final = "session_id"
HOOK_SESSION_START: Final = "SessionStart"
HOOK_USER_PROMPT_SUBMIT: Final = "UserPromptSubmit"
#: A turn that ended normally. A failed or aborted turn fires none; its UserPromptSubmit still
#: did, and the rollout says why.
HOOK_STOP: Final = "Stop"
HOOK_SOURCE: Final = "source"
LAUNCH_SOURCES: Final = ("startup", "resume")
HOOK_LAST_MESSAGE: Final = "last_assistant_message"

#: `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<ts>-<thread>.jsonl`, created at the first turn and
#: appended to by every resume. Codex takes no session id, so the thread is known only from it.
#: Only with the under-development `features.local_thread_store_compression` on is a rollout a
#: week old compressed to `.jsonl.zst` at startup; a resume restores the plain file byte for byte.
ROLLOUT_DIRNAME: Final = "sessions"
ROLLOUT_NAME: Final = re.compile(
    r"rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-"
    r"(?P<thread>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl"
)
#: Rollout lines are `{"timestamp", "type", "payload"}`.
ROLLOUT_TIMESTAMP: Final = "timestamp"
ROLLOUT_TYPE: Final = "type"
ROLLOUT_PAYLOAD: Final = "payload"
RECORD_SESSION_META: Final = "session_meta"
META_ID: Final = "id"
RECORD_EVENT_MSG: Final = "event_msg"
#: A model item. Only its `request_user_input` calls and their outputs are read, and only by
#: the hook signals: the translator skips it.
RECORD_RESPONSE_ITEM: Final = "response_item"
ITEM_FUNCTION_CALL: Final = "function_call"
ITEM_FUNCTION_CALL_OUTPUT: Final = "function_call_output"
CALL_NAME: Final = "name"
#: The call's arguments, as JSON text.
CALL_ARGUMENTS: Final = "arguments"
CALL_ID: Final = "call_id"
CALL_OUTPUT: Final = "output"
#: Codex's question box. In Default mode it is refused unless `FEATURE_DEFAULT_MODE_ASK` is on;
#: its arguments are `questions` as claude's AskUserQuestion takes them, and its output is
#: `{"answers": {<question id>: {"answers": [...]}}}`, an empty mapping when nobody answered.
TOOL_REQUEST_USER_INPUT: Final = "request_user_input"
ASK_ANSWERS: Final = "answers"
#: How long the picker waits before codex answers it with nothing
#: (`tui/src/bottom_pane/request_user_input/mod.rs:72-73`).
ASK_SELF_ANSWER_SEC: Final = 120
#: `[features]`: under development in 0.159.0, so codex warns at launch unless the warning is
#: suppressed.
CONFIG_FEATURES: Final = "features"
FEATURE_DEFAULT_MODE_ASK: Final = "default_mode_request_user_input"
CONFIG_SUPPRESS_UNSTABLE_WARNING: Final = "suppress_unstable_features_warning"
#: After the system prompt with the box on. Codex's Default-mode block, which follows it in the
#: thread and voids "previous instructions for other modes", sends a question the turn cannot go
#: on without to plain words, so the line overrides that rule by name.
ASK_TOOL_INSTRUCTION: Final = (
    "When you cannot go on without a person's answer, ask it with the request_user_input tool, "
    "one question, never in your reply text, and stop. This holds in Default mode too, over "
    "its rule that sends such a question to plain text."
)
#: The other top-level types 0.159.0 can write (`history/src/rollout_payload.rs`), none of which
#: the translator reads.
RECORD_SKIPPED_TYPES: Final = frozenset(
    {
        RECORD_RESPONSE_ITEM,
        "inter_agent_communication",
        "inter_agent_communication_metadata",
        "compacted",
        "turn_context",
        "world_state",
        "retained_context",
        "security_risk_score",
        "realtime_item",
    }
)
PAYLOAD_TYPE: Final = "type"
#: `turn_token_usage` is the turn's running total. Its siblings `usage` (one response) and
#: `thread_token_usage` (the thread's) are not a turn's tokens.
RECORD_TOKEN_USAGE: Final = "token_usage_record"
USAGE_TURN: Final = "turn_token_usage"
#: `event_msg` payload types.
MSG_TASK_STARTED: Final = "task_started"
#: A failed turn (a 401, after codex's retries) ends here with `error.message`.
MSG_TASK_COMPLETE: Final = "task_complete"
#: null on a turn that ended normally.
TASK_ERROR: Final = "error"
#: A turn a human interrupted (Esc). No Stop hook fires for it.
MSG_TURN_ABORTED: Final = "turn_aborted"
MSG_ERROR: Final = "error"
MSG_ITEM_COMPLETED: Final = "item_completed"
PAYLOAD_ITEM: Final = "item"
TURN_ITEM_USER: Final = "UserMessage"
TURN_ITEM_AGENT: Final = "AgentMessage"
#: `fixtures/codex-0.159.0` records one: `command` an argv list, `aggregated_output`,
#: `exit_code`, `status`.
TURN_ITEM_COMMAND: Final = "CommandExecution"
TURN_ITEM_CONTENT: Final = "content"
#: A user message's text parts are typed `text`, an agent message's `Text`.
CONTENT_USER_TEXT: Final = "text"
CONTENT_AGENT_TEXT: Final = "Text"
#: factorial's own item type, not codex's: a human's or the orchestrator's turn, as the rollout
#: translator writes it into the `exec --json` dialect, which has no user item.
ITEM_USER_MESSAGE: Final = "user_message"

# -------------------------------------------------------------- end PINNED CLI SURFACE


class _Cli(Cli):
    """The seam, bound to the `codex` binary."""

    def __init__(self, *, codex_bin: str = CODEX_BIN, git_bin: str = GIT_BIN) -> None:
        super().__init__(agent_bin=codex_bin, git_bin=git_bin)
