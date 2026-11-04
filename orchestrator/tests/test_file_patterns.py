"""Preview patterns (`preview.files`, tech-stack decision 71) follow standard glob rules: `*`
inside one folder, `**` across folders. A predicate's `*` is `fnmatch`'s and crosses `/`."""

from __future__ import annotations

import pytest

from orchestrator.predicates.globs import fixed_folder, matches_file_pattern


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("reports/12/*.html", "reports/12/coverage.html", True),
        ("reports/12/*.html", "reports/12/archive/a.html", False),
        ("docs/**/*.md", "docs/a.md", True),
        ("docs/**/*.md", "docs/x/y/a.md", True),
        ("out/*/report.html", "out/a/b/report.html", False),
        ("specs/12-*/*.md", "specs/12-a/x.md", True),
        ("**/*.md", "notes.md", True),
        ("reports/*.html", "reports/A.HTML", False),
    ],
)
def test_a_preview_pattern_follows_standard_glob_rules(
    pattern: str, path: str, expected: bool
) -> None:
    assert matches_file_pattern(pattern, path) is expected


@pytest.mark.parametrize(
    ("pattern", "folder"),
    [
        ("reports/12/*.html", "reports/12"),
        ("docs/12/**/*", "docs/12"),
        ("specs/12-*/*.md", "specs"),
        ("**/*.md", ""),
        ("*", ""),
    ],
)
def test_a_patterns_fixed_folder_ends_before_its_first_wildcard_segment(
    pattern: str, folder: str
) -> None:
    assert fixed_folder(pattern) == folder
