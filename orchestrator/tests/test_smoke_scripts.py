"""The smoke scripts run only with real keys, so nothing else notices when a name they import
moves: every factorial import they make must resolve, without running the script."""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

from tests.paths import REPO_ROOT

SMOKE = REPO_ROOT / "scripts" / "smoke"


@pytest.mark.parametrize("script", sorted(SMOKE.glob("*.py")), ids=lambda path: path.name)
def test_every_name_a_smoke_script_imports_exists(script: Path) -> None:
    missing = []
    for node in ast.walk(ast.parse(script.read_text())):
        if not isinstance(node, ast.ImportFrom) or node.module is None:
            continue
        if node.module.split(".")[0] not in ("runtime", "orchestrator"):
            continue
        module = importlib.import_module(node.module)
        missing += [f"{node.module}.{a.name}" for a in node.names if not hasattr(module, a.name)]
    assert missing == []
