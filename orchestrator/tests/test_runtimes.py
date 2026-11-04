"""The runtime registry's pure helpers, and the runtime-scoped half of the secret store."""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import BaseModel, SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orchestrator.api.services.organizations import default_organization_id
from orchestrator.db.models import RUNTIME_TYPES, Runtime, RuntimeCredential, RuntimeType
from orchestrator.factorial.runtime_policy import runtime_permitted
from orchestrator.factorial.runtimes import (
    NOT_IMPLEMENTED_TYPES,
    SLUG_RE,
    CredentialValueInvalid,
    RuntimeDetailsInvalid,
    RuntimeMetadataInvalid,
    auth_mode_for,
    create_credential_names,
    credential_name_for,
    credential_value,
    declared_kind_for,
    family,
    is_platform_minted,
    is_subscription,
    parse_offers,
    parse_runtime_details,
    parse_runtime_metadata,
    registry_kind_for,
    requires_stored_credential,
    row_registry_kind,
)
from orchestrator.factorial.runtimes import RUNTIME_TYPES as REEXPORTED_RUNTIME_TYPES
from orchestrator.secrets import store
from orchestrator.secrets.crypto import DecryptionError, encrypt

# ------------------------------------------------------------------------------- helpers


@pytest.mark.parametrize("slug", ["a", "codex-team", "c0dex-1", "x" * 63, "9lives"])
def test_slug_accepts_lowercase_kebab(slug: str) -> None:
    assert SLUG_RE.fullmatch(slug)


@pytest.mark.parametrize(
    "slug", ["", "-lead", "Upper", "under_score", "has space", "x" * 64, "dot.ted", "a/b"]
)
def test_slug_rejects_everything_else(slug: str) -> None:
    assert SLUG_RE.fullmatch(slug) is None


@pytest.mark.parametrize(
    ("rt", "fam", "declared", "registry", "auth"),
    [
        ("codex_api", "codex", "codex", "codex_cli", "api_key"),
        ("codex_subscription", "codex", "codex", "codex_cli", "subscription"),
        ("codex_bedrock", "codex", "codex", "codex_cli", "bedrock"),
        ("claude_api", "claude", "claude", "claude_cli", "api_key"),
        ("claude_subscription", "claude", "claude", "claude_cli", "subscription"),
    ],
)
def test_every_type_maps_to_its_family_kinds_and_auth_mode(
    rt: str, fam: str, declared: str, registry: str, auth: str
) -> None:
    assert family(rt) == fam
    assert declared_kind_for(rt) == declared
    assert registry_kind_for(rt) == registry
    assert auth_mode_for(rt) == auth
    assert is_subscription(rt) is (auth == "subscription")


def test_the_enum_and_the_helper_agree_on_the_vocabulary() -> None:
    assert REEXPORTED_RUNTIME_TYPES is RUNTIME_TYPES
    assert set(RUNTIME_TYPES) == {
        "codex_api",
        "codex_subscription",
        "codex_bedrock",
        "claude_api",
        "claude_subscription",
    }
    assert RuntimeType.CODEX_SUBSCRIPTION == "codex_subscription"
    # Every type is built. The set stays, with its readers, for the next type that lands in the
    # vocabulary before its adapter does.
    assert frozenset() == NOT_IMPLEMENTED_TYPES
    assert set(RUNTIME_TYPES) >= NOT_IMPLEMENTED_TYPES


def test_the_credential_table_is_the_adapters_own_vocabulary() -> None:
    """Decision 36: the runtime row holds exactly one credential, named here, and the adapter
    reads it under its own constant. Drift between the two is a test failure, not a run that
    starts and finds the variable absent."""
    from runtime.adapters.claude_cli.auth import SECRET_OAUTH_TOKEN
    from runtime.adapters.claude_cli.cli import ENV_API_KEY as CLAUDE_ENV_API_KEY
    from runtime.adapters.codex_cli.auth import ENV_BEDROCK_BEARER, SECRET_AUTH_JSON
    from runtime.adapters.codex_cli.cli import ENV_API_KEY as CODEX_ENV_API_KEY

    assert credential_name_for("codex_api") == CODEX_ENV_API_KEY == "OPENAI_API_KEY"
    assert credential_name_for("codex_subscription") == SECRET_AUTH_JSON == "CODEX_AUTH_JSON"
    assert credential_name_for("codex_bedrock") == ENV_BEDROCK_BEARER == "AWS_BEARER_TOKEN_BEDROCK"
    assert credential_name_for("claude_api") == CLAUDE_ENV_API_KEY == "ANTHROPIC_API_KEY"
    assert credential_name_for("claude_subscription") == SECRET_OAUTH_TOKEN
    assert SECRET_OAUTH_TOKEN == "CLAUDE_CODE_OAUTH_TOKEN"
    assert len({credential_name_for(rt) for rt in RUNTIME_TYPES}) == len(RUNTIME_TYPES), (
        "no two types share a credential name"
    )


@pytest.mark.parametrize(
    "fn",
    [
        family,
        declared_kind_for,
        registry_kind_for,
        auth_mode_for,
        is_subscription,
        credential_name_for,
    ],
)
def test_an_unknown_type_is_refused_by_every_helper(fn) -> None:
    with pytest.raises(ValueError, match="gemini_api"):
        fn("gemini_api")


@pytest.mark.parametrize("rt", [rt for rt in RUNTIME_TYPES if rt != "codex_bedrock"])
def test_empty_details_parse_for_every_type(rt: str) -> None:
    parsed = parse_runtime_details(rt, {})
    assert isinstance(parsed, BaseModel)
    assert parsed.model_dump(exclude_defaults=True) == {}


@pytest.mark.parametrize("rt", RUNTIME_TYPES)
def test_an_unknown_details_key_is_refused(rt: str) -> None:
    """v1 details models are empty on purpose: a key becomes legal by a code change."""
    with pytest.raises(RuntimeDetailsInvalid, match="model"):
        parse_runtime_details(rt, {"model": "gpt-5"})


def test_details_for_an_unknown_type_are_refused() -> None:
    with pytest.raises(RuntimeDetailsInvalid, match="gemini_api"):
        parse_runtime_details("gemini_api", {})


# ------------------------------------------------------------------------------- metadata


def test_metadata_is_flat_strings_and_comes_back_as_a_plain_dict() -> None:
    parsed = parse_runtime_metadata({"owner": "platform", "cost.center": "cc-42", "tier_1": ""})
    assert parsed == {"owner": "platform", "cost.center": "cc-42", "tier_1": ""}
    assert type(parsed) is dict
    assert parse_runtime_metadata({}) == {}


@pytest.mark.parametrize(
    ("mapping", "fragment"),
    [
        ({"": "x"}, "''"),
        ({"-lead": "x"}, "'-lead'"),
        ({"has space": "x"}, "'has space'"),
        ({"k" * 65: "x"}, "64"),
        ({"owner": 1}, "'owner'"),
        ({"owner": None}, "'owner'"),
        ({"owner": ["a"]}, "'owner'"),
        ({"owner": "v" * 1025}, "1024"),
        ({f"k{i}": "v" for i in range(33)}, "32"),
    ],
)
def test_bad_metadata_is_refused_with_the_offender_named(mapping: dict, fragment: str) -> None:
    with pytest.raises(RuntimeMetadataInvalid) as excinfo:
        parse_runtime_metadata(mapping)
    assert fragment in str(excinfo.value)


def test_metadata_invalid_is_a_value_error() -> None:
    """The route maps it to a 422 the way it maps `RuntimeDetailsInvalid`."""
    assert issubclass(RuntimeMetadataInvalid, ValueError)


def test_metadata_at_the_limits_parses() -> None:
    parsed = parse_runtime_metadata({f"k{i:02d}": "v" * 1024 for i in range(32)})
    assert len(parsed) == 32
    assert parse_runtime_metadata({"A" + "b" * 63: "x"})


# --------------------------------------------------------------------------------- store


async def _seed_runtime(session: AsyncSession, rt: str = RuntimeType.CODEX_SUBSCRIPTION) -> Runtime:
    row = Runtime(
        organization_id=await default_organization_id(session),
        slug=f"rt-{uuid4().hex[:12]}",
        name="Team Codex",
        type=rt,
    )
    session.add(row)
    await session.flush()
    return row


@pytest.mark.postgres
@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.usefixtures("master_key")
async def test_runtime_credentials_round_trip(async_session: AsyncSession) -> None:
    runtime = await _seed_runtime(async_session)

    await store.put_for_runtime(async_session, runtime.id, "CODEX_AUTH_JSON", '{"tokens": 1}')
    await store.put_for_runtime(async_session, runtime.id, "OTHER", "first")
    await store.put_for_runtime(async_session, runtime.id, "OTHER", "second")  # overwrite

    got = await store.get_for_runtime(async_session, runtime.id, "CODEX_AUTH_JSON")
    assert isinstance(got, SecretStr)
    assert got.get_secret_value() == '{"tokens": 1}'
    other = await store.get_for_runtime(async_session, runtime.id, "OTHER")
    assert other is not None and other.get_secret_value() == "second"
    assert await store.get_for_runtime(async_session, runtime.id, "MISSING") is None
    assert list(await store.list_names_for_runtime(async_session, runtime.id)) == [
        "CODEX_AUTH_JSON",
        "OTHER",
    ]

    resolved = await store.resolve_for_runtime(async_session, runtime.id)
    assert {k: v.get_secret_value() for k, v in resolved.items()} == {
        "CODEX_AUTH_JSON": '{"tokens": 1}',
        "OTHER": "second",
    }

    assert await store.delete_for_runtime(async_session, runtime.id, "OTHER") is True
    assert await store.delete_for_runtime(async_session, runtime.id, "OTHER") is False
    assert list(await store.list_names_for_runtime(async_session, runtime.id)) == [
        "CODEX_AUTH_JSON"
    ]
    await async_session.rollback()


@pytest.mark.postgres
@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.usefixtures("master_key")
async def test_a_project_blob_never_authenticates_as_a_runtime_credential(
    async_session: AsyncSession,
) -> None:
    """The project AAD is bare `<id>:<name>`; equal integer ids across the two tables must
    not let a blob copied from `secret` decrypt under `runtime_credential`."""
    runtime = await _seed_runtime(async_session)
    name = "CODEX_AUTH_JSON"
    smuggled = encrypt("stolen", aad=f"{runtime.id}:{name}".encode())
    async_session.add(
        RuntimeCredential(
            runtime_id=runtime.id,
            name=name,
            value_ciphertext=smuggled.ciphertext,
            enc_key_id=smuggled.enc_key_id,
        )
    )
    await async_session.flush()

    with pytest.raises(DecryptionError):
        await store.get_for_runtime(async_session, runtime.id, name)
    await async_session.rollback()


@pytest.mark.postgres
@pytest.mark.asyncio(loop_scope="session")
@pytest.mark.usefixtures("master_key")
async def test_two_runtimes_do_not_see_each_other(async_session: AsyncSession) -> None:
    one = await _seed_runtime(async_session)
    two = await _seed_runtime(async_session, RuntimeType.CODEX_API)
    await store.put_for_runtime(async_session, one.id, "CODEX_AUTH_JSON", "one")

    assert await store.get_for_runtime(async_session, two.id, "CODEX_AUTH_JSON") is None
    assert await store.resolve_for_runtime(async_session, two.id) == {}
    rows = (
        await async_session.execute(
            select(RuntimeCredential.runtime_id).where(RuntimeCredential.runtime_id == one.id)
        )
    ).all()
    assert len(rows) == 1
    await async_session.rollback()


@pytest.mark.parametrize(
    ("rt", "isolation", "registry"),
    [
        ("codex_api", "e2b", "codex_e2b"),
        ("codex_subscription", "e2b", "codex_e2b"),
        ("claude_api", "e2b", "claude_e2b"),
        ("claude_subscription", "local_cli", "claude_cli"),
    ],
)
def test_isolation_picks_the_sandbox_the_type_runs_on(
    rt: str, isolation: str, registry: str
) -> None:
    assert row_registry_kind(rt, {"isolation": isolation}) == registry


def test_a_row_written_before_isolation_existed_runs_on_the_host() -> None:
    assert row_registry_kind("claude_api", {}) == "claude_cli"


def test_isolation_is_validated_at_the_boundary() -> None:
    parsed = parse_runtime_details("claude_api", {"isolation": "e2b"})
    assert parsed.model_dump(exclude_defaults=True) == {"isolation": "e2b"}
    with pytest.raises(RuntimeDetailsInvalid):
        parse_runtime_details("claude_api", {"isolation": "firecracker"})


def test_permitting_an_e2b_runtime_never_refuses_the_projects_host_runtimes() -> None:
    settings = {"runtime_allowlist": ["claude-e2b"]}
    assert runtime_permitted(settings, "codex-host", "local_cli")
    assert runtime_permitted(settings, "claude-e2b", "e2b")
    assert not runtime_permitted(settings, "other-e2b", "e2b")
    assert not runtime_permitted({}, "claude-e2b", "e2b")


def test_codex_bedrock_is_platform_minted() -> None:
    assert is_platform_minted("codex_bedrock") is True
    assert requires_stored_credential("codex_bedrock") is False
    assert is_platform_minted("codex_api") is False
    assert requires_stored_credential("codex_api") is True


def test_codex_bedrock_is_offered_on_the_host_only() -> None:
    """Its details refuse e2b, so an offer of it on e2b would enable a card no create passes."""
    assert parse_offers(["codex_bedrock@local_cli"]) == {"codex_bedrock@local_cli"}
    with pytest.raises(ValueError, match="unknown runtime offer"):
        parse_offers(["codex_bedrock@e2b"])


def test_a_codex_bedrock_create_carries_no_vendor_key() -> None:
    """The host role mints the bearer at each launch; the row never stores one."""
    assert create_credential_names("codex_bedrock", "local_cli") == frozenset()
    assert create_credential_names("codex_api", "local_cli") == {"OPENAI_API_KEY"}


def test_codex_bedrock_refuses_e2b_isolation() -> None:
    with pytest.raises(RuntimeDetailsInvalid, match="E2B"):
        parse_runtime_details("codex_bedrock", {"isolation": "e2b", "region": "us-east-1"})


def test_codex_bedrock_names_its_region_and_an_unnamed_stored_row_stays_govcloud() -> None:
    """GovCloud was one customer's default, and a row created on it stored `{}`: a new row
    must name its partition, and an old one must not move to another."""
    from orchestrator.factorial.runtimes import bedrock_model_of, bedrock_region_of

    with pytest.raises(RuntimeDetailsInvalid, match="region"):
        parse_runtime_details("codex_bedrock", {})
    parsed = parse_runtime_details("codex_bedrock", {"region": "us-gov-west-1"})
    assert parsed.model_dump(exclude_defaults=True) == {"region": "us-gov-west-1"}
    assert bedrock_region_of({}) == "us-gov-west-1"
    assert bedrock_model_of({}) == "us-gov.xai.grok-4.6"
    assert bedrock_model_of({"region": "us-east-1"}) == "us.xai.grok-4.6"


def test_codex_bedrock_accepts_commercial_region_and_model() -> None:
    parsed = parse_runtime_details(
        "codex_bedrock", {"region": "us-east-1", "model": "us.xai.grok-4.6"}
    )
    dumped = parsed.model_dump(mode="json", exclude_defaults=True)
    assert dumped["region"] == "us-east-1"
    assert dumped["model"] == "us.xai.grok-4.6"


def test_codex_bedrock_refuses_a_partition_mismatch() -> None:
    with pytest.raises(RuntimeDetailsInvalid, match="us-gov"):
        parse_runtime_details(
            "codex_bedrock", {"region": "us-east-1", "model": "us-gov.xai.grok-4.6"}
        )
    with pytest.raises(RuntimeDetailsInvalid, match="commercial"):
        parse_runtime_details(
            "codex_bedrock", {"region": "us-gov-west-1", "model": "us.xai.grok-4.6"}
        )


def test_a_value_is_trimmed_and_must_fit_a_header() -> None:
    assert credential_value("OPENAI_API_KEY", "  sk-abc\n") == "sk-abc"
    with pytest.raises(CredentialValueInvalid) as refused:
        credential_value("OPENAI_API_KEY", "sk-ab c")
    assert "sk-ab" not in str(refused.value)
    bundle = '{\n  "tokens": {\n    "access_token": "eyJ.x.y"\n  }\n}\n'
    assert credential_value("CODEX_AUTH_JSON", bundle) == bundle.strip()
    with pytest.raises(
        CredentialValueInvalid, match=r"^the auth\.json has no tokens\.access_token$"
    ):
        credential_value("CODEX_AUTH_JSON", '{"tokens": {}}')
    with pytest.raises(CredentialValueInvalid, match=r"^the auth\.json does not parse$"):
        credential_value("CODEX_AUTH_JSON", "[" * 100_000)
    for name in ("OPENAI_API_KEY", "CODEX_AUTH_JSON", "SERVICE_ACCOUNT_JSON"):
        with pytest.raises(CredentialValueInvalid, match=r"^the value is empty$"):
            credential_value(name, " \n\t")
    pem = "-----BEGIN PRIVATE KEY-----\nMIIE\n-----END PRIVATE KEY-----\n"
    assert credential_value("DOCS_SIGNING_KEY", pem) == pem
