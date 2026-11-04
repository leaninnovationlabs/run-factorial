"""The API's uvicorn target, `orchestrator.api.serve:app`: `main.app` with the process's logging
configured (tech-stack decision 81).

A module of its own because tests import `main`, and configuring logging there would replace the
test runner's handlers. Logging is configured before `main` is imported, because that import loads
the settings and builds the app: a settings file that fails to load is a JSON record too. uvicorn
imports the app before it logs its startup, so those lines are in the service's format too.
"""

from __future__ import annotations

from orchestrator.logs import configure_logging

configure_logging("api")

from orchestrator.api.main import app  # noqa: E402

__all__ = ["app"]
