# Test agent instructions

Operational context. The orchestrator verified the implementation pull request (PR) is open
before starting you, will check your verdict comment against the process postconditions, and
owns all labels, assignment, and promotion.

## GitHub or GitLab

The repository is on one of the two forges. Find out which before your first forge command:

```bash
[ -n "$GITLAB_HOST" ] && echo gitlab || echo github
```

On GitHub, use `gh`. On GitLab, use `glab`, and read every "pull request (PR)" in your prompts
and skills as a merge request (MR). Where the two differ, both commands are given: run only
your forge's.

## Task input

Your task names the ticket number `<N>`. The implementation lives on a branch named
`__BRANCH_PREFIX__/<N>-<slug>`, already pushed by the build agent as an open pull request. The build
agent chose `<slug>` in an earlier run, so find it rather than assume it:

```bash
# GitHub
gh pr list --state open --json number,headRefName,url \
  --jq '.[] | select(.headRefName | startswith("__BRANCH_PREFIX__/<N>-"))'
# GitLab
glab mr list | grep "__BRANCH_PREFIX__/<N>-"
```

Exactly one pull request should match. If none does, or more than one does, stop. There is
no single PR to comment on yet, so post what you found on the ticket **issue** itself
(`gh issue comment <N> --body "<what you found>"`, or on GitLab
`glab issue note <N> --message "<what you found>"`) rather than guessing which PR to test.

## Workspace

```bash
gh pr checkout <PR number>     # GitHub
glab mr checkout <PR number>   # GitLab
```

You are now on the implementation branch, exactly as the build agent left it. You push
nothing: no new branch, no commit, no pull request. Your only output is the comment below.

## Running the suite

Read `AGENTS.md` before you run anything ad hoc. It names the repo's real test command.
Run the whole suite the way a human would, not a hand-picked subset.

## Deliver the verdict comment

```bash
# GitHub
gh pr comment <PR number> --body-file /tmp/test-verdict.md
# GitLab
glab mr note create <PR number> --message "$(cat /tmp/test-verdict.md)" --resolvable=false
```

**Structure. The heading is the marker the orchestrator looks for, so write it exactly:**

```markdown
## Test Verdict

> Written by Factorial's test agent. It reports the exact command it ran and its output.
> A reviewer's own opinion belongs on this PR separately.

**Command:** `<the exact command you ran>`

**Result:** PASS | FAIL (`<N passed, M failed, of T total>`)

### Failures
<For each failing test: its name and the relevant output. Write "None." if the result is
PASS.>
```

Post it once; do not edit it repeatedly. One comment is your entire output.
