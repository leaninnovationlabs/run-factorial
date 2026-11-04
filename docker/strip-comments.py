"""Remove every comment from the Python files under each directory named, in place.

The Dockerfile runs it in a build stage over the source the image ships, so the published image
holds the code and its docstrings but not the review notes in its comments (tech-stack decision
84). Only the comment text goes: each line stays where it was, so a traceback from the image
names the line it names in the repository. The tree it leaves must parse to the same AST, with
the same positions, as the tree it was given, or it exits non-zero and the build stops.

Docstrings stay: the MCP server, the assistant's toolbox and FastAPI read `__doc__`. A shebang
and an encoding cookie stay, as Python reads both. Pragmas (`# type:`, `# noqa`, `# pyright:`,
`# pragma:`) go: only linters and type checkers read them, and none runs on the image.
Stdlib only, because the stage that runs it installs nothing.
"""

from __future__ import annotations

import ast
import io
import re
import sys
import tokenize
from pathlib import Path

_COOKIE = re.compile(r"^[ \t\f]*#.*?coding[:=][ \t]*[-\w.]+")


def _kept(row: int, text: str) -> bool:
    return (row == 1 and text.startswith("#!")) or (row <= 2 and bool(_COOKIE.match(text)))


def strip(source: bytes) -> bytes:
    lines = source.splitlines(keepends=True)
    cuts: dict[int, int] = {}
    for token in tokenize.tokenize(io.BytesIO(source).readline):
        if token.type == tokenize.COMMENT and not _kept(token.start[0], token.string):
            cuts[token.start[0]] = token.start[1]
    encoding = tokenize.detect_encoding(io.BytesIO(source).readline)[0]
    for row, column in cuts.items():
        line = lines[row - 1].decode(encoding)
        ending = line[len(line.rstrip("\r\n")) :]
        lines[row - 1] = (line[:column].rstrip() + ending).encode(encoding)
    return b"".join(lines)


def _tree(source: bytes, name: str) -> str:
    return ast.dump(ast.parse(source, name), include_attributes=True)


def main(roots: list[str]) -> int:
    if not roots:
        print("usage: strip-comments.py DIRECTORY...", file=sys.stderr)
        return 2
    changed = 0
    for root in roots:
        for path in sorted(Path(root).rglob("*.py")):
            before = path.read_bytes()
            after = strip(before)
            if after == before:
                continue
            if _tree(before, str(path)) != _tree(after, str(path)):
                print(f"strip-comments: {path} parses differently without its comments")
                return 1
            path.write_bytes(after)
            changed += 1
    print(f"strip-comments: removed the comments from {changed} files")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
