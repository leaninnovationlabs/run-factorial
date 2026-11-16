"""Where the tests find the repo's own files."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SAMPLE = REPO_ROOT / "docs" / "samples" / "factorial-simple"
SAMPLE_PROCESS = SAMPLE / "processes" / "process-simple.yaml"
