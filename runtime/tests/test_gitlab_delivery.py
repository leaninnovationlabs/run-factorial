"""GitLab run-token bot usernames count as factory delivery."""

from runtime.gitlab_delivery import gitlab_delivery_author


def test_gitlab_run_project_bot_counts_even_when_bot_login_is_wrong() -> None:
    author = "project_87260009_bot_c2c02e59f286f41a8a1798eedd5f2db1"
    assert gitlab_delivery_author(author, "factorial-agent") is True


def test_gitlab_still_honours_configured_bot_login() -> None:
    assert gitlab_delivery_author("teammate", "teammate") is True


def test_github_shaped_login_without_project_bot_pattern_needs_exact_match() -> None:
    assert gitlab_delivery_author("human", "factorial-agent") is False
