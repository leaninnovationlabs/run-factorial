"""Contract tests — pure values, no subprocess, no network."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import SecretStr, ValidationError

from runtime import (
    REDACTED,
    AgentPrompts,
    AgentSkill,
    Artifact,
    ArtifactKind,
    ExpectedArtifact,
    IdleReason,
    McpServerSpec,
    McpTransport,
    MissingAttachment,
    RunAttachment,
    RunInput,
    RunResult,
    RunSpec,
    RunStatus,
    SessionState,
    SkillScope,
    SpawnError,
    SpawnReason,
    Workspace,
    unmet_expectations,
)
from runtime.contract import CONTROL_PLANE_PORT, PreviewServiceSpec

SECRET_VALUE = "ghs_thisMustNeverBeLogged"
SYSTEM_PROMPT = "You are the plan agent."
INSTRUCTIONS = "Push a branch and open one PR."
SKILL_CONTENT = "---\nname: speckit-specify\n---\n\n# Specify\n"


def make_spec(**overrides: object) -> RunSpec:
    base: dict[str, object] = {
        "agent": "plan-agent",
        "ticket": "42",
        "workspace": Workspace(repo="acme/widgets", base="main", branch="spec/42-{slug}"),
        "prompts": AgentPrompts(system=SYSTEM_PROMPT, instructions=INSTRUCTIONS),
        "skills": (
            AgentSkill(name="speckit-specify", content=SKILL_CONTENT, scope=SkillScope.LOCAL),
        ),
        "inputs": (RunInput(name="ticket_body", value="do the thing"),),
        "secrets": {"GH_TOKEN": SecretStr(SECRET_VALUE)},
        "model": "claude-haiku-4-5-20251001",
        "max_turns": 40,
    }
    return RunSpec.model_validate(base | overrides)


# ------------------------------------------------------------------ shape validation


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"agent": ""}, id="empty-agent"),
        pytest.param({"workspace": "acme/widgets"}, id="workspace-not-a-structure"),
        pytest.param({"model": ""}, id="empty-model"),
        pytest.param({"max_turns": 0}, id="non-positive-max-turns"),
        pytest.param({"inputs": ["ticket_body"]}, id="inputs-are-not-a-bag-of-strings"),
        pytest.param({"branch": "spec/42"}, id="unknown-field"),
        pytest.param({"prompts": {"system": ""}}, id="empty-system-prompt"),
        pytest.param({"prompts": "system-prompt.md"}, id="prompts-are-contents-not-a-path"),
        pytest.param({"skills": ["speckit-specify"]}, id="skills-are-not-a-bag-of-names"),
    ],
)
def test_run_spec_rejects_bad_shape(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        make_spec(**overrides)


def test_run_spec_is_frozen() -> None:
    spec = make_spec()
    with pytest.raises(ValidationError):
        spec.model = "claude-opus-4-6"  # pyright: ignore[reportAttributeAccessIssue]


def test_workspace_requires_all_three_fields() -> None:
    with pytest.raises(ValidationError):
        Workspace.model_validate({"repo": "acme/widgets", "base": "main"})


# ----------------------------------------------------------------------- workspace isolation
# No `path` field: host file paths do not belong in a vendor-neutral contract. What a
# substrate does to honour `isolation`/`hidden` (a worktree, a container mount, a blind clone) is
# its own business.


def test_workspace_defaults_to_shared_with_no_hidden_globs_and_a_minted_credential() -> None:
    workspace = Workspace(repo="acme/widgets", base="main", branch="spec/42-{slug}")
    assert workspace.isolation == "shared"
    assert workspace.hidden == ()
    assert workspace.credential == "minted"


def test_workspace_accepts_a_blind_isolation_request() -> None:
    workspace = Workspace(
        repo="acme/widgets",
        base="main",
        branch="spec/42-{slug}",
        isolation="blind",
        hidden=("acceptance/42/**", "refs/*/tests/42"),
        credential="none",
    )
    assert workspace.isolation == "blind"
    assert workspace.hidden == ("acceptance/42/**", "refs/*/tests/42")
    assert workspace.credential == "none"


def test_workspace_rejects_an_unknown_isolation_value() -> None:
    with pytest.raises(ValidationError):
        Workspace.model_validate(
            {
                "repo": "acme/widgets",
                "base": "main",
                "branch": "spec/42-{slug}",
                "isolation": "hidden",
            }
        )


def test_workspace_rejects_an_unknown_credential_value() -> None:
    with pytest.raises(ValidationError):
        Workspace.model_validate(
            {
                "repo": "acme/widgets",
                "base": "main",
                "branch": "spec/42-{slug}",
                "credential": "scoped",
            }
        )


def test_workspace_has_no_path_field() -> None:
    # A host file path is not a vendor-neutral concept. `extra="forbid"` (see
    # _Frozen) is what makes a stray `path:` in a manifest a validation error instead of a value
    # that silently does nothing.
    with pytest.raises(ValidationError):
        Workspace.model_validate(
            {"repo": "acme/widgets", "base": "main", "branch": "spec/42-{slug}", "path": "/tmp/x"}
        )


def test_workspace_is_still_frozen_with_the_new_fields() -> None:
    workspace = Workspace(repo="acme/widgets", base="main", branch="spec/42-{slug}")
    with pytest.raises(ValidationError):
        workspace.isolation = "blind"  # pyright: ignore[reportAttributeAccessIssue]


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        pytest.param({"public_ports": (3000,)}, "public_ports needs public", id="private"),
        pytest.param(
            {"public": True, "public_ports": (3000, 49983)},
            "port 49983 is reserved for the sandbox's control plane",
            id="control-plane",
        ),
    ],
)
def test_public_ports_are_a_public_runs_and_never_the_control_planes(
    overrides: dict[str, object], expected: str
) -> None:
    with pytest.raises(ValidationError, match=expected):
        make_spec(**overrides)
    assert make_spec(public=True, public_ports=(3000,)).public_ports == (3000,)


def test_a_preview_service_may_not_declare_the_sandbox_control_port() -> None:
    """The sandbox's firewall always admits it, and a contain load cuts every connection on a
    declared port: the control plane's own among them."""
    with pytest.raises(ValidationError, match="reserved for the sandbox's control plane"):
        PreviewServiceSpec(name="web", command="npm run dev", port=CONTROL_PLANE_PORT)


# ------------------------------------------------------------------ prompts and skills
# Contents, fetched by the orchestrator at the agent's pinned sha — so the sha pins behaviour
# instead of the container reading whatever branch it checked out.


def test_run_spec_requires_prompts() -> None:
    # An agent with no system prompt is not a runnable agent.
    with pytest.raises(ValidationError):
        RunSpec.model_validate(
            {
                "agent": "plan-agent",
                "ticket": "42",
                "workspace": Workspace(repo="acme/widgets", base="main", branch="spec/42-{slug}"),
                "model": "claude-haiku-4-5-20251001",
            }
        )


def test_empty_instructions_are_legitimate_but_an_empty_system_prompt_is_not() -> None:
    assert AgentPrompts(system=SYSTEM_PROMPT).instructions == ""
    with pytest.raises(ValidationError):
        AgentPrompts(system="")


def test_duplicate_skill_names_are_rejected() -> None:
    # A local and a shared skill colliding is a config error; keeping the last one silently
    # would change agent behaviour invisibly.
    with pytest.raises(ValidationError, match="duplicate skill name"):
        make_spec(
            skills=(
                AgentSkill(name="constitution", content="local", scope=SkillScope.LOCAL),
                AgentSkill(name="constitution", content="shared", scope=SkillScope.SHARED),
            )
        )


def test_same_content_under_distinct_names_is_fine() -> None:
    spec = make_spec(
        skills=(
            AgentSkill(name="speckit-specify", content=SKILL_CONTENT, scope=SkillScope.LOCAL),
            AgentSkill(name="project-constitution", content=SKILL_CONTENT, scope=SkillScope.SHARED),
        )
    )
    assert [skill.scope for skill in spec.skills] == [SkillScope.LOCAL, SkillScope.SHARED]


def test_skills_default_to_empty() -> None:
    assert make_spec(skills=()).skills == ()


# ------------------------------------------------------------------ secret containment
# design.md §7.1: values are injected at spawn and never logged. A RunSpec reaches logs, the
# event table, and exception payloads, so every rendering of it must be inert.


def test_secrets_never_appear_in_any_rendering() -> None:
    spec = make_spec()
    renderings = [
        repr(spec),
        str(spec),
        str(spec.model_dump()),
        str(spec.model_dump(mode="json")),
        spec.model_dump_json(),
        str(dict(spec.secrets)),
        f"{spec}",
    ]
    for rendering in renderings:
        assert SECRET_VALUE not in rendering, rendering


def test_secret_keys_survive_but_values_are_redacted() -> None:
    dumped = make_spec().model_dump()
    assert list(dumped["secrets"]) == ["GH_TOKEN"]
    assert dumped["secrets"]["GH_TOKEN"] != SECRET_VALUE


def test_resolved_secrets_is_the_only_way_out() -> None:
    assert make_spec().resolved_secrets() == {"GH_TOKEN": SECRET_VALUE}


def test_prompt_and_skill_text_dumps_in_the_clear_while_secrets_still_redact() -> None:
    """Both halves in one test on purpose: the risk is a refactor redacting the wrong field.

    Prompts and skills are untrusted repo text (design.md §7) but they are NOT credentials —
    hiding them would make an injected run unauditable. Secrets stay redacted regardless.
    """
    spec = make_spec()
    dumped = spec.model_dump()

    assert dumped["prompts"]["system"] == SYSTEM_PROMPT
    assert dumped["prompts"]["instructions"] == INSTRUCTIONS
    assert dumped["skills"][0]["content"] == SKILL_CONTENT
    assert dumped["secrets"]["GH_TOKEN"] == REDACTED

    as_json = spec.model_dump_json()
    assert SYSTEM_PROMPT in as_json
    assert SECRET_VALUE not in as_json


# ------------------------------------------------------------------ status


@pytest.mark.parametrize(
    ("status", "terminal"),
    [
        (RunStatus.QUEUED, False),
        (RunStatus.RUNNING, False),
        (RunStatus.IDLE, False),
        (RunStatus.SUCCEEDED, True),
        (RunStatus.FAILED, True),
        (RunStatus.TIMEOUT, True),
        (RunStatus.CANCELLED, True),
    ],
)
def test_is_terminal(status: RunStatus, terminal: bool) -> None:
    assert status.is_terminal is terminal


def test_status_vocabulary_is_exactly_the_design_s() -> None:
    """The guard is on the vocabulary matching design.md §3.2, not on its size — a state added
    here without adding it there is the drift this catches. `interrupted` was added to both when
    decision 26 separated a human stopping a run from a deadline ending one, and `idle` when
    decision 56 kept an interactive session alive between turns."""
    assert {s.value for s in RunStatus} == {
        "queued",
        "running",
        "idle",
        "succeeded",
        "failed",
        "timeout",
        "cancelled",
        "interrupted",
    }


_SINCE = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("fields", "valid"),
    [
        ({}, True),
        ({"idle_reason": IdleReason.TURN_END, "idle_since": _SINCE}, True),
        ({"idle_reason": IdleReason.QUESTION, "idle_since": _SINCE, "question": "Which?"}, True),
        ({"idle_reason": IdleReason.TURN_END}, False),
        ({"idle_since": _SINCE}, False),
        ({"question": "Which?"}, False),
        ({"idle_reason": IdleReason.TURN_END, "idle_since": _SINCE, "question": "Which?"}, False),
        ({"idle_reason": IdleReason.TURN_END, "idle_since": datetime(2026, 10, 1, 12)}, False),
    ],
)
def test_a_session_state_is_idle_whole_or_not_at_all(
    fields: dict[str, object], valid: bool
) -> None:
    """The worker times an idle spell from `idle_since` and quotes `question` to a human: half
    an idle state would park a session on no time, or post a question nobody asked."""
    if valid:
        SessionState.model_validate(fields)
        return
    with pytest.raises(ValidationError):
        SessionState.model_validate(fields)


# ------------------------------------------------------------------ result


def test_run_result_round_trips() -> None:
    result = RunResult(
        run_id="run-1",
        outcome=RunStatus.SUCCEEDED,
        summary="spec written",
        artifacts=(
            Artifact(kind=ArtifactKind.BRANCH, locator={"branch": "spec/42-auth"}),
            Artifact(
                kind=ArtifactKind.PR,
                locator={"number": "7"},
                url="https://github.com/acme/widgets/pull/7",
            ),
        ),
        cost_usd=Decimal("1.2345"),
        input_tokens=1000,
        cached_input_tokens=600,
        cache_write_input_tokens=50,
        output_tokens=250,
    )
    assert RunResult.model_validate(result.model_dump()) == result
    assert RunResult.model_validate_json(result.model_dump_json()) == result


def test_run_result_requires_a_terminal_outcome() -> None:
    with pytest.raises(ValidationError):
        RunResult(run_id="run-1", outcome=RunStatus.RUNNING)


def test_artifact_kind_gains_comment() -> None:
    # orchestrator/migrations/versions/0001_initial.py's ck_artifact_kind CHECK already lists
    # 'comment' — this teaches the runtime contract the vocabulary the DB already allows.
    artifact = Artifact(kind=ArtifactKind.COMMENT, locator={"id": "987"})
    assert Artifact.model_validate(artifact.model_dump()) == artifact
    assert ArtifactKind.COMMENT == "comment"


# --- mcp servers (design.md §7.5) ---------------------------------------------------------------


def make_server(**overrides: object) -> McpServerSpec:
    base: dict[str, object] = {
        "name": "product-docs",
        "transport": McpTransport.HTTP,
        "url": "https://mcp.internal.example.com/product-docs/mcp",
        "headers": {"Authorization": "Bearer {secrets.PRODUCT_DOCS_TOKEN}"},
        "tools_allow": ("search_docs",),
    }
    return McpServerSpec.model_validate(base | overrides)


def test_an_mcp_spec_carries_a_reference_and_not_a_credential() -> None:
    """The reason `McpServerSpec` needs no redaction: there is nothing in it to redact."""
    spec = make_spec(mcp=(make_server(),))
    dumped = spec.model_dump_json()
    assert "{secrets.PRODUCT_DOCS_TOKEN}" in dumped
    assert SECRET_VALUE not in dumped


def test_headers_render_from_the_injected_secrets() -> None:
    server = make_server()
    rendered = server.rendered_headers({"PRODUCT_DOCS_TOKEN": "docs-token"})
    assert rendered == {"Authorization": "Bearer docs-token"}
    # Rendering is not mutation: the spec still holds the reference afterwards.
    assert server.headers == {"Authorization": "Bearer {secrets.PRODUCT_DOCS_TOKEN}"}


def test_an_unrenderable_reference_fails_the_spawn() -> None:
    """Better a refused spawn than a request that looks authenticated and is not."""
    with pytest.raises(SpawnError) as exc:
        make_server().rendered_headers({"SOMETHING_ELSE": "x"})
    assert "PRODUCT_DOCS_TOKEN" in str(exc.value)


def test_stdio_env_renders_the_same_way() -> None:
    server = make_server(
        transport=McpTransport.STDIO,
        url=None,
        headers={},
        command="npx",
        args=("-y", "@acme/repo-index-mcp@1.4.2"),
        env={"REPO_INDEX_TOKEN": "{secrets.REPO_INDEX_TOKEN}"},
    )
    assert server.rendered_env({"REPO_INDEX_TOKEN": "t"}) == {"REPO_INDEX_TOKEN": "t"}


@pytest.mark.parametrize(
    "overrides",
    [
        {"transport": McpTransport.STDIO, "url": None, "command": None},  # stdio without a command
        {"transport": McpTransport.HTTP, "url": None},  # http without a url
        {"command": "npx"},  # http with a command
    ],
)
def test_a_transport_and_its_fields_must_agree(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        make_server(**overrides)


def test_two_servers_cannot_share_a_name() -> None:
    """One name is one tool prefix; two definitions would make the agent's tools depend on which
    the substrate happened to keep."""
    with pytest.raises(ValidationError):
        make_spec(mcp=(make_server(), make_server(url="https://elsewhere.example.com/mcp")))


# --- declared artifact expectations --------------------------------------------------------------


def test_a_spec_declares_no_expectations_by_default() -> None:
    """The default is what keeps every caller and adapter predating expectations valid: nothing
    declared, so every adapter keeps the probe it already had."""
    assert make_spec().expected_artifacts == ()


def test_expected_artifacts_round_trip() -> None:
    spec = make_spec(
        expected_artifacts=(
            ExpectedArtifact(kind=ArtifactKind.BRANCH, selector={"head_glob": "impl/42-*"}),
            ExpectedArtifact(kind=ArtifactKind.PR, selector={"head_glob": "impl/42-*"}),
            ExpectedArtifact(
                kind=ArtifactKind.COMMENT,
                selector={"marker": "## Evidence Pack", "where": "pr", "head_glob": "impl/42-*"},
            ),
        )
    )
    # The whole spec cannot round-trip — its secrets serialize redacted, deliberately — so this
    # asserts the new field survives both renderings intact.
    assert RunSpec.model_validate(spec.model_dump()).expected_artifacts == spec.expected_artifacts
    assert (
        RunSpec.model_validate_json(spec.model_dump_json()).expected_artifacts
        == spec.expected_artifacts
    )


def test_an_expectation_is_frozen() -> None:
    expectation = ExpectedArtifact(kind=ArtifactKind.BRANCH)
    with pytest.raises(ValidationError):
        expectation.kind = ArtifactKind.PR  # type: ignore[misc]


@pytest.mark.parametrize(
    "kind,selector",
    [
        pytest.param(ArtifactKind.BRANCH, {"glob": "impl/*"}, id="branch-unknown-key"),
        pytest.param(ArtifactKind.PR, {"base": "main"}, id="pr-base-is-not-vocabulary"),
        pytest.param(ArtifactKind.COMMENT, {"where": "issue"}, id="comment-without-a-marker"),
        pytest.param(ArtifactKind.COMMENT, {"marker": ""}, id="comment-with-an-empty-marker"),
        pytest.param(
            ArtifactKind.COMMENT, {"marker": "x", "where": "elsewhere"}, id="comment-bad-where"
        ),
        pytest.param(
            ArtifactKind.COMMENT,
            {"marker": "x", "head_glob": "impl/*"},
            id="issue-comment-with-a-head-glob",
        ),
        pytest.param(ArtifactKind.COMMIT, {"sha": "d34db33f"}, id="commit-has-no-vocabulary"),
        pytest.param(ArtifactKind.FILE, {"path": "spec.md"}, id="file-has-no-vocabulary"),
    ],
)
def test_the_selector_vocabulary_is_closed(kind: ArtifactKind, selector: dict[str, str]) -> None:
    """A key an adapter would silently ignore is refused where it is written, not discovered as
    a run that reported delivery on evidence nobody looked for (the `McpServerSpec` rule)."""
    with pytest.raises(ValidationError):
        ExpectedArtifact(kind=kind, selector=selector)


def test_an_empty_marker_is_refused_because_it_could_never_be_satisfied() -> None:
    """It identifies no comment, `satisfied_by` will not pretend it does, and the closure rule
    would then read the run as failed forever — with nothing the agent could do about it."""
    with pytest.raises(ValidationError, match="non-empty"):
        ExpectedArtifact(kind=ArtifactKind.COMMENT, selector={"marker": ""})


def test_a_kind_with_no_vocabulary_is_still_constructible_with_an_empty_selector() -> None:
    """The refusal for COMMIT/FILE belongs to the adapter, not to the contract: an adapter that
    grows a probe for one must be able to accept it without a contract change."""
    assert ExpectedArtifact(kind=ArtifactKind.COMMIT).selector == {}


def test_a_branch_expectation_matches_on_its_glob() -> None:
    expectation = ExpectedArtifact(kind=ArtifactKind.BRANCH, selector={"head_glob": "impl/42-*"})
    assert expectation.satisfied_by(
        Artifact(kind=ArtifactKind.BRANCH, locator={"branch": "impl/42-auth"})
    )
    assert not expectation.satisfied_by(
        Artifact(kind=ArtifactKind.BRANCH, locator={"branch": "spec/42-auth"})
    )
    # Kind is part of the match: a PR on the right branch is not the branch.
    assert not expectation.satisfied_by(
        Artifact(kind=ArtifactKind.PR, locator={"head": "impl/42-auth"})
    )


def test_an_expectation_with_no_glob_falls_back_to_the_runs_own_branch() -> None:
    """Only the adapter knows what the run was told to publish, so the fallback is passed in."""
    expectation = ExpectedArtifact(kind=ArtifactKind.BRANCH)
    artifact = Artifact(kind=ArtifactKind.BRANCH, locator={"branch": "spec/42-auth"})
    assert expectation.satisfied_by(artifact, fallback_glob="spec/42-*")
    assert not expectation.satisfied_by(artifact, fallback_glob="impl/42-*")
    # No glob at all matches nothing: an expectation that can never fail is not an expectation.
    assert not expectation.satisfied_by(artifact)


def test_a_pr_expectation_needs_the_head_ref_in_the_locator() -> None:
    """A PR number says nothing about which branch it heads, so a probe written for the injectable
    seam must carry the head — a bare number cannot answer a glob."""
    expectation = ExpectedArtifact(kind=ArtifactKind.PR, selector={"head_glob": "impl/42-*"})
    assert expectation.satisfied_by(
        Artifact(kind=ArtifactKind.PR, locator={"number": "7", "head": "impl/42-auth"})
    )
    assert not expectation.satisfied_by(Artifact(kind=ArtifactKind.PR, locator={"number": "7"}))


def test_a_comment_expectation_matches_the_marker_the_probe_recorded() -> None:
    expectation = ExpectedArtifact(
        kind=ArtifactKind.COMMENT, selector={"marker": "## Evidence Pack"}
    )
    assert expectation.satisfied_by(
        Artifact(kind=ArtifactKind.COMMENT, locator={"marker": "## Evidence Pack", "issue": "42"})
    )
    assert not expectation.satisfied_by(
        Artifact(kind=ArtifactKind.COMMENT, locator={"marker": "## Something Else"})
    )


def test_unmet_expectations_names_only_what_is_missing_in_declaration_order() -> None:
    expected = (
        ExpectedArtifact(kind=ArtifactKind.BRANCH, selector={"head_glob": "impl/42-*"}),
        ExpectedArtifact(kind=ArtifactKind.PR, selector={"head_glob": "impl/42-*"}),
        ExpectedArtifact(kind=ArtifactKind.COMMENT, selector={"marker": "Evidence Pack"}),
    )
    artifacts = (Artifact(kind=ArtifactKind.BRANCH, locator={"branch": "impl/42-auth"}),)
    assert unmet_expectations(expected, artifacts) == (
        'pr(head_glob="impl/42-*")',
        'comment(marker="Evidence Pack")',
    )
    assert unmet_expectations((), artifacts) == ()


def test_the_summary_is_the_line_a_failure_reason_quotes() -> None:
    """Spelled in the contract so a reason reads the same whichever substrate produced it."""
    artifact = ExpectedArtifact(
        kind=ArtifactKind.COMMENT, selector={"marker": "Evidence Pack", "where": "pr"}
    )
    assert artifact.summary == 'comment(marker="Evidence Pack", where="pr")'
    assert (
        artifact.display_summary("gitlab")
        == 'comment(marker="Evidence Pack", where="mr")'
    )
    assert (
        ExpectedArtifact(kind=ArtifactKind.PR, selector={"head_glob": "impl/42-*"}).display_summary(
            "gitlab"
        )
        == 'mr(head_glob="impl/42-*")'
    )


# --------------------------------------------------------------------------------- spawn reasons


def test_an_unclassified_spawn_error_still_reads_as_retryable() -> None:
    """The backward-compatibility guarantee the migration rests on. Every old raise site, and
    every adapter written against the old signature, constructs `SpawnError(msg)` with no reason —
    and must keep meaning exactly what it meant before: "nothing was launched; retry may help".
    Defaulting the other way would silently escalate every busy substrate as a misconfiguration.
    """
    error = SpawnError("busy: run abc123 still holds the one agent slot")
    assert error.reason is SpawnReason.INFRASTRUCTURE
    assert not error.reason.is_permanent
    assert str(error) == "busy: run abc123 still holds the one agent slot"


def test_a_classified_spawn_error_keeps_its_message_and_its_class() -> None:
    """`reason` is additive: the message a human reads is untouched, and every existing
    `except SpawnError` / `except AgentRuntimeError` still catches it."""
    error = SpawnError("model 'large' is a size alias", reason=SpawnReason.CONFIG)
    assert str(error) == "model 'large' is a size alias"
    assert error.reason is SpawnReason.CONFIG
    assert isinstance(error, SpawnError)


@pytest.mark.parametrize(
    ("reason", "permanent"),
    [
        (SpawnReason.CONFIG, True),
        (SpawnReason.CREDENTIAL, True),
        (SpawnReason.WORKSPACE, True),
        (SpawnReason.INFRASTRUCTURE, False),
    ],
)
def test_which_reasons_mean_a_retry_cannot_help(reason: SpawnReason, permanent: bool) -> None:
    """The one fact the orchestrator branches on (`worker.failures._permanent_spawn`), pinned here
    in the contract rather than inferred at the call site — moving a member across this line changes
    whether a real ticket escalates or spins, and that must never be a silent edit."""
    assert reason.is_permanent is permanent


def test_the_reason_vocabulary_is_closed() -> None:
    """Derived from the shipped adapters' actual refusal sites, not invented. A fifth member is a
    contract change: the orchestrator's permanent/retryable split, the conformance battery, and
    both adapters all read this set."""
    assert {reason.value for reason in SpawnReason} == {
        "config",
        "credential",
        "workspace",
        "infrastructure",
    }


# ------------------------------------------------------------------ attachments

IMAGE = b"\x89PNG\r\n\x1a\n" + b"contract-image"
IMAGE_ORIGIN = "https://github.com/user-attachments/assets/1b92ad15-13e5-4ffe-ac3a-b0c19487acfa"


def _attachment(**overrides: object) -> RunAttachment:
    base: dict[str, object] = {
        "name": "image-1",
        "media_type": "image/png",
        "content": IMAGE,
        "source": "ticket_body",
        "origin": IMAGE_ORIGIN,
        "sha256": hashlib.sha256(IMAGE).hexdigest(),
    }
    return RunAttachment.model_validate(base | overrides)


def test_an_attachment_never_serializes_its_bytes() -> None:
    """A `RunSpec` reaches logs, events and error payloads: a dump carries the image's size."""
    spec = make_spec(attachments=(_attachment(),))
    dumped = spec.model_dump()
    assert dumped["attachments"][0]["content"] == len(IMAGE)
    as_json = json.loads(spec.model_dump_json())
    assert as_json["attachments"][0]["content"] == len(IMAGE)
    assert "contract-image" not in repr(spec)
    assert "contract-image" not in spec.model_dump_json()


def test_an_attachment_whose_digest_does_not_match_is_refused() -> None:
    with pytest.raises(ValidationError, match="sha256"):
        _attachment(sha256="0" * 64)


def test_an_attachment_origin_never_carries_a_query_string() -> None:
    """The origin is what the prompt and the event log show: a signed `?jwt=` URL must not fit."""
    with pytest.raises(ValidationError):
        _attachment(origin="https://private-user-images.githubusercontent.com/1/2-x.png?jwt=abc")


def test_an_attachment_name_is_a_plain_file_stem() -> None:
    with pytest.raises(ValidationError):
        _attachment(name="../escape")


def test_an_attachment_is_named_with_its_media_types_extension() -> None:
    assert _attachment().filename == "image-1.png"
    assert _attachment(media_type="image/jpeg").filename == "image-1.jpg"


def test_two_attachments_may_not_share_a_name() -> None:
    with pytest.raises(ValidationError, match="duplicate attachment name"):
        make_spec(attachments=(_attachment(), _attachment()))


def test_a_missing_attachment_is_named_with_its_reason() -> None:
    missing = MissingAttachment(
        source="ticket_comments",
        origin="https://github.com/user-attachments/files/1/spec.pdf",
        reason="not an image",
    )
    assert make_spec(missing_attachments=(missing,)).missing_attachments == (missing,)


def test_park_reasons_are_exactly_the_designs() -> None:
    from runtime.contract import ParkReason

    assert {r.value for r in ParkReason} == {"user", "idle", "rollover", "ceiling", "exited"}


def test_a_result_says_why_an_interrupted_run_parked() -> None:
    from runtime.contract import ParkReason

    result = RunResult(run_id="r", outcome=RunStatus.INTERRUPTED, park_reason=ParkReason.EXITED)
    assert result.park_reason is ParkReason.EXITED
    assert RunResult(run_id="r", outcome=RunStatus.SUCCEEDED).park_reason is None


def test_every_park_reason_has_a_sentence_on_every_substrate() -> None:
    """`status()` looks a parked run's reason up: one missing raises on every poll of a run
    parked for it."""
    from runtime.adapters._cli import host_session
    from runtime.adapters.e2b import adapter as e2b
    from runtime.contract import ParkReason

    assert set(host_session._PARKED) == set(ParkReason)
    assert set(e2b._PARKED) == set(ParkReason)
