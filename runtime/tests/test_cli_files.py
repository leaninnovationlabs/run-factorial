"""The `# Files` section both CLI prompt builders share (tech-stack decision 71): the files the
dashboard shows from the branch the run pushes, and how each is served, by its extension."""

from __future__ import annotations

import pytest

from runtime.adapters._cli.environment import files_lines
from runtime.contract import (
    PAGE_MAX_BYTES,
    PATTERN_MAX_MATCHES,
    AgentPrompts,
    RunSpec,
    ShownFile,
    Workspace,
)

SUMMARY = ShownFile(path="specs/12-{slug}/summary.html", label="Summary")
SPEC = ShownFile(path="specs/12-{slug}/spec.md", label="Spec")
PLAN = ShownFile(path="specs/12-{slug}/plan.md", label="Plan")

_HTML_RULE = (
    f"One self-contained HTML file of at most {PAGE_MAX_BYTES} bytes. It is served with scripts "
    "and network turned off: no JavaScript, images and fonts only as `data:` URIs, diagrams and "
    'charts as inline SVG, and `target="_blank"` on every link.'
)
_MARKDOWN_RULE = (
    f"One Markdown file of at most {PAGE_MAX_BYTES} bytes. It is rendered as CommonMark with "
    "tables and strikethrough. Raw HTML is shown as text. Images load only as `data:` URIs. "
    "Nothing runs, so a Mermaid block shows as code: draw diagrams as SVG images or as text in a "
    "fenced block. Links open in a new tab."
)
_TEXT_RULE = (
    f"One UTF-8 text file of at most {PAGE_MAX_BYTES} bytes. It is shown as plain text, exactly "
    "as written."
)


def _spec(*shown: ShownFile) -> RunSpec:
    return RunSpec(
        agent="plan-agent",
        ticket="12",
        workspace=Workspace(repo="acme/widgets", base="main", branch="spec/12-{slug}"),
        prompts=AgentPrompts(system="You plan.", instructions="Write a spec."),
        model="claude-sonnet-5-5",
        shown_files=shown,
    )


def test_a_run_that_shows_no_files_is_told_nothing() -> None:
    assert files_lines(_spec()) == []


def test_each_file_is_listed_by_its_path_and_label_in_the_order_declared() -> None:
    lines = files_lines(_spec(SUMMARY, SPEC, PLAN))

    assert lines[:2] == ["", "# Files"]
    assert [line for line in lines if line.startswith("- `specs/")] == [
        "- `specs/12-{slug}/summary.html` (Summary)",
        "- `specs/12-{slug}/spec.md` (Spec)",
        "- `specs/12-{slug}/plan.md` (Plan)",
    ]


def test_the_files_are_read_from_the_branch_the_run_pushes() -> None:
    text = "\n".join(files_lines(_spec(SPEC)))

    assert "the branch you push" in text
    assert "never" not in text.lower()


def test_a_slug_in_a_path_says_to_replace_it_and_a_plain_path_does_not() -> None:
    assert "Replace `{slug}`" in "\n".join(files_lines(_spec(SPEC)))
    assert "{slug}" not in "\n".join(files_lines(_spec(ShownFile(path="notes.md", label="Notes"))))


def test_each_type_present_is_told_its_rules_once() -> None:
    lines = files_lines(_spec(SUMMARY, SPEC, PLAN))

    assert lines.count(f"- `.html`: {_HTML_RULE}") == 1
    assert lines.count(f"- `.md`: {_MARKDOWN_RULE}") == 1
    assert not any(_TEXT_RULE in line for line in lines)


@pytest.mark.parametrize(
    ("path", "rule"),
    [
        ("summary.html", f"- `.html`: {_HTML_RULE}"),
        ("docs/Summary.HTML", f"- `.html`: {_HTML_RULE}"),
        ("specs/{slug}/SUMMARY.MD", f"- `.md`: {_MARKDOWN_RULE}"),
        ("notes.txt", f"- any other file: {_TEXT_RULE}"),
        ("diagram.svg", f"- any other file: {_TEXT_RULE}"),
        ("summary", f"- any other file: {_TEXT_RULE}"),
    ],
)
def test_a_file_is_told_the_rules_its_extension_is_served_under(path: str, rule: str) -> None:
    assert files_lines(_spec(ShownFile(path=path, label="Out")))[-1] == rule


REPORTS = ShownFile(path="reports/12/*.html", label="Reports")
ANY = ShownFile(path="out/12/**", label="Out")
PATTERN_SENTENCE = (
    "A path with `*` is a pattern: `*` stands for any characters inside one folder, `**` for "
    "any number of folders, and each file you commit that matches it shows on its own, up to "
    f"{PATTERN_MAX_MATCHES} for one pattern."
)
RULE_LINE = ("- `.", "- any")


def test_a_pattern_is_listed_as_one_and_explained_once() -> None:
    lines = files_lines(_spec(SPEC, REPORTS, ANY))
    assert "- `reports/12/*.html` (Reports, a pattern)" in lines
    assert "- `specs/12-{slug}/spec.md` (Spec)" in lines
    assert "\n".join(lines).count(PATTERN_SENTENCE) == 1


def test_no_pattern_no_pattern_sentence() -> None:
    assert PATTERN_SENTENCE not in "\n".join(files_lines(_spec(SPEC)))


def test_a_pattern_with_a_fixed_extension_gets_that_rule_only() -> None:
    lines = files_lines(_spec(REPORTS))
    assert [line for line in lines if line.startswith(RULE_LINE)] == [f"- `.html`: {_HTML_RULE}"]


def test_a_pattern_without_one_gets_every_rule() -> None:
    lines = files_lines(_spec(ANY))
    assert [line for line in lines if line.startswith(RULE_LINE)] == [
        f"- `.html`: {_HTML_RULE}",
        f"- `.md`: {_MARKDOWN_RULE}",
        f"- any other file: {_TEXT_RULE}",
    ]
