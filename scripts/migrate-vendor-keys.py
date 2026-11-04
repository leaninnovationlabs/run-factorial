#!/usr/bin/env python3
# Move a vendor API key from the fleet's per-project secret stores onto ONE Runtime row
# (factorial tech-stack decision 36, "the runtime owns its vendor key").
#
#   export FACTORIAL_API_TOKEN=...; export CODEX_KEY=sk-...
#   uv run scripts/migrate-vendor-keys.py --api-url https://factorial.example/factorial \
#       --runtime codex-team --from-env CODEX_KEY            # DRY RUN: print the plan
#   uv run scripts/migrate-vendor-keys.py ... --apply        # write
#
# `--org SLUG` names the organization whose runtimes and projects are read (`default` unless given;
# a slug names a runtime within one organization only, tech-stack decision 68).
# `--runtime SLUG --from-env ENV` is one move and may be repeated pairwise. Per move: the runtime
# row is looked up by slug and its type names the credential (`credential_name_for` — the same
# table the worker and the readiness probe read); the key VALUE is read from the named
# environment variable and PUT on that credential; then every project whose LATEST process
# version names the slug has its same-named project secret DELETED — unless open tickets are
# still pinned to an older snapshot that reads it (config is pinned per ticket; the API's
# `open_tickets_reading_copy` counts them), in which case the copy is KEPT and reported: rerun
# once those tickets close. A bound runtime's key is the only one a process naming it reads, so
# the remaining per-project copies are dead weight that can only mislead — and they may differ
# from each other, which is the reason for consolidating on one key per runtime.
#
# Dry run by default. Only names, slugs and counts are ever printed; the value is never.
#
# Order matters and this script cannot enforce it: the runtime row must EXIST (create it on the
# Runtime page), the fleet's process.yaml files must already name it (the 2026-09-08 fleet sweep,
# 31c3bff) and be synced, and the orchestrator must be on decision 36's worker. Run it after all
# three.
# The runtime's key is written before any project copy is deleted, so there is no tick in which
# neither exists.

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from orchestrator.factorial.runtimes import credential_name_for


class Api(Protocol):
    def get(self, path: str) -> Any: ...
    def put(self, path: str, body: dict[str, Any]) -> Any: ...
    def delete(self, path: str) -> None: ...


class HttpApi:
    """The orchestrator's admin API over urllib — stdlib only, so `uv run` needs nothing extra."""

    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.base_url + path, data=data, method=method)
        request.add_header("Authorization", f"Bearer {self._token}")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            # The body may echo the request (a 422 does); a PUT's request carried the key.
            raise SystemExit(f"{method} {path}: HTTP {exc.code}") from None
        return json.loads(raw) if raw else None

    def get(self, path: str) -> Any:
        return self._call("GET", path)

    def put(self, path: str, body: dict[str, Any]) -> Any:
        return self._call("PUT", path, body)

    def delete(self, path: str) -> None:
        self._call("DELETE", path)


@dataclass
class Move:
    runtime_slug: str
    runtime_id: int
    credential_name: str
    env_name: str
    #: `(id, name)` of every project whose latest version names the runtime.
    bound: list[tuple[int, str]] = field(default_factory=list)
    #: The subset that still stores a project secret under `credential_name` — what gets deleted.
    to_delete: list[tuple[int, str]] = field(default_factory=list)
    #: `(id, name, open tickets)`: bound and holding a copy, but pinned tickets still read it.
    held: list[tuple[int, str, int]] = field(default_factory=list)


def plan(api: Api, moves: Sequence[tuple[str, str]], *, org: str = "default") -> list[Move]:
    """What `apply` would do, from the API alone: no value is read here.

    `GET /projects/{id}` says which runtime the latest version binds and how many open tickets
    are pinned to a snapshot that still reads the project copy — the same probe the project
    page shows, so the script and the page never disagree about which copy is dead."""
    runtimes = {row["slug"]: row for row in api.get(f"/api/orgs/{org}/runtimes")}
    projects = api.get(f"/api/orgs/{org}/projects")
    planned: list[Move] = []
    for slug, env_name in moves:
        row = runtimes.get(slug)
        if row is None:
            raise SystemExit(f"no runtime with slug {slug!r}: create it on the Runtime page first")
        move = Move(
            runtime_slug=slug,
            runtime_id=row["id"],
            credential_name=credential_name_for(row["type"]),
            env_name=env_name,
        )
        for project in projects:
            detail = api.get(f"/api/projects/{project['id']}")
            provided = [
                p for p in detail["runtime_provided_secrets"] if p["runtime_slug"] == slug
            ]
            if not provided:
                continue
            move.bound.append((project["id"], project["name"]))
            if move.credential_name not in detail["secret_names"]:
                continue
            reading = provided[0]["open_tickets_reading_copy"]
            if reading:
                move.held.append((project["id"], project["name"], reading))
            else:
                move.to_delete.append((project["id"], project["name"]))
        planned.append(move)
    return planned


def apply(api: Api, moves: Sequence[Move], environ: dict[str, str]) -> None:
    """The runtime's key first, then the project copies: a run in between reads the runtime."""
    for move in moves:
        value = environ[move.env_name]
        api.put(
            f"/api/runtimes/{move.runtime_id}/credentials/{move.credential_name}",
            {"value": value},
        )
        for project_id, _ in move.to_delete:
            api.delete(f"/api/projects/{project_id}/secrets/{move.credential_name}")


def describe(moves: Sequence[Move], *, applied: bool) -> str:
    verb = "stored" if applied else "would store"
    lines: list[str] = []
    for move in moves:
        lines.append(
            f"{move.runtime_slug}: {verb} {move.credential_name} from ${move.env_name}; "
            f"{len(move.bound)} project(s) bound, {len(move.to_delete)} project copy(ies) "
            f"{'deleted' if applied else 'to delete'}"
        )
        lines.extend(f"  - {name} (project {project_id})" for project_id, name in move.to_delete)
        lines.extend(
            f"  - {name} (project {project_id}): kept, {reading} open ticket(s) pinned to a "
            "snapshot that still reads it — rerun once they close"
            for project_id, name, reading in move.held
        )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Move a vendor API key from per-project secrets onto one Runtime row."
    )
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--token-env", default="FACTORIAL_API_TOKEN")
    parser.add_argument("--org", default="default", metavar="SLUG")
    parser.add_argument("--runtime", action="append", default=[], metavar="SLUG")
    parser.add_argument("--from-env", action="append", default=[], metavar="ENV_NAME")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if not args.runtime or len(args.runtime) != len(args.from_env):
        parser.error("give --runtime SLUG --from-env ENV_NAME in pairs, at least once")

    token = os.environ.get(args.token_env)
    if not token:
        parser.error(f"${args.token_env} is not set")
    missing = [env for env in args.from_env if not os.environ.get(env)]
    if missing:
        parser.error(f"not set in the environment: {', '.join(f'${e}' for e in missing)}")

    api = HttpApi(args.api_url, token)
    moves = plan(api, list(zip(args.runtime, args.from_env, strict=True)), org=args.org)
    if args.apply:
        apply(api, moves, dict(os.environ))
    print(describe(moves, applied=args.apply))
    if not args.apply:
        print("dry run — pass --apply to write")
    return 0


if __name__ == "__main__":
    sys.exit(main())
