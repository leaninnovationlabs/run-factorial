"""MCP tokens: HS256 JWTs whose signature only finds an `mcp_token` row (design.md §7.16).

The row decides principal, scopes, expiry and revocation. The claims are `iss`, `aud`, `jti`,
`iat` and `exp` only, so nothing can be tempted to trust a `sub` or a `scope`. The key and the
audience are derived from `FACTORIAL_API_TOKEN` (tech-stack decision 12): a rotation voids every
token, and a row's stored audience tells the token list which ones it voided.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Final

from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import OctKey
from joserfc.jwt import JWTClaimsRegistry

from orchestrator.api.sessions import derive_key

__all__ = [
    "ALG",
    "CHAT_SCOPE",
    "ISSUER",
    "LIFETIME",
    "READ_SCOPE",
    "WRITE_SCOPE",
    "audience",
    "jti_digest",
    "mint_mcp_jwt",
    "read_mcp_jwt",
]

#: Fixed rather than a URL, so a domain move keeps every token working.
ISSUER: Final = "urn:factorial:orchestrator"
ALG: Final = "HS256"
LIFETIME: Final = timedelta(days=365)
READ_SCOPE: Final = "factorial:read"
WRITE_SCOPE: Final = "factorial:write"
#: An assistant turn's: its tools write the turn's own cards and proposals, never the factory.
CHAT_SCOPE: Final = "factorial:chat"

_TOKEN_KEY: Final = b"mcp-token-v1"
_AUDIENCE_KEY: Final = b"mcp-audience-v1"
#: A minted token is under 300 bytes; anything far past that was never one.
_MAX_TOKEN: Final = 2048
_LEEWAY_SECONDS: Final = 60


def audience(api_token: str) -> str:
    return "urn:factorial:mcp:" + derive_key(api_token, _AUDIENCE_KEY)[:16].hex()


def jti_digest(jti: str) -> str:
    return hashlib.sha256(jti.encode()).hexdigest()


def _key(api_token: str) -> OctKey:
    return OctKey.import_key(derive_key(api_token, _TOKEN_KEY))


def mint_mcp_jwt(*, api_token: str, jti: str, issued_at: datetime, expires_at: datetime) -> str:
    claims = {
        "iss": ISSUER,
        "aud": audience(api_token),
        "jti": jti,
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    return jwt.encode({"alg": ALG, "typ": "JWT"}, claims, _key(api_token), algorithms=[ALG])


def read_mcp_jwt(value: str, *, api_token: str, now: datetime) -> tuple[str, str] | None:
    """`(jti, aud)` of an unexpired token this deployment signed, or None. Never raises; an
    unset api_token verifies nothing: fail closed."""
    if not api_token or not value or len(value) > _MAX_TOKEN:
        return None
    expected = audience(api_token)
    try:
        token = jwt.decode(value, _key(api_token), algorithms=[ALG])
        JWTClaimsRegistry(
            now=int(now.timestamp()),
            leeway=_LEEWAY_SECONDS,
            iss={"essential": True, "value": ISSUER},
            aud={"essential": True, "value": expected},
            exp={"essential": True},
            iat={"essential": True},
            jti={"essential": True},
        ).validate(token.claims)
        jti, aud = token.claims["jti"], token.claims["aud"]
    except (JoseError, ValueError, TypeError, KeyError, AttributeError):
        return None
    # The registry accepts a list `aud` that contains the value, and a `jti` of any type.
    if not isinstance(jti, str) or not jti or aud != expected:
        return None
    return jti, expected
