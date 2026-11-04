# Test agent instructions

Operational context. The orchestrator verified the implementation merge request (MR) is open
before starting you, will check your verdict comment against the process postconditions, and
owns all labels, assignment, and promotion.

## Task input

Your task names the ticket number `<N>`. The implementation lives on a branch named
`impl/<N>-<slug>`, already pushed by the build agent as an open merge request. The
build agent chose `<slug>` in an earlier run, so find it rather than assume it:

```bash
glab mr list | grep "impl/<N>-"
```

Exactly one merge request should match. If none does, or more than one does, stop. There is
no single MR to comment on yet, so post what you found on the ticket **issue** itself
(`glab issue note <N> --message "<what you found>"`) rather than guessing which MR to test.

## Workspace

```bash
glab mr checkout <MR number>
```

You are now on the implementation branch, exactly as the build agent left it. You push
nothing: no new branch, no commit, no merge request. Your only output is the comment below.

## Running the suite

Read `AGENTS.md` before you run anything ad hoc. It names the repo's real test command.
Run the whole suite the way a human would, not a hand-picked subset.

## Deliver the verdict comment

```bash
glab mr note create <MR number> --message "$(cat /tmp/test-verdict.md)" --resolvable=false
```

**Structure. The heading is the marker the orchestrator looks for, so write it exactly:**

```markdown
## Test Verdict

> Written by Factorial's test agent. It reports the exact command it ran and its output.
> A reviewer's own opinion belongs on this MR separately.

**Command:** `<the exact command you ran>`

**Result:** PASS | FAIL (`<N passed, M failed, of T total>`)

### Failures
<For each failing test: its name and the relevant output. Write "None." if the result is
PASS.>
```

Post it once; do not edit it repeatedly. One comment is your entire output.
