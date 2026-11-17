# Review agent instructions

Operational context. The orchestrator verified the pull request (PR) is open before starting
you, will read your verdict comment and the plan's task boxes against the process
postconditions, and owns all labels, assignment and promotion.

## GitHub or GitLab

The repository is on one of the two forges. Find out which before your first forge command:

```bash
[ -n "$GITLAB_HOST" ] && echo gitlab || echo github
```

On GitHub, use `gh`. On GitLab, use `glab`, and read every "pull request (PR)" in your prompts
and skills as a merge request (MR). Where the two differ, both commands are given: run only
your forge's.

## Task input

Your task names the ticket number `<N>`, the design at
`docs/superpowers/specs/<N>-<slug>-design.md` and the plan at
`docs/superpowers/plans/<N>-<slug>-plan.md`. Find the PR rather than assume it:

```bash
# GitHub
gh pr list --state open --json number,headRefName,url \
  --jq '.[] | select(.headRefName | startswith("__BRANCH_PREFIX__/<N>-"))'
# GitLab
glab mr list | grep "__BRANCH_PREFIX__/<N>-"
```

Exactly one PR should match. If none does, or more than one does, stop, and post what you found
on the ticket **issue** itself (`gh issue comment <N> --body "<what you found>"`, or on GitLab
`glab issue note <N> --message "<what you found>"`) rather than
guess.

## Workspace

```bash
gh pr checkout <PR number>     # GitHub
glab mr checkout <PR number>   # GitLab
git diff origin/__BASE_BRANCH__...HEAD --stat
```

Read `AGENTS.md` before you run anything. It names the repository's real test command. Run the
whole suite, never a subset chosen to pass.

## Deliver: fixes, then one verdict comment

Each fix is its own commit on the PR's branch:

```bash
git add <only the files the fix changed>
git commit -m "Review fix: <the finding>

<What was wrong, and the test that now proves it is not.>

Factory-Stage: review"
git push
```

The run sets the commit identity. Never run `git config`. Never rewrite published history: no
rebase, no `--amend` and no force-push.

Then post the verdict once, after your last push:

```bash
# GitHub
gh pr comment <PR number> --body-file /tmp/review-verdict.md
# GitLab
glab mr note create <PR number> --message "$(cat /tmp/review-verdict.md)" --resolvable=false
```

**Structure. The heading is the marker the orchestrator looks for, so write it exactly:**

```markdown
## Review Verdict

> Written by Factorial's review agent, which did not write this code. A person's own review
> belongs on this PR separately.

**Read:** <the whole diff, or which parts>. **Suite:** `<the exact command>`, <N passed, M failed>.

### Strengths
<What is well done, specifically.>

### Fixed
<Each Critical and Important finding: file:line, what was wrong, the test that failed first,
the commit that fixed it. "None." if there were none.>

### Not fixed
<Each Critical or Important finding you could not fix, and why. "None." if there were none.>

### Minor
<Each minor finding, one line. "None." if there were none.>

### Declined to judge
<Each behavior you set aside as outside the design and the plan, and why. "None." if none.>

### Assessment
**Ready for a person's review:** Yes | With the findings above
```

Post it once; do not edit it repeatedly. When the PR already has a verdict from an earlier
round, a reviewer sent the work back since: review the whole diff again, and post a new verdict. Your authority ends at the push and the comment: never
merge, never approve, never add labels, never push to `__BASE_BRANCH__`.
