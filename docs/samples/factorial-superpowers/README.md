# factorial-superpowers: a sample lane for complex work

This folder is a `.factorial/` directory with one lane, `superpowers`, for work too large or too
unclear to plan from the ticket alone. It follows the method of the open-source
[superpowers](https://github.com/obra/superpowers) project: agree a design with a person, write
a plan of small test-first tasks, build it task by task, then review the whole branch with eyes
that did not write it. A person approves the design and the plan in the dashboard, and the code
at one pull request gate.

To use it, import the **Superpowers template** in the dashboard's config editor, which writes
these files. By hand, copy its contents to `<repo>/.factorial/` on the repository's protected
`factorial` branch, or add its process file and its agent and skill folders beside a lane you
already have.
It runs on GitHub and on GitLab, where a pull request below is a merge request: each prompt gives
both the `gh` and the `glab` command.
An issue enters the lane by carrying `factorial:process:superpowers`. The format of every file
here is explained in `../factorial-simple/`; this README covers only what is different.

## Structure

```
.factorial/
  processes/
    process-superpowers.yaml     # the lane, and the reference for request_approval()
                                 #   and checklist_done()
  skills/                        # one SKILL.md each, adapted from superpowers
    brainstorming/  writing-plans/  executing-plans/
    test-driven-development/  verification-before-completion/  reviewing-a-branch/
  agents/
    sp-brainstorm-agent/  sp-planning-agent/  sp-implement-agent/  sp-review-agent/
      agent.yaml  system-prompt.md  instructions.md
```

## The steps

Every step works on one branch, `sp/<N>-<slug>`, cut from `main`. The brainstorm step creates it
and chooses the slug, and each later step continues it. The design and the plan are committed on
that branch and merge with the code.

| Step | Agent | Skills | Done when |
|---|---|---|---|
| `brainstorm` | `sp-brainstorm-agent` | brainstorming | The design is on the branch and a person approved it. |
| `plan` | `sp-planning-agent` | writing-plans | The plan is on the branch and a person approved it. |
| `build` | `sp-implement-agent` | executing-plans, test-driven-development, verification-before-completion | The pull request to `main` is open, every task box in the plan is ticked, and CI is green. |
| `review` | `sp-review-agent` | reviewing-a-branch, test-driven-development, verification-before-completion | Its `## Review Verdict` comment is on the pull request, the plan is still done, and CI is green. |
| `code_review_gate` | none | | A person approves the pull request. It merges. A request for changes sends the ticket back to `build`. |

The files are at fixed paths, so the predicates and the dashboard can find them:

- `docs/superpowers/specs/<N>-<slug>-design.md`, written by the brainstorm step.
- `docs/superpowers/plans/<N>-<slug>-plan.md`, written by the plan step. Its `- [ ]` boxes are
  the build's checklist. The implement agent ticks a task's boxes in the commit that completes
  that task, so the merged plan records what was done.

**The brainstorm step asks before it writes.** When the ticket leaves open something that would
change what gets built, the agent asks the ticket's owner (the person assigned when the ticket
was admitted) one question at a time, and waits at its prompt for the answer. The owner answers
in the run's terminal while the session is live, or on the ticket page with **Resume** once it
has paused. A ticket that already settles everything gets no questions.

**The build step needs CI on pull requests.** `ci_green` reads false on a branch whose CI never
ran. A repository without CI never finishes this step.

## Approving the design and the plan

A step that awaits review stops at its prompt when its agent asks for review, and the ticket
shows `factorial:waiting`. Its comment on the issue says where to go.

1. Open the ticket's page in the dashboard. A banner leads to the step that waits.
2. Read the design or the plan in the step's **Files** tab. It shows the files as of the
   session's latest stop at its prompt.
3. To accept it, press **Approve**, in the footer under the files. Approve is for the commit
   the review shows. If the agent pushed after you looked, the press is refused, so read again
   and approve the new commit.
4. To ask for changes, answer the agent. While the session is live, type in the run's
   terminal. After 10 minutes at its prompt the session pauses and takes no typing: write the
   answer on the ticket page and press **Resume**. The agent changes the document, pushes, and
   asks for review again. An approval of an earlier commit does not carry over.

The approval is the step's postcondition, `request_approval()`, so the step goes on at the next
check after the press. The ticket's timeline names who approved which commit. An approval of
the design never approves the plan: each run needs its own.

**What a wait is bounded by.** The step's `timeout` counts only the time the agent works, so
a review wait never times the step out. A paused session is kept for 7 days by default. After
that, the step is redone in a fresh session.

## Division of labor

The prompts describe the work and nothing else: the orchestrator owns labels, assignment,
promotion, the approval check and the merge. Each prompt keeps the bare prohibition (never
merge, approve, add labels, or push to `main`), because one shared permission profile makes it
the only control there is. The brainstorm and plan prompts end their step by asking for review
and stopping at the prompt, in place of the "do not stop and wait" line the other agents carry.
Each agent's system prompt keeps the section for a person who types into a run, worded for
what its step delivers (`docs/bootstrap.md`, "Harden agent prompts against terminal input").

## Credit

The six skills are adapted from superpowers (https://github.com/obra/superpowers), Copyright (c)
2025 Jesse Vincent, under the MIT License. The adaptation drops what a factory run cannot use:
scripts, the progress ledger, subagents, the visual companion, the choice of where a plan goes,
and the menu at the end of a branch. Approval happens in the dashboard instead of in the
conversation. The license text is in the repository's `NOTICE` file.
