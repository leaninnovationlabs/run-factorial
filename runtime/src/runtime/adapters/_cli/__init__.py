"""What the CLI adapters share (tech-stack.md decision 43): the subprocess seam, the per-run
checkout under `local_root`, the git/gh probes that find what a run delivered, the inspect
shell in a kept worktree, the host's tmux servers and the launch script a session's pane runs,
the host session each host adapter drives with its CLI's profile, and the session reader and
signals model every session supervisor reads a session through.

Vendor-free: nothing here knows which agent CLI runs, apart from the one question tool
`SessionSignals` recognises, claude's. A vendor need is an explicit parameter, never a branch on
the adapter's name.
"""
