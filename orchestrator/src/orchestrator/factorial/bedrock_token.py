"""Mint a short-lived Bedrock bearer from the factory host's AWS identity.

The token is AWS's `bedrock-api-key-` short-term key format: a SigV4 query
presign of `CallWithBearerToken` on the fixed host `bedrock.amazonaws.com`, then
`bedrock-api-key-` + standard base64. Nothing is stored. Codex reads the result from
`AWS_BEARER_TOKEN_BEDROCK`.

Identity resolution, in order:
1. explicit session credentials passed by a test
2. `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / optional `AWS_SESSION_TOKEN` in the process
   environment (EC2 instance profile tools often export these; the app never writes them)
3. EC2 IMDSv2
4. an already-exported `AWS_BEARER_TOKEN_BEDROCK` (a laptop stand-in)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final
from urllib.parse import quote
from urllib.request import Request, urlopen

__all__ = [
    "DEFAULT_MODEL",
    "DEFAULT_REGION",
    "ENV_BEARER",
    "GOVCLOUD_MODEL",
    "GOVCLOUD_REGION",
    "PROVIDER_NAME",
    "BedrockMintError",
    "bedrock_base_url",
    "mint_bearer",
    "presign_bearer",
]

ENV_BEARER: Final = "AWS_BEARER_TOKEN_BEDROCK"
DEFAULT_REGION: Final = "us-east-1"
DEFAULT_MODEL: Final = "us.xai.grok-4.6"
GOVCLOUD_REGION: Final = "us-gov-west-1"
GOVCLOUD_MODEL: Final = "us-gov.xai.grok-4.6"
PROVIDER_NAME: Final = "bedrock-grok"
_SIGN_HOST: Final = "bedrock.amazonaws.com"
_SERVICE: Final = "bedrock"
_EXPIRES: Final = "43200"
_EMPTY_PAYLOAD_HASH: Final = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
_IMDS: Final = "http://169.254.169.254"
_IMDS_TIMEOUT: Final = 2.0


class BedrockMintError(RuntimeError):
    """The host could not produce a Bedrock bearer. Spawn and Test credential surface this."""


@dataclass(frozen=True, slots=True)
class AwsSession:
    access_key_id: str
    secret_access_key: str
    session_token: str | None = None


def bedrock_base_url(region: str = DEFAULT_REGION) -> str:
    return f"https://bedrock-runtime.{region}.amazonaws.com/openai/v1"


def _aws_quote(value: str) -> str:
    return quote(value, safe="-_.~")


def _canonical_query(params: dict[str, str]) -> str:
    return "&".join(
        f"{_aws_quote(key)}={_aws_quote(params[key])}" for key in sorted(params)
    )


def _signing_key(secret: str, datestamp: str, region: str) -> bytes:
    date_key = hmac.new(f"AWS4{secret}".encode(), datestamp.encode(), hashlib.sha256).digest()
    region_key = hmac.new(date_key, region.encode(), hashlib.sha256).digest()
    service_key = hmac.new(region_key, _SERVICE.encode(), hashlib.sha256).digest()
    return hmac.new(service_key, b"aws4_request", hashlib.sha256).digest()


def presign_bearer(
    session: AwsSession, *, region: str = DEFAULT_REGION, now: datetime | None = None
) -> str:
    """Byte-stable for the same inputs (see `test_bedrock_token`)."""
    stamp = (now or datetime.now(UTC)).astimezone(UTC)
    amz_date = stamp.strftime("%Y%m%dT%H%M%SZ")
    datestamp = stamp.strftime("%Y%m%d")
    credential = f"{session.access_key_id}/{datestamp}/{region}/{_SERVICE}/aws4_request"
    query = {
        "Action": "CallWithBearerToken",
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Credential": credential,
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": _EXPIRES,
        "X-Amz-SignedHeaders": "host",
    }
    if session.session_token:
        query["X-Amz-Security-Token"] = session.session_token
    canonical = "\n".join(
        [
            "POST",
            "/",
            _canonical_query(query),
            f"host:{_SIGN_HOST}",
            "",
            "host",
            _EMPTY_PAYLOAD_HASH,
        ]
    )
    scope = f"{datestamp}/{region}/{_SERVICE}/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(canonical.encode()).hexdigest(),
        ]
    )
    signature = hmac.new(
        _signing_key(session.secret_access_key, datestamp, region),
        string_to_sign.encode(),
        hashlib.sha256,
    ).hexdigest()
    # Emission order is the AWS SDK's, not alphabetical: Signature last among the
    # signed params, Version appended after the signed URL.
    ordered = [
        "Action",
        "X-Amz-Algorithm",
        "X-Amz-Credential",
        "X-Amz-Date",
        "X-Amz-Expires",
        "X-Amz-Security-Token",
        "X-Amz-SignedHeaders",
    ]
    parts = [
        f"{_aws_quote(key)}={_aws_quote(query[key])}"
        for key in ordered
        if key in query
    ]
    parts.append(f"{_aws_quote('X-Amz-Signature')}={_aws_quote(signature)}")
    signed = f"{_SIGN_HOST}/?{'&'.join(parts)}&Version=1"
    return "bedrock-api-key-" + base64.b64encode(signed.encode()).decode()


def _imds_json(path: str, token: str) -> object:
    request = Request(
        f"{_IMDS}{path}",
        headers={"X-aws-ec2-metadata-token": token},
        method="GET",
    )
    with urlopen(request, timeout=_IMDS_TIMEOUT) as response:
        return json.loads(response.read().decode())


def _imds_text(path: str, token: str) -> str:
    request = Request(
        f"{_IMDS}{path}",
        headers={"X-aws-ec2-metadata-token": token},
        method="GET",
    )
    with urlopen(request, timeout=_IMDS_TIMEOUT) as response:
        return response.read().decode().strip()


def _session_from_imds() -> AwsSession | None:
    try:
        token_req = Request(
            f"{_IMDS}/latest/api/token",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"},
            method="PUT",
        )
        with urlopen(token_req, timeout=_IMDS_TIMEOUT) as response:
            token = response.read().decode().strip()
        role = _imds_text("/latest/meta-data/iam/security-credentials/", token)
        if not role:
            return None
        payload = _imds_json(f"/latest/meta-data/iam/security-credentials/{role}", token)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    access = payload.get("AccessKeyId")
    secret = payload.get("SecretAccessKey")
    session = payload.get("Token")
    if not isinstance(access, str) or not isinstance(secret, str):
        return None
    return AwsSession(access, secret, session if isinstance(session, str) else None)


def resolve_session() -> AwsSession | None:
    access = os.environ.get("AWS_ACCESS_KEY_ID", "").strip()
    secret = os.environ.get("AWS_SECRET_ACCESS_KEY", "").strip()
    if access and secret:
        token = os.environ.get("AWS_SESSION_TOKEN", "").strip() or None
        return AwsSession(access, secret, token)
    return _session_from_imds()


def mint_bearer(*, region: str | None = None, now: datetime | None = None) -> str:
    """A bearer Codex can send. Raises `BedrockMintError` when the host has no AWS identity
    and no already-exported token."""
    chosen = (region or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
              or DEFAULT_REGION).strip() or DEFAULT_REGION
    session = resolve_session()
    if session is not None:
        return presign_bearer(session, region=chosen, now=now)
    existing = os.environ.get(ENV_BEARER, "").strip()
    if existing:
        return existing
    raise BedrockMintError(
        "the host has no AWS role (and no AWS_BEARER_TOKEN_BEDROCK stand-in). Attach an "
        "instance profile that can CallWithBearerToken, or export AWS_BEARER_TOKEN_BEDROCK in "
        "this process for a local smoke test"
    )
