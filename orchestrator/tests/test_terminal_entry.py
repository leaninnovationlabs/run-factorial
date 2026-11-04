"""`python -m orchestrator.terminal`: where the gateway listens, and how it stops."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import async_sessionmaker

from orchestrator.settings import TerminalSettings
from orchestrator.terminal.__main__ import server_config
from orchestrator.terminal.app import WS_PROTOCOL
from tests.api_harness import make_settings


def test_the_gateway_listens_where_its_settings_say_and_stops_through_its_lifespan() -> None:
    settings = make_settings().model_copy(
        update={"terminal": TerminalSettings(bind="0.0.0.0", port=8123)}
    )
    config = server_config(settings, async_sessionmaker())
    assert (config.host, config.port) == ("0.0.0.0", 8123)
    # The shutdown that closes every tmux client runs in the lifespan, and only once no
    # deadline cut it short: a client left attached in an E2B sandbox stays there for good.
    assert config.lifespan == "on"
    assert config.timeout_graceful_shutdown is None
    assert config.ws == WS_PROTOCOL
    assert config.ws_max_size <= 1 << 20
