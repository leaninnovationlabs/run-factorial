"""`--shard I/N`: run shard I of N, whole modules only, so CI can split the suite between jobs.

Every job lists the same modules and packs them the same way, heaviest first onto the lightest
shard, so each module runs in exactly one job (tech-stack.md decision 66). A job never imports
another job's modules: collecting the whole suite cost every CI worker about fifteen seconds.
The weights only balance the shards; a stale one costs time, never coverage.
"""

from __future__ import annotations

from fnmatch import fnmatch
from pathlib import Path

import pytest

#: Seconds per module on CI's runners, summed from the shards' JUnit reports (tests.yml) on
#: 2026-10-17. Modules under five seconds are left to `_DEFAULT`.
_SECONDS = {
    "orchestrator/tests/test_reconcile_interactive.py": 61,
    "runtime/tests/test_claude_cli_interactive.py": 48,
    "runtime/tests/test_claude_cli_rework.py": 47,
    "orchestrator/tests/test_reconcile_gates.py": 45,
    "runtime/tests/test_codex_cli_interactive.py": 42,
    "orchestrator/tests/test_api_runtimes.py": 40,
    "runtime/tests/test_e2b_session_scripts.py": 40,
    "orchestrator/tests/test_terminal_gateway.py": 38,
    "runtime/tests/test_e2b_interactive.py": 36,
    "runtime/tests/test_cli_inspect.py": 33,
    "orchestrator/tests/test_api.py": 29,
    "runtime/tests/test_codex_cli_rework.py": 26,
    "orchestrator/tests/test_config_commits.py": 24,
    "runtime/tests/test_e2b_adapter.py": 23,
    "orchestrator/tests/test_runtimes.py": 21,
    "orchestrator/tests/test_chat_proposals.py": 18,
    "orchestrator/tests/test_api_runs.py": 13,
    "orchestrator/tests/test_reconcile_failures.py": 13,
    "orchestrator/tests/test_reconcile_parked.py": 12,
    "runtime/tests/test_claude_sdk_mock_api.py": 12,
    "orchestrator/tests/test_api_readiness.py": 11,
    "orchestrator/tests/test_reconcile_rework.py": 11,
    "orchestrator/tests/test_refresh.py": 11,
    "runtime/tests/conformance.py": 11,
    "orchestrator/tests/test_assistant_runner.py": 10,
    "orchestrator/tests/test_reconcile.py": 10,
    "orchestrator/tests/test_poison_ticket.py": 9,
    "orchestrator/tests/test_reconcile_exit.py": 9,
    "orchestrator/tests/test_routes_refresh.py": 9,
    "orchestrator/tests/test_reconcile_previews.py": 8,
    "orchestrator/tests/test_inspect.py": 7,
    "orchestrator/tests/test_migrations.py": 7,
    "orchestrator/tests/test_assistant_loop.py": 6,
    "orchestrator/tests/test_assistant_routes.py": 6,
    "orchestrator/tests/test_reconcile_inputs.py": 6,
    "runtime/tests/test_e2b_codex_interactive.py": 6,
    "orchestrator/tests/test_assistant_stream.py": 5,
    "orchestrator/tests/test_registry.py": 5,
}
#: Any module `_SECONDS` does not name.
_DEFAULT = 0.8

_ELSEWHERE = pytest.StashKey[set[Path]]()


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--shard", metavar="I/N", help="run shard I of N, whole modules only")


def pack(modules: list[str], shards: int) -> dict[str, int]:
    """Module paths to shard indexes, 0-based."""
    load = [0.0] * shards
    placed: dict[str, int] = {}
    weight = {m: _SECONDS.get(m, _DEFAULT) for m in modules}
    for module in sorted(weight, key=lambda m: (-weight[m], m)):
        lightest = min(range(shards), key=lambda i: (load[i], i))
        placed[module] = lightest
        load[lightest] += weight[module]
    return placed


def _modules(config: pytest.Config) -> list[str]:
    patterns = config.getini("python_files")
    return [
        path.relative_to(config.rootpath).as_posix()
        for testpath in config.getini("testpaths")
        for path in (config.rootpath / testpath).rglob("*.py")
        if any(fnmatch(path.name, pattern) for pattern in patterns)
    ]


def pytest_configure(config: pytest.Config) -> None:
    shard = config.getoption("--shard")
    if not shard:
        return
    index, _, total = shard.partition("/")
    i, n = int(index), int(total)
    if not 1 <= i <= n:
        raise pytest.UsageError(f"--shard {shard}: I must be between 1 and N")
    config.stash[_ELSEWHERE] = {
        config.rootpath / module
        for module, placed in pack(_modules(config), n).items()
        if placed != i - 1
    }


def pytest_ignore_collect(collection_path: Path, config: pytest.Config) -> bool | None:
    return True if collection_path in config.stash.get(_ELSEWHERE, set()) else None
