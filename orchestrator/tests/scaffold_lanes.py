"""The two lane shapes the scaffold tests share: the sample-shaped unstacked lane, and the same
lane stacked."""

from __future__ import annotations

# A trimmed copy of docs/samples/factorial-simple/processes/process-simple.yaml's own shape: two
# agent steps, a spec and an impl branch, and a file input under `specs/`. That sample is the repo's
# own canonical lane, and both its `pr_open` calls target the base branch —
# `pr_open("spec/{ticket}-*", "main")` and `pr_open("impl/{ticket}-*", "main")` — so this fixture
# starts unstacked.
UNSTACKED = """\
specVersion: factorial/v1
kind: Process
name: sample-lane
version: 1
entry:
  when: ticket_label("factorial:new")
  step: plan
steps:
  - name: plan
    kind: agent
    agent: plan-agent
    next: build
    workspace:
      repo: "{repo}"
      base: main
      branch: "spec/{ticket}-{slug}"
    inputs:
      - file: "specs/{ticket}-*/spec.md"
    postconditions:
      all:
        - branch_exists("spec/{ticket}-*")
        - pr_open("spec/{ticket}-*", "main")
  - name: build
    kind: agent
    agent: build-agent
    next: done
    workspace:
      repo: "{repo}"
      base: main
      branch: "impl/{ticket}-{slug}"
    postconditions:
      all:
        - branch_exists("impl/{ticket}-*")
        - pr_open("impl/{ticket}-*", "main")
terminal:
  step: done
"""

# The build step's postcondition base becomes the spec branch, so the implementation pull
# request targets it rather than the base branch — the stacked shape the catalog's prompts
# describe.
STACKED = UNSTACKED.replace(
    'pr_open("impl/{ticket}-*", "main")', 'pr_open("impl/{ticket}-*", "spec/{ticket}-*")'
)
