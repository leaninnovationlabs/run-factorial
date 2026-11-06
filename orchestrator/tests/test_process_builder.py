"""Stage 0 of the process builder: the lint layer and the read/validate routes.

Lint findings are warnings about configs that PARSE GREEN and then misbehave — the parser owns
errors, `lint_process` owns the silent hazards. Each test here mutates a known-good config in
exactly one way and asserts the one warning (and, for the clean config, none).
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.api.services.organizations import default_organization_id
from orchestrator.config import parse_process
from orchestrator.config.lint import lint_process
from orchestrator.db.models import ProcessVersion, Project
from orchestrator.factorial import RepoConfigSource
from orchestrator.github import ContentsNotFound
from orchestrator.gitlab.client import GitLabError
from tests.api_harness import authed_client_for, build_app, make_settings, seed_project
from tests.repo_source_fakes import SAMPLE_PROCESS_PATH, SHA, FakeContents, sample_files

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio(loop_scope="session")]


def config_yaml(
    *,
    defaults: str = (
        "defaults:\n"
        "  retry: {maxAttempts: 2, backoff: 10m}\n"
        "  onFailure:\n"
        '    escalate: {ticketLabel: "factorial:blocked"}\n'
    ),
    entry_when: str = (
        '    all:\n      - ticket_label("factorial:new")\n      - ticket_has_assignee()\n'
    ),
    branch: str = "spec/{ticket}-{slug}",
    base: str = "main",
    preconditions: str = "",
    postconditions: str = (
        "    postconditions:\n"
        "      all:\n"
        '        - branch_exists("spec/{ticket}-*")\n'
        '        - pr_open("spec/{ticket}-*", "main")\n'
    ),
    comment: str = "ADLC {step} attempt {attempt}",
) -> str:
    return (
        "specVersion: factorial/v1\n"
        "kind: Process\n"
        "name: t\n"
        "version: 1\n"
        "roles:\n"
        '  reviewers: ["{ticket_owner}"]\n'
        f"{defaults}"
        "entry:\n"
        "  when:\n"
        f"{entry_when}"
        "  step: plan\n"
        "steps:\n"
        "  - name: plan\n"
        "    kind: agent\n"
        "    agent: plan-agent\n"
        "    next: done\n"
        "    workspace:\n"
        '      repo: "{repo}"\n'
        f"      base: {base}\n"
        f'      branch: "{branch}"\n'
        f"{preconditions}"
        f"{postconditions}"
        "    onSuccess:\n"
        f'      comment: "{comment}"\n'
        "terminal:\n"
        "  step: done\n"
    )


def lint_yaml(text: str):
    return lint_process(parse_process(text, origin="test"))


def codes(text: str) -> set[str]:
    return {w.code for w in lint_yaml(text)}


# --- lint --------------------------------------------------------------------


def test_clean_config_lints_clean() -> None:
    assert lint_yaml(config_yaml()) == []


def test_missing_retry_is_flagged() -> None:
    text = config_yaml(
        defaults='defaults:\n  onFailure:\n    escalate: {ticketLabel: "factorial:blocked"}\n'
    )
    warnings = [w for w in lint_yaml(text) if w.code == "missing-retry"]
    assert len(warnings) == 1
    assert "plan" in warnings[0].msg


def test_missing_escalation_is_flagged() -> None:
    text = config_yaml(defaults="defaults:\n  retry: {maxAttempts: 2, backoff: 10m}\n")
    assert "missing-escalation" in codes(text)


def test_unknown_placeholder_is_flagged() -> None:
    text = config_yaml(comment="ADLC {tickett} failed")
    warnings = [w for w in lint_yaml(text) if w.code == "unknown-placeholder"]
    assert len(warnings) == 1
    assert "tickett" in warnings[0].msg


def test_unknown_role_is_flagged() -> None:
    text = config_yaml(comment="ping {roles.approvers}")
    warnings = [w for w in lint_yaml(text) if w.code == "unknown-role"]
    assert len(warnings) == 1
    assert "approvers" in warnings[0].msg


def _check_config_yaml(*, pr: str = '    pr: { head: "impl/{ticket}-*", base: "main" }\n',
                       rerun: str = "") -> str:
    """A check step feeding a gate — the CI-validation shape `kind: check` exists for."""
    return (
        "specVersion: factorial/v1\n"
        "kind: Process\n"
        "name: t\n"
        "version: 1\n"
        "defaults:\n"
        "  retry: {maxAttempts: 2, backoff: 10m}\n"
        "  onFailure:\n"
        '    escalate: {ticketLabel: "factorial:blocked"}\n'
        "entry:\n"
        '  when: ticket_label("factorial:new")\n'
        "  step: validate_ci\n"
        "steps:\n"
        "  - name: validate_ci\n"
        "    kind: check\n"
        "    next: review\n"
        "    conditions:\n"
        "      all:\n"
        '        - ci_green("impl/{ticket}-*")\n'
        f"{pr}"
        "    onSuccess:\n"
        '      comment: "CI green: {pr_url}"\n'
        "  - name: review\n"
        "    kind: human_gate\n"
        "    next: done\n"
        "    gate:\n"
        "      type: pr_approval\n"
        '      pr: { head: "impl/{ticket}-*", base: "main" }\n'
        "      onApprove: { merge: true }\n"
        f"{rerun}"
        "terminal:\n"
        "  step: done\n"
    )


def test_clean_check_config_lints_clean() -> None:
    assert lint_yaml(_check_config_yaml()) == []


def test_gate_rerun_into_a_check_step_is_flagged() -> None:
    """Rework into a check livelocks: the fresh attempt re-evaluates the same observed state
    and completes immediately, so the standing changes-requested review re-rejects on the same
    tick — forever, automatically."""
    text = _check_config_yaml(
        rerun='      onReject: { rerunStep: validate_ci, ticketLabel: "factorial:rework" }\n'
    )
    warnings = [w for w in lint_yaml(text) if w.code == "check-rerun"]
    assert len(warnings) == 1
    assert "validate_ci" in warnings[0].msg


def test_pr_url_in_a_check_outcome_without_pr_is_flagged() -> None:
    """`{pr_url}` in a check outcome resolves only through the step's own `pr:` matcher; with
    no matcher it stays literal in the projected comment, and no unknown-placeholder warning
    fires because `pr_url` is a known name."""
    text = _check_config_yaml(pr="")
    warnings = [w for w in lint_yaml(text) if w.code == "pr-url-without-pr"]
    assert len(warnings) == 1


def test_slug_in_entry_or_base_is_flagged() -> None:
    in_entry = config_yaml(entry_when='    all:\n      - ticket_label("x-{slug}")\n')
    assert "slug-misplaced" in codes(in_entry)
    in_base = config_yaml(base='"rel/{slug}"')
    assert "slug-misplaced" in codes(in_base)


def test_globbed_ticket_label_is_flagged() -> None:
    text = config_yaml(entry_when='    all:\n      - ticket_label("factorial:*")\n')
    assert "globbed-ticket-label" in codes(text)


def test_self_guard_is_flagged() -> None:
    text = config_yaml(
        preconditions=(
            "    preconditions:\n"
            "      not:\n"
            '        branch_exists("spec/{ticket}-*")\n'
        )
    )
    warnings = [w for w in lint_yaml(text) if w.code == "self-guard"]
    assert len(warnings) == 1


def test_branch_prefix_divergence_is_flagged() -> None:
    text = config_yaml(
        postconditions=(
            "    postconditions:\n"
            "      all:\n"
            '        - branch_exists("sepc/{ticket}-*")\n'
        )
    )
    assert "branch-prefix-divergence" in codes(text)


def test_delivery_only_under_any_is_flagged() -> None:
    text = config_yaml(
        postconditions=(
            "    postconditions:\n"
            "      any:\n"
            '        - branch_exists("spec/{ticket}-*")\n'
            '        - pr_open("spec/{ticket}-*", "main")\n'
        )
    )
    assert "delivery-not-conjunctive" in codes(text)


def test_delivery_conjunctive_at_root_is_clean() -> None:
    text = config_yaml(
        postconditions='    postconditions: branch_exists("spec/{ticket}-*")\n'
    )
    assert "delivery-not-conjunctive" not in codes(text)


def _with_preview(block: str) -> str:
    return config_yaml().replace("    onSuccess:\n", f"    preview:\n{block}    onSuccess:\n", 1)


def test_a_retired_page_is_flagged_with_where_the_summary_goes_now() -> None:
    text = _with_preview('      page: {branch: "pages/{ticket}-{slug}", path: "summary.html"}\n')

    warnings = [w for w in lint_yaml(text) if w.code == "retired-page"]

    assert [w.loc for w in warnings] == ["steps[plan].preview.page"]
    assert "preview.files" in warnings[0].msg


def test_a_summary_declared_as_a_file_lints_clean() -> None:
    text = _with_preview(
        '      files:\n        - {path: "specs/{ticket}-{slug}/summary.html", label: Summary}\n'
    )

    assert lint_yaml(text) == []


# --- the routes --------------------------------------------------------------



async def test_vocabulary_serves_the_closed_registries() -> None:
    async with authed_client_for(build_app(make_settings(), None)) as client:
        response = await client.get("/api/vocabulary")
    assert response.status_code == 200
    body = response.json()
    by_name = {p["name"]: p for p in body["predicates"]}
    assert by_name["pr_approved"] == {"name": "pr_approved", "min_args": 2, "max_args": 3}
    assert len(by_name) == 11
    assert body["actions"] == ["fix_the_contract"]
    assert "slug" in body["placeholders"]
    assert "ticket_body" in body["input_sources"]
    assert body["compositions"] == ["all", "any", "not"]


async def test_validate_returns_warnings_for_a_valid_config(async_session: AsyncSession) -> None:
    project = Project(
        organization_id=await default_organization_id(async_session), name="p", github_repo="o/r"
    )
    async_session.add(project)
    await async_session.flush()
    text = config_yaml(defaults="defaults:\n  retry: {maxAttempts: 2, backoff: 10m}\n")
    async with authed_client_for(build_app(make_settings(), async_session)) as client:
        response = await client.post(
            f"/api/projects/{project.id}/process-versions/validate", json={"content": text}
        )
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is True
    assert body["errors"] == []
    assert "missing-escalation" in {w["code"] for w in body["warnings"]}
    # The parsed echo: what previews draw from, so they render the server's read of the text.
    assert body["content"]["name"] == "t"
    assert [s["name"] for s in body["content"]["steps"]] == ["plan"]


async def test_validate_reports_parse_errors(async_session: AsyncSession) -> None:
    project = Project(
        organization_id=await default_organization_id(async_session), name="p", github_repo="o/r"
    )
    async_session.add(project)
    await async_session.flush()
    async with authed_client_for(build_app(make_settings(), async_session)) as client:
        response = await client.post(
            f"/api/projects/{project.id}/process-versions/validate",
            json={"content": "specVersion: adlc/v1\nkind: Process\n"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
    assert body["errors"] and body["warnings"] == []
    assert body["content"] is None


async def test_snapshot_content_get(async_session: AsyncSession) -> None:
    project = Project(
        organization_id=await default_organization_id(async_session), name="p", github_repo="o/r"
    )
    async_session.add(project)
    await async_session.flush()
    config = parse_process(config_yaml(), origin="test")
    row = ProcessVersion(
        project_id=project.id,
        source_repo=project.github_repo,
        process_key=config.name,
        content=config.model_dump(mode="json", by_alias=True),
        source_commit_sha="c" * 40,
    )
    async_session.add(row)
    await async_session.flush()
    async with authed_client_for(build_app(make_settings(), async_session)) as client:
        # Keyed on the row id, not on `version`: two lanes may both be at v1.
        response = await client.get(f"/api/projects/{project.id}/process-versions/{row.id}")
        missing = await client.get(f"/api/projects/{project.id}/process-versions/{row.id + 9000}")
    assert response.status_code == 200
    body = response.json()
    assert body["source_commit_sha"] == "c" * 40
    assert body["content"]["name"] == "t"
    assert missing.status_code == 404


# --- raw version content and the label listing -------------------------------


async def test_version_raw_falls_back_to_the_snapshot(async_session: AsyncSession) -> None:
    """No pin or no App: the stored snapshot is re-emitted as YAML (comments lost, but honest)."""
    project = Project(
        organization_id=await default_organization_id(async_session), name="p", github_repo="o/r"
    )
    async_session.add(project)
    await async_session.flush()
    config = parse_process(config_yaml(), origin="test")
    row = ProcessVersion(
        project_id=project.id,
        source_repo=project.github_repo,
        process_key=config.name,
        content=config.model_dump(mode="json", by_alias=True),
        source_commit_sha="c" * 40,
    )
    async_session.add(row)
    await async_session.flush()
    async with authed_client_for(build_app(make_settings(), async_session)) as client:
        response = await client.get(f"/api/projects/{project.id}/process-versions/{row.id}/raw")
        missing = await client.get(
            f"/api/projects/{project.id}/process-versions/{row.id + 9000}/raw"
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == "snapshot"
    assert parse_process(body["content"], origin="roundtrip").name == "t"
    assert missing.status_code == 404


async def test_version_raw_reads_the_repo_at_the_pin(async_session: AsyncSession) -> None:
    from orchestrator.api.deps import config_source_optional_dependency

    class StubSource:
        async def process_path_at(self, repo: str, key: str, sha: str) -> str:
            assert (repo, key, sha) == ("o/r", "t", "a" * 40)
            return ".factorial/processes/process-t.yaml"

        async def read_process_text(self, repo: str, path: str, sha: str) -> str:
            assert (repo, path, sha) == ("o/r", ".factorial/processes/process-t.yaml", "a" * 40)
            return "# hand-written comments survive\n" + config_yaml()

    project = Project(
        organization_id=await default_organization_id(async_session), name="p", github_repo="o/r"
    )
    async_session.add(project)
    await async_session.flush()
    config = parse_process(config_yaml(), origin="test")
    row = ProcessVersion(
        project_id=project.id,
        source_repo=project.github_repo,
        process_key=config.name,
        source_commit_sha="a" * 40,
        content=config.model_dump(mode="json", by_alias=True),
    )
    async_session.add(row)
    await async_session.flush()
    app = build_app(make_settings(), async_session)
    app.dependency_overrides[config_source_optional_dependency] = lambda: StubSource()
    async with authed_client_for(app) as client:
        response = await client.get(f"/api/projects/{project.id}/process-versions/{row.id}/raw")
    body = response.json()
    assert body["source"] == "repo"
    assert body["content"].startswith("# hand-written comments survive")


async def test_label_listing_names_every_expected_label(async_session: AsyncSession) -> None:
    from orchestrator.api.deps import label_reader_dependency

    project = Project(
        organization_id=await default_organization_id(async_session), name="p", github_repo="o/r"
    )
    async_session.add(project)
    await async_session.flush()
    config = parse_process(config_yaml(), origin="test")
    async_session.add(
        ProcessVersion(
            project_id=project.id,
            source_repo=project.github_repo,
            process_key=config.name,
            content=config.model_dump(mode="json", by_alias=True),
            source_commit_sha="c" * 40,
        )
    )
    await async_session.flush()

    app = build_app(make_settings(), async_session)
    async with authed_client_for(app) as client:
        unchecked = await client.get(f"/api/projects/{project.id}/labels")
    assert unchecked.status_code == 200
    rows = {label["name"]: label for label in unchecked.json()["labels"]}
    assert rows["factorial:new"]["description"] == "Admit this issue to Factorial"
    assert rows["factorial:new"]["exists"] is None  # no App configured: expected, not verified
    assert all(label["color"] for label in rows.values())

    async def read(repo: str) -> set[str]:
        return {"factorial:new"}

    app.dependency_overrides[label_reader_dependency] = lambda: read
    async with authed_client_for(app) as client:
        checked = await client.get(f"/api/projects/{project.id}/labels")
    rows = {label["name"]: label for label in checked.json()["labels"]}
    assert rows["factorial:new"]["exists"] is True
    assert any(label["exists"] is False for label in rows.values())


async def test_label_listing_carries_the_label_that_selects_each_process(
    async_session: AsyncSession,
) -> None:
    """Nothing PROJECTS `factorial:process:<key>` — a human puts it on an issue and the loop
    reads it. It still has to exist in the repo, because GitHub's label picker offers only
    labels that do, so a lane whose selection label is missing cannot be chosen at all.
    """
    project = Project(
        organization_id=await default_organization_id(async_session), name="p", github_repo="o/r"
    )
    async_session.add(project)
    await async_session.flush()
    for key in ("standard", "hotfix"):
        config = parse_process(config_yaml().replace("name: t", f"name: {key}"), origin="test")
        async_session.add(
            ProcessVersion(
                project_id=project.id,
                source_repo=project.github_repo,
                process_key=config.name,
                content=config.model_dump(mode="json", by_alias=True),
                source_commit_sha="c" * 40,
            )
        )
    await async_session.flush()

    async with authed_client_for(build_app(make_settings(), async_session)) as client:
        rows = {
            label["name"]: label
            for label in (await client.get(f"/api/projects/{project.id}/labels")).json()["labels"]
        }
    assert rows["factorial:process:standard"]["description"] == (
        "Run this issue through the standard process"
    )
    assert rows["factorial:process:hotfix"]["color"] == "d4c5f9"


async def test_label_listing_degrades_when_github_fails(async_session: AsyncSession) -> None:
    """The reader failing is a GitHub problem, not a config problem — same degrade contract the
    readiness row honors: expected labels with exists unknown, never a 500."""
    from orchestrator.api.deps import label_reader_dependency

    project = Project(
        organization_id=await default_organization_id(async_session), name="p", github_repo="o/r"
    )
    async_session.add(project)
    await async_session.flush()
    config = parse_process(config_yaml(), origin="test")
    async_session.add(
        ProcessVersion(
            project_id=project.id,
            source_repo=project.github_repo,
            process_key=config.name,
            content=config.model_dump(mode="json", by_alias=True),
            source_commit_sha="c" * 40,
        )
    )
    await async_session.flush()

    async def explode(repo: str) -> set[str]:
        raise RuntimeError("rate limited")

    app = build_app(make_settings(), async_session)
    app.dependency_overrides[label_reader_dependency] = lambda: explode
    async with authed_client_for(app) as client:
        response = await client.get(f"/api/projects/{project.id}/labels")
    assert response.status_code == 200
    assert all(label["exists"] is None for label in response.json()["labels"])


async def test_version_raw_fails_loud_when_the_pinned_read_breaks(
    async_session: AsyncSession,
) -> None:
    """A pinned version whose repo read fails must NOT silently degrade to the comment-stripped
    snapshot: an editor seeded from it would delete every hand-written comment on the next PR."""
    from orchestrator.api.deps import config_source_optional_dependency

    class BrokenSource:
        async def process_path_at(self, repo: str, key: str, sha: str) -> str:
            return ".factorial/processes/process-t.yaml"

        async def read_process_text(self, repo: str, path: str, sha: str) -> str:
            raise RuntimeError("github outage")

    project = Project(
        organization_id=await default_organization_id(async_session), name="p", github_repo="o/r"
    )
    async_session.add(project)
    await async_session.flush()
    config = parse_process(config_yaml(), origin="test")
    row = ProcessVersion(
        project_id=project.id,
        source_repo=project.github_repo,
        process_key=config.name,
        source_commit_sha="b" * 40,
        content=config.model_dump(mode="json", by_alias=True),
    )
    async_session.add(row)
    await async_session.flush()
    app = build_app(make_settings(), async_session)
    app.dependency_overrides[config_source_optional_dependency] = lambda: BrokenSource()
    async with authed_client_for(app) as client:
        response = await client.get(f"/api/projects/{project.id}/process-versions/{row.id}/raw")
    assert response.status_code == 502


async def test_project_repo_input_is_normalized(async_session: AsyncSession) -> None:
    """The two realistic paste shapes — a clone URL and a .git suffix — must not survive into
    github_repo: a bad string strands the project behind an App-access check that cannot pass."""
    async with authed_client_for(build_app(make_settings(), async_session)) as client:
        from_url = await client.post(
            "/api/orgs/default/projects",
            json={"name": "a", "github_repo": "https://github.com/acme/widgets.git"},
        )
        from_suffix = await client.post(
            "/api/orgs/default/projects", json={"name": "b", "github_repo": "acme/gadgets.git"}
        )
        plain = await client.post(
            "/api/orgs/default/projects", json={"name": "c", "github_repo": "acme/plain"}
        )
    assert from_url.status_code == 201, from_url.text
    assert from_url.json()["github_repo"] == "acme/widgets"
    assert from_suffix.json()["github_repo"] == "acme/gadgets"
    assert plain.json()["github_repo"] == "acme/plain"


async def test_an_edited_repo_is_normalized_as_a_new_one_is(async_session: AsyncSession) -> None:
    """Project details edits the repository, and a pasted clone URL reached the 422 the create
    form's normalization exists to avoid."""
    project = await seed_project(async_session)
    async with authed_client_for(build_app(make_settings(), async_session)) as client:
        response = await client.patch(
            f"/api/projects/{project.id}",
            json={"github_repo": "https://github.com/acme/moved.git"},
        )
    assert response.status_code == 200, response.text
    assert response.json()["github_repo"] == "acme/moved"


async def test_a_repository_change_offers_none_of_the_old_repositorys_processes(
    async_session: AsyncSession,
) -> None:
    """The old repository's commits are not in the new one, so offering its process would send
    the editor, the agent cards and every new ticket to a commit that cannot be read there (issue
    #441). The version stays listed: history is readable, it is just not offered."""
    project = await seed_project(async_session, "acme/widgets")
    config = parse_process(config_yaml(), origin="test")
    async_session.add(
        ProcessVersion(
            project_id=project.id,
            source_repo=project.github_repo,
            process_key=config.name,
            source_commit_sha="a" * 40,
            content=config.model_dump(mode="json", by_alias=True),
        )
    )
    await async_session.flush()
    base = f"/api/projects/{project.id}"
    async with authed_client_for(build_app(make_settings(), async_session)) as client:
        before = (await client.get(base)).json()
        moved = await client.patch(base, json={"github_repo": "acme/moved"})
        after = (await client.get(base)).json()
        versions = (await client.get(f"{base}/process-versions")).json()
    assert [lane["process_key"] for lane in before["processes"]] == [config.name]
    assert moved.status_code == 200, moved.text
    assert after["processes"] == []
    assert [version["source_repo"] for version in versions] == ["acme/widgets"]


async def test_version_raw_reads_the_repository_the_version_came_from(
    async_session: AsyncSession,
) -> None:
    """The commit exists only where the sync read it; the project's repository may have moved
    since (issue #441)."""
    from orchestrator.api.deps import config_source_optional_dependency

    class StubSource:
        async def process_path_at(self, repo: str, key: str, sha: str) -> str:
            assert repo == "o/old"
            return ".factorial/processes/process-t.yaml"

        async def read_process_text(self, repo: str, path: str, sha: str) -> str:
            assert repo == "o/old"
            return config_yaml()

    project = await seed_project(async_session, "o/new")
    row = await _old_version(async_session, project)
    app = build_app(make_settings(), async_session)
    app.dependency_overrides[config_source_optional_dependency] = lambda: StubSource()
    async with authed_client_for(app) as client:
        response = await client.get(f"/api/projects/{project.id}/process-versions/{row.id}/raw")
    assert response.status_code == 200, response.text
    assert response.json()["source"] == "repo"


async def test_version_files_and_agents_read_the_repository_the_version_came_from(
    async_session: AsyncSession,
) -> None:
    from orchestrator.api.deps import config_source_dependency

    contents = FakeContents(sample_files(repo="o/old", sha=SHA))
    project = await seed_project(async_session, "o/new")
    row = await _old_version(async_session, project, contents=contents)
    app = build_app(make_settings(), async_session)
    app.dependency_overrides[config_source_dependency] = lambda: RepoConfigSource(contents)
    base = f"/api/projects/{project.id}/process-versions/{row.id}"
    async with authed_client_for(app) as client:
        files = await client.get(f"{base}/files")
        agents = await client.get(f"{base}/agents")
        one = await client.get(f"{base}/files/{SAMPLE_PROCESS_PATH}")
    assert files.status_code == 200, files.text
    assert SAMPLE_PROCESS_PATH in [entry["path"] for entry in files.json()["files"]]
    assert agents.status_code == 200, agents.text
    assert agents.json() and all(agent["warning"] is None for agent in agents.json())
    assert one.status_code == 200, one.text


async def test_an_old_repository_the_app_cannot_see_answers_502_naming_it(
    async_session: AsyncSession,
) -> None:
    """The App may have left the old repository's owner. That is the GitHub side's answer about
    one repository, not this server failing, and the reader needs to know which repository. The
    agents route degrades per agent instead, as it does for any agent it cannot read."""
    from orchestrator.api.deps import config_source_dependency
    from orchestrator.github.client import AppNotInstalled

    class Uninstalled(FakeContents):
        async def read_text(self, repo: str, path: str, sha: str) -> str:
            raise AppNotInstalled("1", "o")

        async def list_dir(self, repo: str, path: str, sha: str) -> Any:
            raise AppNotInstalled("1", "o")

        async def list_tree(self, repo: str, root: str, sha: str) -> Any:
            raise AppNotInstalled("1", "o")

    project = await seed_project(async_session, "o/new")
    row = await _old_version(async_session, project)
    app = build_app(make_settings(), async_session)
    app.dependency_overrides[config_source_dependency] = lambda: RepoConfigSource(
        Uninstalled({})
    )
    base = f"/api/projects/{project.id}/process-versions/{row.id}"
    async with authed_client_for(app) as client:
        files = await client.get(f"{base}/files")
        one = await client.get(f"{base}/files/{SAMPLE_PROCESS_PATH}")
        agents = await client.get(f"{base}/agents")
    for response in (files, one):
        assert response.status_code == 502, response.text
        assert "o/old" in response.json()["detail"]
    assert agents.status_code == 200, agents.text
    assert all("not installed" in agent["warning"] for agent in agents.json())


@pytest.mark.parametrize(
    "failure",
    [ContentsNotFound("o/old@aaaaaaa has no tree"), GitLabError("404 Project Not Found")],
    ids=["github", "gitlab"],
)
async def test_an_old_repository_that_is_gone_answers_502_naming_it(
    async_session: AsyncSession, failure: Exception
) -> None:
    """Deleted, or no longer among the repositories the App was given: GitHub answers the tree
    read with a 404 and GitLab with an error of its own. Either is an answer about that one
    repository, and an old repository is in this state more often than its owner has uninstalled
    the App."""
    from orchestrator.api.deps import config_source_dependency

    class Gone(FakeContents):
        async def list_tree(self, repo: str, root: str, sha: str) -> Any:
            raise failure

    project = await seed_project(async_session, "o/new")
    row = await _old_version(async_session, project)
    app = build_app(make_settings(), async_session)
    app.dependency_overrides[config_source_dependency] = lambda: RepoConfigSource(Gone({}))
    base = f"/api/projects/{project.id}/process-versions/{row.id}"
    async with authed_client_for(app) as client:
        files = await client.get(f"{base}/files")
        one = await client.get(f"{base}/files/{SAMPLE_PROCESS_PATH}")
    for response in (files, one):
        assert response.status_code == 502, response.text
        assert "o/old" in response.json()["detail"]


async def _old_version(
    session: AsyncSession, project: Project, *, contents: FakeContents | None = None
) -> ProcessVersion:
    """A version synced from `o/old`, a repository the project has since left: the sample's lane
    when `contents` serves it, else the builder's minimal config."""
    text = config_yaml()
    if contents is not None:
        text = contents.files[("o/old", SAMPLE_PROCESS_PATH, SHA)]
    config = parse_process(text, origin="test")
    row = ProcessVersion(
        project_id=project.id,
        source_repo="o/old",
        process_key=config.name,
        source_commit_sha=SHA,
        content=config.model_dump(mode="json", by_alias=True),
    )
    session.add(row)
    await session.flush()
    return row
