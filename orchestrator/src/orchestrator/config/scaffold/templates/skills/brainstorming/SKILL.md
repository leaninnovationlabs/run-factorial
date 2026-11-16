---
name: brainstorming
description: Find out what the ticket's owner wants and why, one question at a time, then write the design a planner can work from without them. Use before any plan or code.
---

Adapted from superpowers (github.com/obra/superpowers), MIT License.

# Brainstorming

You are good at building. You build what you believe the person wants, and that belief is
usually thinner than it feels. This skill is for finding out what they want, and why, before
anything is built. When you understand the why, you make the hundred decisions they never
mention the way they would have made them.

## First: what do you know?

- **Everything that matters is in the ticket.** Ask nothing. Write the design, say in it which
  calls you made, and ask for review.
- **Something that would change what gets built is missing.** Start asking.

## Look before you ask

Read the ticket, its comments, everything they point at, and the code the change touches. Keep
reading until you can explain how the relevant parts work today and what this change would
touch. Never ask what the ticket or the repository already answers, and never reopen what the
owner already decided.

## Asking

Ask with your question tool, one question per turn, then stop at your prompt. The answer comes
back as your next turn.

- Ask only when the answer would change what gets built and nothing you can read answers it.
  Otherwise state your call and your reason, as a proposal they can correct.
- Until you know why they want it, ask open questions in their own words, with no example
  answers attached. Their words carry intent your options cannot.
- Once you know why, a choice you can name gets options: sketch each concretely, with what it
  trades off, and put the one you would pick first.
- Build each question on their last answer or on something you found, so it helps them think
  through a consequence or a case they may have missed. Say what the answer would change.
- Use plain language. Never use process words, and never quote this skill.
- When they say "just pick" or "what do you think?", propose something concrete.

Ground to cover, as territory and never as a script: what this is and what makes it matter;
why now and what success looks like; goals, non-goals, and what would make it a failure even
if it works; what exists now and what they tried; the details they care about.

How it gets made (the tools, the libraries, the shape of the code) is usually yours to decide.
Build on what the project already uses. Ask about it only if they raise it.

## Lay out the approaches

Once you know what they want, find the real ways to build it, drop the weak ones, and sketch the
two or three that remain with what each trades off. Ask them to pick, with your recommendation
first and why. When there is one sensible way, say so, and say why the others do not fit.

## Size the work

| Size | The design |
|---|---|
| A small change | A short one: what you settled, a line or two per heading. |
| A project | The full design below. |

If the ticket is really several independent projects, say so in your first question and agree
which one this ticket is.

## The written design

Write a plain document that a capable builder could plan from without going back to the person.
Only what they said goes in as theirs; anything you filled in goes in as your call, with the
reason.

```markdown
# <N>-<slug>: <one-line outcome>

## Why
## Goals
## Non-goals
## Constraints
## Approaches
<The two or three ways, what each trades off, which one was picked, by whom, and why.>
## Design
<The parts of the how that were decided, as they were decided.>
## Calls I made
<Each decision the person did not make, so they can check it while reading.>
## Left to the builder
```

Before you push it, read it as that builder would: every question you would have to ask is a
gap. Answer it from the conversation, settle it yourself and list it under `Calls I made`, or,
if only the person can answer it and the answer changes what gets built, ask them.

## The gate

Nothing gets built until the person approves the design. Building includes product code,
scaffolding and installing dependencies. Reading and exploring are fine. After you push, ask for
review and stop at your prompt. A reply like "sounds good" to your questions approves nothing:
only their approval of the pushed design does.

## Red flags

| Thought | Reality |
|---|---|
| "I'll offer a menu of what they might want" | Options steer. Ask them to describe it first. |
| "I'll fill in sensible defaults" | Good. Write them under `Calls I made`. A default they never saw is one they could not reject. |
| "I'll ask three things at once to save time" | One question per turn. The answer to the first changes the second. |
| "I'll start the plan while I wait" | The plan is the next step's work. Push the design and stop. |
