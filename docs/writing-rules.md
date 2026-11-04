# Writing rules: ASD-STE100 Simplified Technical English

All operational documentation uses ASD-STE100 Simplified Technical English (STE).
This page gives the rules and shows the words to use.

STE is a controlled language. It has a dictionary of approved words. Each approved word
has one meaning and one part of speech. The result is text that a reader can understand
quickly, and that translates correctly.

---

## How to check a document

```bash
./scripts/check-ste.sh                 # all STE documents
./scripts/check-ste.sh README.md       # one document
```

The script finds unapproved words, sentences of more than 25 words, the passive
voice, and em dashes. It cannot find a sentence that follows every rule and says nothing. A person
must read for that.

---

## Which documents use STE

The test is one question: **does a person follow this document, or read it?**

A person follows a procedure, often under pressure, and sometimes in a second language.
STE helps. A person reads an argument to decide if they agree. STE removes the
constructions that carry an argument, so it does damage.

| Document | STE | Why |
|---|---|---|
| `README.md` | Yes | A person follows it to set up the factory |
| `docs/setup.md` | **The procedure section only** | An organisation owner outside the team follows that section once. The rest gives reasons |
| `docs/local-container.md` | Yes | A person follows it to build and run the local image |
| `docs/mcp.md` | Yes | A person follows it to connect Claude Code or Codex to the MCP server |
| `docs/bootstrap.md` | No | Marked `[Procedure]` / `[Reference]` per rule 14, but not STE-checked. Its procedure half is a short command sequence a person runs once per repository, and its reference half is the larger part: it argues why two settings keys are boundaries and traces failure modes the API does not report. STE would damage that half, and adding the file to `STE_DOCS` would fail the whole file for it |


A document a person **reads to decide something** (a decision record, a design rationale, a
line meant to work out loud in a demo) keeps full English on purpose, because STE removes the
constructions that an argument needs.

### A document can contain both

Rule 14 says to write a description and a procedure in different ways. A document that has
both can use both styles, if a heading separates them and the heading says which is which.
`docs/setup.md` does this.

Do not change style inside one section.

---

## The rules

### Sentences

1. Write a maximum of 20 words in an instruction.
2. Write a maximum of 25 words in a description.
3. Write only one instruction in each sentence.
4. Write a maximum of 6 sentences in a paragraph.

### Words

5. Use only approved words. See the table below for the words we replace most.
6. Use one word for one meaning. Do not use `check` for both *examine* and *tick*.
7. Do not use a noun cluster of more than three words.
8. Do not remove words to make a sentence shorter. Keep `the`, `a`, and `that`.

### Grammar

9. Use the active voice. Write `The host mints the token`, not `The token is minted`.
10. Use simple tenses: simple present, simple past, simple future.
11. Do not use the `-ing` form as a verb. Write `To start the agent`, not `Starting the agent`.
12. Use a vertical list when a sentence has more than three conditions.
13. Write the command first in a warning. Write `Do not push to main`, not `Main must not be pushed to`.

### Structure

14. Write a description and a procedure in different ways. Do not mix them.
15. Give a warning before the step it applies to, not after.

---

## Words to use

| Do not write | Write |
|---|---|
| ensure | make sure |
| perform, execute | do |
| utilize | use |
| initiate, commence, launch | start |
| terminate | stop |
| obtain, acquire, receive | get |
| provide | give |
| require | need |
| modify | change |
| locate | find |
| display, indicate | show |
| verify | make sure, test |
| attempt | try |
| assist | help |
| prior to | before |
| subsequent to, following | after |
| in order to | to |
| in the event that | if |
| a number of, several | some, or a number |
| approximately | about |
| additional | more |
| sufficient | enough |
| permit, allow | let |
| retain, maintain | keep |
| commence | start |
| illustrate | show |

## Technical names are always approved

A technical name is a name for a part, a tool, or a system. Use it. These are technical
names in this project:

`agent` · `oracle` · `harness` · `template` · `gate` · `pin` · `token` ·
`GitHub App` · `webhook` · `container` · `Evidence Pack` · `plan agent` ·
`build agent` · `review agent`

Do not write `worker` for `agent`. The worker is the orchestrator's own process.

## Technical verbs are approved

A technical verb describes an action on the system. Use these:

`merge` · `mint` · `push` · `pull` · `label` · `commit` · `deploy` · `build` ·
`install` · `mount` · `inject` · `bump`

---

## An example

**Before:**

> Agent credentials are minted per run by the GitHub App — one hour, scoped to named
> repositories, different for each role. That is what makes the build agent's 404 on the
> oracle a fact rather than a promise.

**After:**

> The GitHub App mints a token for each run. The token is valid for one hour. It gives
> access to named repositories only. Each role gets different access.
>
> Because of this, the build agent gets a 404 error from the oracle. This is a fact that
> you can test. It is not a promise.

The second version is longer. It is also clearer, and each sentence makes one statement
that a reader can agree with or disagree with.

---

## Product copy

Product copy is the text that a person reads in the product: the dashboard, the API messages,
the GitHub comments, the MCP text, the scaffold templates and the website. Product copy is not
STE. It shares these rules with STE: short sentences, one word for one meaning, active voice,
keep the articles, name a control by its exact label, and write `such as`, never `e.g.`. The
dashboard uses contractions (`can't`, `couldn't`, `isn't`). `README.md` and `docs/` do not.

### Punctuation

- Do not write an em dash or an en dash. Use a period, a colon, parentheses, `so` or `because`.
- Write a range with `to`: `1 to 12 of 30`, `$1.00 to $3.00`.
- Write words, not arrows: `From abc1234 to def5678`, `spec/12 into main`.
- Use `…` only at the end of a visible progress label (`Loading the file…`), as the marker of
  cut text, and in the pagination gap. Do not put it on a button that opens a dialog.
- Use ` · ` only between short facts (`Codex · API key`). Use a period between sentences, and
  commas in a screen-reader label.
- Show an empty value as `NO_VALUE` (`-`) from `ui/src/format.ts`. Do not write a word in its
  place, and do not compare a value to it. An unpriced cost is never `$0.00`.

### Words

- Do not write `we`. When the product does an action, the subject is `Factorial`:
  `Factorial couldn't reach GitHub`. An error title has no subject: `Couldn't load the tickets`.
- Write each server message (an HTTP detail or hint, an MCP error) as a sentence, with a capital
  letter and a period. Put the next step in a sentence of its own.
- Write `pull request` in the dashboard, the website and the messages. An agent prompt and the
  MCP reference write `pull request (PR)` at first use, and `PR` after that.
- Write `Unblock`. Use US spelling: `organization`, `color`, `labeled`, `canceled`. The stored
  API value `cancelled` does not change.
- In dashboard failure text, write a branch pattern in words: `a branch starting with spec/12-`.
  On GitHub, write it as code: `spec/12-*`.
- A document quotes the dashboard label exactly. The dashboard label is the source.

### The checks

These tests find the marks above, so CI fails when one comes back:

| Check | What it reads |
|---|---|
| `ui/src/copy.test.ts` | Each string, template and JSX text in `ui/src`, but not the tests |
| `orchestrator/tests/test_copy_style.py` | Each string in `orchestrator/src` and `runtime/src` (not a docstring or a log call), the templates, the samples, the MCP resources and the assistant prompts |
| `scripts/check-ste.sh` | An em dash in an STE document |


A line that must keep a mark (a truncation marker, a CLI prompt that an adapter matches) goes in
the allowlist of its test, as a file and a needle, with the reason.
