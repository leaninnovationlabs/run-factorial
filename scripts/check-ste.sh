#!/usr/bin/env bash
# Check the operational documentation against the Simplified Technical English rules.
#
#   check-ste.sh [file ...]
#
# With no arguments it checks every document that docs/writing-rules.md marks as STE.
#
# This finds the mechanical rules only: unapproved words, long sentences, the
# passive voice, and an em dash. It cannot find a sentence that follows every rule
# and still says nothing. A person must read for that.
#
# A rule that nobody checks is a rule that decays, which is why this exists.
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 - "$@" <<'PY'
import re, sys, pathlib

# Only the documents a person FOLLOWS. See docs/writing-rules.md for the test and
# for why the other docs are not here.
#
# setup.md is absent because only one section of it is STE, and this
# script checks whole files. Check that section by hand, or split the file later.
STE_DOCS = [
    "README.md",
    "docs/local-container.md",
    "docs/mcp.md",
]

REPLACE = {
    "ensure": "make sure", "ensures": "makes sure", "ensuring": "makes sure",
    "perform": "do", "performs": "does", "execute": "do", "executes": "does",
    "utilize": "use", "utilise": "use", "initiate": "start", "commence": "start",
    "terminate": "stop", "obtain": "get", "acquire": "get",
    "provide": "give", "provides": "gives", "require": "need", "requires": "needs",
    "modify": "change", "locate": "find", "display": "show", "indicate": "show",
    "verify": "make sure / test", "attempt": "try", "assist": "help",
    "prior to": "before", "subsequent to": "after", "in order to": "to",
    "approximately": "about", "additional": "more", "sufficient": "enough",
    "permit": "let", "permits": "lets", "retain": "keep", "illustrate": "show",
    "commences": "starts", "leverage": "use", "facilitate": "help",
}

PASSIVE = re.compile(
    r"\b(is|are|was|were|be|been|being)\s+(\w+ed|written|built|made|given|"
    r"taken|held|read|sent|kept|put|set|shown|found|run)\b", re.I)

MAX_WORDS = 25


def strip_noise(text: str) -> str:
    text = re.sub(r"```.*?```", "", text, flags=re.S)      # code blocks
    text = re.sub(r"`[^`]*`", "", text)                     # inline code
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)    # link targets
    return text


def sentences(text: str):
    for line in text.split("\n"):
        s = line.strip()
        if not s or s.startswith(("|", "#", "```", "---", ">")):
            continue
        s = re.sub(r"^[-*\d.]+\s+", "", s)                  # list markers
        for part in re.split(r"(?<=[.!?])\s+", s):
            if part.strip():
                yield part.strip()


targets = sys.argv[1:] or STE_DOCS
problems = 0

for f in targets:
    p = pathlib.Path(f)
    if not p.exists():
        print(f"  skip   {f} (not found)")
        continue
    text = p.read_text()
    body = strip_noise(text)
    found = []

    for m in re.finditer("\u2014", text):
        line = text[:m.start()].count("\n") + 1
        found.append((line, f"line {line}: em dash, write a period, a colon or parentheses"))

    for word, better in REPLACE.items():
        for m in re.finditer(rf"\b{re.escape(word)}\b", body, re.I):
            line = body[:m.start()].count("\n") + 1
            found.append((line, f"word '{m.group(0)}': write '{better}'"))

    for s in sentences(body):
        n = len(s.split())
        if n > MAX_WORDS:
            found.append((0, f"{n} words (max {MAX_WORDS}): {s[:64]}…"))
        m = PASSIVE.search(s)
        if m:
            found.append((0, f"passive '{m.group(0)}': {s[:56]}…"))

    if found:
        problems += len(found)
        print(f"  ✗ {f}: {len(found)} problem(s)")
        for _, msg in found[:8]:
            print(f"      {msg}")
        if len(found) > 8:
            print(f"      … and {len(found) - 8} more")
    else:
        print(f"  ✓ {f}")

print()
if problems:
    print(f"  {problems} problem(s). See docs/writing-rules.md")
    sys.exit(1)
print("  All checked documents follow the mechanical rules.")
PY
