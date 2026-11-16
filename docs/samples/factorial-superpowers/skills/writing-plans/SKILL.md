---
name: writing-plans
description: Turn an approved design into a plan of small, test-first tasks whose steps are checkboxes, for an engineer who has seen neither this codebase nor the design. Use after the design is approved, before any code.
---

Adapted from superpowers (github.com/obra/superpowers), MIT License.

# Writing plans

Write the plan for an engineer who has not seen this codebase or the design's conversation.
Assume they write idiomatic code once they know the exact interface and the exact test. What
they cannot know is what you decided: which files, which names and signatures, which values from
the design, which tests prove each task. Write those down. DRY. YAGNI. Test first. Small commits.

## Scope

If the design covers several independent parts, say so in your request for review: each part
should be its own ticket. Every plan produces working, tested software on its own.

## File structure first

Before the tasks, map which files are created or changed and what each is responsible for. One
clear responsibility per file. Files that change together live together. In an existing
codebase, follow its patterns.

## Tasks and steps

A task is the smallest unit that carries its own test cycle and that a reviewer could reject on
its own. Fold setup and documentation into the task that needs them. A step is one action with a
checkable result: write the failing test, run it and see it fail, write the code, run it and see
it pass, commit.

Every step is a `- [ ]` box. The implement agent ticks a task's boxes in the commit that
completes it, and the build is not done until every box in the file is ticked. So:

- Every box is work that can be done and checked. No box for "think about" or "consider".
- Never put a box inside a fenced code block: it is not counted as a task.
- A box the builder cannot tick holds the ticket, so a step that might prove impossible says
  what to do instead.

## The plan's format

````markdown
# <Feature> plan

**Goal:** <one sentence>
**Architecture:** <two or three sentences>
**Design:** docs/superpowers/specs/<N>-<slug>-design.md

## Global constraints
<The design's project-wide requirements, one line each, with exact values.>

## Review focus
<Up to five inputs or failure modes the design implies but no task's tests exercise, most
likely first. For each, add its test to the task that owns the code.>

### Task 1: <component>

**Files:** create `path/to/file.py`; modify `path/to/other.py`; test `tests/test_file.py`
**Produces:** <names and signatures later tasks rely on>

- [ ] **Step 1: Write the failing test** `test_<behavior>` asserting <the exact values>
- [ ] **Step 2: Run it and see it fail:** `<command>`, expected: fails with <reason>
- [ ] **Step 3: Implement** `function(arg: Type) -> Result` in `path/to/file.py`
- [ ] **Step 4: Run it and see it pass:** `<command>`
- [ ] **Step 5: Commit**
````

Show code only where it decides something: a test's name and assertions, a signature, an
algorithm the signature does not determine. Write it in a fenced block below the step, never
with a box in it.

## What a step contains

A step is done when the builder can write exactly one reasonable thing from it.

- **A test step:** the test's name and its assertions, with the design's exact values.
- **A code step:** the signature, the file, and the values the design pins. The builder writes
  the body.
- **A run step:** the command, and the output that means it passed.

A plan longer than the code it describes has written the code instead. A line that decides
nothing ("handle edge cases", "add validation", "TBD") is the opposite failure.

## Review it yourself

With the design open beside it:

1. **Coverage:** every part of the design has a task. Add the task for a gap.
2. **Steps:** every step decides one thing, and none carries a whole function body.
3. **Names:** a name or signature used in a later task matches the task that defines it.
4. **Review focus:** each line has its test in a task.
5. **Proportion:** a plan several times longer than the design is a transcript. Cut it back.

Fix what you find in place, then push, and ask for review.
