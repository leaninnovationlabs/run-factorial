"""The pinned `claude` CLI surface, and the shared subprocess seam bound to its binary."""

from __future__ import annotations

from typing import Final

from runtime.adapters._claude.credentials import ENV_API_KEY as ENV_API_KEY
from runtime.adapters._claude.credentials import ENV_OAUTH_TOKEN as ENV_OAUTH_TOKEN
from runtime.adapters._cli.process import GIT_BIN, Cli

# ------------------------------------------------------------------ PINNED CLI SURFACE
#
# Pinned against `claude --version` 2.1.310, by the stream and TUI L1 tests; every name below was
# observed, not assumed, the first of them on 2.1.236. Re-pin the block (not the parsers) when the
# installed CLI changes. The TUI names (decision 56) were first observed on 2.1.284 by issue
# #243's probe and L1, and `askUserQuestionTimeout` in the binary's strings. The whole documents
# `tui.py` builds are proven by the L1 test, `test_claude_tui_local.py`. A host run is refused on
# any other version (`session.py`'s `spawn_refusal`).

CLAUDE_BIN: Final = "claude"

#: The version the parsers were last hand-checked against: a host run is refused on any other
#: (`spawn_refusal`), and the local image installs it (`test_image.py`). An E2B template installs
#: the CLI's latest instead, and its builder's smoke run checks the parse.
PINNED_VERSION: Final = "2.1.310"
FLAG_VERSION: Final = "--version"

#: Continue an existing conversation by id, rather than starting one. The agent keeps the
#: context it had, which is the whole difference between resuming a run and re-running it.
FLAG_RESUME: Final = "--resume"
FLAG_MODEL: Final = "--model"
#: Takes a path, so the system prompt stays out of argv, which is neither private (`ps`) nor large
#: enough for it (design.md §7).
FLAG_SYSTEM_PROMPT_FILE: Final = "--system-prompt-file"
FLAG_PERMISSION_MODE: Final = "--permission-mode"
VALUE_BYPASS_PERMISSIONS: Final = "bypassPermissions"
#: Passed unconditionally: it makes `--mcp-config` the complete server list rather than an
#: addition to the target repo's `.mcp.json`. CLAUDE_CONFIG_DIR keeps the user's settings and
#: skills the run's own; the checkout's project settings still load, except in a session view
#: (`FLAG_SETTING_SOURCES`).
FLAG_STRICT_MCP: Final = "--strict-mcp-config"
#: Takes a comma-separated list of `user`, `project` and `local`. A session view passes `user`
#: alone: it runs beside the model key, so nothing a checkout ships may load in it.
FLAG_SETTING_SOURCES: Final = "--setting-sources"
#: Takes a path (2.1.237 also accepts inline JSON — not used: the file carries rendered secret
#: values, and argv is not private). Only servers named here exist for the run.
FLAG_MCP_CONFIG: Final = "--mcp-config"
#: Tool narrowing within a server, as `mcp__<server>__<tool>` patterns. Best-effort, not
#: containment: the run uses `--permission-mode bypassPermissions`, so what actually bounds an
#: agent's MCP reach is which servers exist in the config file at all (design.md §7.5). Do not
#: describe it as enforcement without re-verifying against the CLI.
FLAG_ALLOWED_TOOLS: Final = "--allowed-tools"
#: `{"mcpServers": {...}}`, written 0600 under the per-run config dir — it holds rendered
#: credentials, and the worktree is a git checkout the agent pushes.
MCP_CONFIG_FILE: Final = "mcp.json"
#: Server-entry keys, observed against 2.1.237's config format. `stdio` entries are the historical
#: default shape and carry no `type`; remote ones must name theirs.
MCP_KEY_TYPE: Final = "type"
MCP_KEY_URL: Final = "url"
MCP_KEY_HEADERS: Final = "headers"
MCP_KEY_COMMAND: Final = "command"
MCP_KEY_ARGS: Final = "args"
MCP_KEY_ENV: Final = "env"
MCP_ROOT_KEY: Final = "mcpServers"

#: There is no `--max-turns` flag in 2.1.236, only `--max-budget-usd`, so a manifest's
#: `limits.max_turns` has nowhere to go and the run would be silently uncapped. Each supervisor
#: counts turns in the translated stream and ends the run itself. If a future CLI grows the
#: flag, pass it here and delete the counting.
NO_MAX_TURNS_FLAG: Final = True

#: Per-run config root. Points `claude` at a private directory, so the developer's own settings,
#: skills and CLAUDE.md cannot change agent behaviour (a host session's HOME is the run's own
#: too: `HOME_DIRNAME`), and the manifest's skills can be laid down without writing anything into
#: the target repo's worktree.
ENV_CONFIG_DIR: Final = "CLAUDE_CONFIG_DIR"
#: Set to "1" in every child. The CLI otherwise updates itself in place, which would move a host
#: or a sandbox off the version it was installed at without anyone choosing to.
ENV_DISABLE_AUTOUPDATER: Final = "DISABLE_AUTOUPDATER"
# The two credentials, ENV_API_KEY and ENV_OAUTH_TOKEN, are pinned in `_claude/credentials.py`
# with the rule for setting one, because the assistant's `claude_sdk` adapter launches this CLI
# too; they are re-exported above.

# The TUI: no `-p`, so the CLI's own interface runs in a tmux pane (decision 56).

#: A new session under an id the caller chose, so its log's path is known before the CLI runs.
FLAG_SESSION_ID: Final = "--session-id"
#: Ends the options. `--allowed-tools` is variadic and would take a positional prompt after it
#: as one more tool pattern; the TUI would then start with no turn.
END_OF_OPTIONS: Final = "--"
#: In a positional prompt, `@<path>` attaches the file: an image arrives as image content. A bare
#: path is only text, which the model must choose to open.
ATTACH_PREFIX: Final = "@"
#: Names the session log's directory under `<config dir>/projects/`, which is otherwise derived
#: from the workspace path: `projects/run/<session id>.jsonl`. `--resume` appends to that file.
ENV_PROJECT_DIR_NAME: Final = "CLAUDE_CODE_PROJECT_DIR_NAME"
VALUE_PROJECT_DIR: Final = "run"
ENV_TERM: Final = "TERM"
VALUE_TERM: Final = "xterm-256color"
#: Set to "1". At its first launch in a config dir the TUI otherwise clones the official plugin
#: marketplace into `<config dir>/plugins/`, with `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` set
#: or not: a fetch no egress allowlist names, and plugins nobody chose.
ENV_DISABLE_MARKETPLACE: Final = "CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL"
#: Set to "1": the classic renderer, whose finished turns scroll into tmux's history. The
#: fullscreen one draws on the alternate screen and scrolls in-app, so a viewer could not.
ENV_CLASSIC_RENDERER: Final = "CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN"
#: NEVER set. Either one answers an `AskUserQuestion` for the human after a countdown.
ENV_AFK_TIMEOUT: Final = "CLAUDE_AFK_TIMEOUT_MS"
ENV_AFK_COUNTDOWN: Final = "CLAUDE_AFK_COUNTDOWN_MS"
ENV_AFK_PREFIX: Final = "CLAUDE_AFK_"
#: The one CLI variable a host session carries from the worker's environment: a host may point
#: the CLI at a gateway, and the local TUI tests point it at their mock.
ENV_BASE_URL: Final = "ANTHROPIC_BASE_URL"
#: What the TUI's input line opens with, followed by a no-break space tmux captures as U+00A0.
INPUT_MARK: Final = "\u276f"
#: Typed at the input line, it ends the session after the CLI writes its cost.
EXIT_COMMAND: Final = "/exit"

#: Both seed files live in CLAUDE_CONFIG_DIR. Without them the TUI opens on a dialog, and no hook
#: fires until the workspace is trusted.
CLAUDE_JSON_FILE: Final = ".claude.json"
SETTINGS_FILE: Final = "settings.json"
CLAUDE_JSON_ONBOARDED: Final = "hasCompletedOnboarding"
CLAUDE_JSON_PROJECTS: Final = "projects"
#: Under the workspace's real path exactly: any other spelling of it brings the dialog back.
CLAUDE_JSON_TRUSTED: Final = "hasTrustDialogAccepted"
#: `{"approved": [<the trimmed key's last 20 characters>], "rejected": []}` answers the "use
#: this API key?" dialog that an `ANTHROPIC_API_KEY` in the environment raises.
CLAUDE_JSON_KEY_RESPONSES: Final = "customApiKeyResponses"
KEY_RESPONSES_APPROVED: Final = "approved"
KEY_RESPONSES_REJECTED: Final = "rejected"
API_KEY_TAIL: Final = 20
SETTINGS_SKIP_BYPASS_PROMPT: Final = "skipDangerousModePermissionPrompt"
#: One of "60s", "5m", "10m", "never". Read from user settings, not a project's.
SETTINGS_ASK_TIMEOUT: Final = "askUserQuestionTimeout"
VALUE_ASK_TIMEOUT_NEVER: Final = "never"
SETTINGS_HOOKS: Final = "hooks"
#: claude reads the CLAUDE.md files (`.claude/rules/` too) of every ancestor of its cwd: on a host
#: that is `local_root`, which every run shares and any agent can write, and all above it. The
#: values are picomatch globs over each file's absolute path; one negated pattern, everything
#: outside the workspace, keeps the repo's own. The path needs no escaping: a host's `local_root`
#: holds no glob character (`tmux.socket_path`), and E2B's workspace is fixed.
SETTINGS_CLAUDE_MD_EXCLUDES: Final = "claudeMdExcludes"
HOOK_MATCHER: Final = "matcher"
HOOK_LIST: Final = "hooks"
HOOK_TYPE: Final = "type"
VALUE_HOOK_COMMAND: Final = "command"
#: Run through a shell as the CLI's user, with the event's JSON on stdin. Its stdout can reach
#: the model's context, and exit code 2 blocks the tool call or prompt it fired for.
HOOK_COMMAND: Final = "command"
HOOK_SESSION_START: Final = "SessionStart"
HOOK_USER_PROMPT_SUBMIT: Final = "UserPromptSubmit"
HOOK_PRE_TOOL_USE: Final = "PreToolUse"
HOOK_POST_TOOL_USE: Final = "PostToolUse"
#: A turn that ended normally. It can fire before the session log holds the turn's last line.
HOOK_STOP: Final = "Stop"
#: A turn that ended in an API error, after the CLI's own retries. `Stop` does not fire then.
HOOK_STOP_FAILURE: Final = "StopFailure"
HOOK_SESSION_END: Final = "SessionEnd"
#: Offered under bypassPermissions in the TUI; a `-p` run is not offered it.
TOOL_ASK_USER_QUESTION: Final = "AskUserQuestion"

#: Stream event shapes, observed from a real run.
EVENT_TYPE: Final = "type"
EVENT_ASSISTANT: Final = "assistant"
EVENT_RESULT: Final = "result"

#: Every event carries the harness's own conversation id, and it is stable for a whole run.
#: It is what `--resume` addresses, so it is the one field that has to outlive the stream:
#: without it a stopped session can be re-run from scratch but never continued.
EVENT_SESSION_ID: Final = "session_id"
#: One API request == one turn. A single turn emits several `assistant` events (thinking, then
#: text), so counting events would over-count; counting distinct request ids does not.
EVENT_REQUEST_ID: Final = "request_id"
#: Set on messages produced *inside* a subagent. Those are not main-loop turns.
EVENT_PARENT_TOOL_USE: Final = "parent_tool_use_id"

RESULT_IS_ERROR: Final = "is_error"
RESULT_TEXT: Final = "result"
RESULT_SUBTYPE: Final = "subtype"
RESULT_COST: Final = "total_cost_usd"
RESULT_USAGE: Final = "usage"
RESULT_NUM_TURNS: Final = "num_turns"
USAGE_INPUT: Final = "input_tokens"
USAGE_OUTPUT: Final = "output_tokens"
USAGE_CACHE_CREATE: Final = "cache_creation_input_tokens"
USAGE_CACHE_READ: Final = "cache_read_input_tokens"
EVENT_USER: Final = "user"
EVENT_SYSTEM: Final = "system"
SUBTYPE_INIT: Final = "init"
RESULT_SUCCESS: Final = "success"
RESULT_ERROR_DURING_EXECUTION: Final = "error_during_execution"
#: On a `user` event, beside its `tool_result` blocks: the tool's structured result.
EVENT_TOOL_USE_RESULT: Final = "tool_use_result"

# The TUI's session log, `<config dir>/projects/run/<session id>.jsonl`: one JSON object per line,
# which `transcript.py` rewrites into the stream events above. Its `type`s are the stream's
# `user`, `assistant` and `system`, plus the ones below.

#: The CLI writes one line per content block, every line of one API response under the same
#: `message.id`, so a response is merged before it is a turn.
LOG_MESSAGE: Final = "message"
LOG_MESSAGE_ID: Final = "id"
LOG_MODEL: Final = "model"
LOG_CONTENT: Final = "content"
LOG_USAGE: Final = "usage"
LOG_STOP_REASON: Final = "stop_reason"
#: The API request id. A mock API that sends no `request-id` header leaves it off.
LOG_REQUEST_ID: Final = "requestId"
LOG_SIDECHAIN: Final = "isSidechain"
LOG_TIMESTAMP: Final = "timestamp"
#: A user line the CLI wrote for itself, such as the "[Image: source: …]" beside a pasted image.
LOG_META: Final = "isMeta"
LOG_COMPACT_SUMMARY: Final = "isCompactSummary"
#: Slash-command and `!` shell lines are user text that opens with one of these tags (read from
#: the binary, not yet seen in a captured log).
LOG_COMMAND_TAGS: Final = (
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "<local-command-stdout>",
    "<local-command-stderr>",
    "<local-command-caveat>",
    "<bash-input>",
    "<bash-stdout>",
    "<bash-stderr>",
)
#: On a user line carrying a tool's result. For `AskUserQuestion` its `answers` maps each
#: question's text to the label chosen.
LOG_TOOL_USE_RESULT: Final = "toolUseResult"
LOG_ANSWERS: Final = "answers"
#: The model on an assistant line the CLI made up rather than received, such as an API error's
#: text. It is never a turn.
LOG_SYNTHETIC_MODEL: Final = "<synthetic>"
LOG_API_ERROR: Final = "isApiErrorMessage"
LOG_SUBTYPE: Final = "subtype"
#: A `system` line closing every turn, a failed one included.
LOG_TURN_DURATION: Final = "turn_duration"
LOG_TYPE_ATTACHMENT: Final = "attachment"
#: The session's running totals, written only when the CLI exits (on SIGHUP too, never on
#: SIGKILL), twice on `/exit`, and carried on across `--resume`.
LOG_TYPE_COST_STATE: Final = "cost-state"
COST_TOTAL_USD: Final = "totalCostUSD"
COST_MODEL_USAGE: Final = "modelUsage"
COST_INPUT: Final = "inputTokens"
COST_OUTPUT: Final = "outputTokens"
COST_CACHE_READ: Final = "cacheReadInputTokens"
COST_CACHE_CREATE: Final = "cacheCreationInputTokens"
#: Lines with nothing for the stream. The first five are in every captured session; the rest
#: are the binary's own names for session metadata.
LOG_SKIPPED_TYPES: Final = frozenset(
    {
        "mode",
        "permission-mode",
        "file-history-snapshot",
        "last-prompt",
        "atis-latch",
        "summary",
        "custom-title",
        "ai-title",
        "agent-name",
        "tag",
        "pr-link",
        "worktree-state",
        "content-replacement",
        # 2.1.310's record of each request it sent, whose turns are already lines of their own.
        "api-request",
        "api-request-blob",
        "api-request-shape",
    }
)

#: A hook's stdin JSON.
HOOK_EVENT_NAME: Final = "hook_event_name"
HOOK_TOOL_NAME: Final = "tool_name"
HOOK_TOOL_INPUT: Final = "tool_input"
HOOK_TOOL_USE_ID: Final = "tool_use_id"
#: On `SessionStart`: "startup" for a new session, "resume" for `--resume`. A compaction or a
#: `/clear` starts one too, under another source.
HOOK_SOURCE: Final = "source"
LAUNCH_SOURCES: Final = ("startup", "resume")
#: On `Stop` and `StopFailure`.
HOOK_LAST_MESSAGE: Final = "last_assistant_message"
#: On `StopFailure`: the error's category, such as "authentication_failed".
HOOK_ERROR: Final = "error"
#: `AskUserQuestion`'s input: `{"questions": [{"question", "header", "options": [{"label",
#: "description"}], "multiSelect"}]}`.
ASK_QUESTIONS: Final = "questions"
ASK_QUESTION: Final = "question"
ASK_OPTIONS: Final = "options"
ASK_LABEL: Final = "label"

# -------------------------------------------------------------- end PINNED CLI SURFACE


class _Cli(Cli):
    """The seam, bound to the `claude` binary."""

    def __init__(self, *, claude_bin: str = CLAUDE_BIN, git_bin: str = GIT_BIN) -> None:
        super().__init__(agent_bin=claude_bin, git_bin=git_bin)
