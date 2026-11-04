"""Permission ceiling config: the manifest's `permissions:` shape and the operator ceiling
(design.md §7.1 amendment, decision S7).

No database and no network — Pydantic and pure policy functions, mirroring
`test_mcp_config.py`'s allowlist section. `permission_policy.check_permissions` is the ONE place
the read/write coverage rule and the "absent means the default profile" fallback are exercised;
`config.models.Permissions` and `factorial.permission_policy.PermissionCeiling` are exercised
directly
here since neither needs YAML or a repo to construct.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from orchestrator.config import Permissions
from orchestrator.factorial.permission_policy import (
    CEILING_KEY,
    PERMISSION_SCOPES,
    PermissionCeiling,
    PermissionCeilingInvalid,
    check_permissions,
    parse_ceiling,
    requested_permissions,
)
from orchestrator.github.client import APP_PERMISSIONS

# --- parse_ceiling --------------------------------------------------------------------------


def test_an_absent_ceiling_is_none_not_permissive() -> None:
    assert parse_ceiling({}) is None
    assert parse_ceiling({"agent_runtime": "claude"}) is None


def test_a_malformed_ceiling_raises_rather_than_degrading() -> None:
    with pytest.raises(PermissionCeilingInvalid):
        parse_ceiling({CEILING_KEY: "nonsense"})
    with pytest.raises(PermissionCeilingInvalid):
        parse_ceiling({CEILING_KEY: ["contents:write"]})
    with pytest.raises(PermissionCeilingInvalid) as exc:
        parse_ceiling({CEILING_KEY: {"contents": "admin"}})  # not a valid level
    assert CEILING_KEY in str(exc.value)


def test_a_well_formed_ceiling_parses() -> None:
    ceiling = parse_ceiling({CEILING_KEY: {"contents": "write", "checks": "read"}})
    assert ceiling == PermissionCeiling(contents="write", checks="read")


# --- workflows is ungrantable at any level, in the pydantic model itself --------------------


def test_workflows_is_never_declarable_on_the_ceiling() -> None:
    with pytest.raises(ValidationError) as exc:
        PermissionCeiling.model_validate({"workflows": "write"})
    assert "workflows" in str(exc.value)


def test_workflows_is_never_declarable_on_the_manifest() -> None:
    with pytest.raises(ValidationError) as exc:
        Permissions.model_validate({"workflows": "write"})
    assert "workflows" in str(exc.value)


def test_an_unknown_scope_is_rejected_on_the_ceiling() -> None:
    with pytest.raises(PermissionCeilingInvalid):
        parse_ceiling({CEILING_KEY: {"deployments": "write"}})


def test_an_unknown_scope_is_rejected_on_the_manifest() -> None:
    with pytest.raises(ValidationError):
        Permissions.model_validate({"deployments": "write"})


# --- check_permissions: absence falls back to the default profile --------------------------


def test_absent_manifest_and_absent_ceiling_both_fall_back_to_the_default_profile() -> None:
    """The back-compat position: every manifest written before this feature existed declares
    no `permissions:` key and every project has no ceiling — nothing about that combination may
    start failing resolution."""
    assert check_permissions(None, None) == []


def test_an_explicit_request_within_the_default_profile_needs_no_ceiling() -> None:
    narrower = Permissions(contents="read")  # the default profile grants contents:write
    assert check_permissions(narrower, None) == []


def test_an_explicit_request_beyond_the_default_profile_is_refused_without_a_ceiling() -> None:
    wider = Permissions(checks="write")  # not in the default profile at all
    problems = check_permissions(wider, None)
    assert problems and "checks" in problems[0]


def test_absent_manifest_permissions_is_checked_against_an_explicit_ceiling_not_skipped() -> None:
    """Unlike the MCP allowlist, there is no "declares nothing -> skip" shortcut: an absent
    `permissions:` key still requests the default profile, so a ceiling narrower than the
    default still refuses it."""
    ceiling = PermissionCeiling(contents="read")  # narrower than the default's contents:write
    problems = check_permissions(None, ceiling)
    # Only `contents` even appears in this ceiling, so every scope in the default profile is
    # a problem — counted off the profile itself, so adding a scope to it cannot skew this.
    assert len(problems) == len(APP_PERMISSIONS)


# --- requested_permissions: the one map both the ceiling check and the mint use ---------------


def test_an_absent_permissions_key_requests_the_default_profile() -> None:
    assert requested_permissions(None) == dict(APP_PERMISSIONS)


def test_a_declared_block_is_requested_verbatim_with_unset_scopes_dropped() -> None:
    """`None` on a field means "did not ask", not "asked for nothing" — a scope-level null would
    be sent to GitHub as a request it cannot parse."""
    assert requested_permissions(Permissions(contents="read", metadata="read")) == {
        "contents": "read",
        "metadata": "read",
    }


@pytest.mark.parametrize(
    "requested",
    [None, Permissions(contents="read"), Permissions(contents="write", issues="write")],
)
def test_check_and_mint_resolve_absent_permissions_through_one_function(
    requested: Permissions | None,
) -> None:
    """Both halves of the ceiling invariant read a manifest through `requested_permissions`, so
    "absent means the default profile" cannot mean two different things. This test proves the
    property of the shared function only — that the map it returns is itself ceiling-clean exactly
    when `check_permissions` says the manifest is. That `worker.agent_steps` is the caller passing
    that map to the mint is pinned separately, by the two mint assertions in `test_reconcile.py`;
    nothing here imports the worker or can see its call site."""
    ceiling = PermissionCeiling(contents="write", issues="write", metadata="read")
    permitted = check_permissions(requested, ceiling) == []
    minted = requested_permissions(requested)
    # Same map, re-checked directly: whatever the mint sends must itself clear the ceiling.
    as_model = Permissions.model_validate(dict(minted))
    assert (check_permissions(as_model, ceiling) == []) is permitted


# --- check_permissions: over-ceiling refusal names ALL violations ---------------------------


def test_an_over_ceiling_request_is_refused_with_every_violation_named() -> None:
    requested = Permissions(contents="write", checks="write")
    ceiling = PermissionCeiling(contents="read")  # checks isn't granted at all
    problems = check_permissions(requested, ceiling)
    assert len(problems) == 2
    assert any("contents" in p and "read" in p for p in problems)
    assert any("checks" in p for p in problems)


def test_a_permitted_request_produces_no_violations() -> None:
    requested = Permissions(contents="read", metadata="read")
    ceiling = PermissionCeiling(contents="write", metadata="read", issues="write")
    assert check_permissions(requested, ceiling) == []


# --- read/write coverage matrix ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("requested_level", "ceiling_level", "permitted"),
    [
        ("read", "read", True),
        ("read", "write", True),  # a write grant covers a read request
        ("write", "write", True),
        ("write", "read", False),  # a read grant never covers a write request
    ],
)
def test_read_write_coverage(requested_level: str, ceiling_level: str, permitted: bool) -> None:
    requested = Permissions.model_validate({"contents": requested_level})
    ceiling = PermissionCeiling.model_validate({"contents": ceiling_level})
    assert (check_permissions(requested, ceiling) == []) is permitted


@pytest.mark.parametrize("scope", PERMISSION_SCOPES)
def test_every_permitted_scope_is_independently_checkable(scope: str) -> None:
    """Each of the closed set's scopes behaves the same way: granted at `write`, a `read`
    request passes; ungranted entirely, any request is refused."""
    requested = Permissions.model_validate({scope: "read"})
    granting_ceiling = PermissionCeiling.model_validate({scope: "write"})
    assert check_permissions(requested, granting_ceiling) == []

    empty_ceiling = PermissionCeiling()
    problems = check_permissions(requested, empty_ceiling)
    assert problems and scope in problems[0]


def test_the_closed_set_matches_the_default_profiles_keys() -> None:
    """The default profile (github/client.py's APP_PERMISSIONS) must be expressible entirely
    within the closed scope set, or the back-compat fallback would be unrepresentable."""
    assert set(APP_PERMISSIONS) <= set(PERMISSION_SCOPES)


def test_workflows_is_not_in_the_permitted_scope_set() -> None:
    assert "workflows" not in PERMISSION_SCOPES


def test_the_three_hand_maintained_copies_cannot_drift() -> None:
    """`PERMISSION_SCOPES`, `config.models.Permissions`, and `PermissionCeiling` each enumerate
    the same six scope names independently (the transport-Literal precedent `mcp_policy.py`
    already sets, not a cross-layer import — see the module docstring). Nothing else catches a
    scope added to one and not the others; this is that tripwire."""
    assert (
        set(PERMISSION_SCOPES)
        == set(Permissions.model_fields)
        == set(PermissionCeiling.model_fields)
    )


# --- PermissionCeiling shape --------------------------------------------------------------


def test_ceiling_entries_reject_unknown_keys() -> None:
    with pytest.raises(ValidationError):
        PermissionCeiling.model_validate({"contents": "write", "extra_field": True})


# --- The unchecked cast's runtime backstop ------------------------------------------------


def test_an_unrecognized_level_in_the_default_profile_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_DEFAULT_PROFILE`'s cast is unchecked, so a rogue level (e.g. `admin`) can only ever
    arrive through it — and must be REFUSED by the rank lookups, never surface as a KeyError
    inside a security check."""
    from orchestrator.factorial import permission_policy

    rogue = {**permission_policy._DEFAULT_PROFILE, "contents": "admin"}
    monkeypatch.setattr(permission_policy, "_DEFAULT_PROFILE", rogue)
    problems = permission_policy.check_permissions(None, None)
    assert any("contents" in problem for problem in problems)
