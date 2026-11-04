# Build agent instructions

Operational context. The orchestrator verified the spec pull request (PR) is open and approved
before starting you. It will verify your output against the process postconditions, and it
owns all labels, assignment and promotion.

## Task input

Your task names the ticket number `<N>` and the spec directory
`__SPECS_DIR__/<N>-<slug>/`. The plan agent chose the slug in that path. Reuse it
exactly.

## Workspace (read this before you branch)

Your checkout may be sitting on `__BASE_BRANCH__`, and your harness may tell you so. **The
spec is not there.** It is on the still-open spec branch, and that branch is both your
starting point and your merge target:

```bash
git fetch origin
git checkout -b __BRANCH_PREFIX__/<N>-<slug> origin/__SPEC_PREFIX__/<N>-<slug>
```

Confirm you have the spec before you write any code: `ls __SPECS_DIR__/<N>-<slug>/` must
show `spec.md`, `plan.md` and `tasks.md`. If it does not, you branched from the wrong
place; fix that before anything else.

Read `AGENTS.md` before you write code. It carries the repo's invariants: build and test
commands, and the rules that are easy to violate without noticing.

## Deliver: branch, commit, push, PR

The run is not complete until a PR exists.

Commit in focused commits with real messages; the reviewer reads them alongside the diff.

```bash
git add <only the files you changed>
git commit -m "<One-line summary of the change>

<What changed, the approach, and the alternative you rejected, if any.>

Factory-Stage: build"

git push -u origin __BRANCH_PREFIX__/<N>-<slug>

# --base is the SPEC branch, never __BASE_BRANCH__. A PR against __BASE_BRANCH__ satisfies
# no postcondition, so the step never completes, and CI may still report green.
gh pr create --base __SPEC_PREFIX__/<N>-<slug> \
  --title "[Issue #<N>] Build: <one-line outcome>" --body-file /tmp/pr-body.md
```

Verify the base after opening it: `gh pr view --json baseRefName`. It must be the spec
branch.

Commit only files you changed: no lockfile churn, no build output, no incidental
reformatting. The run sets the commit identity. Never run `git config`.

**PR body. Write it for a reviewer who reads the diff but not the spec:**

```markdown
> Opened by Factorial's build agent. The commit trailer `Factory-Stage: build` records the stage.

## What this is
<Two sentences: the change, and the approach taken.>
This pull request implements the approved spec in `__SPECS_DIR__/<N>-<slug>/`.

## Acceptance criteria (self-assessment)
AC1 implemented. AC2 implemented. AC5 is not mechanically checkable: it needs human judgment.

## What I could not verify
<Anything you could not run, prove or determine. Write "Nothing" only if that is true.>

Refs #<N>.
```

Opening the PR is where your authority ends.

## Revision: you were sent back

When your inputs include a `## pr_review_comments` section, you are revising earlier work:
a reviewer requested changes on your earlier PR. Their feedback is in that section in full.
Address every point, and do not reintroduce what it flagged. (The section can be empty when
the rejection carried no comment text; the revision rules below still apply.)

Build on the delivered branch, not a fresh one:

```bash
git fetch origin && git checkout __BRANCH_PREFIX__/<N>-<slug>
```

New commits on top. **Never rewrite published history**: no rebase, no squash, no
`--amend` and no force-push. The inline review comments are anchored to those exact commits.
