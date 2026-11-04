"""`config_inventory` — the `.factorial/` listing the configuration explorer draws.

Seeded from `docs/samples/factorial-simple/` through the same `FakeContents` the resolver's own
tests use, so a listing here is a listing of the canonical folder rather than of a fixture shaped
to match the code.

The three things worth pinning: a file's `kind` comes from its position and nothing else, `drift`
is only ever asserted from two blob shas actually compared, and a manifest that does not resolve
hangs its warning on that manifest's own path.
"""

from __future__ import annotations

from collections.abc import Sequence

from orchestrator.config import ProcessConfig, parse_process
from orchestrator.factorial import RepoConfigSource
from orchestrator.factorial.inventory import classify, config_inventory
from orchestrator.github import TreeEntry, TreeTruncated
from tests.repo_source_fakes import REPO, SAMPLE_PROCESS_PATH, SHA, FakeContents, sample_files

HEAD = "b" * 40


def config() -> ProcessConfig:
    return parse_process(sample_files()[(REPO, SAMPLE_PROCESS_PATH, SHA)], origin="test")


def source_for(files: dict[tuple[str, str, str], str]) -> RepoConfigSource:
    return RepoConfigSource(FakeContents(files))


def at(files: dict[tuple[str, str, str], str], sha: str) -> dict[tuple[str, str, str], str]:
    """The same file map re-keyed at another sha — a second commit with identical content."""
    return {(repo, path, sha): text for (repo, path, _), text in files.items()}


# --- kind ---------------------------------------------------------------------------------------


def test_kind_comes_from_position_under_the_config_root() -> None:
    root = ".factorial"
    # Both layouts: the legacy root file is read for ever, and `processes/` is what is written.
    assert classify(f"{root}/process.yaml", root=root) == "process"
    assert classify(f"{root}/processes/process-hotfix.yaml", root=root) == "process"
    assert classify(f"{root}/agents/plan-agent/agent.yaml", root=root) == "agent"
    assert classify(f"{root}/agents/plan-agent/system-prompt.md", root=root) == "prompt"
    assert classify(f"{root}/agents/plan-agent/skills/x/SKILL.md", root=root) == "skill"
    assert classify(f"{root}/agents/plan-agent/mcp/x.yaml", root=root) == "mcp"
    assert classify(f"{root}/skills/project-constitution/SKILL.md", root=root) == "skill"
    assert classify(f"{root}/mcp/github.yaml", root=root) == "mcp"
    assert classify(f"{root}/README.md", root=root) == "other"


def test_a_legacy_root_classifies_the_same_way() -> None:
    """A repo pinned before issue #138 still lists `.adlc/`, and its files are the same kinds."""
    assert classify(".adlc/agents/plan-agent/agent.yaml", root=".adlc") == "agent"


# --- listing ------------------------------------------------------------------------------------


async def test_it_lists_every_file_of_the_sample_folder() -> None:
    files = sample_files()
    inventory = await config_inventory(
        source_for({**files, **at(files, HEAD)}), REPO, pinned_sha=SHA, config=config()
    )
    assert inventory.config_dir == ".factorial"
    assert {f.path for f in inventory.files} == {path for (_, path, _) in files}
    assert [f.path for f in inventory.files] == sorted(f.path for f in inventory.files)


async def test_identical_content_at_head_is_no_drift() -> None:
    files = sample_files()
    inventory = await config_inventory(
        source_for({**files, **at(files, HEAD)}), REPO, pinned_sha=SHA, config=config()
    )
    assert {f.drift for f in inventory.files} == {"same"}
    assert inventory.head_sha == HEAD


async def test_an_edited_file_is_the_only_one_that_differs() -> None:
    files = sample_files()
    head = at(files, HEAD)
    head[(REPO, ".factorial/agents/plan-agent/system-prompt.md", HEAD)] = "rewritten"
    inventory = await config_inventory(
        source_for({**files, **head}), REPO, pinned_sha=SHA, config=config()
    )
    drifted = {f.path for f in inventory.files if f.drift == "differs"}
    assert drifted == {".factorial/agents/plan-agent/system-prompt.md"}


async def test_a_file_added_since_the_pin_is_listed_as_added() -> None:
    files = sample_files()
    head = at(files, HEAD)
    head[(REPO, ".factorial/agents/new-agent/agent.yaml", HEAD)] = "specVersion: factorial/v1"
    inventory = await config_inventory(
        source_for({**files, **head}), REPO, pinned_sha=SHA, config=config()
    )
    added = {f.path: f.drift for f in inventory.files if f.drift == "added"}
    assert added == {".factorial/agents/new-agent/agent.yaml": "added"}


async def test_a_file_deleted_since_the_pin_is_listed_as_removed() -> None:
    files = sample_files()
    head = at(files, HEAD)
    del head[(REPO, ".factorial/skills/project-constitution/SKILL.md", HEAD)]
    inventory = await config_inventory(
        source_for({**files, **head}), REPO, pinned_sha=SHA, config=config()
    )
    removed = {f.path for f in inventory.files if f.drift == "removed"}
    assert removed == {".factorial/skills/project-constitution/SKILL.md"}


async def test_a_head_that_no_longer_has_the_folder_reads_as_removed() -> None:
    """Read, and empty: gone at head is a fact, which is not the same as unreadable."""
    inventory = await config_inventory(
        source_for(sample_files()), REPO, pinned_sha=SHA, config=config()
    )
    assert inventory.head_sha == HEAD
    assert {f.drift for f in inventory.files} == {"removed"}


async def test_an_unreadable_head_degrades_to_unknown_rather_than_failing() -> None:
    """The pin is the subject; drift is the extra. A head that will not read must not take the
    listing down with it, or an outage closes the explorer."""

    class TruncatedAtHead(FakeContents):
        async def list_tree(self, repo: str, root: str, sha: str) -> Sequence[TreeEntry]:
            if sha == HEAD:
                raise TreeTruncated(f"{repo}@{sha[:7]}: tree truncated by GitHub")
            return await super().list_tree(repo, root, sha)

    files = sample_files()
    source = RepoConfigSource(TruncatedAtHead({**files, **at(files, HEAD)}))
    inventory = await config_inventory(source, REPO, pinned_sha=SHA, config=config())
    assert inventory.head_sha is None
    assert {f.drift for f in inventory.files} == {"unknown"}
    assert len(inventory.files) == len(files)  # the listing itself still stands


async def test_a_head_that_is_the_pin_needs_no_second_listing() -> None:
    """The commit is the same one, so its own listing already is the answer."""
    contents = FakeContents(at(sample_files(), HEAD))
    inventory = await config_inventory(
        RepoConfigSource(contents), REPO, pinned_sha=HEAD, config=config()
    )
    assert {f.drift for f in inventory.files} == {"same"}
    assert len({sha for (_, _, sha) in contents.tree_fetches}) == 1


# --- manifest warnings --------------------------------------------------------------------------


async def test_a_sound_sample_carries_no_warnings() -> None:
    files = sample_files()
    inventory = await config_inventory(
        source_for({**files, **at(files, HEAD)}), REPO, pinned_sha=SHA, config=config()
    )
    assert [f.path for f in inventory.files if f.warning] == []


async def test_an_unparseable_manifest_warns_on_its_own_path() -> None:
    files = sample_files()
    files[(REPO, ".factorial/agents/plan-agent/agent.yaml", SHA)] = "# comments only\n"
    inventory = await config_inventory(
        source_for({**files, **at(files, HEAD)}), REPO, pinned_sha=SHA, config=config()
    )
    warned = {f.path: f.warning for f in inventory.files if f.warning}
    assert list(warned) == [".factorial/agents/plan-agent/agent.yaml"]
    assert warned[".factorial/agents/plan-agent/agent.yaml"]


async def test_a_manifest_the_process_never_names_is_not_a_gap() -> None:
    """Which agents matter is a property of the process — an unreferenced folder is just files."""
    files = sample_files()
    files[(REPO, ".factorial/agents/spare-agent/agent.yaml", SHA)] = "# comments only\n"
    inventory = await config_inventory(
        source_for({**files, **at(files, HEAD)}), REPO, pinned_sha=SHA, config=config()
    )
    assert [f.path for f in inventory.files if f.warning] == []


async def test_no_snapshot_parse_means_no_warnings_rather_than_guessed_ones() -> None:
    files = sample_files()
    files[(REPO, ".factorial/agents/plan-agent/agent.yaml", SHA)] = "# comments only\n"
    inventory = await config_inventory(
        source_for({**files, **at(files, HEAD)}), REPO, pinned_sha=SHA, config=None
    )
    assert [f.path for f in inventory.files if f.warning] == []


async def test_each_file_carries_its_blob_at_the_pin() -> None:
    files = sample_files()
    source = source_for({**files, **at(files, HEAD)})
    inventory = await config_inventory(source, REPO, pinned_sha=SHA, config=config())
    listed = {entry.path: entry.sha for entry in await source.list_files(REPO, SHA)}
    assert {f.path: f.sha for f in inventory.files} == listed
