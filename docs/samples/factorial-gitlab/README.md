# factorial-gitlab: a sample `.factorial` folder for GitLab

This folder is the `factorial-simple` sample for a project whose issues and merge requests are on
GitLab. The lanes, the predicates and the folder layout are the same. The agents use the `glab`
CLI and open merge requests (MRs) instead of pull requests. Do not use this folder in a GitHub
project.

The editor's **Simple template (GitLab)** is this folder's process files, one lane each, with
comments for operators and each `runtime:` line commented out as an example. Tests hold each
template lane to the sample lane of the same name, once that line is turned on.

Commit the `simple` lane into a GitLab project with no config, every file ticked, and the commit
writes every path in this folder except this README and the other lane's file. The text of each
agent and skill file is the text in this folder, and a test keeps them equal. The one difference
is the MCP server: the commit writes it as the inert `mcp/product-docs.yaml.example`, and leaves
the `mcp:` block of `plan-agent` commented out until you set it up.

The project's issue source selects the agents, not the template. A commit into a GitLab project
writes these GitLab agents from either template, and a commit into a GitHub project writes the
GitHub agents of `factorial-simple`.

The prompts are per agent, not per lane. Both lanes use the same three agent folders: `primary`
uses `plan-agent` and `build-agent`, and `simple` also uses `test-agent`.

## Contents

- `processes/process-primary.yaml`: plan, spec review, build, code review, done.
- `processes/process-simple.yaml`: the same lane with a test step. The test step needs no GitLab
  CI: the verdict comment of the test agent is its acceptance gate.
- `agents/plan-agent/`, `agents/build-agent/`, `agents/test-agent/`: the manifest, the
  `system-prompt.md` and the `instructions.md` of each agent. `plan-agent` also has the
  `speckit-*` skills, which are the same as in `factorial-simple`.
- `skills/project-constitution/`: the rules each agent obeys, with GitLab names.
- `skills/spec-page/`: the summary page that the plan step shows. It links the spec files on
  GitLab.
- `mcp/product-docs.yaml`: an example MCP server. Its URL is a placeholder. See the
  `factorial-simple` README, "MCP servers: selecting is not granting".
