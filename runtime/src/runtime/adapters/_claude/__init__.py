"""What the Claude adapters share: `claude_cli` and `claude_e2b` (agent runs) and the assistant's
`claude_sdk` (chat turns) launch the same `claude` binary, so its credential rule and the flags
more than one of them sets live in one place."""
