"""Fact derivation: lane-scoped and per-agent, and what distinguishes a stacked lane."""

from __future__ import annotations

from orchestrator.config import parse_process
from orchestrator.config.scaffold.facts import (
    derive_agent_facts,
    derive_lane_facts,
    is_stacked,
)
from tests.scaffold_lanes import STACKED, UNSTACKED


def test_stacked_lane_is_detected():
    assert is_stacked(parse_process(STACKED, origin="t")) is True


def test_unstacked_lane_is_detected():
    assert is_stacked(parse_process(UNSTACKED, origin="t")) is False


def test_branch_prefix_is_per_agent_on_an_unstacked_lane():
    """The regression this whole task exists for.

    `_only()` over the heads of the unstacked lane's `pr_open` calls sees {"spec", "impl"} and
    returns None, so the lane-scoped prefixes are absent. Each step's own `workspace.branch` is
    unambiguous, and is what a base-target prompt needs.
    """
    config = parse_process(UNSTACKED, origin="t")
    assert derive_agent_facts(config, "plan-agent")["__BRANCH_PREFIX__"] == "spec"
    assert derive_agent_facts(config, "build-agent")["__BRANCH_PREFIX__"] == "impl"


def test_base_branch_is_per_agent():
    config = parse_process(UNSTACKED, origin="t")
    assert derive_agent_facts(config, "build-agent")["__BASE_BRANCH__"] == "main"


def test_agent_name_is_supplied_per_agent():
    config = parse_process(UNSTACKED, origin="t")
    assert derive_agent_facts(config, "plan-agent")["__AGENT_NAME__"] == "plan-agent"


def test_lane_facts_still_carry_impl_prefix_when_stacked():
    facts = derive_lane_facts(parse_process(STACKED, origin="t"))
    assert facts["__IMPL_PREFIX__"] == "impl"
    assert facts["__SPEC_PREFIX__"] == "spec"


def test_lane_facts_omit_impl_prefix_when_unstacked():
    assert "__IMPL_PREFIX__" not in derive_lane_facts(parse_process(UNSTACKED, origin="t"))


def test_specs_dir_reads_from_file_inputs():
    assert derive_lane_facts(parse_process(STACKED, origin="t"))["__SPECS_DIR__"] == "specs"


def test_unknown_agent_gets_lane_facts_only():
    facts = derive_agent_facts(parse_process(UNSTACKED, origin="t"), "nope")
    assert facts["__AGENT_NAME__"] == "nope"
    assert "__BRANCH_PREFIX__" not in facts
