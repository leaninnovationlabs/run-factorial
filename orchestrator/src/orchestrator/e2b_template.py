"""Build the E2B sandbox templates in one E2B team, smoke each build, and print the pins that pass.

    E2B_API_KEY=e2b_... ANTHROPIC_API_KEY=... OPENAI_API_KEY=... \\
        uv run python -m orchestrator.e2b_template                              # both harnesses
    E2B_API_KEY=e2b_... OPENAI_API_KEY=... uv run python -m orchestrator.e2b_template codex
    E2B_API_KEY=e2b_... uv run python -m orchestrator.e2b_template --skip-smoke claude
    E2B_API_KEY=e2b_... ANTHROPIC_API_KEY=... \\
        uv run python -m orchestrator.e2b_template --interactive-smoke <claude name>:<build id>
    E2B_API_KEY=e2b_... OPENAI_API_KEY=... \\
        uv run python -m orchestrator.e2b_template --interactive-smoke <codex name>:<build id>

Each build installs the CLI's npm `latest`, resolved to an exact version before the build.
`CLAUDE_CLI_VERSION` or `CODEX_CLI_VERSION` names a version instead, to roll back, never one below
the floor in `MIN_VERSIONS`. The version is in the template's name (`claude-2-1-310`), so the
Runtime page's build list shows it, and each run's `agent_spawned` event records the pin it booted.

Every build skips E2B's layer cache: `e2bdev/base` is a moving tag, and a cached layer keeps the
trust store the base had when it was first built.

A pin is printed only after the build passes two smokes, both on the same E2B key. The first is
the containment smoke (`runtime.adapters.e2b.smoke.containment_smoke`, design.md §7.10), on every
build and with no vendor key: in one public sandbox from the build, the preview's uid must not get
the header the egress proxy injects while the agent's does, a server it runs on a declared port
must still answer, an undeclared port must stay closed, and a connection it opened before the
switch to contain must be cut. A preview beside a session's credentials depends on that firewall.

The second is one small session: the TUI must reach its prompt, which only its Stop hook reports,
with no first-run dialog on screen; wait there 75 s; be found by a restarted worker; come back on
the same session after a rollover; and end on `finish` with its session log translated whole, its
cost reported (claude) or its tokens (codex, which reports no cost); for codex it also prints how
long the first turn waited for a token, since its WebSocket to the API retries before HTTPS when
the egress refuses it. That takes about two minutes and bills the vendor key in the environment:
`ANTHROPIC_API_KEY` for claude, `OPENAI_API_KEY` for codex. Without the key the harness is refused
before it builds, unless `--skip-smoke` is passed. A pass is recorded on the build as the E2B tag
`interactive-ok-<build id>`, which the Runtime page shows and the worker requires before it runs
a session on the build (decision 25). On a tagged codex build a person may type into a run that
holds a ChatGPT subscription login until its park, a cost design §7.14 accepts.

A failed smoke prints why and marks the build FAILED, and the exit status is 1; the build itself
stays in the team and in the Runtime page's list, so do not pick it. `--skip-smoke` prints the pin
marked UNVERIFIED, and the worker refuses it until `--interactive-smoke <pin>` records a pass. That
mode gives an existing build both smokes and the tag, without building: a `--skip-smoke` build or
one whose tag E2B refused to record. Each harness reports as soon as it is known, so a later
failure never hides a good pin.

Delete a retired template in E2B only once no runtime pins it: E2B deletes whole templates, and
refuses while one of its sandboxes is running or paused.

A template is private to the team that built it, so each team an E2B runtime spawns into gets
its own builds. `E2B_API_KEY` names that team; `TEMPLATE_CPU_COUNT` and
`TEMPLATE_MEMORY_MB` come from `.env` like every other setting. Each build then appears in the
template list of every runtime holding that team's key. Every smoke's sandbox is killed after it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
import uuid
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import fields
from functools import partial
from typing import Any, Final

import httpx

from orchestrator.factorial.runtimes import TEMPLATE_BUILD_RE
from orchestrator.settings import Settings
from runtime.adapters.e2b import smoke as e2b_smoke
from runtime.adapters.e2b.adapter import E2bAdapter
from runtime.adapters.e2b.client import E2bSandboxClient, record_session_verdict
from runtime.adapters.e2b.smoke import (
    VENDOR_KEYS,
    containment_smoke,
    session_smoke_run,
    smoke_spec,
)
from runtime.adapters.e2b.template import (
    PACKAGES,
    check_floor,
    names_harness,
    template_name,
)

#: Per harness, the environment variable that names a version instead of npm's `latest`.
VERSION_OVERRIDES: Final = {"claude": "CLAUDE_CLI_VERSION", "codex": "CODEX_CLI_VERSION"}
SKIP_SMOKE: Final = "--skip-smoke"
INTERACTIVE_SMOKE: Final = "--interactive-smoke"


def latest_version(package: str) -> str:
    """What npm's `latest` dist-tag names now, read from the registry: no npm binary needed."""
    response = httpx.get(f"https://registry.npmjs.org/{package}/latest", timeout=30)
    return str(response.raise_for_status().json()["version"])


def resolve_version(
    harness: str, env: Mapping[str, str], latest: Callable[[str], str] = latest_version
) -> str:
    """The exact version to install; it reaches a root shell, so both sources are checked."""
    return check_floor(harness, env.get(VERSION_OVERRIDES[harness]) or latest(PACKAGES[harness]))


#: (harness, version) -> the finished build's `<name>:<build id>`.
Builder = Callable[[str, str], str]
#: (harness, pin, vendor key) -> every reason not to trust the build's session; empty: it passed.
Smoker = Callable[[str, str, str], Coroutine[Any, Any, list[str]]]
#: pin -> every property the preview's firewall failed to hold on the build; empty means it held.
Container = Callable[[str], Coroutine[Any, Any, list[str]]]
#: pin -> the build tagged as having passed the session smoke.
Recorder = Callable[[str], Coroutine[Any, Any, None]]


def run(
    harnesses: list[str],
    *,
    env: Mapping[str, str],
    skip_smoke: bool,
    build: Builder,
    containment: Container,
    session_smoke: Smoker,
    record: Recorder,
    latest: Callable[[str], str] = latest_version,
) -> int:
    """Build, smoke and report each harness; 1 when any of them yields no usable pin."""
    unknown = [h for h in harnesses if h not in PACKAGES]
    if unknown:
        sys.exit(f"unknown harness {', '.join(unknown)}; expected {', '.join(PACKAGES)}")
    if not skip_smoke:
        keyless = [VENDOR_KEYS[h] for h in harnesses if not env.get(VENDOR_KEYS[h])]
        if keyless:
            sys.exit(
                f"{', '.join(keyless)} not set: each build's session smoke bills it. Export it, or "
                f"pass {SKIP_SMOKE} to print pins nobody has run"
            )
    versions = {harness: resolve_version(harness, env, latest) for harness in harnesses}
    failed = False
    for harness in harnesses:
        version = versions[harness]
        package = PACKAGES[harness].rsplit("/", 1)[-1]
        # Each harness reports as soon as it is known, so a later build failing never hides an
        # earlier pin that passed.
        try:
            pin = build(harness, version)
        except Exception as exc:
            failed = True
            print(f"\n{harness} template: build failed ({type(exc).__name__}: {exc})", flush=True)
            continue
        if skip_smoke:
            print(
                f"\n{harness} template: {pin}  ({package} {version}, UNVERIFIED: cannot run "
                f"until {INTERACTIVE_SMOKE} {pin})"
            )
            continue
        problems = _contained(containment, pin)
        if problems:
            failed = True
            _print_failed(harness, pin, problems, "FAILED its containment smoke; do not pick it")
            continue
        problems = _verdict(session_smoke, record, harness, pin, env)
        if problems:
            failed = True
            _print_failed(harness, pin, problems)
            continue
        print(f"\n{harness} template: {pin}  ({package} {version}, session ok)", flush=True)
    return 1 if failed else 0


def smoke_existing(
    pin: str,
    *,
    env: Mapping[str, str],
    containment: Container,
    session_smoke: Smoker,
    record: Recorder,
) -> int:
    """Both smokes, and the tag on a pass, for a build that already exists: a `--skip-smoke`
    build ran neither, and the tag lets previews run beside a session's credentials."""
    if not TEMPLATE_BUILD_RE.fullmatch(pin):
        sys.exit(f"{pin!r} is not a pin: expected <name>:<build id>, as the Runtime page lists")
    harness = next((h for h in PACKAGES if names_harness(pin.split(":")[0], h)), "")
    if not harness:
        expected = " or ".join(sorted(PACKAGES))
        sys.exit(f"{pin} is not a {expected} build: only those run a session on E2B")
    if not env.get(VENDOR_KEYS[harness]):
        sys.exit(f"{VENDOR_KEYS[harness]} not set: the session bills it")
    problems = _contained(containment, pin)
    if problems:
        _print_failed(harness, pin, problems, "FAILED its containment smoke; do not pick it")
        return 1
    problems = _verdict(session_smoke, record, harness, pin, env)
    if problems:
        _print_failed(harness, pin, problems)
        return 1
    print(f"\n{harness} template {pin}: session ok, recorded on the build", flush=True)
    return 0


def _print_failed(
    harness: str, pin: str, problems: list[str], verdict: str = "FAILED; the worker refuses it"
) -> None:
    print(f"\n{harness} template {pin} {verdict}:")
    print(*(f"  - {problem}" for problem in problems), sep="\n")


def _contained(containment: Container, pin: str) -> list[str]:
    try:
        return asyncio.run(containment(pin))
    except Exception as exc:  # a sandbox the check could not set up proves nothing either
        return [f"the containment smoke raised {type(exc).__name__}: {exc}"]


def _verdict(
    session_smoke: Smoker, record: Recorder, harness: str, pin: str, env: Mapping[str, str]
) -> list[str]:
    """Why the build may not run a session; empty once its pass is recorded on it."""
    try:
        problems = asyncio.run(session_smoke(harness, pin, env[VENDOR_KEYS[harness]]))
    except Exception as exc:  # a refused spawn is a failed smoke, and the next harness runs
        return [f"the session smoke raised {type(exc).__name__}: {exc}"]
    if problems:
        return [f"the session smoke failed: {problem}" for problem in problems]
    try:
        asyncio.run(record(pin))
    except Exception as exc:
        return [
            f"the session smoke passed, but E2B did not record it ({type(exc).__name__}: "
            f"{exc}); record it with {INTERACTIVE_SMOKE} {pin}"
        ]
    return []


def _build(harness: str, version: str, *, key: str) -> str:
    from e2b import Template

    from runtime.adapters.e2b.template import definition

    e2b = Settings().runtime.e2b
    info = Template.build(
        definition(harness, version),
        template_name(harness, version),
        cpu_count=e2b.template_cpu_count,
        memory_mb=e2b.template_memory_mb,
        api_key=key,
        skip_cache=True,
        on_build_logs=lambda entry: print(entry, flush=True),
    )
    return f"{info.name}:{info.build_id}"


async def _containment(pin: str, *, key: str) -> list[str]:
    verdict = await containment_smoke(E2bSandboxClient(key), pin)
    failed = [field.name for field in fields(verdict) if not getattr(verdict, field.name)]
    return [f"{name} did not hold" for name in failed]


async def _session_smoke(harness: str, pin: str, model_key: str, *, key: str) -> list[str]:
    """The session smoke on an adapter of its own, whose sandboxes are killed after it: a run
    parks its sandbox when it ends, and no reaper serves this deployment. `fresh` is what a
    restarted worker would build: another adapter on the same state root and team."""
    from runtime.adapters.e2b import ClaudeE2bAdapter, CodexE2bAdapter

    adapter_class = {"claude": ClaudeE2bAdapter, "codex": CodexE2bAdapter}[harness]
    client = E2bSandboxClient(key)
    deployment = f"template-smoke-{uuid.uuid4().hex[:12]}"
    state_root = tempfile.mkdtemp(prefix="e2b-template-smoke-")

    def fresh() -> E2bAdapter:
        return adapter_class(
            client=client,
            template=pin,
            state_root=state_root,
            deployment=deployment,
            poll_interval=1,
        )

    try:
        spec = smoke_spec(harness, model_key)
        _, problems = await session_smoke_run(fresh(), spec, reattach=fresh)
    finally:
        await _discard(client, deployment)
    return problems


async def _discard(client: E2bSandboxClient, deployment: str) -> None:
    """Kill the smoke's sandboxes. A failure only warns: the verdict is already known, and a
    sandbox left behind is the operator's to delete in E2B."""
    try:
        left = [s.sandbox_id for s in await client.list({"factorial_deployment": deployment})]
    except Exception as exc:
        print(
            f"warning: the smoke's sandboxes (metadata factorial_deployment={deployment}) could "
            f"not be listed to kill them ({type(exc).__name__}); delete them in E2B",
            flush=True,
        )
        return
    kept = []
    for sandbox_id in left:
        try:
            await client.kill(sandbox_id)
        except Exception:
            kept.append(sandbox_id)
    if kept:
        print(f"warning: sandboxes {', '.join(kept)} were not killed; delete them in E2B")


def main() -> None:
    logging.basicConfig(format="%(message)s")
    # The smoke logs at INFO what is no failure but worth reading, such as codex's first-token wait.
    logging.getLogger(e2b_smoke.__name__).setLevel(logging.INFO)
    key = os.environ.get("E2B_API_KEY", "")
    if not key:
        sys.exit("E2B_API_KEY is not set: export the key of the E2B team to build in")
    args = sys.argv[1:]
    if args[:1] == [INTERACTIVE_SMOKE]:
        if len(args) != 2:
            sys.exit(f"usage: {INTERACTIVE_SMOKE} <name>:<build id>")
        sys.exit(
            smoke_existing(
                args[1],
                env=os.environ,
                containment=partial(_containment, key=key),
                session_smoke=partial(_session_smoke, key=key),
                record=partial(record_session_verdict, key),
            )
        )
    harnesses = [arg for arg in args if arg != SKIP_SMOKE] or list(PACKAGES)
    sys.exit(
        run(
            harnesses,
            env=os.environ,
            skip_smoke=SKIP_SMOKE in args,
            build=partial(_build, key=key),
            containment=partial(_containment, key=key),
            session_smoke=partial(_session_smoke, key=key),
            record=partial(record_session_verdict, key),
        )
    )


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    main()
