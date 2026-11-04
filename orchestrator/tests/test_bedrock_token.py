"""AWS `bedrock-api-key-` bearer mint. No network: the golden key is the byte pin."""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import pytest

from orchestrator.factorial.bedrock_token import (
    ENV_BEARER,
    AwsSession,
    BedrockMintError,
    bedrock_base_url,
    mint_bearer,
    presign_bearer,
)

# The golden value was produced independently from these inputs by a reference implementation.
_SESSION = AwsSession(
    access_key_id="ASIAEXAMPLE",
    secret_access_key="secretsecretsecretsecret",
    session_token="session-token-value",
)
_SIGN_AT = datetime(2026, 7, 21, 21, 0, 0, tzinfo=UTC)
_GOLDEN = (
    "bedrock-api-key-YmVkcm9jay5hbWF6b25hd3MuY29tLz9BY3Rpb249Q2FsbFdpdGhCZWFyZXJUb2tlbiZYLUFte"  # gitleaks:allow  # noqa: E501
    "i1BbGdvcml0aG09QVdTNC1ITUFDLVNIQTI1NiZYLUFtei1DcmVkZW50aWFsPUFTSUFFWEFNUExFJTJGMjAyNjA3Mj"
    "ElMkZ1cy1nb3Ytd2VzdC0xJTJGYmVkcm9jayUyRmF3czRfcmVxdWVzdCZYLUFtei1EYXRlPTIwMjYwNzIxVDIxMDA"
    "wMFomWC1BbXotRXhwaXJlcz00MzIwMCZYLUFtei1TZWN1cml0eS1Ub2tlbj1zZXNzaW9uLXRva2VuLXZhbHVlJlgt"
    "QW16LVNpZ25lZEhlYWRlcnM9aG9zdCZYLUFtei1TaWduYXR1cmU9OTNjZmZlMTAzNzdmMWMxODFmOWNlMDc2NjkxO"
    "TlmNmZhMDVlZWYxZTUzNzM5MmYxZjM5MjIwNGUzNGNhMzc1NyZWZXJzaW9uPTE="
)


def test_presign_matches_the_golden_value() -> None:
    assert presign_bearer(_SESSION, region="us-gov-west-1", now=_SIGN_AT) == _GOLDEN


def test_presign_payload_is_the_fixed_host_and_action() -> None:
    raw = base64.b64decode(
        presign_bearer(_SESSION, region="us-gov-west-1", now=_SIGN_AT).removeprefix(
            "bedrock-api-key-"
        )
    ).decode()
    assert raw.startswith("bedrock.amazonaws.com/?")
    assert raw.endswith("&Version=1")
    parsed = urlparse("https://" + raw.removesuffix("&Version=1"))
    query = parse_qs(parsed.query)
    assert parsed.hostname == "bedrock.amazonaws.com"
    assert query["Action"] == ["CallWithBearerToken"]
    assert query["X-Amz-Expires"] == ["43200"]
    assert "20260721/us-gov-west-1/bedrock/aws4_request" in query["X-Amz-Credential"][0]


def test_mint_uses_process_env_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", _SESSION.access_key_id)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", _SESSION.secret_access_key)
    monkeypatch.setenv("AWS_SESSION_TOKEN", _SESSION.session_token or "")
    monkeypatch.setenv("AWS_REGION", "us-gov-west-1")
    monkeypatch.delenv(ENV_BEARER, raising=False)
    assert mint_bearer(now=_SIGN_AT) == _GOLDEN


def test_mint_falls_back_to_an_exported_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
    monkeypatch.setenv(ENV_BEARER, "bedrock-api-key-exported")
    monkeypatch.setattr(
        "orchestrator.factorial.bedrock_token._session_from_imds", lambda: None
    )
    assert mint_bearer() == "bedrock-api-key-exported"


def test_mint_refuses_when_the_host_has_no_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    monkeypatch.delenv(ENV_BEARER, raising=False)
    monkeypatch.setattr(
        "orchestrator.factorial.bedrock_token._session_from_imds", lambda: None
    )
    with pytest.raises(BedrockMintError, match="no AWS role"):
        mint_bearer()


def test_base_url_defaults_to_a_commercial_region() -> None:
    assert bedrock_base_url() == "https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1"
