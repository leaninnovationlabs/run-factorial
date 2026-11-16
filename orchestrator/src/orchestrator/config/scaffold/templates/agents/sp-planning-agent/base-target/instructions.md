# Planning agent instructions

Operational context. A person approved the design before you started. The orchestrator will
verify your output against the process postconditions, and owns all labels, assignment and
promotion.

## Task input

Your task names the ticket number `<N>`, its body, and the design at
`docs/superpowers/specs/<N>-<slug>-design.md`. The design is on the ticket's branch, not on
`__BASE_BRANCH__`.

## Workspace

You start in a checkout of `__BASE_BRANCH__`. Continue the ticket's branch, the one your `# Workspace`
section names:

```bash
git fetch origin
git branch -r --list 'origin/__BRANCH_PREFIX__/<N>-*'
git checkout __BRANCH_PREFIX__/<N>-<slug>
git merge --ff-only origin/__BRANCH_PREFIX__/<N>-<slug>
```

The second command prints the branch's full name. Check out that name without `origin/`:
`git checkout` takes a name, never a pattern such as `__BRANCH_PREFIX__/<N>-*`. The merge brings in what a
person pushed since an earlier step, when the checkout already had the branch.

If the repository has an `AGENTS.md`, read it: it names the build and test commands the plan's
steps must use.

## Deliver: the plan, committed and pushed

The plan goes at exactly this path, with the branch's `<N>-<slug>`:

```
docs/superpowers/plans/<N>-<slug>-plan.md
```

```bash
git add docs/superpowers/plans/<N>-<slug>-plan.md
# The run sets the commit identity. Never run `git config`.
git commit -m "Plan: <one-line outcome>

<How many tasks, and the one you are least sure of.>

Factory-Stage: plan"
git push origin __BRANCH_PREFIX__/<N>-<slug>
```

Commit only the plan. Open no pull request: the build step opens it.

## Ask for review

Then end your turn with a short message and stop at your prompt:

```text
The plan is pushed at <short commit sha>: docs/superpowers/plans/<N>-<slug>-plan.md, <K> tasks.
The one I am least sure of: <task and why>.
Read it in this step's Files tab and press Approve if it is right, or tell me what to change.
```

Approve counts for that commit only. If you push again, ask again.

Your authority ends at the push: never merge, never approve, never add labels, never push to
`__BASE_BRANCH__`.

## You were sent back

On a retry, or when a person asked for changes, the plan may already be on the branch. Update it
in place with new commits on top. **Never rewrite published history**: no rebase, no `--amend`
and no force-push.
