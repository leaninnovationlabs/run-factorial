"""Process configuration — environment only, one source of truth.

Distinct from *ADLC config* (`orchestrator.config`), which is the per-project process
definition read from a target repo's `.factorial/` folder. This module is deployment wiring:
database URL, credentials, intervals.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import os
import socket
import tempfile
from datetime import timedelta
from email.utils import parseaddr
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Final, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    BeforeValidator,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import (
    BaseSettings,
    NoDecode,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)


class GitLabSettings(BaseModel):
    """Self-hosted GitLab credentials; unused when empty."""

    base_url: str = ""
    token: SecretStr = SecretStr("")
    webhook_secret: SecretStr = SecretStr("")
    ca_bundle: Path | None = None
    #: Extra CA PEM (any private PKI). Combined with the system store; not a DoD-only hook.
    ca_pem: str = ""
    bot_login: str = ""
    api_prefix: str = "/api/v4"

    @property
    def configured(self) -> bool:
        return bool(self.base_url.strip() and self.token.get_secret_value())

    @property
    def api_url(self) -> str:
        return self.base_url.rstrip("/") + self.api_prefix

    @property
    def host(self) -> str:
        from urllib.parse import urlsplit

        return urlsplit(self.base_url).netloc


class WorkerSettings(BaseModel):
    """Reconciliation loop knobs. Polling is authoritative (design.md §2.1)."""

    poll_interval_seconds: float = 30.0
    #: The least time between the starts of two fleet passes, however often the fleet is woken.
    #: A webhook delivery that names no ticket wakes it and each pass reads every ticket from
    #: GitHub, so a burst run back to back could spend the App's rate limit and trip
    #: `quarantine_after`. A delivery that names a ticket wakes that ticket alone, and one about
    #: an issue that is no ticket yet asks admission about that issue alone, both outside the gap.
    min_pass_gap_seconds: float = Field(default=30.0, ge=0)
    #: Must span everything from `worker.loop.tick`'s `acquire_leases` through the blocking
    #: `adapter.spawn()` call (decision 41). `_start_attempt`'s pre-spawn renewal does not extend
    #: the horizon for the spawn in flight: Postgres's `now()` is the enclosing transaction's start
    #: time, and the renewed row stays invisible to other sessions until this tick commits, which
    #: happens after `spawn()` returns. What actually stops a concurrent double-spawn is
    #: `step_run`'s `(ticket_id, step_name, attempt)` unique constraint, not this lease.
    #:
    #: 3600s is far above a CLI adapter's spawn, which is a fetch and a worktree; shortening it
    #: would buy nothing.
    #:
    #: Deployment requirement: the tick holds an open Postgres transaction across the whole spawn,
    #: so an `idle_in_transaction_session_timeout` below `lease_seconds` kills the tick mid-spawn
    #: — rolling back the RUNNING `step_run` row, so the pass cancels the agent it just spawned
    #: (decision 87) and the next tick spawns it again. Disable that timeout, or set it well above
    #: this value.
    #:
    #: `acquire_leases` matches `lease_owner IS NULL OR expired` and never same-owner, so a
    #: worker that dies mid-tick would strand its leased tickets until expiry; the next worker's
    #: startup releases them instead (`leases.release_all`, decision 40).
    lease_seconds: int = 3600
    #: Written to `ticket.lease_owner`; must be unique per running loop process.
    name: str = Field(default_factory=socket.gethostname)
    #: Ticket leases claimed per `acquire_leases` call. Not a per-tick cap: the tick claims again
    #: until a claim comes back short, so every reconcilable ticket gets every tick.
    batch_size: int = 10
    #: Consecutive raising ticks before a ticket is quarantined. An orchestrator-level floor,
    #: deliberately NOT in `.factorial`: a project must not be able to configure its way into an
    #: unbounded loop, and this counts ticks that died before any step ran, which is a different
    #: thing from `defaults.retry.maxAttempts` (that budgets failed WORK). `ge=1`: 0 or negative
    #: would quarantine on the first raising tick, fleet-wide.
    #:
    #: 20 is ten minutes at the default poll. The point is bounding infinity, not bounding it
    #: tightly: a correlated GitHub outage counts against every ticket at once, so a threshold
    #: short enough to trip on a blip quarantines the fleet and charges an operator a requeue per
    #: row. Waiting longer costs only a bounded spin (decision 31).
    quarantine_after: int = Field(default=20, ge=1)

    @model_validator(mode="after")
    def _gap_within_poll(self) -> WorkerSettings:
        # A wider gap silently becomes the poll interval, and a session's mid-turn rollover
        # (`worker.sessions`) needs a pass inside the last two minutes of its token.
        if self.min_pass_gap_seconds > self.poll_interval_seconds:
            raise ValueError(
                f"worker.min_pass_gap_seconds ({self.min_pass_gap_seconds}) must not exceed "
                f"worker.poll_interval_seconds ({self.poll_interval_seconds})"
            )
        return self


def _split_list(value: object, *, lower: bool = False) -> object:
    """Accept `a,b` as well as `["a","b"]`, each entry stripped and blanks dropped.

    pydantic-settings parses a `list[str]` as JSON, and a JSON list is unusable from a shell
    (`export FACTORIAL_CORS_ORIGINS=["http://x"]` loses its inner quotes). Each list field is
    `NoDecode` and parsed here, so both spellings behave the same whether the Settings object came
    from the environment or from direct construction.
    """
    if isinstance(value, str):
        text = value.strip()
        value = json.loads(text) if text.startswith("[") else text.split(",")
    if not isinstance(value, list):
        return value
    entries: list[str] = []
    for entry in value:
        if not isinstance(entry, str):
            raise ValueError(f"entry {entry!r} is not a string")
        if entry := entry.strip():
            entries.append(entry.lower() if lower else entry)
    return entries


class ClaudeCliSettings(BaseModel):
    """The `claude_cli` substrate (tech-stack decision 16).

    `local_root` has no default and the adapter refuses to spawn without one. That is deliberate:
    this substrate runs an agent with permissions bypassed directly on the host, and a defaulted
    directory would decide where on someone's filesystem that happens.
    """

    claude_bin: str = "claude"
    #: Every run's worktree lives under here, and a resolved path that escapes it is refused.
    #: One deployment per root: its reaper removes every workspace its own database does not keep.
    local_root: Path | None = None
    #: Which credential a run authenticates with. `api_key` bills API credits through the
    #: stored `ANTHROPIC_API_KEY`; `subscription` bills the operator's plan through a stored
    #: `CLAUDE_CODE_OAUTH_TOKEN` (`claude setup-token`), with the key kept out of the child. A
    #: runtime row's type overrides it for every run.
    auth_mode: Literal["api_key", "subscription"] = "api_key"


class CodexCliSettings(BaseModel):
    """The `codex_cli` substrate (tech-stack decision 34).

    `local_root` has no default for decision 16's reason, unchanged by the vendor: this
    substrate runs an agent directly on the host, and a defaulted directory would decide where on
    someone's filesystem its writable root lands.
    """

    codex_bin: str = "codex"
    #: Every run's worktree lives under here, and a resolved path that escapes it is refused.
    #: One deployment per root: its reaper removes every workspace its own database does not keep.
    local_root: Path | None = None
    #: Which credential a run authenticates with. `api_key` bills OpenAI API credits and takes
    #: the key from the project's secret store; `subscription` bills the operator's own Codex
    #: subscription by copying their `codex login` into each run's config home. The default is
    #: the one that works unattended: a subscription is a credential no orchestrator can hold.
    auth_mode: Literal["api_key", "subscription"] = "api_key"
    #: Where that login is read from. None means `~/.codex/auth.json`, where `codex login`
    #: leaves it. Read only — the copy each run refreshes is its own.
    subscription_auth_file: Path | None = None


class E2bSettings(BaseModel):
    """The E2B substrate (tech-stack decision 25), shared by `claude_e2b` and `codex_e2b`.

    What the deployment supplies: where the mirrors live, its own name, and the default
    template size. The team a sandbox runs in — its key and its template — is each E2B runtime's
    own (`E2B_API_KEY`, `details.template`). No key is ever read from here, and the API process
    needs none for transcripts: it reads the mirror under `state_root`, which it must share with
    the worker.
    """

    #: Where each run's mirror and state live; the API reads transcripts from here. Unset
    #: leaves E2B off.
    state_root: Path | None = None
    #: Names this deployment in every sandbox's metadata, so deployments sharing a team never
    #: reap each other's sandboxes. Without it the reaper does nothing.
    deployment: str = ""
    #: The size of each sandbox `python -m orchestrator.e2b_template` builds.
    template_cpu_count: int = Field(default=2, ge=1)
    template_memory_mb: int = Field(default=4096, ge=512)
    #: Hosts every E2B run may reach beyond its harness's, GitHub's and its project's.
    egress: list[str] = Field(default_factory=list)
    #: How long a finished run's mirror is kept.
    retain_days: int = Field(default=30, ge=1)

    @field_validator("state_root", mode="before")
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        # `.env.example` ships `KEY=` lines, and an empty value would otherwise read as a state
        # root of `.`, which is E2B switched on by accident.
        return None if value == "" else value

    @field_validator("egress")
    @classmethod
    def _hosts(cls, value: list[str]) -> list[str]:
        from orchestrator.factorial.runtime_policy import egress_hosts

        return list(egress_hosts(value, source="FACTORIAL_RUNTIME__E2B__EGRESS"))


class RuntimeSettings(BaseModel):
    """Per-kind adapter options (tech-stack decisions 16, 25, 34 and 44). Which kind runs a
    process is its Runtime row's, never the deployment's."""

    claude_cli: ClaudeCliSettings = Field(default_factory=ClaudeCliSettings)
    codex_cli: CodexCliSettings = Field(default_factory=CodexCliSettings)
    e2b: E2bSettings = Field(default_factory=E2bSettings)
    #: `<type>@<isolation>` entries this deployment offers on the Create runtime page. Unset:
    #: see `Settings.offers`.
    offered: Annotated[list[str] | None, NoDecode] = None

    @field_validator("offered", mode="before")
    @classmethod
    def _split_offered(cls, value: object) -> object:
        """An empty list, however spelled, is unset: no deployment means to offer nothing."""
        entries = _split_list(value)
        return None if entries == [] else entries

    @field_validator("offered")
    @classmethod
    def _known_offers(cls, value: list[str] | None) -> list[str] | None:
        from orchestrator.factorial.runtimes import parse_offers

        if value is not None:
            parse_offers(value)
        return value


def _seconds_or_iso(value: object) -> object:
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return value
    return value


#: Seconds or ISO 8601, never negative. pydantic alone reads `"600"` as a malformed ISO duration.
_Duration = Annotated[timedelta, BeforeValidator(_seconds_or_iso), Field(ge=timedelta(0))]


class TerminalSettings(BaseModel):
    """Sessions (tech-stack decisions 56 and 57). Every agent run is a session;
    `enabled` gates only a browser's attach, typing and reopen through the terminal gateway."""

    enabled: bool = False
    #: How long, at the earliest, an unmet idle session waits at its prompt before it parks for a
    #: person or, when nothing was asked and nobody engaged, ends and fails on its postconditions.
    #: That end also waits for the step's CI to settle.
    idle_park_after: _Duration = timedelta(minutes=10)
    #: How long after the last writer input a session whose postconditions hold is finished.
    finish_grace: _Duration = timedelta(seconds=60)
    #: How long a session lives from its spawn or a dashboard Resume before it parks for a human.
    #: Nothing the session reports moves it: its hooks are agent-writable (§7.14).
    session_ceiling: _Duration = timedelta(hours=4)
    #: How often a parked session's postconditions are read again.
    parked_recheck: _Duration = timedelta(minutes=5)
    #: codex's question box is under development in codex; a resume within seconds of the
    #: question is unproven (decision 56).
    codex_ask_tool: bool = True
    #: Where the terminal gateway listens (decision 57). Loopback: in development the UI reaches
    #: this port cross-origin. The image binds 0.0.0.0, and on ECS the load balancer sends
    #: `/factorial/terminal/*` on the dashboard's origin here.
    bind: str = "127.0.0.1"
    port: int = Field(default=8001, ge=1, le=65535)
    #: The origin a browser dials the gateway on. Empty is the API's own origin, where a proxy
    #: sends `/factorial/terminal/*` to `port`, as the ECS task's load balancer does. Unset, see
    #: `Settings.terminal_origin`.
    public_url: str | None = None

    @field_validator("public_url")
    @classmethod
    def _an_origin(cls, value: str | None) -> str | None:
        if value is None:
            return None
        url = value.strip().rstrip("/")
        parts = urlsplit(url)
        if url and (normalized_origin(url) is None or parts.path or parts.query or parts.fragment):
            raise ValueError(
                f"FACTORIAL_TERMINAL__PUBLIC_URL must be an http(s) origin with no path: {url}"
            )
        return url


_DEFAULT_PORTS: Final = {"http": 80, "https": 443}


def normalized_origin(url: str) -> str | None:
    """`url`'s origin as `scheme://host:port`, lower-cased with the port always written, so a
    browser's `Origin` header and a configured URL compare equal.

    None, never a raise, for anything that is not a plain http(s) origin: an `Origin` header is
    whatever the client sent. A host must be ASCII, so a configured IDN is written in punycode;
    otherwise case folding could match a look-alike (the Kelvin sign lowers to `k`).
    """
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    host = parts.hostname
    if (
        scheme not in _DEFAULT_PORTS
        or not host
        or not parts.netloc.isascii()
        # Userinfo, and a backslash before an `@` that a browser reads as a path.
        or "@" in parts.netloc
        or port == 0
    ):
        return None
    if ":" in host:
        host = f"[{host}]"
    return f"{scheme}://{host}:{_DEFAULT_PORTS[scheme] if port is None else port}"


class WorkspaceSettings(BaseModel):
    """Kept workspaces (tech-stack decision 54)."""

    #: How long a finished run's workspace is kept on a ticket that has not exited.
    retention: _Duration = timedelta(days=7)


class AssistantProcessSettings(BaseModel):
    """The assistant's process (tech-stack decision 63). What a turn may cost, which model it
    runs and whether the assistant is on are each organization's `organization_setting` row, which
    a person edits on the Assistant settings page; this is deployment wiring."""

    #: Each conversation's working directory is `<work_root>/<conversation id>`.
    work_root: Path = Path(tempfile.gettempdir()) / "factorial-assistant"
    #: Turns running at once on this process.
    concurrency: int = Field(default=4, ge=1, le=32)
    turn_wall_clock_seconds: float = Field(default=300, gt=0)
    #: The claim interval. A `NOTIFY` only shortens the wait; the interval is the guarantee.
    tick_seconds: float = Field(default=5, gt=0)
    #: The longest a streaming reply goes unwritten.
    text_flush_ms: int = Field(default=200, ge=0)
    proposal_ttl_hours: float = Field(default=24, gt=0)
    #: A running turn renews its lease every third of this. A process that dies leaves its turns
    #: running until the lease runs out, and then the next tick of any process interrupts them.
    lease_seconds: float = Field(default=60, gt=0)
    #: A conversation is deleted this many days after its last item.
    retain_days: int = Field(default=30, ge=1)
    #: Where this process asks the API for its credential and read-only GitHub token: loopback,
    #: since both run on one machine, so the request never leaves it through the public origin.
    api_url: str = "http://127.0.0.1:8000"
    #: Set by the image's `assistant` service, where the process runs as its own OS user: a
    #: master key in reach there is a leak, so the process refuses to start (design.md §7.18).
    own_user: bool = False
    #: `<backend>:<auth_mode>` entries a person may switch the assistant to. Unset: see
    #: `Settings.assistant_offers`.
    offered: Annotated[list[str] | None, NoDecode] = None

    @field_validator("offered", mode="before")
    @classmethod
    def _split_offered(cls, value: object) -> object:
        """An empty list, however spelled, is unset: no deployment means to offer nothing."""
        entries = _split_list(value)
        return None if entries == [] else entries

    @field_validator("offered")
    @classmethod
    def _known_offers(cls, value: list[str] | None) -> list[str] | None:
        from orchestrator.assistant.vocabulary import parse_assistant_offers

        if value is not None:
            parse_assistant_offers(value)
        return value


class MailSettings(BaseModel):
    """The one SMTP server the deployment sends email through (tech-stack decision 91). An empty
    host turns mail off: nothing is sent, and the email-verification gate is off."""

    smtp_host: str = ""
    #: Unset, 465 for `tls` and 587 otherwise.
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_username: str = ""
    smtp_password: SecretStr = SecretStr("")
    #: `starttls` upgrades a plain connection (port 587), `tls` opens one (port 465), `none` is
    #: for a server on the same machine or network that takes no credential.
    security: Literal["starttls", "tls", "none"] = "starttls"
    #: The sender, `no-reply@example.com` or `Factorial <no-reply@example.com>`.
    from_address: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.smtp_host.strip())

    @model_validator(mode="after")
    def _complete(self) -> MailSettings:
        if self.security == "tls" and "smtp_port" not in self.model_fields_set:
            self.smtp_port = 465
        if not self.enabled:
            return self
        if "@" not in parseaddr(self.from_address)[1]:
            raise ValueError(
                "FACTORIAL_MAIL__FROM_ADDRESS must be an address when FACTORIAL_MAIL__SMTP_HOST "
                f"is set, got {self.from_address!r}"
            )
        if self.security == "none" and self.smtp_password.get_secret_value():
            raise ValueError(
                "FACTORIAL_MAIL__SECURITY=none would send FACTORIAL_MAIL__SMTP_PASSWORD in the "
                "clear; use starttls or tls"
            )
        return self


class MultiOrgSettings(BaseModel):
    """Accounts, organizations, roles and invites (design.md §7.19, tech-stack decision 80). Off
    in the self-hosted build: every route they add answers 404 and every check they add passes,
    so the deployment behaves as it did before."""

    enabled: bool = False


class AuthSettings(BaseModel):
    """Dashboard sign-in. Unset (the default) disables the `/auth` routes and leaves the bearer
    token as the only way in — local dev needs no identity provider.

    Google stays a first-class shortcut. Any other OpenID Connect issuer (Okta, Entra, Keycloak,
    Auth0, another Google OAuth client) is the generic `oidc_*` set: authorization-code +
    client secret, identity from userinfo, same session cookie as Google.
    """

    google_client_id: str = ""
    google_client_secret: SecretStr = SecretStr("")
    #: Issuer URL, no trailing slash required. Discovery reads
    #: `{issuer}/.well-known/openid-configuration`
    #: unless authorize/token/userinfo are all set explicitly (Okta's token host can differ).
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: SecretStr = SecretStr("")
    oidc_authorize_url: str = ""
    oidc_token_url: str = ""
    oidc_userinfo_url: str = ""
    oidc_scopes: str = "openid email profile"
    #: Button label on the sign-in card, e.g. `Okta` or `Microsoft`. Empty uses `Okta` when
    #: the Okta host fields are set, else `SSO`.
    oidc_label: str = ""
    #: An Okta tenant's host only, or a full origin. Browser authorize uses `okta_domain`;
    #: token/userinfo use `okta_token_domain` when set, for a tenant whose browser host and token
    #: host differ (`okta.example.com` vs `okta-token.example.com`). Paths are
    #: `/oauth2/{authorization_server}/v1/*`.
    okta_domain: str = ""
    okta_token_domain: str = ""
    okta_client_id: str = ""
    okta_client_secret: SecretStr = SecretStr("")
    okta_authorization_server: str = "default"
    #: Email domains allowed to sign in, matched on the suffix after the last "@",
    #: case-insensitive. Enforced here rather than by the IdP: an External OAuth client lets any
    #: account through the consent screen.
    allowed_domains: Annotated[list[str], NoDecode] = Field(default_factory=list)
    #: The origin the browser reaches the dashboard on, with no path: the app's own URLs add
    #: `APP_PREFIX`. The OAuth redirect URI is derived from it and must equal the registered value
    #: exactly, so it is configuration rather than something guessed from proxy headers.
    external_url: str = ""
    #: Seconds a session lives; signing in is required again after.
    session_max_age: int = 7 * 24 * 3600
    #: Set, the first account on an empty install owns it only when its signup sends this; an SSO
    #: sign-in cannot, so it waits for that owner. Unset, whoever signs up first owns it.
    bootstrap_token: SecretStr = SecretStr("")

    def admits_first_owner(self, presented: str | None) -> bool:
        """Whether a signup that sent `presented` may own an empty install."""
        expected = self.bootstrap_token.get_secret_value()
        return not expected or (
            presented is not None and hmac.compare_digest(presented.encode(), expected.encode())
        )

    @property
    def google_enabled(self) -> bool:
        return bool(
            self.google_client_id
            and self.google_client_secret.get_secret_value()
            and self.external_url
        )

    def resolved_oidc_client_id(self) -> str:
        return self.oidc_client_id or self.okta_client_id

    def resolved_oidc_client_secret(self) -> str:
        return (
            self.oidc_client_secret.get_secret_value() or self.okta_client_secret.get_secret_value()
        )

    def resolved_oidc_label(self) -> str:
        if self.oidc_label:
            return self.oidc_label
        return "Okta" if self.okta_domain else "SSO"

    def okta_endpoints(self) -> dict[str, str] | None:
        """Authorize / token / userinfo for an Okta custom authorization server."""
        if not self.okta_domain:
            return None
        authorize_host = _okta_host(self.okta_domain)
        token_host = _okta_host(self.okta_token_domain or self.okta_domain)
        server = self.okta_authorization_server.strip() or "default"
        return {
            "authorize": f"https://{authorize_host}/oauth2/{server}/v1/authorize",
            "token": f"https://{token_host}/oauth2/{server}/v1/token",
            "userinfo": f"https://{token_host}/oauth2/{server}/v1/userinfo",
            "end_session": f"https://{authorize_host}/oauth2/{server}/v1/logout",
        }

    @property
    def oidc_enabled(self) -> bool:
        if not (
            self.resolved_oidc_client_id()
            and self.resolved_oidc_client_secret()
            and self.external_url
        ):
            return False
        if self.okta_domain:
            return True
        explicit = self.oidc_authorize_url and self.oidc_token_url and self.oidc_userinfo_url
        return bool(self.oidc_issuer or explicit)

    @property
    def enabled(self) -> bool:
        return self.google_enabled or self.oidc_enabled

    @field_validator("allowed_domains", mode="before")
    @classmethod
    def _split_domains(cls, value: object) -> object:
        return _split_list(value, lower=True)

    @field_validator("external_url")
    @classmethod
    def _origin_only(cls, value: str) -> str:
        # A path here would double the prefix in the redirect URI Google compares exactly.
        if urlsplit(value.strip()).path.strip("/"):
            raise ValueError(
                f"FACTORIAL_AUTH__EXTERNAL_URL is an origin with no path, got {value!r}"
            )
        return value


def _okta_host(value: str) -> str:
    """`okta.example.com` or `https://okta.example.com/`: configs mix both."""
    return value.strip().removeprefix("https://").removeprefix("http://").rstrip("/")


#: What every environment variable is called. `SF_` was the name until issue #138 and is now
#: REFUSED rather than read: the VM's environment and the IaC template that renders it both moved
#: on 2026-09-06, so an `SF_`-prefixed variable no longer means "not migrated yet", it means a
#: stale config that would otherwise resolve to a DEFAULT — a localhost database — and start
#: anyway. `_refuse_the_retired_prefix` turns that into a startup failure.
ENV_PREFIX: Final = "FACTORIAL_"
RETIRED_ENV_PREFIX: Final = "SF_"

#: Where the whole app lives on its origin: the dashboard, the API, MCP and the terminal, in
#: every mode (tech-stack decision 74). `ui/vite.config.ts`'s `base` is the other copy.
APP_PREFIX: Final = "/factorial"


def _is_settings_name(name: str, model: type[BaseModel] | None = None) -> bool:
    """Whether `name` (already unprefixed, e.g. `GITHUB__APP_ID`) addresses a real field.

    Resolved against the model rather than matched on the prefix alone, so an unrelated `SF_`
    variable in somebody's shell is not mistaken for a stale setting of ours.
    """
    head, _, rest = name.lower().partition("__")
    field = (model or Settings).model_fields.get(head)
    if field is None:
        return False
    if not rest:
        return True
    inner = field.annotation
    if not (isinstance(inner, type) and issubclass(inner, BaseModel)):
        return False
    return _is_settings_name(rest, inner)


def _dotenv_keys(path: object) -> set[str]:
    """Bare `KEY` names from a dotenv file. Values are never read."""
    if not path or not isinstance(path, (str, Path)):
        return set()
    file = Path(path)
    if not file.is_file():
        return set()
    keys = set()
    for line in file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            keys.add(line.split("=", 1)[0].strip())
    return keys


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_nested_delimiter="__",
        env_file=".env",
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """The default chain, plus a refusal to start on a config that still says `SF_`.

        Without the refusal the retirement is silent: an un-migrated `.env` simply stops being
        read and every field falls back to its default, which for `database_url` is a local
        development database. A wrong database that starts is worse than none that does not.

        Checked here rather than in a validator because this is where the env file the CALLER
        resolved is visible. `_env_file=None` means "read no file", and a guard that consulted
        `model_config` instead would scan the developer's own `.env` anyway.
        """
        candidates = set(os.environ) | _dotenv_keys(getattr(dotenv_settings, "env_file", None))
        stale = sorted(
            name
            for name in candidates
            if name.startswith(RETIRED_ENV_PREFIX)
            and _is_settings_name(name.removeprefix(RETIRED_ENV_PREFIX))
        )
        if stale:
            raise ValueError(
                f"{RETIRED_ENV_PREFIX} was retired in issue #138; rename to {ENV_PREFIX}: "
                + ", ".join(stale)
            )
        return (init_settings, env_settings, dotenv_settings, file_secret_settings)

    database_url: str = "postgresql+asyncpg://factory:factory@localhost:5432/factory"
    #: Shared-secret API/dashboard auth (tech-stack decision 12).
    api_token: SecretStr = SecretStr("")
    #: Browser origins allowed to call the API. Empty by default: the API is a machine interface
    #: unless someone points a browser at it. Accepts `http://localhost:5173,http://localhost:4173`
    #: or a JSON list. `NoDecode` stops the env source from JSON-decoding the field before any
    #: validator runs — without it a comma-separated value fails with a `SettingsError` that names
    #: the field but not the reason. All parsing lives in `_split_list`.
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)
    #: Accept plain-http preview URLs (`events.previews`). Local development only: a deployment
    #: frames previews beside an https dashboard, and a mixed-content frame never loads there.
    preview_allow_http: bool = False
    #: FastAPI's /docs, /redoc and /openapi.json. `make api` turns them on; the image exports false.
    api_docs: bool = False
    #: The built dashboard the API serves under `APP_PREFIX`; unset, it serves none.
    dashboard_dir: Path | None = None
    #: Master key (base64, 32 bytes) wrapping per-project data keys (tech-stack decision 8).
    master_key: SecretStr = SecretStr("")
    master_key_id: str = "local-dev-1"

    gitlab: GitLabSettings = Field(default_factory=GitLabSettings)
    worker: WorkerSettings = Field(default_factory=WorkerSettings)
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    terminal: TerminalSettings = Field(default_factory=TerminalSettings)
    workspace: WorkspaceSettings = Field(default_factory=WorkspaceSettings)
    assistant: AssistantProcessSettings = Field(default_factory=AssistantProcessSettings)
    multi_org: MultiOrgSettings = Field(default_factory=MultiOrgSettings)
    mail: MailSettings = Field(default_factory=MailSettings)

    @property
    def dashboard_origins(self) -> frozenset[str]:
        """Every origin the dashboard is served on, normalized. Not CORS alone: a deployment that
        serves the dashboard same-origin (the ECS task, behind its load balancer) leaves CORS
        empty."""
        return frozenset(
            origin
            for url in (self.auth.external_url, *self.cors_origins)
            if (origin := normalized_origin(url)) is not None
        )

    @property
    def webhook_reachable(self) -> bool:
        """False when the webhook URL would name this machine's own loopback address, which
        GitHub cannot reach. An unset dashboard address stays reachable: nothing says it is not."""
        return not is_loopback_host(urlsplit(self.dashboard_url).hostname or "")

    @property
    def dashboard_url(self) -> str:
        """Where a link back to the dashboard points; empty when nothing says."""
        url = self.auth.external_url.strip() or next(
            (origin.strip() for origin in self.cors_origins if normalized_origin(origin)), ""
        )
        return f"{url.rstrip('/')}{APP_PREFIX}" if url else ""

    def admits_origin(self, origin: str | None) -> bool:
        """Whether a browser's `Origin` header names the dashboard. A missing header is refused:
        the cookie rides along on a request from any page, and the header is the only thing that
        says which page sent it."""
        return origin is not None and normalized_origin(origin) in self.dashboard_origins

    @property
    def assistant_offers(self) -> frozenset[str]:
        """What a person may switch the assistant to: `assistant.offered` when set, else the two
        API keys, which is what the commercial release sells. Multi-org never offers the host's
        Bedrock role, which a stranger's organization would mint from (design.md §7.14). The list
        gates a switch only: an organization keeps a saved sign-in the list no longer names."""
        offered = (
            frozenset(self.assistant.offered)
            if self.assistant.offered is not None
            else frozenset({"claude_sdk:api_key", "codex_cli:api_key"})
        )
        if not self.multi_org.enabled:
            return offered
        return frozenset(offer for offer in offered if not offer.endswith(":bedrock"))

    @property
    def offers(self) -> frozenset[str]:
        """What a runtime may be created on (decision 72): `runtime.offered` when set. Unset,
        multi-org offers the cloud sandbox alone, because a stranger may sign up and a host
        runtime's agent is a shell beside the master key (design.md §7.14), and a Bedrock one
        mints from the host's AWS role. One organization offers the API-key types on the host,
        and in the sandbox too once E2B has a deployment name to reap by (the image names one).
        A subscription or Bedrock runtime comes back through `runtime.offered`."""
        from orchestrator.factorial.runtimes import ALL_OFFERS

        if self.runtime.offered is not None:
            return frozenset(self.runtime.offered)
        if self.multi_org.enabled:
            return frozenset(o for o in ALL_OFFERS if o.endswith("@e2b"))
        named = bool(self.runtime.e2b.deployment)
        return frozenset(
            o
            for o in ALL_OFFERS
            if o.startswith(("claude_api@", "codex_api@")) and (named or not o.endswith("@e2b"))
        )

    @property
    def terminal_origin(self) -> str:
        """Where the dashboard dials the terminal gateway: `terminal.public_url` when set. Unset,
        a dashboard on a loopback host, as under `make dev` and `make up`, dials the gateway's own
        port there, and any other dials the API's origin."""
        if self.terminal.public_url is not None:
            return self.terminal.public_url
        host = urlsplit(self.auth.external_url.strip() or "http://localhost").hostname or ""
        if not is_loopback_host(host):
            return ""
        return f"http://{f'[{host}]' if ':' in host else host}:{self.terminal.port}"

    @property
    def mcp_url(self) -> str:
        """Where the MCP server answers, as the dashboard shows it to a person setting up a
        client. The slash is part of it: the bare path answers a redirect some clients drop."""
        base = self.auth.external_url.strip().rstrip("/") or "http://localhost:8000"
        return f"{base}{APP_PREFIX}/mcp/"

    @property
    def mcp_endpoints(self) -> frozenset[tuple[str, int]]:
        """The (host, port) pairs this deployment's own MCP server answers on. No agent is given
        a server on one of these (design.md §7.5 item 9)."""
        # Deferred: `factorial` imports this module through the GitHub client.
        from orchestrator.factorial.mcp_policy import own_endpoints

        return own_endpoints(self.mcp_url, self.auth.external_url)

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        return _split_list(value)

    @model_validator(mode="after")
    def _assistant_offers_something(self) -> Settings:
        """Multi-org drops Bedrock after the list is read, so the list alone cannot say this."""
        if not self.assistant_offers:
            raise ValueError(
                "FACTORIAL_ASSISTANT__OFFERED leaves the assistant no sign-in here: multi-org "
                "never offers a bedrock sign-in, so list another entry"
            )
        return self


def mail_problems(settings: Settings) -> list[str]:
    """What keeps the API from starting. Open signup without verification lets a stranger take
    an invite to an address they registered (design.md §7.19 item 17), and a link needs an
    origin that is configuration, never the request's `Host`. The image's startup check names
    them too, because there a crashing API reads as a working app."""
    found: list[str] = []
    if settings.multi_org.enabled and not settings.mail.enabled:
        found.append(
            "FACTORIAL_MULTI_ORG__ENABLED needs mail: set FACTORIAL_MAIL__SMTP_HOST, so a "
            "password signup can verify its email"
        )
    if settings.mail.enabled and not settings.auth.external_url.strip():
        found.append(
            "FACTORIAL_MAIL__SMTP_HOST needs FACTORIAL_AUTH__EXTERNAL_URL: every link in an "
            "email starts with it"
        )
    return found


def is_loopback_host(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
