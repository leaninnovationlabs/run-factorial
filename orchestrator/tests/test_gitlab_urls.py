"""GitLab web URL helpers. No network."""

from __future__ import annotations

from orchestrator.gitlab.urls import (
    gitlab_blob_url,
    gitlab_commit_url,
    gitlab_compare_url,
    gitlab_issue_url,
    gitlab_origin,
)


def test_origin_prefers_the_project_host_then_the_fallback() -> None:
    assert gitlab_origin({"gitlab": {"base_url": "https://gitlab.example.com/"}}) == (
        "https://gitlab.example.com"
    )
    assert gitlab_origin({}, fallback="https://gitlab.internal") == "https://gitlab.internal"
    assert gitlab_origin(None) == "https://gitlab.com"


def test_issue_compare_commit_and_blob_urls() -> None:
    settings = {"gitlab": {"base_url": "https://gitlab.example"}}
    assert gitlab_issue_url(settings, "group/app", 7) == (
        "https://gitlab.example/group/app/-/issues/7"
    )
    assert gitlab_compare_url(settings, "group/app", "aaa", "bbb") == (
        "https://gitlab.example/group/app/-/compare/aaa...bbb"
    )
    assert gitlab_commit_url(settings, "group/app", "abc") == (
        "https://gitlab.example/group/app/-/commit/abc"
    )
    assert gitlab_blob_url(settings, "group/app", "abc", "docs/a b.html") == (
        "https://gitlab.example/group/app/-/blob/abc/docs/a%20b.html"
    )


def test_tls_verify_uses_system_store_when_no_extra_ca() -> None:
    from orchestrator.gitlab.client import tls_verify
    from orchestrator.settings import GitLabSettings

    assert tls_verify(GitLabSettings()) is True


def test_write_run_ca_file_merges_extra_pem(tmp_path) -> None:
    from orchestrator.gitlab.ca_file import extra_ca_pem, write_run_ca_file
    from orchestrator.settings import GitLabSettings

    extra = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"
    settings = GitLabSettings(ca_pem=extra)
    assert extra.strip() in extra_ca_pem(settings)
    dest = tmp_path / "ca.pem"
    written = write_run_ca_file(settings, dest)
    assert written == dest
    text = dest.read_text()
    assert extra.strip() in text
    assert dest.stat().st_mode & 0o777 == 0o644


def test_write_run_ca_file_skips_when_no_extra() -> None:
    from orchestrator.gitlab.ca_file import write_run_ca_file
    from orchestrator.settings import GitLabSettings

    assert write_run_ca_file(GitLabSettings()) is None
