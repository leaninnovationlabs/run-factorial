"""The build the running image came from, for the dashboard's Help menu."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter

from orchestrator.api.access import SIGNED_IN, needs
from orchestrator.api.schemas import VersionOut

router = APIRouter(tags=["version"])

#: Written at image build from the `REVISION` build argument. A file, not a `FACTORIAL_*`
#: variable: init refuses those in the container's environment.
REVISION_FILE = Path("/etc/factorial/revision")


@router.get("/version", response_model=VersionOut)
@needs(SIGNED_IN)
def get_version() -> VersionOut:
    try:
        revision = REVISION_FILE.read_text().strip()
    except OSError:
        revision = ""
    return VersionOut(revision=revision or None)
