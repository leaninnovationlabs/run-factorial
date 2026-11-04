"""The sandbox templates E2B runs boot from, one per harness and CLI version (decision 25).

Each installs one exact version of one CLI with its updater off, and says which: the version is
in the template's name and in `FACTORIAL_CLI_VERSION`. E2B's own `claude` and `codex` templates
are not the base: they carry whatever CLI was latest when E2B last rebuilt them. Each also
installs a pinned `glab` for the GitLab agents. No template holds a credential.
`python -m orchestrator.e2b_template` builds them from settings.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Final

from runtime.adapters.e2b.layout import AGENT_USER, CODEX_NATIVE

if TYPE_CHECKING:
    from e2b.template.main import TemplateBuilder

PACKAGES: Final = {
    "claude": "@anthropic-ai/claude-code",
    "codex": "@openai/codex",
}

#: E2B's base image ships Node 20, and claude-code needs 22 or newer. An exact release, checked
#: against nodejs.org's published sums, so a build does not drift with the mirror. The agent's
#: `env -i` drops the base's NODE_OPTIONS (--use-openssl-ca), so Node trusts its own roots.
NODE_VERSION: Final = "22.23.4"

#: The GitLab starter agents open merge requests with glab. An exact release, checked against its
#: published sums, so a build does not drift with whatever release is latest.
GLAB_VERSION: Final = "1.123.0"

#: Both CLIs carry their own roots; earlier releases trust the compromised eMudhra chains.
MIN_VERSIONS: Final = {"claude": "2.1.304", "codex": "0.164.0"}

FIXED_CA_CERTIFICATES: Final = "20250419~deb12u2"
DISTRUSTED_OS_ROOTS: Final = ("emSign ECC Root CA - G3", "AAA Certificate Services")
#: Found first, so a listing the match cannot read (openssl 3.0 prints `CN = X`, 3.5 `CN=X`)
#: fails the build instead of passing every check.
PRESENT_OS_ROOT: Final = "ISRG Root X1"
DISTRUSTED_NODE_ROOTS: Final = (
    "emSign ECC Root CA - G3",
    "emSign Root CA - C1",
    "emSign ECC Root CA - C3",
)

#: Lowercase so it maps onto a template name E2B accepts; it also reaches a root shell.
VERSION_RE: Final = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(-[0-9a-z]+(\.[0-9a-z]+)*)?")


def check_version(version: str) -> str:
    if not VERSION_RE.fullmatch(version):
        raise ValueError(f"{version!r} is not an exact CLI version such as 2.1.310")
    return version


def check_floor(harness: str, version: str) -> str:
    floor = MIN_VERSIONS[harness]
    if _release_order(version) < _release_order(floor):
        raise ValueError(
            f"{harness} {version} predates {floor}, the first release whose own root store "
            "no longer trusts the compromised eMudhra chains"
        )
    return version


def _release_order(version: str) -> tuple[int, int, int, bool]:
    """A prerelease sorts before its release, so `2.1.304-rc.1` is below a `2.1.304` floor."""
    release, _, prerelease = check_version(version).partition("-")
    major, minor, patch = (int(part) for part in release.split("."))
    return major, minor, patch, not prerelease


def template_name(harness: str, version: str) -> str:
    """`claude-2-1-310`, the name the Runtime page lists a build under. E2B shows it under the
    team's own name, so it carries no prefix of its own."""
    return f"{harness}-{check_version(version).replace('.', '-')}"


def names_harness(template: str, harness: str) -> bool:
    """Whether `template` is a name `template_name` gives `harness`'s builds, or one it gave them
    under a prefix (`factorial-claude-2-1-284`)."""
    return re.search(rf"(?:^|-){re.escape(harness)}-[0-9][0-9a-z-]*$", template) is not None


#: Finds `PRESENT_OS_ROOT` first, as the OS-store guard does, so a subject format it cannot read
#: fails the build.
_NODE_ROOTS_GUARD: Final = (
    'const { X509Certificate } = require("crypto");'
    ' const subjects = require("tls").rootCertificates'
    ' .map((pem) => new X509Certificate(pem).subject.split("\\n"));'
    " const has = (cn) => subjects.some((lines) => lines.includes(`CN=${cn}`));"
    f' if (!has("{PRESENT_OS_ROOT}")) {{'
    f' console.error("Node lists no {PRESENT_OS_ROOT}: this guard cannot read it");'
    " process.exit(1); }"
    f" const bad = {json.dumps(list(DISTRUSTED_NODE_ROOTS))}.filter(has);"
    ' if (bad.length) { console.error("Node trusts distrusted roots:", bad); process.exit(1); }'
)


#: Fails the build on a stale base, which a cached layer can keep.
_OS_STORE_GUARD: Final = (
    "set -eu; v=$(dpkg-query -W -f='${Version}' ca-certificates)"
    f'; dpkg --compare-versions "$v" ge {FIXED_CA_CERTIFICATES}'
    f' || {{ echo "ca-certificates $v predates {FIXED_CA_CERTIFICATES}: the base is stale"'
    " >&2; exit 1; }"
    "; subjects=$(openssl crl2pkcs7 -nocrl -certfile /etc/ssl/certs/ca-certificates.crt"
    " | openssl pkcs7 -print_certs -noout)"
    '; has() { printf \'%s\\n\' "$subjects" | grep -qE "CN ?= ?$1(,|$)"; }'
    f"; has '{PRESENT_OS_ROOT}'"
    f' || {{ echo "the store listing lacks {PRESENT_OS_ROOT}: this guard cannot read it" >&2'
    "; exit 1; }"
    f"; for root in {' '.join(repr(root) for root in DISTRUSTED_OS_ROOTS)}; do"
    ' if has "$root"; then echo "the OS store still trusts $root" >&2; exit 1; fi; done'
)


def _install_node(version: str) -> str:
    tarball = f"node-v{version}-linux-x64.tar.xz"
    dist = f"https://nodejs.org/dist/v{version}"
    return (
        "set -eu; cd /tmp"
        f" && curl -fsSLO {dist}/{tarball} && curl -fsSLO {dist}/SHASUMS256.txt"
        f" && grep ' {tarball}$' SHASUMS256.txt | sha256sum -c -"
        # The base keeps Node 20's npm here too; unpacking over it leaves an npm that exits 1.
        " && rm -rf /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/corepack"
        " /usr/local/bin/npm /usr/local/bin/npx /usr/local/bin/corepack"
        f" && tar -xJf {tarball} -C /usr/local --strip-components=1"
        f" && rm {tarball} SHASUMS256.txt"
        # The base's own Node must not shadow this one on the agent's PATH.
        f' && test "$(command -v node)" = /usr/local/bin/node'
        f' && test "$(node --version)" = "v{version}"'
        f" && node -e '{_NODE_ROOTS_GUARD}'"
    )


def _install_glab(version: str) -> str:
    tarball = f"glab_{version}_linux_amd64.tar.gz"
    release = f"https://gitlab.com/gitlab-org/cli/-/releases/v{version}/downloads"
    return (
        "set -eu; cd /tmp"
        f" && curl -fsSLO {release}/{tarball} && curl -fsSLO {release}/checksums.txt"
        f" && grep ' {tarball}$' checksums.txt | sha256sum -c -"
        f""" && member=$(tar -tzf {tarball} | grep -E '(^|/)glab$' || true)"""
        """ && { [ -n "$member" ] && [ "$(printf '%s\\n' "$member" | wc -l)" -eq 1 ]"""
        f""" || {{ echo "expected one glab in {tarball}, found: $member" >&2; false; }}; }}"""
        f""" && tar -xzf {tarball} -O "$member" > /usr/local/bin/glab"""
        " && chmod 0755 /usr/local/bin/glab"
        f" && rm {tarball} checksums.txt"
        ' && test "$(command -v glab)" = /usr/local/bin/glab'
        # A temporary HOME keeps root's glab config out of the template.
        ' && home=$(mktemp -d)'
        f""" && HOME="$home" glab --version | grep -qF '{version}'"""
        ' && rm -rf "$home"'
    )


def link_native(version: str, link: str = CODEX_NATIVE) -> str:
    """Links the codex binary npm vendors at `link`, for a session to run. A change
    in npm's layout, or a binary of another version, fails the build rather than a run."""
    return (
        """bin=$(find "$(npm root -g)/@openai" -type f -path '*/vendor/*/bin/codex')"""
        """ && { [ -n "$bin" ] && [ "$(printf '%s\\n' "$bin" | wc -l)" -eq 1 ]"""
        """ || { echo "expected one vendored codex binary, found: $bin" >&2; false; }; }"""
        f' && ln -sf "$bin" {link}'
        f' && test "$({link} --version)" = "codex-cli {check_version(version)}"'
    )


def definition(harness: str, version: str) -> TemplateBuilder:
    from e2b import Template

    steps = [
        _install_node(NODE_VERSION),
        f"useradd --create-home --shell /bin/bash {AGENT_USER}",
        # --engine-strict: a CLI that wants a newer Node fails the build, not a run.
        f"npm install -g --engine-strict {PACKAGES[harness]}@{check_floor(harness, version)}",
    ]
    if harness == "codex":
        steps.append(link_native(version))
    return (
        Template()
        .from_base_image()
        .apt_install(["tmux", "ca-certificates"], no_install_recommends=True)
        .run_cmd(_OS_STORE_GUARD, user="root")
        # Its own layer: joined into the steps below, its `set -eu;` would let the `&&` chain
        # before it fail without failing the build.
        .run_cmd(_install_glab(GLAB_VERSION), user="root")
        # `useradd`, not `set_user`: that would also make the agent the template's default user,
        # and every command here names its user explicitly.
        .run_cmd(steps, user="root")
        .set_envs({"DISABLE_AUTOUPDATER": "1", "FACTORIAL_CLI_VERSION": version})
    )
