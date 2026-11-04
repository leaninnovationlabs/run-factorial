"""codex_cli — OpenAI's `codex` CLI in a local git worktree, inside codex's own
workspace-write sandbox (tech-stack.md decision 34). A fleet substrate behind `runtime.contract`."""

from runtime.adapters.codex_cli.adapter import CodexCliAdapter

__all__ = ["CodexCliAdapter"]
