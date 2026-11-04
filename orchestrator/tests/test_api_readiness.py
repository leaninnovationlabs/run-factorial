"""API tests for a project's readiness: the activation probes and the readiness route."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal
from urllib.parse import quote

import httpx
import pytest
import yaml
from fastapi import FastAPI
from githubkit.exception import RequestFailed
from githubkit.response import Response
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.api.deps import (
    GITHUB_NOT_CONNECTED,
    GITLAB_NOT_CONFIGURED,
    access_reader_dependency,
    config_branch_reader_dependency,
    config_source_dependency,
    label_reader_dependency,
    protection_reader_dependency,
    workflows_reader_dependency,
)
from orchestrator.api.readiness import secret_gap
from orchestrator.config import ProcessConfig
from orchestrator.db.models import (
    StepKind,
    StepRun,
    StepRunStatus,
    TicketSource,
    TicketSourceKind,
    TicketStatus,
)
from orchestrator.factorial import RepoConfigSource
from orchestrator.github import (
    BranchProtection,
    ConfigBranchRules,
    InstallationAccess,
)
from orchestrator.secrets import store
from tests.api_harness import (
    build_app,
    client_for,
    make_settings,
    probe_version_id,
    seed_project,
    seed_ticket,
)
from tests.api_probes import (
    MINI_PROCESS_YAML,
    MINI_SHA,
    MINI_TWO_AGENT_PROCESS_YAML,
    ProbeContents,
    app_access_reader,
    mini_config_files,
    probe_app,
    seed_probe_project,
)

pytestmark = [
    pytest.mark.usefixtures("master_key"),
    pytest.mark.asyncio(loop_scope="session"),
]


# -------------------------------------------------------------- activation readiness probes
#
# The secrets blocker and the label warning both derive from the latest process version, but the
# manifests live in the repo (an `agent` row is a pointer, not a snapshot), so the probes read
# GitHub best-effort. These tests stand in at the two deliberate seams `api.deps` exposes:
# `config_source_optional_dependency` (manifest reads) and `label_reader_dependency` (repo labels).


@pytest.mark.postgres
async def test_missing_declared_secret_blocks_activation(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    """A declared secret with no stored value is a blocker, and the 409 names the key —
    the same fact the project page shows, because both read `readiness.blockers` off one probe."""
    project = await seed_probe_project(async_session)
    app = probe_app(async_session, files=mini_config_files(["SERVICE_TOKEN", "GH_TOKEN"]))
    async with client_for(app) as client:
        detail = (await client.get(f"/api/projects/{project.id}", headers=auth)).json()
        assert any("SERVICE_TOKEN" in entry for entry in detail["blockers"])

        refused = await client.put(
            f"/api/projects/{project.id}/active", headers=auth, json={"active": True}
        )
        assert refused.status_code == 409
        assert "SERVICE_TOKEN" in refused.json()["detail"]
        # GH_TOKEN is declared too, but minted per run — it must not be named missing.
        assert "GH_TOKEN" not in refused.json()["detail"]


@pytest.mark.postgres
@pytest.mark.usefixtures("master_key")
async def test_stored_declared_secrets_clear_the_blocker(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    project = await seed_probe_project(async_session)
    app = probe_app(async_session, files=mini_config_files(["SERVICE_TOKEN"]))
    async with client_for(app) as client:
        stored = await client.put(
            f"/api/projects/{project.id}/secrets/SERVICE_TOKEN",
            headers=auth,
            json={"value": "sk-ant-test"},
        )
        assert stored.status_code == 200

        detail = (await client.get(f"/api/projects/{project.id}", headers=auth)).json()
        assert detail["blockers"] == []

        activated = await client.put(
            f"/api/projects/{project.id}/active", headers=auth, json={"active": True}
        )
        assert activated.status_code == 200


@pytest.mark.postgres
async def test_gh_token_declaration_alone_is_never_a_blocker(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    """The orchestrator mints GH_TOKEN per run; an empty store must still activate."""
    project = await seed_probe_project(async_session)
    app = probe_app(async_session, files=mini_config_files(["GH_TOKEN"]))
    async with client_for(app) as client:
        detail = (await client.get(f"/api/projects/{project.id}", headers=auth)).json()
        probes = (await client.get(f"/api/projects/{project.id}/readiness", headers=auth)).json()
        assert detail["blockers"] == []
        # No label reader was configured, so the label check says so instead of guessing.
        assert any("labels not checked" in entry for entry in probes["warnings"])

        activated = await client.put(
            f"/api/projects/{project.id}/active", headers=auth, json={"active": True}
        )
        assert activated.status_code == 200


@pytest.mark.postgres
@pytest.mark.parametrize(
    ("kind", "missing"),
    [
        (TicketSourceKind.GITLAB, GITLAB_NOT_CONFIGURED),
        (TicketSourceKind.GITHUB, GITHUB_NOT_CONNECTED),
    ],
)
async def test_secrets_not_checked_names_the_forge_the_project_reads(
    async_session: AsyncSession, kind: TicketSourceKind, missing: str
) -> None:
    project = await seed_project(async_session)
    async_session.add(
        TicketSource(project_id=project.id, kind=kind, external_ref=project.github_repo)
    )
    await async_session.flush()
    config = ProcessConfig.model_validate(yaml.safe_load(MINI_PROCESS_YAML))

    gap = await secret_gap(async_session, None, project, {config.name: config}, {})

    assert gap.notes == [f"Declared secrets not checked. {missing}"]


@pytest.mark.postgres
async def test_unreadable_manifests_warn_instead_of_blocking(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    """The degraded path: GitHub errors mid-probe. A blocker needs positive evidence, so the
    failure becomes a "not checked" warning and activation is not refused."""
    project = await seed_probe_project(async_session)
    app = probe_app(async_session, files={})  # every manifest read 404s
    async with client_for(app) as client:
        detail = (await client.get(f"/api/projects/{project.id}", headers=auth)).json()
        assert detail["blockers"] == []
        assert any("Declared secrets not checked" in entry for entry in detail["warnings"])

        activated = await client.put(
            f"/api/projects/{project.id}/active", headers=auth, json={"active": True}
        )
        assert activated.status_code == 200


@pytest.mark.postgres
async def test_the_config_explorer_lists_the_pinned_folder_with_kinds(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    """The listing the configuration section draws, scoped to the version's pinned commit."""
    project = await seed_probe_project(async_session)
    version_id = await probe_version_id(async_session, project)
    app = probe_app(async_session, files=mini_config_files(["SERVICE_TOKEN"]))
    async with client_for(app) as client:
        body = (
            await client.get(
                f"/api/projects/{project.id}/process-versions/{version_id}/files", headers=auth
            )
        ).json()

    assert body["config_dir"] == ".factorial"
    assert body["pinned_sha"] == MINI_SHA
    by_path = {f["path"]: f for f in body["files"]}
    assert by_path[".factorial/agents/mini-agent/agent.yaml"]["kind"] == "agent"
    assert by_path[".factorial/agents/mini-agent/system-prompt.md"]["kind"] == "prompt"


@pytest.mark.postgres
async def test_an_unusable_manifest_warns_on_its_own_node(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    """Where the retired `agents` readiness row's verdict lives now: on the file it is about.

    Without it, scaffolding just relocates the silent failure — a merged stub still activates
    green and still dies at the first spawn.
    """
    project = await seed_probe_project(async_session)
    version_id = await probe_version_id(async_session, project)
    files = mini_config_files(["SERVICE_TOKEN"])
    files[".factorial/agents/mini-agent/agent.yaml"] = "# comments only\n"
    app = probe_app(async_session, files=files)
    async with client_for(app) as client:
        body = (
            await client.get(
                f"/api/projects/{project.id}/process-versions/{version_id}/files", headers=auth
            )
        ).json()
        detail = (await client.get(f"/api/projects/{project.id}", headers=auth)).json()

    warned = {f["path"] for f in body["files"] if f["warning"]}
    assert warned == {".factorial/agents/mini-agent/agent.yaml"}
    # Still never a blocker: `readiness.blockers` reads only `core_items`, so no outage refuses
    # activation.
    assert not any("mini-agent" in blocker for blocker in detail["blockers"])


@pytest.mark.postgres
async def test_the_explorer_reads_one_file_at_the_pinned_commit(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    project = await seed_probe_project(async_session)
    version_id = await probe_version_id(async_session, project)
    app = probe_app(async_session, files=mini_config_files(["SERVICE_TOKEN"]))
    async with client_for(app) as client:
        response = await client.get(
            f"/api/projects/{project.id}/process-versions/{version_id}/files"
            "/.factorial/agents/mini-agent/system-prompt.md",
            headers=auth,
        )
    assert response.status_code == 200
    assert response.json()["content"] == "You are mini."


@pytest.mark.postgres
async def test_the_config_section_reads_each_named_agent_at_the_pinned_commit(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    """What a selected agent step shows: its manifest's facts, and for an agent with no files the
    same verdict the explorer hangs on its manifest, rather than a failed request."""
    project = await seed_probe_project(
        async_session,
        process_yaml=MINI_TWO_AGENT_PROCESS_YAML,
        agents=("mini-agent", "other-agent"),
    )
    version_id = await probe_version_id(async_session, project)
    app = probe_app(async_session, files=mini_config_files(["SERVICE_TOKEN"]))
    async with client_for(app) as client:
        response = await client.get(
            f"/api/projects/{project.id}/process-versions/{version_id}/agents", headers=auth
        )

    assert response.status_code == 200
    by_name = {agent["name"]: agent for agent in response.json()}
    mini = by_name["mini-agent"]
    assert (mini["model"], mini["max_turns"]) == ("claude-haiku-4-5-20251001", None)
    assert "max_duration" not in mini
    assert mini["secrets"] == ["SERVICE_TOKEN"]
    assert mini["warning"] is None
    assert by_name["other-agent"]["warning"]


@pytest.mark.postgres
async def test_the_explorer_refuses_a_path_outside_the_listing(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    """Containment is membership of the listing the route recomputes, never a prefix test —
    the same rule `scaffold.assemble` enforces on the write side, and for the same reason."""
    project = await seed_probe_project(async_session)
    app = probe_app(async_session, files=mini_config_files(["SERVICE_TOKEN"]))
    async with client_for(app) as client:
        for path in (
            ".factorial/../.github/workflows/ci.yml",
            ".github/workflows/ci.yml",
            ".factorial/agents/mini-agent/../../../secrets.env",
        ):
            response = await client.get(
                f"/api/projects/{project.id}/process-versions/1/files/{path}", headers=auth
            )
            assert response.status_code == 404, path


@pytest.mark.postgres
async def test_partial_manifest_failure_keeps_the_proven_blocker(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    """One unreadable manifest must not discard the evidence of the one that read: the probe
    degrades PER AGENT, so the resolved agent's missing key still blocks activation while the
    unreadable agent is named in a warning. (The world is real: `upsert_agent_rows` writes
    `repo_path` by convention without reading it, so a renamed agent folder 404s exactly
    like this.)"""
    project = await seed_probe_project(
        async_session,
        process_yaml=MINI_TWO_AGENT_PROCESS_YAML,
        agents=("mini-agent", "other-agent"),
    )
    # mini-agent's files exist and declare SERVICE_TOKEN; other-agent has no files at all.
    app = probe_app(async_session, files=mini_config_files(["SERVICE_TOKEN", "GH_TOKEN"]))
    async with client_for(app) as client:
        detail = (await client.get(f"/api/projects/{project.id}", headers=auth)).json()
        assert any("SERVICE_TOKEN" in entry for entry in detail["blockers"])
        assert any(
            "other-agent" in entry and "not checked" in entry for entry in detail["warnings"]
        )

        refused = await client.put(
            f"/api/projects/{project.id}/active", headers=auth, json={"active": True}
        )
        assert refused.status_code == 409
        assert "SERVICE_TOKEN" in refused.json()["detail"]


@pytest.mark.postgres
async def test_unpinned_agent_degrades_to_a_note_beside_the_proven_blocker(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    """The not-pinned branch of the per-agent degrade: an agent row with no
    `pinned_commit_sha` (registered, never synced) becomes a warning carrying the sync hint,
    while the pinned agent's provably-missing key still blocks — same per-agent contract as the
    unreadable-manifest case, different branch."""
    project = await seed_probe_project(
        async_session,
        process_yaml=MINI_TWO_AGENT_PROCESS_YAML,
        agents=("mini-agent", "other-agent"),
        unpinned=("other-agent",),
    )
    app = probe_app(async_session, files=mini_config_files(["SERVICE_TOKEN", "GH_TOKEN"]))
    async with client_for(app) as client:
        detail = (await client.get(f"/api/projects/{project.id}", headers=auth)).json()
        assert any("SERVICE_TOKEN" in entry for entry in detail["blockers"])
        assert any("other-agent" in entry and "not synced" in entry for entry in detail["warnings"])

        refused = await client.put(
            f"/api/projects/{project.id}/active", headers=auth, json={"active": True}
        )
        assert refused.status_code == 409


@pytest.mark.postgres
async def test_secret_store_failure_raises_on_both_endpoints(
    auth: dict[str, str], async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A DATABASE failure is not an outage to fail open on. The probe's fail-open covers GitHub
    only; a store read that fails raises out of BOTH endpoints identically (the deployed app's
    generic handler turns each into the same 500), so activation can never silently succeed with
    zero signal while the page 500s."""
    project = await seed_probe_project(async_session)
    app = probe_app(async_session, files=mini_config_files(["SERVICE_TOKEN"]))

    async def broken(session: AsyncSession, project_id: int) -> list[str]:
        raise RuntimeError("secret store unavailable")

    monkeypatch.setattr(store, "list_names", broken)
    async with client_for(app) as client:
        with pytest.raises(RuntimeError, match="secret store unavailable"):
            await client.get(f"/api/projects/{project.id}", headers=auth)
        with pytest.raises(RuntimeError, match="secret store unavailable"):
            await client.put(
                f"/api/projects/{project.id}/active", headers=auth, json={"active": True}
            )
    await async_session.refresh(project)
    assert project.active is False  # the failed activation changed nothing


@pytest.mark.postgres
async def test_label_check_is_advisory_only(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    """Missing labels warn — never block — because a missing label is not provably fatal
    (bootstrap step 6). The expected set is both families: named (`factorial:new` from
    entry.when) and derived (`factorial:plan` from the step, plus the two status labels)."""
    project = await seed_probe_project(async_session)
    app = probe_app(
        async_session,
        files=mini_config_files(["GH_TOKEN"]),
        labels=frozenset({"factorial:new", "factorial:plan"}),
    )
    async with client_for(app) as client:
        detail = (await client.get(f"/api/projects/{project.id}", headers=auth)).json()
        probes = (await client.get(f"/api/projects/{project.id}/readiness", headers=auth)).json()
        assert detail["blockers"] == []
        [warning] = [w for w in probes["warnings"] if w.startswith("Label(s)")]
        assert "factorial:blocked, factorial:done, factorial:exit" in warning

        activated = await client.put(
            f"/api/projects/{project.id}/active", headers=auth, json={"active": True}
        )
        assert activated.status_code == 200


@pytest.mark.postgres
async def test_label_check_degrades_on_api_failure(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    project = await seed_probe_project(async_session)
    app = probe_app(
        async_session,
        files=mini_config_files(["GH_TOKEN"]),
        labels=RuntimeError("github is down"),
    )
    async with client_for(app) as client:
        blockers = (await client.get(f"/api/projects/{project.id}", headers=auth)).json()
        response = await client.get(f"/api/projects/{project.id}/readiness", headers=auth)
        assert response.status_code == 200  # the probe degrades; it never takes down the read
        assert blockers["blockers"] == []
        [warning] = [w for w in response.json()["warnings"] if w.startswith("Ticket labels")]
        assert "Ticket labels not checked" in warning
        assert "github is down" in warning


@pytest.mark.postgres
async def test_readiness_reports_one_row_per_requirement(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    """The configuration page renders a row per requirement, so every requirement reports a row
    even when it passes. `blockers` says only what is wrong; a checklist has to say what is
    right, or a finished project shows an empty box instead of five ticks."""
    project = await seed_probe_project(async_session)
    app = probe_app(
        async_session,
        files=mini_config_files(["GH_TOKEN"]),
        labels=frozenset(
            {
                "factorial:new",
                "factorial:plan",
                "factorial:blocked",
                "factorial:done",
                "factorial:exit",
                "factorial:interrupted",
                "factorial:waiting",
                # The label that selects this project's one process. Expected like the rest, and
                # the only one nothing projects — a human puts it on an issue (design.md §2.10).
                "factorial:process:mini",
            }
        ),
    )
    async with client_for(app) as client:
        detail = (await client.get(f"/api/projects/{project.id}", headers=auth)).json()
        probes = (await client.get(f"/api/projects/{project.id}/readiness", headers=auth)).json()

    # Which payload a row rides on follows what its verdict costs: the read the page waits for
    # answers from the database, the repo-side rows from a live GitHub call.
    assert [row["key"] for row in detail["readiness"]] == [
        "ticket_source",
        "github_connection",
        "github_webhook",
        "process_config",
        "secrets",
        "runtime",
    ]
    assert [row["key"] for row in probes["readiness"]] == [
        "app_access",
        "config_branch",
        "config_branch_rules",
        "labels",
        "branch_protection",
    ]
    rows = {row["key"]: row for row in detail["readiness"] + probes["readiness"]}
    assert rows["ticket_source"]["state"] == "ok"
    assert rows["process_config"]["state"] == "ok"
    assert rows["secrets"]["state"] == "ok"
    assert rows["labels"]["state"] == "ok"


#: `MINI_PROCESS_YAML` with a CI-validation check step between `plan` and `done` — the shape
#: the `ci_workflows` readiness row exists for.
MINI_CI_PROCESS_YAML = MINI_PROCESS_YAML.replace(
    """  - name: plan
    kind: agent
    agent: mini-agent
    next: done""",
    """  - name: plan
    kind: agent
    agent: mini-agent
    next: validate_ci""",
).replace(
    "terminal:",
    """  - name: validate_ci
    kind: check
    next: done
    conditions: ci_green("spec/{ticket}-*")
terminal:""",
)

#: `MINI_PROCESS_YAML` with a merging gate that lets anyone with write access decide it.
MINI_GATE_PROCESS_YAML = MINI_PROCESS_YAML.replace(
    "    next: done", "    next: merge_gate", 1
).replace(
    "terminal:",
    """  - name: merge_gate
    kind: human_gate
    next: done
    gate:
      type: pr_approval
      pr: { head: "spec/{ticket}-*", base: main }
      approverPermission: write
      onApprove:
        merge: true
terminal:""",
)

CODEOWNERS_MAIN = BranchProtection(
    branch="main",
    protected=True,
    required_approvals=1,
    requires_codeowners=True,
    dismisses_stale=True,
    required_checks=frozenset({"verify"}),
)


def _protection_refused(status: int) -> RequestFailed:
    """GitHub's answer to the protection read, as githubkit raises it."""
    request = httpx.Request(
        "GET", "https://api.github.com/repos/acme/widgets/branches/main/protection"
    )
    raw = httpx.Response(
        status, json={"message": "Resource not accessible by integration"}, request=request
    )
    return RequestFailed(Response(raw, dict))


@pytest.mark.postgres
async def test_ci_workflows_row_is_absent_without_ci_green(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    """A process that never references `ci_green` gets no row at all — same drop-not-mute rule
    as `policy.branchProtection: ignored`: a row nobody will ever act on teaches a reader to
    skim the whole list."""
    project = await seed_probe_project(async_session)
    app = probe_app(async_session, files=mini_config_files(["GH_TOKEN"]), workflows=())
    async with client_for(app) as client:
        detail = (await client.get(f"/api/projects/{project.id}", headers=auth)).json()

    assert [r for r in detail["readiness"] if r["key"] == "ci_workflows"] == []


@pytest.mark.postgres
async def test_no_workflows_with_a_ci_green_stage_warns_and_never_blocks(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    """The softer touch for a no-CI repo: the runtime deliberately does not skip (zero check
    runs is never green, and "no CI" is indistinguishable from "CI did not trigger"), so this
    row says at configuration time what a ticket would otherwise say by timing out."""
    project = await seed_probe_project(async_session, process_yaml=MINI_CI_PROCESS_YAML)
    app = probe_app(async_session, files=mini_config_files(["GH_TOKEN"]), workflows=())
    async with client_for(app) as client:
        detail = (await client.get(f"/api/projects/{project.id}", headers=auth)).json()
        probes = (await client.get(f"/api/projects/{project.id}/readiness", headers=auth)).json()

    [row] = [r for r in probes["readiness"] if r["key"] == "ci_workflows"]
    assert row["state"] == "warn"
    assert "ci_green" in row["summary"]
    assert "timeout" in (row["detail"] or "")
    assert detail["blockers"] == []


@pytest.mark.postgres
async def test_present_workflows_with_a_ci_green_stage_report_ok(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    project = await seed_probe_project(async_session, process_yaml=MINI_CI_PROCESS_YAML)
    app = probe_app(async_session, files=mini_config_files(["GH_TOKEN"]), workflows=("verify.yml",))
    async with client_for(app) as client:
        probes = (await client.get(f"/api/projects/{project.id}/readiness", headers=auth)).json()

    [row] = [r for r in probes["readiness"] if r["key"] == "ci_workflows"]
    assert row["state"] == "ok"
    assert "verify.yml" in row["summary"]


@pytest.mark.postgres
async def test_unreadable_workflows_are_unknown_never_a_cross(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    """Same degrade contract as every GitHub-backed row: an unconfigured App (the un-overridden
    seam) resolves to "not checked", never to "no CI"."""
    project = await seed_probe_project(async_session, process_yaml=MINI_CI_PROCESS_YAML)
    app = probe_app(async_session, files=mini_config_files(["GH_TOKEN"]))
    async with client_for(app) as client:
        probes = (await client.get(f"/api/projects/{project.id}/readiness", headers=auth)).json()

    [row] = [r for r in probes["readiness"] if r["key"] == "ci_workflows"]
    assert row["state"] == "unknown"
    assert row["summary"] == "not checked"


@pytest.mark.postgres
async def test_unprotected_default_branch_warns_and_never_blocks(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    """Branch protection is what actually stops a bad merge, so the checklist reports it — but
    it cannot gate activation: the orchestrator has no way to prove protection is absent rather
    than unreadable, and a project may legitimately run against an unprotected repo."""
    project = await seed_probe_project(async_session)
    app = probe_app(
        async_session,
        files=mini_config_files(["GH_TOKEN"]),
        labels=frozenset(
            {
                "factorial:new",
                "factorial:plan",
                "factorial:blocked",
                "factorial:done",
                "factorial:exit",
            }
        ),
        protection=BranchProtection(branch="main", protected=False),
    )
    async with client_for(app) as client:
        detail = (await client.get(f"/api/projects/{project.id}", headers=auth)).json()
        probes = (await client.get(f"/api/projects/{project.id}/readiness", headers=auth)).json()

    [row] = [r for r in probes["readiness"] if r["key"] == "branch_protection"]
    assert row["state"] == "warn"
    assert "main" in row["summary"]
    assert detail["blockers"] == []


@pytest.mark.postgres
async def test_unreadable_protection_is_unknown_never_a_cross(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    """The App holds no `administration` permission by default, so the protection read 403s. That
    must resolve to "not checked", never to "unprotected": reporting a properly protected repo as
    unprotected would send someone to re-apply protection that is already there, and would teach
    the operator to ignore the row."""
    project = await seed_probe_project(async_session)
    app = probe_app(
        async_session,
        files=mini_config_files(["GH_TOKEN"]),
        protection=_protection_refused(403),
    )
    async with client_for(app) as client:
        response = await client.get(f"/api/projects/{project.id}/readiness", headers=auth)
        assert response.status_code == 200  # the probe degrades; it never takes down the read

    [row] = [r for r in response.json()["readiness"] if r["key"] == "branch_protection"]
    assert row["state"] == "unknown"
    assert "administration: read" in (row["detail"] or "")


@pytest.mark.postgres
async def test_a_forbidden_protection_read_names_the_permission_on_the_branch_row(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    """The row says what to grant; GitHub's raw 403 says nothing an operator can act on."""
    project = await seed_probe_project(async_session)
    app = probe_app(async_session, files=mini_config_files([]), protection=_protection_refused(403))
    async with client_for(app) as client:
        probes = (await client.get(f"/api/projects/{project.id}/readiness", headers=auth)).json()

    [row] = [r for r in probes["readiness"] if r["key"] == "branch_protection"]
    assert row["state"] == "unknown"
    assert "administration: read" in row["detail"]
    assert "403" not in row["detail"]
    assert "RequestFailed" not in row["detail"]


@pytest.mark.postgres
async def test_any_other_protection_failure_leaves_the_branch_row_without_the_raw_error(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    project = await seed_probe_project(async_session)
    app = probe_app(async_session, files=mini_config_files([]), protection=_protection_refused(502))
    async with client_for(app) as client:
        probes = (await client.get(f"/api/projects/{project.id}/readiness", headers=auth)).json()

    [row] = [r for r in probes["readiness"] if r["key"] == "branch_protection"]
    assert row["state"] == "unknown"
    assert "administration: read" not in row["detail"]
    assert "502" not in row["detail"]
    assert "RequestFailed" not in row["detail"]


@pytest.mark.postgres
async def test_protection_short_of_the_required_profile_names_every_gap(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    """A protection rule that exists but requires no approval is worse than none: it looks
    configured. The row names each part of CLAUDE.md's profile the branch does not meet."""
    project = await seed_probe_project(async_session)
    app = probe_app(
        async_session,
        files=mini_config_files(["GH_TOKEN"]),
        protection=BranchProtection(
            branch="main",
            protected=True,
            required_approvals=0,
            requires_codeowners=False,
            dismisses_stale=True,
            required_checks=frozenset({"verify"}),
        ),
    )
    async with client_for(app) as client:
        probes = (await client.get(f"/api/projects/{project.id}/readiness", headers=auth)).json()

    [row] = [r for r in probes["readiness"] if r["key"] == "branch_protection"]
    assert row["state"] == "warn"
    assert "no required approval" in row["detail"]
    assert "no CODEOWNERS review" in row["detail"]
    assert "stale approvals not dismissed" not in row["detail"]
    assert "verify" not in row["detail"]


@pytest.mark.postgres
async def test_creating_labels_touches_only_the_missing_ones(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    """The fix button is additive. A label the repo already defines is left exactly as it is —
    a repo may have recoloured or re-described one deliberately, and the orchestrator has no
    claim on those. Only names in the expected set that the repo lacks get created."""
    project = await seed_probe_project(async_session)
    created: list[tuple[str, str, str]] = []
    app = probe_app(
        async_session,
        files=mini_config_files(["GH_TOKEN"]),
        labels=frozenset({"factorial:new", "factorial:plan"}),
        label_writer=created,
    )
    async with client_for(app) as client:
        response = await client.post(f"/api/projects/{project.id}/labels", headers=auth)

    assert response.status_code == 200
    # `factorial:process:mini` among them: GitHub's picker offers only labels that exist, so a
    # lane whose selection label is missing cannot be chosen on an issue at all.
    expected = [
        "factorial:blocked",
        "factorial:done",
        "factorial:exit",
        "factorial:interrupted",
        "factorial:process:mini",
        "factorial:waiting",
    ]
    assert sorted(name for name, _, _ in created) == expected
    assert response.json()["created"] == expected


@pytest.mark.postgres
async def test_created_labels_carry_a_colour_derived_from_their_role(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    """Colour comes from what the label means in *this* config, not a hardcoded table: step
    labels are spelled from step names that no table can know in advance (bootstrap step 6,
    "derived from step names, and never written in config")."""
    project = await seed_probe_project(async_session)
    created: list[tuple[str, str, str]] = []
    app = probe_app(
        async_session,
        files=mini_config_files(["GH_TOKEN"]),
        labels=frozenset(),
        label_writer=created,
    )
    async with client_for(app) as client:
        await client.post(f"/api/projects/{project.id}/labels", headers=auth)

    colours = {name: colour for name, colour, _ in created}
    assert colours["factorial:new"] == "0e8a16"  # entry.when reads it — admit
    assert colours["factorial:blocked"] == "b60205"  # escalated
    assert colours["factorial:done"] == "5319e7"  # terminal
    assert colours["factorial:exit"] == "6e7781"  # sent away; neutral, like the pill
    assert colours["factorial:interrupted"] == "d93f0b"  # bootstrap step 6's colour
    assert colours["factorial:waiting"] == "d93f0b"  # bootstrap step 6's colour
    descriptions = {name: description for name, _, description in created}
    assert descriptions["factorial:waiting"] == (
        "Agent waiting for an answer. Remove this label or press Resume to continue."
    )
    assert descriptions["factorial:blocked"] == (
        "Stopped on a failure. Fix it, then remove this label to retry."
    )
    assert descriptions["factorial:done"] == "Factorial finished"
    assert descriptions["factorial:exit"] == "Left Factorial. Remove this label to bring it back."
    assert descriptions["factorial:plan"] == "plan agent running"
    assert colours["factorial:plan"] == "1d76db"  # an agent step is running


@pytest.mark.postgres
async def test_app_access_is_the_first_row_because_the_others_depend_on_it(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    """Reachability is the precondition for every repo-side probe. When it fails the label and
    protection rows degrade to "not checked", which explains nothing on its own — so this row
    goes first and carries the reason."""
    project = await seed_probe_project(async_session)
    app = probe_app(
        async_session,
        files=mini_config_files(["GH_TOKEN"]),
        access=InstallationAccess(
            account="leaninnovationlabs",
            account_is_org=True,
            installation_id="12345",
            selection="selected",
            permissions={"issues": "write", "contents": "write"},
            repo_reachable=True,
        ),
    )
    async with client_for(app) as client:
        probes = (await client.get(f"/api/projects/{project.id}/readiness", headers=auth)).json()

    assert probes["readiness"][0]["key"] == "app_access"
    assert probes["readiness"][0]["state"] == "ok"
    assert "leaninnovationlabs" in probes["readiness"][0]["summary"]


@pytest.mark.postgres
async def test_a_repo_outside_the_installation_is_an_error_that_gates_nothing(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    """The App can be installed and healthy while this particular repo is not among its selected
    repositories. That reads as a total outage everywhere else, so the row has to name it as the
    error it is — and point at the only page where it can be fixed — without refusing an
    activation over a live read."""
    project = await seed_probe_project(async_session)
    app = probe_app(
        async_session,
        files=mini_config_files(["GH_TOKEN"]),
        access=InstallationAccess(
            account="leaninnovationlabs",
            account_is_org=True,
            installation_id="12345",
            selection="selected",
            permissions={"issues": "write"},
            repo_reachable=False,
        ),
    )
    async with client_for(app) as client:
        detail = (await client.get(f"/api/projects/{project.id}", headers=auth)).json()
        probes = (await client.get(f"/api/projects/{project.id}/readiness", headers=auth)).json()

    [row] = [r for r in probes["readiness"] if r["key"] == "app_access"]
    assert row["state"] == "blocked"
    assert "selected repositories" in row["detail"]
    # One actionable sentence, not a paragraph about what else will look broken.
    assert len(row["detail"]) < 140
    assert detail["blockers"] == []


@pytest.mark.postgres
async def test_rows_behind_an_unreachable_repo_say_unread_rather_than_guess(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    """Every repo-side read fails when the App cannot see the repo, and each row used to turn
    that into its own wrong verdict — a config branch "missing" sent the operator to create a
    branch the App could not have created. The cause is said once, on `app_access`."""
    project = await seed_probe_project(async_session)
    app = probe_app(
        async_session,
        files=mini_config_files(["GH_TOKEN"]),
        access=InstallationAccess(
            account="leaninnovationlabs",
            account_is_org=True,
            installation_id="12345",
            selection="selected",
            permissions={"issues": "write"},
            repo_reachable=False,
        ),
    )
    async with client_for(app) as client:
        probes = (await client.get(f"/api/projects/{project.id}/readiness", headers=auth)).json()

    access, *rest = probes["readiness"]
    assert access["key"] == "app_access"
    assert rest
    for row in rest:
        assert (row["state"], row["detail"]) == ("unknown", None), row["key"]
        assert row["summary"] == f"not checked: Factorial cannot reach {project.github_repo}"
    assert probes["warnings"] == [access["detail"]]


async def test_the_settings_link_follows_the_account_type() -> None:
    """An org installation and a user installation live at different URLs, and the org one is the
    only page where "selected repositories" can be edited. Guessing wrong sends the operator to a
    404 on the exact page they were told to open."""
    org = InstallationAccess(
        account="leaninnovationlabs",
        account_is_org=True,
        installation_id="12345",
        selection="selected",
        permissions={},
        repo_reachable=True,
    )
    assert org.settings_url == (
        "https://github.com/organizations/leaninnovationlabs/settings/installations/12345"
    )
    user = replace(org, account="operator", account_is_org=False)
    assert user.settings_url == "https://github.com/settings/installations/12345"


@pytest.mark.postgres
async def test_the_labels_row_counts_what_is_missing_without_a_ratio(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    """ "3 of 5 missing" makes a reader do arithmetic to learn the only number that matters.
    The row states the work left; the denominator is noise."""
    project = await seed_probe_project(async_session)
    app = probe_app(
        async_session,
        files=mini_config_files(["GH_TOKEN"]),
        labels=frozenset({"factorial:new", "factorial:plan"}),
    )
    async with client_for(app) as client:
        probes = (await client.get(f"/api/projects/{project.id}/readiness", headers=auth)).json()

    [row] = [r for r in probes["readiness"] if r["key"] == "labels"]
    assert row["summary"] == "6 missing"


@pytest.mark.postgres
async def test_app_access_does_not_dump_the_permission_set(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    """The granted permissions were shown so an operator could confirm a queued permission
    request had landed. The branch_protection row already answers that — it flips off "not
    checked" the moment administration: read arrives — so the dump was a second, longer way to
    say the same thing next to the row that says it best."""
    project = await seed_probe_project(async_session)
    app = probe_app(
        async_session,
        files=mini_config_files(["GH_TOKEN"]),
        access=InstallationAccess(
            account="leaninnovationlabs",
            account_is_org=True,
            installation_id="12345",
            selection="selected",
            permissions={"issues": "write", "contents": "write", "metadata": "read"},
            repo_reachable=True,
        ),
    )
    async with client_for(app) as client:
        probes = (await client.get(f"/api/projects/{project.id}/readiness", headers=auth)).json()

    [row] = [r for r in probes["readiness"] if r["key"] == "app_access"]
    assert "issues" not in json.dumps(row)
    assert row["link"].endswith("/settings/installations/12345")


@pytest.mark.postgres
async def test_branch_protection_off_drops_the_row(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    """A repo that deliberately does not use protection should not carry a permanent amber row
    telling it so. `off` removes the row rather than muting it: a row nobody will ever act on
    teaches the reader to skim the list."""
    process = MINI_PROCESS_YAML.replace(
        "kind: Process", "kind: Process\npolicy:\n  branchProtection: ignored"
    )
    project = await seed_probe_project(async_session, process_yaml=process)
    app = probe_app(
        async_session,
        files=mini_config_files(["GH_TOKEN"]),
        protection=BranchProtection(branch="main", protected=False),
    )
    async with client_for(app) as client:
        detail = (await client.get(f"/api/projects/{project.id}", headers=auth)).json()

    assert [r["key"] for r in detail["readiness"] if r["key"] == "branch_protection"] == []


@pytest.mark.postgres
async def test_required_branch_protection_says_so_but_still_never_blocks(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    """`required` changes what the row says, never what activation does. The toggle lives in
    .factorial/, which agents write to, so enforcement here would be a control an agent could switch
    off — worse than none, because it looks like one."""
    process = MINI_PROCESS_YAML.replace(
        "kind: Process", "kind: Process\npolicy:\n  branchProtection: required"
    )
    project = await seed_probe_project(async_session, process_yaml=process)
    app = probe_app(
        async_session,
        files=mini_config_files(["GH_TOKEN"]),
        protection=BranchProtection(branch="main", protected=False),
    )
    async with client_for(app) as client:
        detail = (await client.get(f"/api/projects/{project.id}", headers=auth)).json()
        probes = (await client.get(f"/api/projects/{project.id}/readiness", headers=auth)).json()
        activated = await client.put(
            f"/api/projects/{project.id}/active", headers=auth, json={"active": True}
        )

    [row] = [r for r in probes["readiness"] if r["key"] == "branch_protection"]
    assert "this process requires it" in row["detail"]
    assert detail["blockers"] == []
    assert activated.status_code == 200


@pytest.mark.postgres
async def test_required_but_unreadable_protection_is_loud_about_the_gap(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    """The trap this row exists to close: set `required`, never grant `administration: read`,
    and protection can never be proven — so the process claims a guarantee nothing checks. The
    row has to name the missing permission rather than shrug at "not checked"."""
    process = MINI_PROCESS_YAML.replace(
        "kind: Process", "kind: Process\npolicy:\n  branchProtection: required"
    )
    project = await seed_probe_project(async_session, process_yaml=process)
    app = probe_app(
        async_session,
        files=mini_config_files(["GH_TOKEN"]),
        protection=_protection_refused(403),
    )
    async with client_for(app) as client:
        probes = (await client.get(f"/api/projects/{project.id}/readiness", headers=auth)).json()

    [row] = [r for r in probes["readiness"] if r["key"] == "branch_protection"]
    assert row["state"] == "warn"  # not `unknown`: the process asked for a guarantee
    assert "administration: read" in row["detail"]


@pytest.mark.postgres
async def test_a_writer_gate_into_a_code_owner_branch_warns_and_never_blocks(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    """A writer who is not a code owner can pass the gate, and GitHub then refuses the merge."""
    project = await seed_probe_project(async_session, process_yaml=MINI_GATE_PROCESS_YAML)
    app = probe_app(async_session, files=mini_config_files([]), protection=CODEOWNERS_MAIN)
    async with client_for(app) as client:
        detail = (await client.get(f"/api/projects/{project.id}", headers=auth)).json()
        probes = (await client.get(f"/api/projects/{project.id}/readiness", headers=auth)).json()

    assert "gate_approvers" in detail["probe_keys"]
    [row] = [r for r in probes["readiness"] if r["key"] == "gate_approvers"]
    assert row["state"] == "warn"
    assert "mini.merge_gate" in row["detail"]
    assert "code owner" in row["detail"]
    assert detail["blockers"] == []


@pytest.mark.postgres
async def test_a_writer_gate_without_a_code_owner_rule_is_ok(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    project = await seed_probe_project(async_session, process_yaml=MINI_GATE_PROCESS_YAML)
    app = probe_app(
        async_session,
        files=mini_config_files([]),
        protection=BranchProtection(branch="main", protected=True, required_approvals=1),
    )
    async with client_for(app) as client:
        probes = (await client.get(f"/api/projects/{project.id}/readiness", headers=auth)).json()

    [row] = [r for r in probes["readiness"] if r["key"] == "gate_approvers"]
    assert row["state"] == "ok"


@pytest.mark.postgres
async def test_the_gate_approvers_row_is_absent_without_a_writer_gate(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    project = await seed_probe_project(async_session)
    app = probe_app(async_session, files=mini_config_files([]), protection=CODEOWNERS_MAIN)
    async with client_for(app) as client:
        detail = (await client.get(f"/api/projects/{project.id}", headers=auth)).json()
        probes = (await client.get(f"/api/projects/{project.id}/readiness", headers=auth)).json()

    assert "gate_approvers" not in detail["probe_keys"]
    assert all(r["key"] != "gate_approvers" for r in probes["readiness"])


@pytest.mark.postgres
async def test_unreadable_protection_leaves_the_gate_approvers_row_unknown(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    project = await seed_probe_project(async_session, process_yaml=MINI_GATE_PROCESS_YAML)
    app = probe_app(
        async_session,
        files=mini_config_files([]),
        protection=RuntimeError("403 Resource not accessible by integration"),
    )
    async with client_for(app) as client:
        probes = (await client.get(f"/api/projects/{project.id}/readiness", headers=auth)).json()

    [row] = [r for r in probes["readiness"] if r["key"] == "gate_approvers"]
    assert row["state"] == "unknown"


@pytest.mark.postgres
async def test_a_forbidden_protection_read_names_the_permission_not_the_raw_error(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    """The App holds no `administration: read` by default, so this 403 is the usual case. The row
    says what to grant; GitHub's own text says nothing an operator can act on."""
    project = await seed_probe_project(async_session, process_yaml=MINI_GATE_PROCESS_YAML)
    app = probe_app(async_session, files=mini_config_files([]), protection=_protection_refused(403))
    async with client_for(app) as client:
        probes = (await client.get(f"/api/projects/{project.id}/readiness", headers=auth)).json()

    [row] = [r for r in probes["readiness"] if r["key"] == "gate_approvers"]
    assert row["state"] == "unknown"
    assert "administration: read" in row["detail"]
    assert "403" not in row["detail"]
    assert "RequestFailed" not in row["detail"]


@pytest.mark.postgres
async def test_any_other_protection_failure_leaves_the_gate_row_without_the_raw_error(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    project = await seed_probe_project(async_session, process_yaml=MINI_GATE_PROCESS_YAML)
    app = probe_app(async_session, files=mini_config_files([]), protection=_protection_refused(502))
    async with client_for(app) as client:
        probes = (await client.get(f"/api/projects/{project.id}/readiness", headers=auth)).json()

    [row] = [r for r in probes["readiness"] if r["key"] == "gate_approvers"]
    assert row["state"] == "unknown"
    assert "administration: read" not in row["detail"]
    assert "502" not in row["detail"]
    assert "RequestFailed" not in row["detail"]


@pytest.mark.postgres
async def test_both_protection_rows_answer_from_one_read(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    project = await seed_probe_project(async_session, process_yaml=MINI_GATE_PROCESS_YAML)
    calls: list[str] = []
    app = recording_probe_app(async_session, calls, files=mini_config_files([]))
    async with client_for(app) as client:
        body = (await client.get(f"/api/projects/{project.id}/readiness", headers=auth)).json()

    assert {"branch_protection", "gate_approvers"} <= {row["key"] for row in body["readiness"]}
    assert calls.count("protection") == 1


# ------------------------------------------------------------------- the readiness route
#
# The four rows above are the only ones whose verdict comes from a LIVE GitHub read, and the
# project page used to wait on all four before it could draw anything. They answer on their own
# route now, which the configuration pages ask for; `GET /projects/{id}` is what the page itself
# waits on and must stay free of them.


FULL_RULES = ConfigBranchRules(
    branch="factorial", head_sha=MINI_SHA, rule_types=frozenset({"deletion", "non_fast_forward"})
)


def recording_probe_app(
    session: AsyncSession, calls: list[str], *, files: dict[str, str]
) -> FastAPI:
    """`probe_app` with every live reader answering instantly and naming itself in `calls`."""
    app = probe_app(session, files=files)

    def reader(name: str, answer: Any) -> Any:
        async def read(repo: str) -> Any:
            calls.append(name)
            return answer

        return lambda: read

    app.dependency_overrides[access_reader_dependency] = app_access_reader(
        reader(
            "access",
            InstallationAccess(
                account="leaninnovationlabs",
                account_is_org=True,
                installation_id="12345",
                selection="selected",
                permissions={},
                repo_reachable=True,
            ),
        )()
    )
    app.dependency_overrides[label_reader_dependency] = reader("labels", frozenset())
    app.dependency_overrides[protection_reader_dependency] = reader(
        "protection", BranchProtection(branch="main", protected=False)
    )
    app.dependency_overrides[workflows_reader_dependency] = reader("workflows", ())
    app.dependency_overrides[config_branch_reader_dependency] = reader("config_branch", FULL_RULES)
    return app


@pytest.mark.postgres
async def test_the_project_payload_reads_no_live_github_state(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    """The request the page holds its spinner for asks GitHub nothing.

    Four serial round trips WERE that spinner. None of the rows they answer can reach `blocked`
    — that is deliberate, so an outage cannot refuse an activation — so none of them is needed
    to draw the header, the tabs or the ticket table.
    """
    project = await seed_probe_project(async_session)
    calls: list[str] = []
    app = recording_probe_app(async_session, calls, files=mini_config_files([]))
    async with client_for(app) as client:
        detail = (await client.get(f"/api/projects/{project.id}", headers=auth)).json()

    assert calls == []
    assert {row["key"] for row in detail["readiness"]} == {
        "ticket_source",
        "github_connection",
        "github_webhook",
        "process_config",
        "secrets",
        "runtime",
    }


@pytest.mark.postgres
async def test_the_readiness_route_carries_every_repo_side_row(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    """And carries them in the same order the old single payload did: `app_access` first,
    because the other three fail the same anonymous way without it."""
    project = await seed_probe_project(async_session, process_yaml=MINI_CI_PROCESS_YAML)
    calls: list[str] = []
    app = recording_probe_app(async_session, calls, files=mini_config_files([]))
    async with client_for(app) as client:
        body = (await client.get(f"/api/projects/{project.id}/readiness", headers=auth)).json()

    assert [row["key"] for row in body["readiness"]] == [
        "app_access",
        "config_branch",
        "config_branch_rules",
        "labels",
        "branch_protection",
        "ci_workflows",
    ]
    assert sorted(calls) == ["access", "config_branch", "labels", "protection", "workflows"]


@pytest.mark.postgres
async def test_a_probe_that_degrades_warns_on_the_readiness_route(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    """The "not checked" notes follow their rows off the project payload — a warning about a
    probe belongs with the probe, and the payload that no longer runs it cannot report on it."""
    project = await seed_probe_project(async_session)
    app = probe_app(async_session, files=mini_config_files([]), labels=RuntimeError("github down"))
    async with client_for(app) as client:
        detail = (await client.get(f"/api/projects/{project.id}", headers=auth)).json()
        body = (await client.get(f"/api/projects/{project.id}/readiness", headers=auth)).json()

    assert not any("Ticket labels not checked" in entry for entry in detail["warnings"])
    assert any("Ticket labels not checked" in entry for entry in body["warnings"])


@pytest.mark.postgres
async def test_the_probes_run_concurrently(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    """All five are in flight at once, so the checklist costs one round trip and not five.

    The barrier is the assertion: a reader only answers once all five have arrived, so a handler
    that awaits them in turn breaks it on the first timeout and every row degrades to a shrug.
    """
    project = await seed_probe_project(async_session, process_yaml=MINI_CI_PROCESS_YAML)
    barrier = asyncio.Barrier(5)

    def gated(answer: Any) -> Any:
        async def read(repo: str) -> Any:
            async with asyncio.timeout(0.5):
                await barrier.wait()
            return answer

        return lambda: read

    app = probe_app(async_session, files=mini_config_files([]))
    app.dependency_overrides[access_reader_dependency] = app_access_reader(
        gated(
            InstallationAccess(
                account="leaninnovationlabs",
                account_is_org=True,
                installation_id="12345",
                selection="selected",
                permissions={},
                repo_reachable=True,
            )
        )()
    )
    app.dependency_overrides[label_reader_dependency] = gated(frozenset())
    app.dependency_overrides[protection_reader_dependency] = gated(
        BranchProtection(branch="main", protected=False)
    )
    app.dependency_overrides[workflows_reader_dependency] = gated(("verify.yml",))
    app.dependency_overrides[config_branch_reader_dependency] = gated(FULL_RULES)
    async with client_for(app) as client:
        body = (await client.get(f"/api/projects/{project.id}/readiness", headers=auth)).json()

    assert [row["summary"] for row in body["readiness"] if row["summary"] == "not checked"] == []


@pytest.mark.postgres
@pytest.mark.parametrize(
    ("rules", "state", "summary", "has_remedy"),
    [
        ("missing", "warn", "missing", False),
        # The rules are the Git Repo page's row now; a short ruleset leaves this one ready.
        (replace(FULL_RULES, rule_types=frozenset()), "ok", "factorial · on a known commit", False),
        (FULL_RULES, "ok", "factorial · on a known commit", False),
        (replace(FULL_RULES, head_sha="e" * 40), "warn", "unknown head eeeeeee", False),
    ],
)
async def test_the_config_branch_row(
    auth: dict[str, str],
    async_session: AsyncSession,
    rules: ConfigBranchRules | Literal["missing"],
    state: str,
    summary: str,
    has_remedy: bool,
) -> None:
    """`missing` is the marker the page's Create button keys on. Only `MINI_SHA` was merged by a
    human, so any other head is one the builder refuses to build on until the branch is reset."""
    project = await seed_probe_project(async_session)
    app = probe_app(
        async_session, files=mini_config_files([]), config_branch=rules, merged=(MINI_SHA,)
    )
    async with client_for(app) as client:
        body = (await client.get(f"/api/projects/{project.id}/readiness", headers=auth)).json()

    row = next(r for r in body["readiness"] if r["key"] == "config_branch")
    assert (row["state"], row["summary"]) == (state, summary)
    assert (row["remedy"] is not None) == has_remedy
    assert row["checks"] == []
    if summary == "missing":
        assert "Process config page" in row["detail"]
        assert "created by hand is refused" in row["detail"]
    elif summary.startswith("unknown head"):
        assert "written by another Factorial install, or created by hand" in row["detail"]
        assert "Process config page" not in row["detail"]
        if not has_remedy:
            assert "Review and adopt the head" in row["detail"]


@pytest.mark.postgres
@pytest.mark.parametrize(
    ("rules", "state", "summary", "failing"),
    [
        ("missing", "unknown", "no `factorial` branch yet", None),
        (
            replace(FULL_RULES, rule_types=frozenset({"deletion"})),
            "warn",
            "factorial: force-push allowed",
            ["no_force_push"],
        ),
        (
            replace(FULL_RULES, rule_types=FULL_RULES.rule_types | {"pull_request"}),
            "warn",
            "factorial: pull_request stops the builder's commits",
            ["builder_can_commit"],
        ),
        (FULL_RULES, "ok", "factorial · no force-push or deletion", []),
        # An unknown head is the other row's problem: the rules still read as they are.
        (replace(FULL_RULES, head_sha="e" * 40), "ok", "factorial · no force-push or deletion", []),
    ],
)
async def test_the_config_branch_rules_row(
    auth: dict[str, str],
    async_session: AsyncSession,
    rules: ConfigBranchRules | Literal["missing"],
    state: str,
    summary: str,
    failing: list[str] | None,
) -> None:
    """One check per rule, each failing one with its own fix: a command for the rules a ruleset
    adds, a link for a rule that must come out of a ruleset someone else wrote."""
    project = await seed_probe_project(async_session)
    app = probe_app(
        async_session, files=mini_config_files([]), config_branch=rules, merged=(MINI_SHA,)
    )
    async with client_for(app) as client:
        body = (await client.get(f"/api/projects/{project.id}/readiness", headers=auth)).json()

    row = next(r for r in body["readiness"] if r["key"] == "config_branch_rules")
    assert (row["state"], row["summary"]) == (state, summary)
    if failing is None:
        assert row["checks"] == []
        return
    checks = {check["key"]: check for check in row["checks"]}
    assert list(checks) == ["no_force_push", "no_deletion", "builder_can_commit"]
    assert [key for key, check in checks.items() if not check["passed"]] == failing
    for check in checks.values():
        assert check["title"] and check["description"]
    for key in ("no_force_push", "no_deletion"):
        assert "refs/heads/factorial" in checks[key]["remedy"]
        assert project.github_repo in checks[key]["remedy"]
    blocker = checks["builder_can_commit"]
    assert blocker["remedy"] is None
    if not blocker["passed"]:
        assert "pull_request" in blocker["description"]
        assert blocker["link"] == f"https://github.com/{project.github_repo}/settings/rules"


@pytest.mark.postgres
@pytest.mark.parametrize(
    ("rules", "state", "failing"),
    [
        # GitLab's reading: a protected branch refuses both, an unprotected one neither.
        (replace(FULL_RULES, rule_types=frozenset()), "warn", ["no_force_push", "no_deletion"]),
        (FULL_RULES, "ok", []),
    ],
)
async def test_the_config_branch_rules_row_on_gitlab(
    auth: dict[str, str],
    async_session: AsyncSession,
    rules: ConfigBranchRules,
    state: str,
    failing: list[str],
) -> None:
    """On GitLab the fix is protecting the branch with glab, not a GitHub ruleset, and no check
    claims to have looked for a rule stopping the builder: GitLab's reading cannot see one."""
    project = await seed_probe_project(async_session)
    source = (
        await async_session.execute(
            select(TicketSource).where(TicketSource.project_id == project.id)
        )
    ).scalar_one()
    source.kind = TicketSourceKind.GITLAB
    await async_session.flush()
    app = probe_app(
        async_session, files=mini_config_files([]), config_branch=rules, merged=(MINI_SHA,)
    )
    async with client_for(app) as client:
        body = (await client.get(f"/api/projects/{project.id}/readiness", headers=auth)).json()

    row = next(r for r in body["readiness"] if r["key"] == "config_branch_rules")
    assert row["state"] == state
    checks = {check["key"]: check for check in row["checks"]}
    assert list(checks) == ["no_force_push", "no_deletion"]
    assert [key for key, check in checks.items() if not check["passed"]] == failing
    protect = (
        f"glab api --method POST projects/{quote(project.github_repo, safe='')}/protected_branches"
        " -f name=factorial -f push_access_level=30"
    )
    for check in checks.values():
        assert check["remedy"].startswith(protect)
        assert "ruleset" not in check["description"]
    if failing:
        assert row["remedy"].startswith(protect)
        assert "ruleset" not in row["detail"]


async def _protection_checks(
    auth: dict[str, str], async_session: AsyncSession, protection: BranchProtection
) -> dict[str, dict[str, Any]]:
    project = await seed_probe_project(async_session)
    app = probe_app(async_session, files=mini_config_files([]), protection=protection)
    async with client_for(app) as client:
        body = (await client.get(f"/api/projects/{project.id}/readiness", headers=auth)).json()
    [row] = [r for r in body["readiness"] if r["key"] == "branch_protection"]
    return {check["key"]: check for check in row["checks"]}


@pytest.mark.postgres
async def test_an_unprotected_branch_is_one_check_with_the_whole_profile(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    """Four more crosses would each say the same thing: there is no rule to have settings."""
    checks = await _protection_checks(
        auth, async_session, BranchProtection(branch="main", protected=False)
    )
    assert list(checks) == ["protected"]
    assert not checks["protected"]["passed"]
    assert "-X PUT" in checks["protected"]["remedy"]
    assert '"contexts":["verify"]' in checks["protected"]["remedy"]


@pytest.mark.postgres
async def test_each_protection_setting_is_fixed_on_its_own(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    """While reviews and status checks are enforced, each command changes its one setting and
    leaves the rest of the rule as someone made it."""
    checks = await _protection_checks(
        auth,
        async_session,
        BranchProtection(
            branch="trunk",
            protected=True,
            reviews_enabled=True,
            checks_enabled=True,
            required_approvals=1,
            required_checks=frozenset({"lint"}),
        ),
    )
    assert list(checks) == ["protected", "approval", "codeowners", "dismiss_stale", "verify"]
    assert [key for key, check in checks.items() if not check["passed"]] == [
        "codeowners",
        "dismiss_stale",
        "verify",
    ]
    reviews = "branches/trunk/protection/required_pull_request_reviews"
    assert checks["codeowners"]["remedy"].endswith(f"{reviews} -F require_code_owner_reviews=true")
    assert checks["dismiss_stale"]["remedy"].endswith(f"{reviews} -F dismiss_stale_reviews=true")
    assert "-X PATCH" in checks["codeowners"]["remedy"]
    assert checks["verify"]["remedy"].endswith(
        "branches/trunk/protection/required_status_checks/contexts -f 'contexts[]=verify'"
    )
    assert all("-X PUT" not in (check["remedy"] or "") for check in checks.values())


@pytest.mark.postgres
async def test_a_setting_whose_enforcement_is_off_falls_back_to_the_whole_rule(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    """GitHub switches review enforcement on only by replacing the rule. The replacement keeps
    the checks the branch already requires, and the check says it replaces the rule."""
    checks = await _protection_checks(
        auth,
        async_session,
        BranchProtection(
            branch="main",
            protected=True,
            reviews_enabled=False,
            checks_enabled=True,
            required_checks=frozenset({"lint"}),
        ),
    )
    for key in ("approval", "codeowners", "dismiss_stale"):
        assert "-X PUT" in checks[key]["remedy"]
        assert '"contexts":["lint","verify"]' in checks[key]["remedy"]
        assert "replaces the protection rule" in checks[key]["description"]
    assert "-X POST" in checks["verify"]["remedy"]
    assert "replaces" not in checks["verify"]["description"]


@pytest.mark.postgres
async def test_project_patch_is_partial(
    client: AsyncClient, auth: dict[str, str], async_session: AsyncSession
) -> None:
    project = await seed_project(async_session)
    original_name = project.name

    patched = await client.patch(
        f"/api/projects/{project.id}", headers=auth, json={"description": "now documented"}
    )
    assert patched.status_code == 200
    assert patched.json()["description"] == "now documented"
    assert patched.json()["name"] == original_name

    other = await seed_project(async_session)
    clash = await client.patch(
        f"/api/projects/{project.id}", headers=auth, json={"name": other.name}
    )
    assert clash.status_code == 409


@pytest.mark.postgres
async def test_project_patch_takes_the_repo_in_its_ticket_sources_shape(
    client: AsyncClient, auth: dict[str, str], async_session: AsyncSession
) -> None:
    """A nested path is a GitLab project's: a PATCH takes one only when the ticket source is on
    GitLab, and there from a URL on any host. On GitHub the repo stays owner/name."""
    github = await seed_project(async_session)
    refused = await client.patch(
        f"/api/projects/{github.id}", headers=auth, json={"github_repo": "harbor/finance/ledger"}
    )
    assert refused.status_code == 422
    assert "github_repo" in refused.json()["detail"]
    moved = await client.patch(
        f"/api/projects/{github.id}",
        headers=auth,
        json={"github_repo": "https://github.com/harbor/ledger.git"},
    )
    assert moved.json()["github_repo"] == "harbor/ledger"

    gitlab = await seed_project(async_session)
    async_session.add(
        TicketSource(
            project_id=gitlab.id, kind=TicketSourceKind.GITLAB, external_ref="harbor/finance/ledger"
        )
    )
    await async_session.flush()
    for sent in ("harbor/finance/ledger", "https://gitlab.example.com/harbor/finance/ledger.git"):
        patched = await client.patch(
            f"/api/projects/{gitlab.id}", headers=auth, json={"github_repo": sent}
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["github_repo"] == "harbor/finance/ledger"


@pytest.mark.postgres
async def test_ticket_list_itemises_the_board(
    client: AsyncClient, auth: dict[str, str], async_session: AsyncSession
) -> None:
    project = await seed_project(async_session)
    running = await seed_ticket(async_session, project, step="develop")
    blocked = await seed_ticket(async_session, project, step="spec", status=TicketStatus.BLOCKED)
    closed = await seed_ticket(async_session, project, step="develop", status=TicketStatus.DONE)
    closed.closed_at = datetime.now(UTC)
    async_session.add(
        StepRun(
            ticket_id=running.id,
            step_name="develop",
            kind=StepKind.AGENT,
            status=StepRunStatus.RUNNING,
        )
    )
    await async_session.flush()

    base = f"/api/projects/{project.id}/tickets"
    body = (await client.get(base, headers=auth)).json()
    # Newest first, and closed tickets are still listed — unlike the board.
    assert [t["id"] for t in body["tickets"]] == [closed.id, blocked.id, running.id]
    assert next(t for t in body["tickets"] if t["id"] == running.id)["attempts"] == 1
    assert next(t for t in body["tickets"] if t["id"] == blocked.id)["attempts"] == 0

    filtered = (await client.get(base, params={"status": "blocked"}, headers=auth)).json()
    assert [t["id"] for t in filtered["tickets"]] == [blocked.id]
    assert filtered["status"] == "blocked"

    open_only = (await client.get(base, params={"open_only": True}, headers=auth)).json()
    assert closed.id not in [t["id"] for t in open_only["tickets"]]

    typo = await client.get(base, params={"status": "runnning"}, headers=auth)
    assert typo.status_code == 422


@pytest.mark.postgres
async def test_ticket_rows_carry_what_each_ticket_spent(
    client: AsyncClient, auth: dict[str, str], async_session: AsyncSession
) -> None:
    """Per row, summed over that ticket's attempts — the same three figures `step_run` carries per
    attempt. A correlated subquery, so a ticket that has never run still appears, with zeroes."""
    project = await seed_project(async_session)
    spent = await seed_ticket(async_session, project, step="develop")
    admitted = await seed_ticket(async_session, project, step="spec")
    async_session.add_all(
        [
            StepRun(
                ticket_id=spent.id,
                step_name="spec",
                kind=StepKind.AGENT,
                attempt=1,
                status=StepRunStatus.FAILED,
                cost_usd=Decimal("0.30"),
                input_tokens=200,
                output_tokens=90,
            ),
            # The retry counts too: a ticket costs what every attempt at it cost, failures and all.
            StepRun(
                ticket_id=spent.id,
                step_name="spec",
                kind=StepKind.AGENT,
                attempt=2,
                status=StepRunStatus.SUCCEEDED,
                cost_usd=Decimal("0.64"),
                input_tokens=800,
                output_tokens=310,
            ),
        ]
    )
    await async_session.flush()

    rows = (await client.get(f"/api/projects/{project.id}/tickets", headers=auth)).json()["tickets"]
    by_id = {row["id"]: row for row in rows}
    assert by_id[spent.id]["input_tokens"] == 1000
    assert by_id[spent.id]["output_tokens"] == 400
    assert Decimal(by_id[spent.id]["cost_usd"]) == Decimal("0.94")
    assert by_id[spent.id]["cost_estimated"] is False
    assert by_id[admitted.id]["input_tokens"] == 0
    assert by_id[admitted.id]["output_tokens"] == 0
    assert Decimal(by_id[admitted.id]["cost_usd"]) == Decimal("0")
    assert by_id[admitted.id]["cost_estimated"] is False


@pytest.mark.postgres
async def test_every_ticket_read_carries_the_stored_title(
    client: AsyncClient, auth: dict[str, str], async_session: AsyncSession
) -> None:
    """The project table, the factory board and the ticket page read the same column, and all
    three have to survive a ticket that has never been observed — every row admitted before
    revision 0005, plus anything already `done` when it shipped. Those come back as null, and the
    reader falls back to the issue number."""
    project = await seed_project(async_session)
    named = await seed_ticket(async_session, project, title="Add a widget")
    unseen = await seed_ticket(async_session, project)
    await async_session.flush()

    listed = (await client.get(f"/api/projects/{project.id}/tickets", headers=auth)).json()
    titles = {t["id"]: t["title"] for t in listed["tickets"]}
    assert titles[named.id] == "Add a widget"
    assert titles[unseen.id] is None

    board = (await client.get("/api/orgs/default/overview", headers=auth)).json()
    on_board = {t["ticket_id"]: t["title"] for t in board["tickets"]}
    assert on_board[named.id] == "Add a widget"
    assert on_board[unseen.id] is None

    detail = (await client.get(f"/api/tickets/{named.id}", headers=auth)).json()
    assert detail["title"] == "Add a widget"
    assert (await client.get(f"/api/tickets/{unseen.id}", headers=auth)).json()["title"] is None


@pytest.mark.postgres
async def test_config_sync_is_503_without_a_github_connection(
    client: AsyncClient, auth: dict[str, str], async_session: AsyncSession
) -> None:
    """A project with no GitHub connection must get a named 503 rather than a githubkit failure
    surfacing as "internal error"."""
    project = await seed_project(async_session)
    response = await client.post(
        f"/api/projects/{project.id}/process-versions/sync", headers=auth, json={}
    )
    assert response.status_code == 503
    assert "no GitHub connection" in response.json()["detail"]


@pytest.mark.postgres
async def test_sync_warns_when_a_referenced_agent_does_not_resolve(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    """Sync pins agents by convention without reading a manifest, so a repo whose agents/ folder
    is missing or half-written has always synced clean and died at the first spawn. Still a 201 —
    the process did parse — but the gap is now named in the response."""
    project = await seed_project(async_session)
    files = mini_config_files(["SERVICE_TOKEN"]) | {
        ".factorial/process.yaml": MINI_PROCESS_YAML
    }
    del files[".factorial/agents/mini-agent/instructions.md"]  # a declared prompt that is not there

    app = build_app(make_settings(), async_session)
    source = RepoConfigSource(ProbeContents(files))
    app.dependency_overrides[config_source_dependency] = lambda: source
    async with client_for(app) as client:
        response = await client.post(
            f"/api/projects/{project.id}/process-versions/sync", headers=auth, json={}
        )
    assert response.status_code == 201
    (warning,) = response.json()["agent_warnings"]
    assert warning.startswith("mini-agent: ")


@pytest.mark.postgres
async def test_sync_reports_no_warnings_when_every_agent_resolves(
    auth: dict[str, str], async_session: AsyncSession
) -> None:
    project = await seed_project(async_session)
    files = mini_config_files(["SERVICE_TOKEN"]) | {
        ".factorial/process.yaml": MINI_PROCESS_YAML
    }

    app = build_app(make_settings(), async_session)
    source = RepoConfigSource(ProbeContents(files))
    app.dependency_overrides[config_source_dependency] = lambda: source
    async with client_for(app) as client:
        response = await client.post(
            f"/api/projects/{project.id}/process-versions/sync", headers=auth, json={}
        )
    assert response.status_code == 201
    assert response.json()["agent_warnings"] == []


@pytest.mark.postgres
@pytest.mark.postgres
@pytest.mark.usefixtures("master_key")
async def test_secret_endpoint_never_echoes_the_value(
    client: AsyncClient, auth: dict[str, str], async_session: AsyncSession
) -> None:
    project = await seed_project(async_session)
    value = "ghp_super_secret_value"

    response = await client.put(
        f"/api/projects/{project.id}/secrets/GH_TOKEN", headers=auth, json={"value": value}
    )
    assert response.status_code == 200
    assert response.json() == {"name": "GH_TOKEN"}
    assert value not in response.text

    listed = await client.get(f"/api/projects/{project.id}/secrets", headers=auth)
    assert listed.json() == {
        "names": ["GH_TOKEN"],
        "deliveries": [{"name": "GH_TOKEN", "mode": "env", "host": None, "header": None}],
    }
    assert value not in listed.text

    # Stored as ciphertext, never as plaintext (design.md §7.1).
    from orchestrator.db.models import Secret

    row = (
        await async_session.execute(
            select(Secret).where(Secret.project_id == project.id, Secret.name == "GH_TOKEN")
        )
    ).scalar_one()
    assert value.encode() not in row.value_ciphertext

    missing = await client.put(
        "/api/projects/999999999/secrets/GH_TOKEN", headers=auth, json={"value": "x"}
    )
    assert missing.status_code == 404


@pytest.mark.postgres
@pytest.mark.usefixtures("master_key")
async def test_a_project_secret_can_be_deleted(
    client: AsyncClient, auth: dict[str, str], async_session: AsyncSession
) -> None:
    """The one way a project-stored copy of a runtime-provided key leaves the store (decision
    36's migration and the page's Remove both use it). Absent is 404, like a runtime credential."""
    project = await seed_project(async_session)
    await client.put(
        f"/api/projects/{project.id}/secrets/OPENAI_API_KEY", headers=auth, json={"value": "x"}
    )

    path = f"/api/projects/{project.id}/secrets/OPENAI_API_KEY"
    removed = await client.delete(path, headers=auth)
    assert removed.status_code == 204
    assert (await client.get(f"/api/projects/{project.id}/secrets", headers=auth)).json() == {
        "names": [], "deliveries": []
    }
    again = await client.delete(f"/api/projects/{project.id}/secrets/OPENAI_API_KEY", headers=auth)
    assert again.status_code == 404
    missing = await client.delete("/api/projects/999999999/secrets/OPENAI_API_KEY", headers=auth)
    assert missing.status_code == 404
