"""The runtime registry's vocabulary: what a `runtime.type` means to the rest of the system.

A product-level Runtime is operator-owned state a target repo's `process.yaml` may only
REFERENCE by slug (`runtime: <slug>`) — design.md §7.5's L2 shape applied to substrates, the
same trust direction as `mcp_allowlist`: the repo (which agents write to) selects; Postgres
(which operators own) defines. Every process names one (tech-stack decision 70).

One type fans out four ways, and each consumer needs a different one:
- the family a run is stamped with (`step_run.runtime_kind`, `declared_kind_for`);
- the EXECUTING kind the adapter registry builds (`runtime.adapters.create_adapter`);
- the auth mode the adapter is constructed with;
- the vendor credential the runtime row holds for it (`credential_name_for`).

An E2B runtime also holds its workspace: `E2B_CREDENTIAL` and `details.template`, the build it
boots in that workspace (decision 25).

Pure functions, no I/O. Everything here is derived from the type string alone so the API, the
worker and the sync check cannot disagree about a row.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from typing import Final, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    ValidationError,
    field_validator,
    model_validator,
)

from orchestrator.config.models import RUNTIME_SLUG_RE
from orchestrator.db.models import RUNTIME_TYPES
from orchestrator.factorial.bedrock_token import (
    DEFAULT_MODEL,
    GOVCLOUD_MODEL,
    GOVCLOUD_REGION,
)

__all__ = [
    "ALL_OFFERS",
    "E2B_CREDENTIAL",
    "E2B_KINDS",
    "HOST_ONLY_TYPES",
    "ISOLATION_PLACES",
    "METADATA_KEY_RE",
    "METADATA_MAX_KEYS",
    "METADATA_MAX_VALUE_LEN",
    "NOT_IMPLEMENTED_TYPES",
    "PLATFORM_MINTED_TYPES",
    "RUNTIME_TYPES",
    "SLUG_RE",
    "TEMPLATE_BUILD_RE",
    "CredentialValueInvalid",
    "RuntimeDetailsInvalid",
    "RuntimeMetadataInvalid",
    "auth_mode_for",
    "bedrock_model_of",
    "bedrock_region_of",
    "create_credential_names",
    "credential_name_for",
    "credential_value",
    "declared_kind_for",
    "family",
    "is_platform_minted",
    "is_subscription",
    "offer_of",
    "parse_offers",
    "parse_runtime_details",
    "parse_runtime_metadata",
    "registry_kind_for",
    "requires_stored_credential",
    "template_of",
]

#: Immutable after create, because repos reference it.
SLUG_RE: Final[re.Pattern[str]] = RUNTIME_SLUG_RE

#: The E2B workspace's key, beside the vendor credential on an E2B runtime. It reaches the
#: adapter's E2B client only: `run_secrets` never hands it to a run.
E2B_CREDENTIAL: Final = "E2B_API_KEY"

#: `<name>:<build id>`, the pin `python -m orchestrator.e2b_template` prints: a build, not a tag,
#: because a tag re-points when rebuilt.
TEMPLATE_BUILD_RE: Final[re.Pattern[str]] = re.compile(
    r"[a-z0-9][a-z0-9_-]*:[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}"
)

#: `runtime.metadata` is operator notes and nothing else reads it, so the shape is what a row of
#: two inputs can hold: a key an operator would type, a value a line can show.
METADATA_KEY_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
METADATA_MAX_KEYS: Final = 32
METADATA_MAX_VALUE_LEN: Final = 1024

#: In `RUNTIME_TYPES` (a row may exist) but refused at spawn, at login and by readiness until
#: its adapter lands. Empty since `claude_subscription` was built; the readers stay, so the
#: next type can enter the vocabulary (and the CHECK) before its flow does.
NOT_IMPLEMENTED_TYPES: Final[frozenset[str]] = frozenset()

Family = Literal["codex", "claude"]
DeclaredKind = Literal["codex", "claude"]
RegistryKind = Literal["codex_cli", "claude_cli", "codex_e2b", "claude_e2b"]
#: Where a runtime's agents run (decision 25): on the worker's host, or in an E2B sandbox.
Isolation = Literal["local_cli", "e2b"]
ISOLATIONS: Final[tuple[Isolation, ...]] = ("local_cli", "e2b")
#: Types with no E2B arm: `codex_bedrock`'s sandbox proxy still injects api.openai.com.
HOST_ONLY_TYPES: Final[frozenset[str]] = frozenset({"codex_bedrock"})
#: How a sentence says where an isolation runs: the stored value names a vendor, so users never
#: read it.
ISOLATION_PLACES: Final[dict[Isolation, str]] = {
    "local_cli": "on the host",
    "e2b": "in a cloud sandbox",
}
#: What a deployment can offer: `<runtime.type>@<details.isolation>`, e.g. `claude_api@e2b`.
ALL_OFFERS: Final[tuple[str, ...]] = tuple(
    f"{t}@{i}"
    for t in RUNTIME_TYPES
    for i in ISOLATIONS
    if not (i == "e2b" and t in HOST_ONLY_TYPES)
)
AuthMode = Literal["api_key", "subscription", "bedrock"]

_DECLARED: Final[dict[Family, DeclaredKind]] = {"codex": "codex", "claude": "claude"}
_REGISTRY: Final[dict[tuple[Family, Isolation], RegistryKind]] = {
    ("codex", "local_cli"): "codex_cli",
    ("claude", "local_cli"): "claude_cli",
    ("codex", "e2b"): "codex_e2b",
    ("claude", "e2b"): "claude_e2b",
}
#: The registry kinds that run in an E2B sandbox: only a runtime row, which holds its team's key
#: and template, can name one.
E2B_KINDS: Final[frozenset[str]] = frozenset(
    kind for (_, isolation), kind in _REGISTRY.items() if isolation == "e2b"
)


class RuntimeDetailsInvalid(ValueError):
    """`runtime.details` does not fit the model for its type."""


class RuntimeMetadataInvalid(ValueError):
    """`runtime.metadata` is not a small flat map of strings."""


class CredentialValueInvalid(ValueError):
    """A pasted credential cannot be sent. The sentence never holds the value."""


class _Details(BaseModel):
    """Per-type config; adding a key is a code change, not a migration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: The runtime's sandbox. Every type may run on either.
    isolation: Isolation = "local_cli"
    #: The template build an E2B runtime boots. Templates are private to the E2B team that built
    #: them, so the pin belongs with the workspace's key, on the runtime.
    template: str | None = None

    @model_validator(mode="after")
    def _template_is_an_e2b_build(self) -> _Details:
        if self.template is None:
            return self
        if self.isolation != "e2b":
            raise ValueError("template is set only on a runtime whose isolation is e2b")
        if not TEMPLATE_BUILD_RE.fullmatch(self.template):
            raise ValueError(
                f"template {self.template!r} is not `<name>:<build id>`, the pin the builder prints"
            )
        return self


class _CodexApiDetails(_Details):
    pass


class _CodexSubscriptionDetails(_Details):
    pass


class _ClaudeApiDetails(_Details):
    pass


class _ClaudeSubscriptionDetails(_Details):
    pass


class _CodexBedrockDetails(_Details):
    """Host-only: the worker mints the Bedrock bearer from the instance role. E2B is not
    implemented for this type (its proxy still injects api.openai.com).

    `region` selects the partition and has no default: a row written before it was required
    stores `{}` for GovCloud, which `bedrock_region_of` still reads that way. A missing `model`
    follows the region.
    """

    region: str
    model: str | None = None

    @field_validator("region")
    @classmethod
    def _region_shape(cls, value: str) -> str:
        region = value.strip()
        if not _BEDROCK_REGION_RE.fullmatch(region):
            raise ValueError(
                f"region {value!r} is not an AWS region ID, such as us-gov-west-1 or us-east-1"
            )
        return region

    @field_validator("model")
    @classmethod
    def _model_shape(cls, value: str | None) -> str | None:
        if value is None:
            return None
        model = value.strip()
        if not model:
            raise ValueError("model is required")
        return model

    @model_validator(mode="after")
    def _host_only(self) -> _CodexBedrockDetails:
        if self.isolation == "e2b":
            raise ValueError("codex_bedrock runs on the host; E2B is not implemented for it yet")
        if self.model is None:
            return self
        gov_region = _is_govcloud_region(self.region)
        gov_model = self.model.startswith("us-gov.")
        if gov_region and not gov_model:
            raise ValueError(
                "a GovCloud region needs a us-gov.* model; us.xai.grok-4.6 is commercial"
            )
        if not gov_region and gov_model:
            raise ValueError(
                "a commercial region cannot use a us-gov.* model; use us.xai.grok-4.6"
            )
        return self


_DETAILS: Final[dict[str, type[_Details]]] = {
    "codex_api": _CodexApiDetails,
    "codex_subscription": _CodexSubscriptionDetails,
    "codex_bedrock": _CodexBedrockDetails,
    "claude_api": _ClaudeApiDetails,
    "claude_subscription": _ClaudeSubscriptionDetails,
}
assert set(_DETAILS) == set(RUNTIME_TYPES), "every RuntimeType needs a details model"


#: Spelled out rather than split on `_`: `api_key` is not the suffix of `codex_api`.
_FAMILY_AND_AUTH: Final[dict[str, tuple[Family, AuthMode]]] = {
    "codex_api": ("codex", "api_key"),
    "codex_subscription": ("codex", "subscription"),
    "codex_bedrock": ("codex", "bedrock"),
    "claude_api": ("claude", "api_key"),
    "claude_subscription": ("claude", "subscription"),
}
assert set(_FAMILY_AND_AUTH) == set(RUNTIME_TYPES), "every RuntimeType needs a family and auth"

#: The name each adapter reads its credential under. The subscription names are what the login
#: routes store; the api-key names are the variables the CLIs read. Pinned to the adapters'
#: own constants by test, not by import: the orchestrator does not depend on adapter modules
#: for vocabulary.
_CREDENTIAL_NAME: Final[dict[str, str]] = {
    "codex_api": "OPENAI_API_KEY",
    "codex_subscription": "CODEX_AUTH_JSON",
    "codex_bedrock": "AWS_BEARER_TOKEN_BEDROCK",
    "claude_api": "ANTHROPIC_API_KEY",
    "claude_subscription": "CLAUDE_CODE_OAUTH_TOKEN",
}
assert set(_CREDENTIAL_NAME) == set(RUNTIME_TYPES), "every RuntimeType needs a credential name"

#: Types whose vendor credential is minted at spawn from the factory host, never stored on
#: the row. The name still exists so the adapter and the child env agree on one variable.
PLATFORM_MINTED_TYPES: Final[frozenset[str]] = frozenset({"codex_bedrock"})


def _split(rt: str) -> tuple[Family, AuthMode]:
    try:
        return _FAMILY_AND_AUTH[rt]
    except KeyError:
        raise ValueError(
            f"unknown runtime type {rt!r}; expected one of {', '.join(RUNTIME_TYPES)}"
        ) from None


def family(rt: str) -> Family:
    return _split(rt)[0]


def declared_kind_for(rt: str) -> DeclaredKind:
    """The family `step_run.runtime_kind` records for a run on this type."""
    return _DECLARED[family(rt)]


def registry_kind_for(rt: str, isolation: Isolation = "local_cli") -> RegistryKind:
    """The adapter the worker builds for this type on this sandbox
    (`runtime.adapters.create_adapter`)."""
    return _REGISTRY[(family(rt), isolation)]


def template_of(details: Mapping[str, object] | None) -> str | None:
    """A stored row's template pin, or None when it names none."""
    value = (details or {}).get("template")
    return value if isinstance(value, str) and value else None


def isolation_of(details: Mapping[str, object] | None) -> Isolation:
    """A stored row's isolation. A row written before the field existed runs on the host; one
    naming an isolation this code does not know is refused, never quietly run on the host."""
    value = (details or {}).get("isolation", "local_cli")
    if value not in ISOLATIONS:
        raise RuntimeDetailsInvalid(f"unknown runtime isolation {value!r}")
    return value  # type: ignore[return-value]


_BEDROCK_REGION_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z]{2}(?:-gov)?-[a-z]+-\d+$")


def _is_govcloud_region(region: str) -> bool:
    return "-gov-" in region


def bedrock_region_of(details: Mapping[str, object] | None) -> str:
    """The Bedrock Runtime region. Missing is a row from when GovCloud west was the default
    and defaults were not stored."""
    value = (details or {}).get("region")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return GOVCLOUD_REGION


def bedrock_model_of(details: Mapping[str, object] | None) -> str:
    """The inference-profile id Codex sends as `-m`. Missing follows the region."""
    value = (details or {}).get("model")
    if isinstance(value, str) and value.strip():
        return value.strip()
    region = bedrock_region_of(details)
    return GOVCLOUD_MODEL if _is_govcloud_region(region) else DEFAULT_MODEL


def row_registry_kind(rt: str, details: Mapping[str, object] | None) -> RegistryKind:
    """The adapter a runtime row builds: its type crossed with its isolation."""
    return registry_kind_for(rt, isolation_of(details))


def auth_mode_for(rt: str) -> AuthMode:
    return _split(rt)[1]


def is_subscription(rt: str) -> bool:
    return auth_mode_for(rt) == "subscription"


def is_platform_minted(rt: str) -> bool:
    """The host role (or a local env stand-in) mints this type's vendor credential."""
    _split(rt)
    return rt in PLATFORM_MINTED_TYPES


def requires_stored_credential(rt: str) -> bool:
    """False when readiness and Test credential must not ask the operator to store a key."""
    return not is_platform_minted(rt)


def credential_name_for(rt: str) -> str:
    """The one credential a runtime row holds for its type (tech-stack decision 36): the name
    the login route stores under and the adapter reads back — never any other.

    For a platform-minted type this is the env var the mint writes and Codex reads. The row
    does not store it.
    """
    _split(rt)
    return _CREDENTIAL_NAME[rt]


def offer_of(rt: str, isolation: Isolation) -> str:
    return f"{rt}@{isolation}"


def parse_offers(entries: Iterable[str]) -> frozenset[str]:
    """The set a deployment offers, each entry stripped; an unknown one is refused by name."""
    offers: set[str] = set()
    for raw in entries:
        entry = raw.strip()
        if entry not in ALL_OFFERS:
            raise ValueError(
                f"unknown runtime offer {entry!r}; expected <type>@<isolation> from "
                f"{', '.join(ALL_OFFERS)}"
            )
        offers.add(entry)
    return frozenset(offers)


def create_credential_names(rt: str, isolation: Isolation) -> frozenset[str]:
    """The credentials a create may carry: the type's own, unless the host mints it, and the
    sandbox key in a sandbox."""
    names = set() if is_platform_minted(rt) else {credential_name_for(rt)}
    if isolation == "e2b":
        names.add(E2B_CREDENTIAL)
    return frozenset(names)


_HEADER_SAFE_RE: Final[re.Pattern[str]] = re.compile(r"[\x21-\x7e]+")
_CODEX_AUTH_JSON: Final = _CREDENTIAL_NAME["codex_subscription"]
#: The credentials a vendor or E2B call carries. Any other name is an extra that reaches runs as
#: an environment variable, where a PEM or a JSON key file spans lines.
_SENT_IN_A_HEADER: Final = frozenset({*_CREDENTIAL_NAME.values(), E2B_CREDENTIAL})


def credential_value(name: str, value: str) -> str:
    """A model or sandbox key without surrounding whitespace (a pasted key often ends in a
    newline), or `CredentialValueInvalid` when it could not ride in an HTTP header. An extra is
    kept as pasted."""
    if not value.strip():
        raise CredentialValueInvalid("the value is empty")
    if name not in _SENT_IN_A_HEADER:
        return value
    value = value.strip()
    if name == _CODEX_AUTH_JSON:
        try:
            parsed = json.loads(value)
        except (ValueError, RecursionError):
            raise CredentialValueInvalid("the auth.json does not parse") from None
        tokens = parsed.get("tokens") if isinstance(parsed, dict) else None
        if not (isinstance(tokens, dict) and isinstance(tokens.get("access_token"), str)):
            raise CredentialValueInvalid("the auth.json has no tokens.access_token")
    elif not _HEADER_SAFE_RE.fullmatch(value):
        raise CredentialValueInvalid("the value holds a character no HTTP header can carry")
    return value


def parse_runtime_details(rt: str, details: Mapping[str, object]) -> BaseModel:
    """Validate a `runtime.details` bag at the boundary, for the type that owns it."""
    model = _DETAILS.get(rt)
    if model is None:
        raise RuntimeDetailsInvalid(f"unknown runtime type {rt!r}")
    try:
        return model.model_validate(dict(details))
    except ValidationError as exc:
        raise RuntimeDetailsInvalid(f"details for runtime type {rt!r} are invalid: {exc}") from exc


def parse_runtime_metadata(metadata: Mapping[str, object]) -> dict[str, str]:
    """Validate a `runtime.metadata` map at the boundary: flat, string to string, bounded."""
    if len(metadata) > METADATA_MAX_KEYS:
        raise RuntimeMetadataInvalid(
            f"at most {METADATA_MAX_KEYS} keys are allowed, got {len(metadata)}"
        )
    parsed: dict[str, str] = {}
    for key, value in metadata.items():
        if not METADATA_KEY_RE.match(key):
            raise RuntimeMetadataInvalid(
                f"key {key!r} must be 1 to 64 characters of [A-Za-z0-9_.-] "
                "and start with a letter or digit"
            )
        if not isinstance(value, str):
            raise RuntimeMetadataInvalid(
                f"value for {key!r} must be a string, got {type(value).__name__}"
            )
        if len(value) > METADATA_MAX_VALUE_LEN:
            raise RuntimeMetadataInvalid(
                f"value for {key!r} is {len(value)} characters; "
                f"at most {METADATA_MAX_VALUE_LEN} are allowed"
            )
        parsed[key] = value
    return parsed
