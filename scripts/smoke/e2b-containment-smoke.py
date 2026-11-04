"""E2B containment smoke: the preview firewall, proven on a REAL sandbox from a template.

Run from the repo root:
  `uv run python scripts/smoke/e2b-containment-smoke.py [--key-file PATH] [TEMPLATE]`
TEMPLATE defaults to `base`, the image the factorial template derives from; a template build
passes its own. `python -m orchestrator.e2b_template` runs the same check on every build it
makes. The E2B key is read from `E2B_API_KEY`, or from the first `e2b_...` token in the file
`--key-file` names. It is never printed or logged.

What it proves (`runtime.adapters.e2b.smoke.containment_smoke`, design.md §7.10), in one public
sandbox with a header rule on httpbin.org standing in for a stored credential:
  1. agent_injected        as the agent, a request to the injected host carries the header.
  2. preview_blocked       as the preview uid, the same request fails and never carries it.
  3. preview_serves        with the firewall in contain mode, a server run as the preview on a
                           declared port answers root over loopback and the public ingress.
  4. undeclared_port_closed  a server run as the agent on an undeclared port is a 502 at the edge.
  5. declared_port_cut     a connection the preview opened FROM a declared port while the table
                           was open is dead after the switch to contain (its next send errors or
                           reads EOF; `ss -K` exiting 0 is not taken as proof).
`fs.protected_regular` is logged, since a sticky group-writable checkout root interacts with it.

It writes nothing to GitHub, kills its one sandbox in `finally`, and exits 1 unless all five hold.
"""

import argparse
import asyncio
import logging
import os
import re
import sys
from pathlib import Path

from runtime.adapters.e2b.client import E2bSandboxClient
from runtime.adapters.e2b.smoke import containment_smoke

KEY = re.compile(r"e2b_[A-Za-z0-9]+")


def api_key(key_file: str | None) -> str:
    text = Path(key_file).read_text() if key_file else os.environ.get("E2B_API_KEY", "")
    found = KEY.search(text)
    if found is None:
        sys.exit("set E2B_API_KEY or pass --key-file with an e2b_ key in it; nothing ran")
    return found.group(0)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("template", nargs="?", default="base")
    parser.add_argument("--key-file")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    verdict = await containment_smoke(E2bSandboxClient(api_key(args.key_file)), args.template)
    for name in (
        "agent_injected",
        "preview_blocked",
        "preview_serves",
        "undeclared_port_closed",
        "declared_port_cut",
    ):
        print(f"{'PASS' if getattr(verdict, name) else 'FAIL'}  {name}")
    print("ok" if verdict.ok else "NOT ok")
    return 0 if verdict.ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
