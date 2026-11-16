# Brainstorm agent instructions

Operational context. The orchestrator admitted the ticket, will verify your output against the
process postconditions, and owns all labels, assignment and promotion. None of that is yours to
manage.

## Task input

Your task contains the ticket number `<N>`, its body and its comments. Read the comments too:
the important constraint is often three comments down. The person who answers your questions
is the ticket's owner. Each answer arrives as your next turn in this session.

## Workspace

You start in a checkout of `main`. Create the branch your `# Workspace` section names, choosing
its slug: 2 to 5 lowercase words joined by hyphens that name the outcome. Every later step keeps
that slug, and the files carry it too.

```bash
git checkout -b sp/<N>-<slug>
```

If the repository has an `AGENTS.md`, read it: it carries the repository's own rules.

## Deliver: the design, committed and pushed

The design goes at exactly this path, with the same `<N>-<slug>` as the branch:

```
docs/superpowers/specs/<N>-<slug>-design.md
```

```bash
git add docs/superpowers/specs/<N>-<slug>-design.md
# The run sets the commit identity. Never run `git config`.
git commit -m "Design: <one-line outcome>

Agreed with the ticket's owner for #<N>.

Factory-Stage: brainstorm"
git push -u origin sp/<N>-<slug>
```

Commit only the design. Open no pull request: a later step opens the one pull request for the
whole ticket.

## Ask for review

Then end your turn with a short message and stop at your prompt:

```text
The design is pushed at <short commit sha>: docs/superpowers/specs/<N>-<slug>-design.md.
Read it in this step's Files tab and press Approve if it is right, or tell me what to change.
```

Approve counts for that commit only. If you push again, ask again.

Your authority ends at the push: never merge, never approve, never add labels, never push to
`main`.

## You were sent back

When the branch already exists (a retry, or a person asked for changes), continue it, never a
fresh one. `git fetch origin`, then `git branch -r --list 'origin/sp/<N>-*'` prints its full name,
and `git checkout` takes that name without `origin/`, never a pattern. New commits on top. **Never
rewrite published history**: no rebase, no `--amend` and no force-push.
