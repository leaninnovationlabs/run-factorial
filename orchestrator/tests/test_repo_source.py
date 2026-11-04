"""`RepoConfigSource` — resolving a repo's `.factorial/` at a sha. No network, no database.

The fake repo is **seeded from `docs/samples/factorial-simple/` on disk**, so these tests prove the
resolver handles the canonical folder layout rather than a fixture invented to match the code. If
the sample changes shape, this file is supposed to notice.
"""

from __future__ import annotations

import pytest

from orchestrator.config import ConfigError
from orchestrator.factorial import RepoConfigSource
from tests.repo_source_fakes import (
    HEAD_SHA,
    REPO,
    SAMPLE,
    SAMPLE_KEY,
    SAMPLE_PROCESS_PATH,
    SHA,
    FakeContents,
    FileMap,
    sample_files,
)


def make_source(files: FileMap | None = None) -> tuple[RepoConfigSource, FakeContents]:
    contents = FakeContents(sample_files() if files is None else files)
    return RepoConfigSource(contents), contents


def disk(rel: str) -> str:
    return (SAMPLE / rel).read_text(encoding="utf-8")


# --- process ------------------------------------------------------------------------------------


async def test_load_processes_parses_the_sample() -> None:
    source, _ = make_source()
    processes = await source.load_processes(REPO, SHA)
    assert set(processes) == {SAMPLE_KEY}
    process = processes[SAMPLE_KEY]
    assert process.name == SAMPLE_KEY
    assert process.step_names == ("plan", "spec_review_gate", "build", "test", "code_review_gate")


async def test_a_repo_still_on_the_root_file_loads_under_its_own_name() -> None:
    """The legacy layout is read for ever, and keeps whatever `name:` it carries: identity rules
    guard a NEW lane, never an established one."""
    files = {
        (repo, path.replace(f"processes/process-{SAMPLE_KEY}.yaml", "process.yaml"), sha): text
        for (repo, path, sha), text in sample_files().items()
    }
    source, _ = make_source(files)
    assert set(await source.load_processes(REPO, SHA)) == {SAMPLE_KEY}


async def test_a_repo_carrying_both_layouts_is_refused() -> None:
    """Silently preferring one would leave an operator editing a file nothing reads."""
    files = sample_files()
    files[(REPO, ".factorial/process.yaml", SHA)] = files[(REPO, SAMPLE_PROCESS_PATH, SHA)]
    source, _ = make_source(files)
    with pytest.raises(ConfigError) as exc:
        await source.load_processes(REPO, SHA)
    assert "both layouts" in str(exc.value)


async def test_a_file_that_is_not_a_process_file_is_refused_not_skipped() -> None:
    """A lane an operator believes they added, silently absent because of a filename typo, is
    exactly what the strictness prevents."""
    files = sample_files()
    files[(REPO, ".factorial/processes/hotfix.yaml", SHA)] = files[(REPO, SAMPLE_PROCESS_PATH, SHA)]
    source, _ = make_source(files)
    with pytest.raises(ConfigError) as exc:
        await source.load_processes(REPO, SHA)
    assert "'hotfix.yaml' is not one" in str(exc.value)


async def test_the_path_a_version_reads_follows_the_layout_of_its_own_commit() -> None:
    """A version registered before `processes/` existed still reads the root file."""
    new = sample_files()
    legacy = {
        (repo, path.replace(f"processes/process-{SAMPLE_KEY}.yaml", "process.yaml"), HEAD_SHA): text
        for (repo, path, _), text in new.items()
    }
    source, _ = make_source(new | legacy)
    assert await source.process_path_at(REPO, SAMPLE_KEY, SHA) == SAMPLE_PROCESS_PATH
    assert await source.process_path_at(REPO, SAMPLE_KEY, HEAD_SHA) == ".factorial/process.yaml"


async def test_missing_process_names_the_origin() -> None:
    """A repo with neither folder is told to create the current one, not the retired one."""
    source, _ = make_source(files={})
    with pytest.raises(ConfigError) as exc:
        await source.load_processes(REPO, SHA)
    assert f"{REPO}@{SHA[:7]}:.factorial/process.yaml" in str(exc.value)


# --- agents -------------------------------------------------------------------------------------


async def test_plan_agent_resolves_with_prompts_and_both_skill_scopes() -> None:
    source, _ = make_source()
    agent = await source.load_agent(REPO, ".factorial/agents/plan-agent/agent.yaml", SHA)

    assert (agent.name, agent.repo, agent.commit_sha) == ("plan-agent", REPO, SHA)
    assert agent.manifest.model == "claude-sonnet-5-5"
    # Contents, not paths — and never an empty string standing in for a missing file.
    assert agent.system_prompt == disk("agents/plan-agent/system-prompt.md")
    assert agent.instructions == disk("agents/plan-agent/instructions.md")
    assert agent.system_prompt.strip() and agent.instructions.strip()

    assert tuple((s.name, s.scope) for s in agent.skills) == (
        ("speckit-specify", "local"),
        ("speckit-clarify", "local"),
        ("speckit-tasks", "local"),
        ("project-constitution", "shared"),
        ("spec-page", "shared"),
    )
    by_name = {s.name: s.content for s in agent.skills}
    assert by_name["speckit-specify"] == disk("agents/plan-agent/skills/speckit-specify/SKILL.md")
    assert by_name["project-constitution"] == disk("skills/project-constitution/SKILL.md")


async def test_agent_with_no_local_skills_resolves() -> None:
    source, _ = make_source()
    agent = await source.load_agent(REPO, ".factorial/agents/build-agent/agent.yaml", SHA)
    assert tuple((s.name, s.scope) for s in agent.skills) == (("project-constitution", "shared"),)


@pytest.mark.parametrize(
    "repo_path",
    [
        ".factorial/agents/build-agent",
        ".factorial/agents/build-agent/",
        "/.factorial/agents/build-agent",
    ],
)
async def test_repo_path_pointing_at_the_folder_still_works(repo_path: str) -> None:
    """Operators write the folder as often as the file; guessing wrong is a confusing 404."""
    source, _ = make_source()
    agent = await source.load_agent(REPO, repo_path, SHA)
    assert agent.name == "build-agent"


async def test_missing_prompt_raises_naming_the_origin() -> None:
    files = sample_files()
    del files[(REPO, ".factorial/agents/plan-agent/system-prompt.md", SHA)]
    source, _ = make_source(files)

    with pytest.raises(ConfigError) as exc:
        await source.load_agent(REPO, ".factorial/agents/plan-agent/agent.yaml", SHA)
    message = str(exc.value)
    assert "prompts.system" in message
    assert f"{REPO}@{SHA[:7]}:.factorial/agents/plan-agent/system-prompt.md" in message
    # The manifest is named too, so an operator knows which agent is broken.
    assert exc.value.origin == f"{REPO}@{SHA[:7]}:.factorial/agents/plan-agent/agent.yaml"


async def test_missing_skill_raises_naming_the_origin() -> None:
    files = sample_files()
    del files[(REPO, ".factorial/skills/project-constitution/SKILL.md", SHA)]
    source, _ = make_source(files)

    with pytest.raises(ConfigError) as exc:
        await source.load_agent(REPO, ".factorial/agents/build-agent/agent.yaml", SHA)
    assert "shared skill 'project-constitution'" in str(exc.value)
    assert ".factorial/skills/project-constitution/SKILL.md" in str(exc.value)


async def test_folder_name_must_match_the_manifest_name() -> None:
    """That equality is what makes `agent: plan-agent` in `process.yaml` resolvable."""
    files = sample_files()
    manifest = files.pop((REPO, ".factorial/agents/plan-agent/agent.yaml", SHA))
    files[(REPO, ".factorial/agents/planner/agent.yaml", SHA)] = manifest
    source, _ = make_source(files)

    with pytest.raises(ConfigError) as exc:
        await source.load_agent(REPO, ".factorial/agents/planner/agent.yaml", SHA)
    assert "'plan-agent'" in str(exc.value) and "'planner'" in str(exc.value)


async def test_skill_declared_in_both_scopes_is_an_error() -> None:
    files = sample_files()
    key = (REPO, ".factorial/agents/build-agent/agent.yaml", SHA)
    files[key] = files[key].replace(
        "skills:\n  shared:", "skills:\n  local:\n    - project-constitution\n  shared:"
    )
    source, _ = make_source(files)

    with pytest.raises(ConfigError) as exc:
        await source.load_agent(REPO, ".factorial/agents/build-agent/agent.yaml", SHA)
    assert "skills.local" in str(exc.value) and "skills.shared" in str(exc.value)


# --- refs and call counts -----------------------------------------------------------------------


async def test_resolve_ref_none_uses_the_config_branch() -> None:
    source, contents = make_source()
    assert await source.resolve_ref(REPO, None) == HEAD_SHA
    assert contents.default_branch_calls == []
    assert contents.resolve_ref_calls == [(REPO, "heads/factorial")]


async def test_resolve_ref_with_an_explicit_ref_skips_the_default_branch() -> None:
    source, contents = make_source()
    assert await source.resolve_ref(REPO, SHA) == SHA
    assert contents.default_branch_calls == []


async def test_agent_files_are_fetched_concurrently() -> None:
    """Eight reads of pure latency if they were serialized; the manifest read must precede them."""
    source, contents = make_source()
    await source.load_agent(REPO, ".factorial/agents/plan-agent/agent.yaml", SHA)
    # 2 prompts + 3 local skills + 2 shared skills + 1 shared mcp server
    assert contents.max_inflight == 8


async def test_a_shared_skill_used_by_two_agents_is_read_once() -> None:
    source, contents = make_source()
    await source.load_agent(REPO, ".factorial/agents/plan-agent/agent.yaml", SHA)
    await source.load_agent(REPO, ".factorial/agents/build-agent/agent.yaml", SHA)

    shared = (REPO, ".factorial/skills/project-constitution/SKILL.md", SHA)
    assert contents.reads.count(shared) == 2  # both agents ask
    assert contents.fetches.count(shared) == 1  # one round trip, because the sha is immutable


# --- which folder (issue #138) --------------------------------------------------------------


async def test_resolve_dir_prefers_factorial() -> None:
    source, _ = make_source(sample_files() | sample_files(config_dir=".adlc"))
    assert await source.resolve_dir(REPO, SHA) == ".factorial"


async def test_resolve_dir_falls_back_to_adlc() -> None:
    source, _ = make_source(sample_files(config_dir=".adlc"))
    assert await source.resolve_dir(REPO, SHA) == ".adlc"


async def test_resolve_dir_is_probed_once_per_commit() -> None:
    source, contents = make_source(sample_files(config_dir=".adlc"))
    assert await source.resolve_dir(REPO, SHA) == ".adlc"
    assert await source.resolve_dir(REPO, SHA) == ".adlc"
    # `list_dir` returns () for an absent folder rather than raising, so the memo keeps the
    # miss — probing a repo that has not moved yet costs one call, not one per caller.
    assert contents.dir_fetches.count((REPO, ".factorial", SHA)) == 1


async def test_load_processes_reads_the_folder_that_exists() -> None:
    source, _ = make_source(sample_files(config_dir=".adlc"))
    processes = await source.load_processes(REPO, SHA)
    assert processes[SAMPLE_KEY].name == SAMPLE_KEY


async def test_shared_skills_follow_the_manifest_root_not_the_source_default() -> None:
    """An agent pinned at an older sha still lives under `.factorial/`, and so do its shared skills.

    Reading the manifest from one root and its shared skills from another resolves a skill
    against a folder that pin never had.
    """
    contents = FakeContents(sample_files(config_dir=".adlc"))
    source = RepoConfigSource(contents, config_dir=".factorial")
    agent = await source.load_agent(REPO, ".adlc/agents/plan-agent/agent.yaml", SHA)
    assert [s.name for s in agent.skills if s.scope == "shared"] == [
        "project-constitution",
        "spec-page",
    ]
    assert [m.name for m in agent.mcp_servers] == ["product-docs"]


# --- shared component names -----------------------------------------------------------------


async def test_mcp_server_names_reads_file_stems_not_folder_names() -> None:
    """Unlike `agent_names`/`shared_skill_names`, one file IS one server — no folder to list."""
    source, _ = make_source()
    assert await source.mcp_server_names(REPO, SHA) == ("product-docs",)


async def test_an_mcp_example_counts_as_a_file_the_repo_holds_for_its_name() -> None:
    """Read only to keep the catalog off a file the repo already has, so an example the catalog
    once wrote must count, or a later commit would write over the operator's copy of it."""
    files = {
        (REPO, f".factorial/mcp/{name}", SHA): "# x\n"
        for name in ("product-docs.yaml.example", "product-docs.yaml", "search.yaml.example")
    }
    source, _ = make_source(files=files)
    assert await source.mcp_server_names(REPO, SHA) == ("product-docs", "search")


async def test_mcp_server_names_with_no_mcp_folder_is_empty() -> None:
    source, _ = make_source(files={})
    assert await source.mcp_server_names(REPO, SHA) == ()
