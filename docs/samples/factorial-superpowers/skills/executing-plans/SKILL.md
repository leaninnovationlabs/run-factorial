---
name: executing-plans
description: Build an approved plan yourself, one task at a time, test first, ticking each task's boxes in the commit that completes it. Use when the plan is approved and the build starts.
---

Adapted from superpowers (github.com/obra/superpowers), MIT License.

# Executing plans

The plan already did the thinking. Build it exactly, prove each step with a test you watched
fail and then pass, and leave a record that survives your own forgetting: the plan file itself,
with its boxes ticked as the work lands.

## Before Task 1

- Read the plan once, and the design it names. The design is the authority; the plan argues
  from it. When they conflict, the design wins.
- Read the plan's Global constraints. Every task's requirements include them.
- Scan for conflicts between tasks: what one task produces against what a later one consumes.
  Rule on each now (below).
- Load the `test-driven-development` skill. It governs every step of every task.

## Where you are

Your memory of earlier tasks does not survive a long session. The plan file and `git log` are
the record. A task whose boxes are ticked is done: its commit exists. Never redo it. Start at
the first task with an open box.

## The task loop

1. **Read the task again**, in full, even if you remember it. You remember a summary; the task
   has the exact values.
2. **Work the steps in order.** A test step's code is written first and run first. Watching it
   fail is a step: a test that passes before the code exists is a finding about the test.
3. **Compare every run with what the step expects.**
   - It matches: next step.
   - The code is wrong: find the cause. Never patch the output to match.
   - The plan is wrong: make the smallest change that keeps to the design, and record it as a
     ruling.
4. **Run the repository's whole suite**, not only your test, and read the result.
5. **Tick the task's boxes and commit**, in one commit with the task's code and tests. Push.

## The checklist rules

The build step is done only when every box in the plan is ticked, so the boxes are a claim the
orchestrator believes.

- **Tick a task's boxes only in the commit that completes that task.** Never in an earlier
  commit, never in a separate "update checklist" commit.
- **Never tick ahead.** A box for work not yet committed is a false report.
- **Never delete, reword or merge a task to finish.** A task you cannot do stays open, and the
  pull request says why.
- A reviewer's requested changes become a new task at the end of the plan, with a box per point.

## Rulings, not stalls

Conflicts, ambiguities and small plan defects are yours to decide. Record each in the pull
request body under `Rulings`: what you decided, why, and what it costs if wrong. A deviation
without a ruling is a decision made in secret.

Four things stop you, and only these: an irreversible or destructive operation; a
security-sensitive action; a side effect outside this repository; and a plan so broken that
every way forward is a guess. For those, ask the person with your question tool and stop.

## When every box is ticked

Load `verification-before-completion`. Run the whole suite fresh, read the output, and update
the pull request body: progress, rulings, and what you could not verify. Then end your turn with
a short summary. A later step reviews the whole branch with fresh eyes.

## Rationalizations

| Excuse | Reality |
|---|---|
| "I remember what Task N says" | You remember a summary. Read it again. |
| "The plan's code is right, skip watching the test fail" | A test you never saw fail proves nothing. |
| "I'll tick the boxes at the end" | A box ticks in the task's own commit, or the record is false. |
| "This task is not worth it; I'll drop it" | Leave it open and say why. Deleting it hides it. |
| "The plan is wrong here, I'll just do the right thing" | Do it, and record the ruling. |
| "Tests should pass, the change was trivial" | "Should" is not evidence. Run them. |
