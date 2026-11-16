import ast
import re
from pathlib import Path

import pytest

from orchestrator.mcp_server.teaching import vocabulary_text
from tests.paths import REPO_ROOT

ORCHESTRATOR = REPO_ROOT / "orchestrator" / "src" / "orchestrator"
SOURCES = sorted(
    path
    for base in (REPO_ROOT / "orchestrator" / "src", REPO_ROOT / "runtime" / "src")
    for path in base.rglob("*.py")
)
RESOURCES = [
    *sorted(
        path
        for base in (
            ORCHESTRATOR / "config" / "scaffold" / "templates",
            ORCHESTRATOR / "mcp_server" / "resources",
            ORCHESTRATOR / "assistant" / "prompts",
            REPO_ROOT / "docs" / "samples",
        )
        for path in base.rglob("*")
        if path.is_file() and path.name != "CATALOG.yaml"
    ),
    REPO_ROOT / "docs" / "process-yaml.md",
]

MARKS = {
    "em dash": re.compile("\N{EM DASH}"),
    "en dash": re.compile("\N{EN DASH}"),
    "curly quote": re.compile(
        "[\N{LEFT DOUBLE QUOTATION MARK}\N{RIGHT DOUBLE QUOTATION MARK}"
        "\N{LEFT SINGLE QUOTATION MARK}\N{RIGHT SINGLE QUOTATION MARK}]"
    ),
    "arrow": re.compile("[\N{LEFTWARDS ARROW}-\N{RIGHTWARDS DOUBLE ARROW}]"),
    "glyph": re.compile(
        "[\N{BULLET}\N{SINGLE RIGHT-POINTING ANGLE QUOTATION MARK}\N{CHECK MARK}\N{BALLOT X}"
        "\N{BLACK UP-POINTING TRIANGLE}\N{BLACK RIGHT-POINTING TRIANGLE}"
        "\N{BLACK DOWN-POINTING TRIANGLE}\N{MULTIPLICATION SIGN}]"
    ),
    "emoji": re.compile("[\N{BLACK SUN WITH RAYS}-\N{DOUBLE CURLY LOOP}\U0001f300-\U0001faff]"),
    "section sign": re.compile("\N{SECTION SIGN}"),
    "decision number": re.compile(r"\bdecision \d+", re.IGNORECASE),
    "we": re.compile(r"\b(?:[Ww]e|[Oo]ur)\b"),
    "UK spelling": re.compile(
        r"organis|colour|\blabelled\b|judgement|serialis|afterwards", re.IGNORECASE
    ),
    "ellipsis": re.compile("\N{HORIZONTAL ELLIPSIS}"),
    # Not a file name that ends in one, such as the superpowers sample's `<N>-<slug>-design.md`.
    "design document": re.compile(r"(?<![\w-])(?:tech-stack|design)\.md"),
    "UK canceled": re.compile(r"(?s)^(?=.*\s).*(?<![`'\"])\b[Cc]ancelled\b(?![:`'\"])"),
}
CODE_MARKS = {
    **MARKS,
    "capability flag": re.compile(r"supports_\w+="),
    "e.g.": re.compile(r"\be\.g\."),
}

ELLIPSIS = "\N{HORIZONTAL ELLIPSIS}"
TRUNCATION = "truncation marker"
ALLOWED: list[tuple[str, str, str]] = [
    ("orchestrator/src/orchestrator/api/activity.py", ELLIPSIS, TRUNCATION),
    ("orchestrator/src/orchestrator/mcp_server/results.py", ELLIPSIS, TRUNCATION),
    ("orchestrator/src/orchestrator/terminal/protocol.py", ELLIPSIS, TRUNCATION),
    ("orchestrator/src/orchestrator/worker/sessions.py", ELLIPSIS, TRUNCATION),
    ("runtime/src/runtime/adapters/_cli/session_signals.py", ELLIPSIS, TRUNCATION),
    (
        "runtime/src/runtime/adapters/codex_cli/cli.py",
        "\N{SINGLE RIGHT-POINTING ANGLE QUOTATION MARK}",
        "matches the codex prompt",
    ),
    (
        "runtime/src/runtime/adapters/claude_cli/cli.py",
        "\N{HEAVY RIGHT-POINTING ANGLE QUOTATION MARK ORNAMENT}",
        "matches the claude prompt",
    ),
    ("runtime/src/runtime/adapters/_cli/checkout.py", "not our ref", "matches git's error"),
    ("runtime/src/runtime/adapters/e2b/adapter.py", "not our ref", "matches git's error"),
    ("orchestrator/src/orchestrator/actions/registry.py", "(design.md", "import-time table check"),
    (
        "orchestrator/src/orchestrator/predicates/library.py",
        "(design.md",
        "import-time table check",
    ),
    ("orchestrator/src/orchestrator/assistant/prompts/system.md", '"we"', "states the rule"),
    (
        "runtime/src/runtime/adapters/_cli/host_session.py",
        "cancelled by",
        "the runtime's stop reason",
    ),
    ("runtime/src/runtime/adapters/e2b/adapter.py", "cancelled by", "the runtime's stop reason"),
    (
        "spec-page/SKILL.md",
        "\N{BOX DRAWINGS LIGHT HORIZONTAL}\N{BLACK RIGHT-POINTING TRIANGLE}",
        "the ASCII diagram the skill shows",
    ),
]

VERDICT = "## Test Verdict"
VERDICT_PAIRS = [
    (
        ORCHESTRATOR
        / "config/scaffold/templates/process-templates/simple/processes/process-simple.yaml",
        ORCHESTRATOR / "config/scaffold/templates/agents/test-agent/base-target/instructions.md",
    ),
    (
        REPO_ROOT / "docs/samples/factorial-simple/processes/process-simple.yaml",
        REPO_ROOT / "docs/samples/factorial-simple/agents/test-agent/instructions.md",
    ),
]


def _at(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _allowed(at: str, text: str) -> bool:
    return any(
        (at == file or at.endswith(f"/{file}")) and needle in text for file, needle, _ in ALLOWED
    )


def _skipped(tree: ast.AST) -> set[int]:
    skip: set[int] = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            first = body[0] if body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                skip.add(id(first.value))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            owner = node.func.value
            name = owner.id if isinstance(owner, ast.Name) else getattr(owner, "attr", "")
            if name in {"logger", "log", "_log", "logging"}:
                skip.update(id(child) for child in ast.walk(node))
    return skip


def strings(path: Path) -> list[str]:
    tree = ast.parse(path.read_text())
    skip = _skipped(tree)
    out: list[str] = []
    for node in ast.walk(tree):
        if id(node) in skip:
            continue
        if isinstance(node, ast.JoinedStr):
            skip.update(id(child) for child in ast.walk(node))
            out.append(
                "".join(
                    v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else "{x}"
                    for v in node.values
                )
            )
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append(node.value)
    return [text for text in out if not text.startswith("#!")]


def _hits(at: str, texts: list[str], marks: dict[str, re.Pattern[str]]) -> list[str]:
    return [
        f"{at} [{name}] {text.strip()[:100]!r}"
        for text in texts
        if not _allowed(at, text)
        for name, mark in marks.items()
        if mark.search(text)
    ]


def test_code_strings_follow_the_copy_standard() -> None:
    found = [hit for path in SOURCES for hit in _hits(_at(path), strings(path), CODE_MARKS)]
    assert found == []


def test_text_resources_follow_the_copy_standard() -> None:
    found = [
        hit for path in RESOURCES for hit in _hits(_at(path), path.read_text().splitlines(), MARKS)
    ]
    assert found == []


def test_the_served_predicate_meanings_follow_the_copy_standard() -> None:
    assert _hits("vocabulary", vocabulary_text().splitlines(), MARKS) == []


def test_every_allowlist_entry_still_matches() -> None:
    texts = [(_at(path), strings(path)) for path in SOURCES]
    texts += [(_at(path), path.read_text().splitlines()) for path in RESOURCES]
    unused = [
        (file, needle)
        for file, needle, _ in ALLOWED
        if not any(
            (at == file or at.endswith(f"/{file}")) and any(needle in text for text in lines)
            for at, lines in texts
        )
    ]
    assert unused == []


@pytest.mark.parametrize("process, agent", VERDICT_PAIRS, ids=lambda p: _at(p))
def test_the_verdict_marker_survives(process: Path, agent: Path) -> None:
    assert f'"{VERDICT}")' in process.read_text()
    assert VERDICT in agent.read_text().splitlines()
