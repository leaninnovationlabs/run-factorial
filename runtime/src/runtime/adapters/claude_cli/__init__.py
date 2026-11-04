"""The `claude` CLI, in a local git worktree: runtime kind `claude_cli`, which the fleet runs.

It has no containment beyond `local_root` and one worktree per run, an accepted risk
(tech-stack.md decision 16).
"""

from __future__ import annotations

from runtime.adapters.claude_cli.adapter import ClaudeCliAdapter

__all__ = ["ClaudeCliAdapter"]
