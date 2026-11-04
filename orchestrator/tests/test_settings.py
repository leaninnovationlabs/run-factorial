"""Settings parsing that is easy to get wrong from a shell."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from orchestrator.settings import Settings, WorkerSettings, normalized_origin
from orchestrator.worker.context import SessionPolicy, adapter_options

DATABASE_URL = "postgresql+asyncpg://u:p@localhost:1/db"


def build(**overrides: Any) -> Settings:
    """`Settings(...)` with a fixed database URL. Kwargs are `Any` because these fields take a
    raw string from the environment and the `before` validator is what turns it into the
    declared type — which is exactly the behaviour under test.

    `_env_file=None` because `Settings.model_config` names `.env`, so without it every default
    asserted here is really an assertion about the developer's own file. That is not theoretical:
    `.env.example` tells an operator to set `SF_CORS_ORIGINS=http://localhost:5173` to run
    `make ui`, and doing exactly that used to fail `test_cors_origins_defaults_to_nothing` — a
    suite that breaks when you follow the setup instructions. These tests are about the parser,
    not about what happens to be on this machine.
    """
    # `_env_file` is a pydantic-settings init kwarg; pyright does not model the dunder-prefixed
    # settings-source overrides, so it reads as an unknown parameter.
    return Settings(_env_file=None, database_url=DATABASE_URL, **overrides)  # pyright: ignore[reportCallIssue]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # What an operator types. A JSON list cannot survive `export` unquoted, so this is the
        # form that has to work.
        ("http://localhost:5173", ["http://localhost:5173"]),
        (
            "http://localhost:5173, http://localhost:4173",
            ["http://localhost:5173", "http://localhost:4173"],
        ),
        ("", []),
        ("  ", []),
        # Still accepted, for a JSON-shaped .env or a config management tool.
        ('["http://localhost:5173"]', ["http://localhost:5173"]),
    ],
)
def test_cors_origins_accepts_both_spellings(value: str, expected: list[str]) -> None:
    assert build(cors_origins=value).cors_origins == expected


def test_cors_origins_takes_a_real_list_unchanged() -> None:
    """The validator must not mangle a value that arrives already parsed."""
    assert build(cors_origins=["http://a", "http://b"]).cors_origins == ["http://a", "http://b"]


def test_cors_origins_defaults_to_nothing() -> None:
    """No origin is allowed unless someone says so — a permissive default would outlive its
    reason."""
    assert build().cors_origins == []


def test_lease_seconds_defaults_to_3600() -> None:
    """Decision 41: long enough for the lease taken at acquisition to span checkout
    through the substrate's own startup — a ~1995s floor with zero declared secrets, plus 120s
    per secret on top (settings.py's `lease_seconds` comment has the corrected arithmetic)."""
    assert build().worker.lease_seconds == 3600


@pytest.mark.parametrize("bad", [0, -1])
def test_quarantine_after_refuses_a_non_positive_floor(bad: int) -> None:
    """0 or negative would quarantine on the first raising tick, fleet-wide."""
    with pytest.raises(ValidationError):
        WorkerSettings(quarantine_after=bad)


def test_quarantine_after_defaults_to_20() -> None:
    """Ten minutes at the default poll: long enough that a GitHub blip resolves without taking
    the fleet with it, which is the failure mode a short threshold has (decision 31)."""
    assert WorkerSettings().quarantine_after == 20


@pytest.mark.parametrize(
    ("variable", "value", "field", "expected"),
    [
        ("FACTORIAL_TERMINAL__ENABLED", "true", "terminal_enabled", True),
        ("FACTORIAL_TERMINAL__IDLE_PARK_AFTER", "300", "idle_park_after", timedelta(minutes=5)),
        ("FACTORIAL_TERMINAL__FINISH_GRACE", "PT30S", "finish_grace", timedelta(seconds=30)),
        ("FACTORIAL_WORKSPACE__RETENTION", "P2D", "retention", timedelta(days=2)),
        ("FACTORIAL_TERMINAL__PARKED_RECHECK", "PT1M", "parked_recheck", timedelta(minutes=1)),
    ],
)
def test_terminal_and_workspace_settings_read_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, variable: str, value: str, field: str, expected: object
) -> None:
    """Through to the policy the worker reads: a variable that stops short of it would leave the
    deployment on its default without a word."""
    monkeypatch.setenv("FACTORIAL_DATABASE_URL", DATABASE_URL)
    assert getattr(SessionPolicy.from_settings(build()), field) != expected
    monkeypatch.setenv(variable, value)
    settings = build()
    assert getattr(SessionPolicy.from_settings(settings), field) == expected


@pytest.mark.parametrize(
    "variable",
    [
        "FACTORIAL_TERMINAL__IDLE_PARK_AFTER",
        "FACTORIAL_TERMINAL__FINISH_GRACE",
        "FACTORIAL_WORKSPACE__RETENTION",
    ],
)
def test_a_negative_duration_is_refused(monkeypatch: pytest.MonkeyPatch, variable: str) -> None:
    """A negative retention would release every kept workspace at once."""
    monkeypatch.setenv("FACTORIAL_DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv(variable, "0")
    build()
    monkeypatch.setenv(variable, "-60")
    with pytest.raises(ValidationError):
        build()


def test_dashboard_origins_include_the_external_url_when_cors_is_empty() -> None:
    """The deployed config: the ALB serves the dashboard same-origin, so CORS is empty and the
    external URL is the only place its origin is written. Read from CORS alone, the terminal
    gateway would refuse every browser there."""
    settings = build(auth={"external_url": "https://Factorial.example.com/"})
    assert settings.cors_origins == []
    assert normalized_origin("https://factorial.example.com") in settings.dashboard_origins
    assert settings.dashboard_url == "https://Factorial.example.com/factorial"


@pytest.mark.parametrize(
    ("dashboard_dir", "external_url", "warned"),
    [
        ("/app/ui/dist", "", True),
        ("/app/ui/dist", "https://f.example.com", False),
        (None, "", False),
    ],
)
def test_an_api_serving_the_dashboard_says_when_no_external_url_names_its_origin(
    caplog: pytest.LogCaptureFixture, dashboard_dir: str | None, external_url: str, warned: bool
) -> None:
    """Off localhost the origin-checked routes refuse the dashboard, and the refusal names no
    cause; the API's log is the one place that can."""
    from orchestrator.api.main import warn_without_external_url

    settings = build(dashboard_dir=dashboard_dir, auth={"external_url": external_url})
    with caplog.at_level("WARNING", logger="orchestrator.api.main"):
        warn_without_external_url(settings)
    assert ("FACTORIAL_AUTH__EXTERNAL_URL" in caplog.text) is warned


@pytest.mark.parametrize("multi_org", [False, True])
def test_the_api_warns_without_a_setup_token_whether_organizations_are_open_or_not(
    caplog: pytest.LogCaptureFixture, multi_org: bool
) -> None:
    """With multi-org on, the first signup on an empty install still owns `default`."""
    from orchestrator.api.main import warn_without_setup_token

    settings = build(multi_org={"enabled": multi_org})
    with caplog.at_level("WARNING", logger="orchestrator.api.main"):
        warn_without_setup_token(settings)
    assert "FACTORIAL_AUTH__BOOTSTRAP_TOKEN is unset" in caplog.text


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("HTTPS://Factorial.Example.COM", "https://factorial.example.com:443"),
        ("https://factorial.example.com:443", "https://factorial.example.com:443"),
        ("http://localhost:5173/path?q=1#f", "http://localhost:5173"),
        ("https://[::1]", "https://[::1]:443"),
        ("https://[::1]:443", "https://[::1]:443"),
        ("null", None),
        ("", None),
        ("ftp://factorial.example.com", None),
        ("https://good.example@evil.example", None),
        ("https://x.com\\@evil.com", None),
        ("https://[::1", None),
        ("https://x]", None),
        ("https://a\uff03b.com", None),
        ("https://\u212aelvin.example", None),
        ("https://factorial.example.com:0", None),
        ("https://factorial.example.com:99999", None),
    ],
)
def test_normalized_origin_compares_equal_origins_and_refuses_the_rest(
    url: str, expected: str | None
) -> None:
    """The terminal gateway's cross-site check reads a header the client chose: a raise refuses
    every handshake, and a lenient parse lets another site's page through."""
    assert normalized_origin(url) == expected
    if expected is not None:
        assert normalized_origin(expected) == expected


# --- the retired prefix (issue #138) -----------------------------------------------------------


def test_the_new_prefix_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FACTORIAL_DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("FACTORIAL_WORKER__POLL_INTERVAL_SECONDS", "47")
    settings = build()
    assert settings.database_url == DATABASE_URL
    assert settings.worker.poll_interval_seconds == 47


def test_the_retired_prefix_is_refused_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """Loudly, not by falling back to defaults. An un-migrated `.env` simply stops being read,
    so every field takes its default — and `database_url`'s default is a local development
    database. A wrong database that starts is worse than no database that does not."""
    monkeypatch.setenv("FACTORIAL_DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("SF_API_TOKEN", "x")
    with pytest.raises(ValueError, match="SF_ was retired"):
        build()


def test_an_unrelated_sf_variable_is_not_mistaken_for_a_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard matches against the model, not the prefix, so somebody's `SF_HOME` is theirs."""
    monkeypatch.setenv("FACTORIAL_DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("SF_HOME", "/somewhere")
    monkeypatch.setenv("SF_NOT_A_FIELD__AT_ALL", "x")
    assert build().database_url == DATABASE_URL


def test_claude_cli_auth_mode_defaults_to_api_key_and_reaches_the_adapter() -> None:
    """Mirrors `CodexCliSettings.auth_mode`: forwarded as the adapter's own keyword, which a
    runtime row's type overrides."""
    assert build().runtime.claude_cli.auth_mode == "api_key"
    assert adapter_options(build(), "claude_cli")["auth_mode"] == "api_key"
    tuned = build(runtime={"claude_cli": {"auth_mode": "subscription"}})
    assert adapter_options(tuned, "claude_cli")["auth_mode"] == "subscription"
    with pytest.raises(ValidationError):
        build(runtime={"claude_cli": {"auth_mode": "oauth"}})


def test_a_stale_dotenv_file_is_refused_too(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The case os.environ misses, and the only one that still had a plausible victim: a
    developer's `.env` that nobody renamed."""
    stale = tmp_path / ".env"
    stale.write_text("SF_DATABASE_URL=postgresql+asyncpg://old:old@localhost:1/old\n")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="SF_ was retired"):
        Settings()


def test_mcp_endpoints_cover_the_mcp_url_and_the_dashboard_origin() -> None:
    settings = build(auth={"external_url": "https://App.example.com/"})
    assert settings.mcp_endpoints == frozenset({("app.example.com", 443)})


@pytest.mark.parametrize("empty", ["", "[]", ",", " , "])
def test_offered_reads_a_comma_list_and_refuses_an_unknown_entry(
    monkeypatch: pytest.MonkeyPatch, empty: str
) -> None:
    monkeypatch.setenv("FACTORIAL_RUNTIME__OFFERED", "claude_api@e2b, codex_api@e2b")
    assert build().offers == {"claude_api@e2b", "codex_api@e2b"}
    monkeypatch.setenv("FACTORIAL_RUNTIME__OFFERED", empty)
    assert build().runtime.offered is None
    monkeypatch.setenv("FACTORIAL_RUNTIME__OFFERED", "claude_api@firecracker")
    with pytest.raises(ValueError, match="claude_api@firecracker"):
        build()


API_HOST = {"claude_api@local_cli", "codex_api@local_cli"}
API_SANDBOX = {"claude_api@e2b", "codex_api@e2b"}
SANDBOX = {
    "claude_api@e2b",
    "claude_subscription@e2b",
    "codex_api@e2b",
    "codex_subscription@e2b",
}


def test_one_organization_offers_api_keys_on_the_host_and_in_the_sandbox_once_it_is_named() -> None:
    assert build().offers == API_HOST
    assert build(runtime={"e2b": {"deployment": "here"}}).offers == API_HOST | API_SANDBOX
    listed = build(runtime={"offered": "claude_subscription@local_cli"})
    assert listed.offers == {"claude_subscription@local_cli"}


def test_multi_org_offers_only_the_sandbox_unless_the_operator_says_otherwise() -> None:
    """Under open signup a stranger creates runtimes: a host one's agent is a shell beside the
    master key, and a Bedrock one mints from the host's AWS role."""
    multi = {"enabled": True}
    assert build(multi_org=multi).offers == SANDBOX
    assert build(multi_org=multi, runtime={"e2b": {"deployment": "here"}}).offers == SANDBOX
    chosen = build(multi_org=multi, runtime={"offered": "claude_api@local_cli"})
    assert chosen.offers == {"claude_api@local_cli"}


@pytest.mark.parametrize(
    ("external_url", "public_url", "expected"),
    [
        # `make dev` and `make up`: the gateway publishes its own port beside the API's.
        ("", None, "http://localhost:8001"),
        ("http://127.0.0.1:8000", None, "http://127.0.0.1:8001"),
        # The ECS task: its load balancer routes /factorial/terminal/* on the dashboard's origin.
        ("https://factorial.example.com", None, ""),
        ("https://factorial.example.com", "https://term.example.com/", "https://term.example.com"),
        ("", "", ""),
    ],
)
def test_the_terminal_origin_follows_the_deployment_shape(
    external_url: str, public_url: str | None, expected: str
) -> None:
    """The dashboard learns it at run time, so one image serves both shapes: a wrong answer
    leaves every terminal dialing a port nothing publishes."""
    terminal = {} if public_url is None else {"public_url": public_url}
    settings = build(auth={"external_url": external_url}, terminal=terminal)
    assert settings.terminal_origin == expected


@pytest.mark.parametrize("url", ["term.example.com", "https://term.example.com/factorial"])
def test_a_terminal_public_url_that_is_not_an_origin_is_refused(url: str) -> None:
    with pytest.raises(ValidationError, match="TERMINAL__PUBLIC_URL"):
        build(terminal={"public_url": url})
