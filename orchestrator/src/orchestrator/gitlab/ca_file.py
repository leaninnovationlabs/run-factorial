"""A CA file curl, git and Node can read.

httpx can take an SSLContext with extra CAs. Agent tools cannot: they honour
SSL_CERT_FILE / CURL_CA_BUNDLE / GIT_SSL_CAINFO. Those replace the default store,
so the file must be the system bundle plus any extra PEM — any private PKI, not a
vendor-specific list. Same file on a laptop and on a deployment.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from orchestrator.settings import GitLabSettings

__all__ = ["CA_ENV_KEYS", "extra_ca_pem", "write_run_ca_file"]

CA_ENV_KEYS: tuple[str, ...] = (
    "SSL_CERT_FILE",
    "CURL_CA_BUNDLE",
    "GIT_SSL_CAINFO",
    "REQUESTS_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
)

_SYSTEM_BUNDLES: tuple[Path, ...] = (
    Path("/etc/pki/tls/certs/ca-bundle.crt"),
    Path("/etc/ssl/certs/ca-certificates.crt"),
    Path("/etc/ssl/cert.pem"),
)


def extra_ca_pem(settings: GitLabSettings) -> str:
    """The operator's extra CA: inline PEM, or a file (PEM or a path to PEM)."""
    pem = (settings.ca_pem or "").strip()
    path = settings.ca_bundle
    if path is not None and path.exists():
        text = path.read_text()
        if text.strip().startswith("-----BEGIN"):
            pem = f"{pem}\n{text}".strip() if pem else text
        elif not pem:
            pem = text
    return pem.strip()


def _system_ca_pem() -> str:
    try:
        import certifi

        return Path(certifi.where()).read_text()
    except Exception:
        pass
    for candidate in _SYSTEM_BUNDLES:
        if candidate.exists():
            return candidate.read_text()
    return ""


def write_run_ca_file(settings: GitLabSettings, dest: Path | None = None) -> Path | None:
    """Write system CAs + extra PEMs. None when there is nothing extra to add."""
    extra = extra_ca_pem(settings)
    if not extra:
        return None
    system = _system_ca_pem()
    body = (f"{system.rstrip()}\n{extra}\n" if system.strip() else f"{extra}\n")
    target = dest if dest is not None else Path(tempfile.gettempdir()) / "factorial-gitlab-ca.pem"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body)
    target.chmod(0o644)
    return target
