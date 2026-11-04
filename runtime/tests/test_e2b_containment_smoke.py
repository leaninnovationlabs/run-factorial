"""The containment smoke's sequence and verdict against the fake. The fake plays commands, so
this pins what the probes ask and how their output is read; only a real build proves the kernel
does it (`scripts/smoke/e2b-containment-smoke.py`)."""

from __future__ import annotations

from dataclasses import asdict

import pytest
from fake_e2b import FakeSandboxClient

from runtime.adapters.e2b.client import CommandOutput
from runtime.adapters.e2b.layout import FIREWALL
from runtime.adapters.e2b.smoke import _CLIENT_PY, ContainmentVerdict, containment_smoke


async def _fetch(url: str) -> tuple[int, str]:
    if url.startswith("https://9090-"):
        return 502, '{"message":"The sandbox is running but port is not open"}'
    return 200, "preview-8080\n"


def _client(**results: CommandOutput) -> FakeSandboxClient:
    client = FakeSandboxClient()
    client.command_results = {
        "# agent-probe": CommandOutput(0, "0 1\n", ""),
        "# preview-probe": CommandOutput(0, "6 0\n", ""),
        "# preview-local": CommandOutput(0, "preview-8080\n", ""),
        "# agent-local": CommandOutput(0, "agent-9090\n", ""),
        "# client-log": CommandOutput(0, "CONNECTED\nCUT BrokenPipeError\n", ""),
        "# peer-listening": CommandOutput(0, "LISTEN 0 5 127.0.0.1:9091 0.0.0.0:*\n", ""),
        "# protected": CommandOutput(0, "fs.protected_regular = 2\n", ""),
        **results,
    }
    return client


async def test_the_smoke_passes_when_every_probe_reads_as_contained() -> None:
    client = _client()

    verdict = await containment_smoke(client, "tmpl", fetch=_fetch, patience=0)

    assert verdict == ContainmentVerdict(True, True, True, True, True)
    assert verdict.ok
    sandbox = next(iter(client.sandboxes.values()))
    assert sandbox.options.template == "tmpl"
    assert sandbox.options.network.public
    assert client.killed == [sandbox.sandbox_id]
    modes = [c.envs.get("MODE") for c in sandbox.commands if c.command == FIREWALL]
    assert modes == ["open", "contain"]


async def test_each_probe_that_reads_wrong_fails_only_its_own_field() -> None:
    cases = {
        "# agent-probe": ("agent_injected", CommandOutput(0, "0 0\n", "")),
        "# preview-probe": ("preview_blocked", CommandOutput(0, "0 1\n", "")),
        "# preview-local": ("preview_serves", CommandOutput(28, "", "timeout")),
        "# client-log": ("declared_port_cut", CommandOutput(0, "CONNECTED\nALIVE b'ping'\n", "")),
        "# agent-local": ("undeclared_port_closed", CommandOutput(28, "", "down")),
    }
    for marker, (field_name, output) in cases.items():
        verdict = await containment_smoke(
            _client(**{marker: output}), "tmpl", fetch=_fetch, patience=0
        )
        failed = [name for name, value in asdict(verdict).items() if not value]
        assert failed == [field_name], marker
        assert not verdict.ok


async def test_a_listener_the_edge_serves_on_an_undeclared_port_fails_the_check() -> None:
    async def open_edge(url: str) -> tuple[int, str]:
        return (200, "preview-8080\n") if "8080-" in url else (200, "agent-9090\n")

    verdict = await containment_smoke(_client(), "tmpl", fetch=open_edge, patience=0)

    assert not verdict.undeclared_port_closed
    assert verdict.preview_serves


async def test_only_the_edges_502_counts_as_a_closed_port() -> None:
    async def not_found(url: str) -> tuple[int, str]:
        return (200, "preview-8080\n") if url.startswith("https://8080-") else (404, "")

    verdict = await containment_smoke(_client(), "tmpl", fetch=not_found, patience=0)

    assert not verdict.undeclared_port_closed


async def test_a_502_that_is_not_the_edges_closed_port_answer_does_not_count() -> None:
    async def other_502(url: str) -> tuple[int, str]:
        return (200, "preview-8080\n") if url.startswith("https://8080-") else (502, "bad gateway")

    verdict = await containment_smoke(_client(), "tmpl", fetch=other_502, patience=0)

    assert not verdict.undeclared_port_closed


async def test_a_client_that_only_timed_out_is_not_a_cut() -> None:
    timed_out = CommandOutput(0, "CONNECTED\nTIMEOUT\n", "")

    verdict = await containment_smoke(
        _client(**{"# client-log": timed_out}), "tmpl", fetch=_fetch, patience=0
    )

    assert not verdict.declared_port_cut


def test_the_client_program_names_a_timeout_before_the_general_error() -> None:
    # socket.timeout is a plain OSError before Python 3.10, so only its own clause keeps a
    # connection that merely timed out from reading as cut.
    assert _CLIENT_PY.index("except socket.timeout:") < _CLIENT_PY.index("except OSError")


async def test_the_sandbox_is_killed_when_a_probe_raises() -> None:
    client = _client()

    async def broken(url: str) -> tuple[int, str]:
        raise RuntimeError("edge down")

    with pytest.raises(RuntimeError):
        await containment_smoke(client, "tmpl", fetch=broken, patience=0)

    assert client.killed
