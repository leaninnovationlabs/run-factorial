# factorial-simple: a sample `.factorial` folder

This folder is the canonical shape of a `.factorial/` directory. To use it in a real repo, copy
its contents to `<repo>/.factorial/` on the repo's protected `factorial` branch, which is where
the orchestrator reads config (docs/bootstrap.md, Step 1a). The orchestrator reads every
`processes/process-<name>.yaml`, snapshots each into `process_version` on ticket admission, and
resolves each step's `agent:` name against `agents/<name>/agent.yaml`.

**One process per file, and the file stem names it.** The stem minus its `process-` prefix must
equal the file's own `name:`, so `processes/process-hotfix.yaml` declares `name: hotfix` and an
issue enters it by carrying `factorial:process:hotfix`. Adding a second lane is adding a second
file; each keeps its own history of commits. This sample ships two: `simple`, and `primary`,
which is `simple` without its test step. The project's `default_process` setting names one of
them, and an issue with no process label enters that one.

**Copying this by hand is no longer the only way to get a folder.** The process builder's commit
to `factorial` writes your process file plus an `agents/<name>/` folder for every
agent it names that your repo does not already have, offered in a file explorer where every
candidate file is readable and editable before you select it. Nothing is consented to by name
alone. Each catalog agent carries lane **variants**, `stacked` and/or `base-target`; only the one
matching your config's own branch topology ever contributes a file. A project whose issues are on
GitLab gets the GitLab copy of `base-target` instead, whichever template it starts from
(`../factorial-gitlab/`). An agent left with no
matching variant (or with every one of its files left unticked) gets the same
commented-but-empty `agent.yaml` it always would. Those prompts declare
`skills/project-constitution/`, itself variant-per-lane, and the same commit writes whichever
copy matches: a copy of the one below, with its naming table pointed at your lane's own branches.

**This sample's lanes are the editor's GitHub template, and the variant that serves them is
`base-target`.** The editor's **Simple template** is this folder's process files, one lane each,
with comments written for operators and each `runtime:` line commented out as an example; tests
hold each template lane to the sample lane of the same name, once that line is turned on. Every `pr_open`
in it targets `main` outright, never a branch glob. Gate 1 merges the spec
to the base branch, and the implementation branches from there. That is the
shape `base-target` describes. Commit the `simple` lane into a repo with no config, every
file ticked, and the commit writes every path this folder has except this README and the other
lane's file, which that lane's own commit writes (the seeded
`factorial` branch carries its own): all three of `plan-agent`, `build-agent`, and `test-agent`
with the text that sits here, which a test keeps equal, the same `speckit-*`
skills, and `skills/spec-page/`, which the plan agent declares to write the summary page its
plan step shows (docs/enable-previews.md). The one other difference is the MCP server, which needs an endpoint, a
secret and an allowlist entry: the commit writes it as the inert `mcp/product-docs.yaml.example`,
and leaves `plan-agent`'s `mcp:` block commented out until you set it up. The **`stacked`**
variant remains a separate, leaner prompt set for the other lane shape, where the implementation
pull request targets the spec branch instead of the base branch. It ships no agent-local skills and
no MCP server at all; it is not what this sample's own lane matches.

The builder never rewrites an agent folder or a shared skill your repo already has from its
templates, so tuning the constitution after the first commit is safe. Later commits leave your
copy alone unless you edit that file in the builder yourself.

## Structure (finalized)

```
.factorial/
  processes/                   # one file per ADLC lane; the stem minus `process-` is its name
    process-simple.yaml        # THE workflow definition, and the commented reference
                               #   for the format
    process-primary.yaml       # `simple` without its test step
  skills/                      # skills shared by every agent in this process
    project-constitution/SKILL.md
    spec-page/SKILL.md         #   the plan step's summary page (see its `preview.files`)
  mcp/                         # MCP servers shared by any agent that names them; one YAML
    product-docs.yaml          #   document per server, file stem == server name. Also the
                               #   commented reference for the format
  agents/
    <agent-name>/              # folder name == the `agent:` name a process file uses
      agent.yaml               # manifest: prompts, skills, mcp, model, limits, declared
                               #   secrets, permissions. The process's Runtime picks the CLI
      system-prompt.md         # who the agent is + its output contract
      instructions.md          # operational context: workspace, delivery, revision flow
      skills/                  # skills only this agent uses
        <skill-name>/SKILL.md
      mcp/                     # MCP servers only this agent uses (same format as .factorial/mcp/)
        <server-name>.yaml
```

Conventions: one folder per agent, folder name is the registry name (kebab-case); prompts are
plain markdown, never inlined in YAML; skills follow the SKILL.md format with `---` YAML
frontmatter (`name`, `description`); secrets are *declared* in agent.yaml (and in each MCP server
file) and *stored* in the orchestrator (encrypted), never in this folder. The vendor API key is
the exception: when the process file names a `runtime:`, that Runtime holds it and the manifest's
declaration of it is inert.

## MCP servers: selecting is not granting

`.factorial/` lives in the repo agents open PRs against, so this folder cannot be the thing that grants
an agent a tool. A server file *selects*; the project's operator allowlist
(`project.settings.mcp_allowlist` in the orchestrator's database, which no agent can reach)
*permits*. A declared server whose name, transport, URL, command/args or tool patterns the
allowlist does not cover blocks the agent from running at all, with the mismatch
named. `agent.yaml` names servers and nothing else: tool narrowing lives
in the server file, so one policy never has two homes.

**Before copying this folder into a real repo:** `plan-agent` references `product-docs`, whose URL
is a placeholder. Either drop the `mcp:` block from `agent.yaml` (and the `mcp/` folder), or point
the server at a real endpoint and add the matching entry to `project.settings.mcp_allowlist`.
Otherwise `plan-agent` will not resolve, and the orchestrator will say exactly that.

## Runtime: selecting is not defining either

A process file may carry a top-level `runtime: <slug>` naming the Runtime its agents run on: a
type, a name and credentials that an operator defines on the dashboard's Runtimes page and that
live in the orchestrator's database. The same trust direction as MCP servers: this file *selects*
by slug, and can neither create a Runtime nor reach its credentials. The key is required, and sync
refuses a slug that does not exist. The Runtime's type says which CLI runs the agents, so they
declare no `runtime.kind`. This sample names `claude-api`.

## Agents

The process file references three agents, all present. Human gates need none:

| Step | Agent | Skills | MCP |
|---|---|---|---|
| `plan` | `plan-agent` | speckit-specify, speckit-clarify, speckit-tasks + shared project-constitution, spec-page | shared product-docs |
| `build` | `build-agent` | shared project-constitution | none |
| `test` | `test-agent` | shared project-constitution | none |

## Division of labor: why the prompts are small

The orchestrator owns admission, labels, assignment, promotion, gate approval + merge, and
every postcondition. The runtime wrapper owns checkout, secret injection, and result
reporting. So an agent prompt describes the WORK and nothing else: `plan-agent` and
`build-agent` push a branch and open a PR, `test-agent` posts a verdict comment on a PR it did
not create. A prompt that explains labels or promotion duplicates the orchestrator and drifts
out of sync with it. The one thing prompts DO keep is the bare prohibition (never merge,
self-approve, add labels, or push to `main`), because one shared permission profile makes it
the only control there is.

`plan-agent` and `build-agent` also carry one section for a person who types into a run's
terminal: what the person says can steer the work but never replaces it, and a request to stop
gets the PR opened first. The shared constitution limits a typed instruction the same way, since
its precedence list otherwise ranks a person's instruction above its own rules.
`docs/bootstrap.md`, "Harden agent prompts against terminal input", says why each line is there
and how to give the section to another agent.

## The test agent

`test-agent` runs in the `simple` lane only, after the build agent's PR is open. It runs the
repository's suite on a normal checkout and posts one pass/fail comment, `## Test Verdict`. It
pushes nothing. `ci_green` is the acceptance gate of the step; the comment is evidence that the
step ran, and the diagnostic when it failed. `pr_comment_contains` checks that the verdict
exists, not what it says, so a `FAIL` verdict next to green CI still moves the ticket to
`code_review_gate`. The `primary` lane has no test step, for a repository without CI on pull
requests.
