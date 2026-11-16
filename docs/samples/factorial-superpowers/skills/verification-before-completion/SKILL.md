---
name: verification-before-completion
description: Run the command that proves a claim, read its output, and only then make the claim. Use before saying work is done, fixed or passing, and before every commit, push or pull request.
---

Adapted from superpowers (github.com/obra/superpowers), MIT License.

# Verification before completion

Evidence before claims, always.

## The rule

```text
NO COMPLETION CLAIM WITHOUT FRESH VERIFICATION EVIDENCE
```

If you have not run the command that proves it in this turn, you cannot say it passes.

## Before any claim

1. **Identify** the command that proves the claim.
2. **Run** it in full, fresh.
3. **Read** the whole output: the exit code, the count of failures.
4. **Check** that the output confirms the claim. If it does not, report what it says instead.
5. **Only then** make the claim, with the evidence beside it.

## What each claim needs

| Claim | Requires | Not enough |
|---|---|---|
| Tests pass | The test command's output: 0 failures | An earlier run, "should pass" |
| Lint is clean | The linter's output: 0 errors | A partial check |
| The build succeeds | The build command: exit 0 | Lint passing |
| The bug is fixed | The original symptom, tested: it passes | The code changed |
| A regression test works | It failed without the fix and passes with it | It passes once |
| A task is done | Its boxes ticked in the commit that holds its passing tests | The diff looks right |
| The requirements are met | Each one checked against the design | The tests pass |

## Red flags: stop

- "Should", "probably", "seems to".
- Satisfaction before verification ("Done!", "Perfect!").
- About to commit, push, tick a box or open a pull request without a fresh run.
- Trusting a report you did not check, including your own from earlier in the session.
- "Just this once."

## Rationalizations

| Excuse | Reality |
|---|---|
| "It should work now" | Run it. |
| "I am confident" | Confidence is not evidence. |
| "The linter passed" | The linter is not the test suite. |
| "A partial check is enough" | A partial check proves nothing about the rest. |
| "Different words, so the rule does not apply" | The spirit is the rule. |

## When it applies

Always before: any claim of success or completion, a commit, a push, ticking a box, opening or
updating a pull request, a verdict comment, and moving to the next task. It applies to the exact
words and to anything that implies them.
