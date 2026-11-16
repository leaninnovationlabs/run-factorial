"""The agent gap probe: which referenced agents cannot produce a runnable definition.

`upsert_agent_rows` creates an agent row per referenced agent without ever reading the manifest,
so a 201 from sync proves the process parsed and nothing more. This is what notices.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from orchestrator.config import ConfigError, ProcessConfig, parse_process
from orchestrator.factorial.probe import agent_gaps, resolve_agents
from orchestrator.github import ContentsNotFound

pytestmark = pytest.mark.asyncio(loop_scope="session")

PROCESS_YAML = """
specVersion: adlc/v1
kind: Process
name: two
version: 1
entry:
  when: ticket_label("adlc:new")
  step: plan
steps:
  - name: plan
    kind: agent
    agent: plan-agent
    next: build
    workspace:
      repo: "{repo}"
      base: main
      branch: spec/{ticket}-{slug}
  - name: build
    kind: agent
    agent: build-agent
    next: done
    workspace:
      repo: "{repo}"
      base: main
      branch: impl/{ticket}-{slug}
terminal:
  step: done
"""


class FakeSource:
    """Raises whatever the test says, for whichever agent it names."""

    def __init__(self, failures: dict[str, Exception]) -> None:
        self._failures = failures

    def agent_manifest_path(self, name: str) -> str:
        return f".factorial/agents/{name}/agent.yaml"

    async def load_agent(self, repo: str, manifest_path: str, sha: str) -> object:
        name = manifest_path.split("/")[2]
        if name in self._failures:
            raise self._failures[name]
        return object()


def config() -> ProcessConfig:
    return parse_process(PROCESS_YAML, origin="test")


async def test_no_gaps_when_every_agent_resolves() -> None:
    assert await agent_gaps(FakeSource({}), "o/r", "a" * 40, config()) == []


async def test_a_missing_folder_is_a_gap() -> None:
    source = FakeSource({"build-agent": ContentsNotFound("no such path")})
    gaps = await agent_gaps(source, "o/r", "a" * 40, config())
    assert len(gaps) == 1
    assert gaps[0].name == "build-agent"


async def test_an_unparseable_stub_is_the_same_verdict_as_absent() -> None:
    """A merged-but-unfilled stub is the likelier failure: the file existing looks like done."""
    source = FakeSource({"plan-agent": ConfigError("agent.yaml", "empty document")})
    gaps = await agent_gaps(source, "o/r", "a" * 40, config())
    assert len(gaps) == 1
    assert gaps[0].name == "plan-agent"


async def test_both_kinds_of_failure_are_reported_together() -> None:
    source = FakeSource(
        {
            "plan-agent": ConfigError("agent.yaml", "empty document"),
            "build-agent": ContentsNotFound("no such path"),
        }
    )
    gaps = await agent_gaps(source, "o/r", "a" * 40, config())
    assert [gap.name for gap in gaps] == ["build-agent", "plan-agent"]


async def test_a_gap_line_is_bounded() -> None:
    """These land in a readiness `detail` and a sync response; a stack-trace-sized line is noise."""
    source = FakeSource({"plan-agent": ConfigError("agent.yaml", "x" * 5000)})
    (gap,) = await agent_gaps(source, "o/r", "a" * 40, config())
    assert len(gap.line()) < 300


class HeldSource(FakeSource):
    """Holds every read open until `expected` of them are in flight at once. A caller that reads
    one agent at a time never gets there, and each of its reads times out."""

    def __init__(self, expected: int) -> None:
        super().__init__({})
        self._expected = expected
        self._all_started = asyncio.Event()
        self._in_flight = 0
        self.most_in_flight = 0

    async def load_agent(self, repo: str, manifest_path: str, sha: str) -> object:
        self._in_flight += 1
        self.most_in_flight = max(self.most_in_flight, self._in_flight)
        if self._in_flight == self._expected:
            self._all_started.set()
        try:
            await asyncio.wait_for(self._all_started.wait(), timeout=1)
        finally:
            self._in_flight -= 1
        return SimpleNamespace(manifest=manifest_path)


async def test_the_gap_probe_reads_its_agents_together() -> None:
    source = HeldSource(expected=2)
    assert await agent_gaps(source, "o/r", "a" * 40, config()) == []
    assert source.most_in_flight == 2


async def test_agents_are_resolved_together_and_reported_in_name_order() -> None:
    source = HeldSource(expected=2)
    resolved = await resolve_agents(source, "o/r", "a" * 40, config())  # type: ignore[arg-type]
    assert source.most_in_flight == 2
    assert [(agent.name, agent.reason) for agent in resolved] == [
        ("build-agent", None),
        ("plan-agent", None),
    ]
