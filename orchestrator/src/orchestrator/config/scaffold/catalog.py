"""Reading the template tree. A variant folder IS the target folder, verbatim.

Which markers a variant needs is SCANNED from its file bodies, never declared in CATALOG.yaml,
so a template that starts naming a new lane fact is gated by the derivation automatically
instead of silently shipping an unsubstituted marker. Its agent-local skills are scanned with
it, because they ship together.

A variant has two axes, both read from `CATALOG.yaml`: the lane topology its prompts describe
(`lane:`, absent for one that fits any lane) and the forge whose CLI they call (`forge:`,
`github` when absent). The lane decides which offered variant is ticked; the forge decides whether
a variant is offered at all, because a prompt that opens pull requests with `gh` cannot deliver on
a GitLab project.

Agents resolve by NAME, from one flat catalog — never by role within a chosen process template.
An operator edits the template's text before committing it, so keying files on the template
would make that edited `process.yaml` unscaffoldable; name lookup takes the config as given,
whatever produced it.

Templates are files, substituted and then validated by parsing (`scaffold.validate`), rather than
models constructed and dumped. Both give a valid manifest; only files keep the comments explaining
why `model:` cannot be a size alias and why `secrets:` is not an isolation boundary
(design.md §7.4) — in a folder the operator is expected to edit.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any, Literal, get_args

import yaml

from orchestrator.config.loader import MCP_EXAMPLE_SUFFIX, MCP_SUFFIX
from orchestrator.config.models import ProcessConfig
from orchestrator.config.scaffold.facts import MARKERS, is_stacked, stub_reason

__all__ = [
    "MARKERS",
    "TEMPLATE_ROOT",
    "CatalogEntry",
    "Forge",
    "Kind",
    "Lane",
    "VariantMeta",
    "entries",
    "variant_fit",
]

TEMPLATE_ROOT = Path(__file__).parent / "templates"
Kind = Literal["agent", "skill", "mcp"]
Lane = Literal["stacked", "base-target"]
Forge = Literal["github", "gitlab"]


@dataclass(frozen=True, slots=True)
class VariantMeta:
    summary: str
    files: Mapping[str, str]          # relative path -> unsubstituted body
    markers: frozenset[str]
    declares_skills: tuple[str, ...]  # skills.shared, read back out of the manifest
    declares_mcp: tuple[str, ...]     # mcp.shared, same
    lane: Lane | None = None          # None: fits any lane
    forge: Forge = "github"


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    name: str
    kind: Kind
    version: int
    variants: Mapping[str, VariantMeta]
    #: Agents whose files this skill or MCP example ships beside, undeclared (`CATALOG.yaml`
    #: says why).
    offered_with: tuple[str, ...] = ()


def _read_tree(folder: Path) -> Mapping[str, str]:
    return {
        str(path.relative_to(folder)): path.read_text()
        for path in sorted(folder.rglob("*"))
        if path.is_file()
    }


def _declared(files: Mapping[str, str], block: str) -> tuple[str, ...]:
    manifest = yaml.safe_load(files.get("agent.yaml", "")) or {}
    return tuple(manifest.get(block, {}).get("shared", ()))


def _meta(
    files: Mapping[str, str], summary: str, lane: Lane | None = None, forge: Forge = "github"
) -> VariantMeta:
    body = "".join(files.values())
    return VariantMeta(
        summary=summary,
        files=files,
        markers=frozenset(marker for marker in MARKERS if marker in body),
        declares_skills=_declared(files, "skills"),
        declares_mcp=_declared(files, "mcp"),
        lane=lane,
        forge=forge,
    )


def _variant(folder: Path, spec: Any, *, lane_required: bool = False) -> VariantMeta:
    lane = spec.get("lane")
    forge = spec.get("forge", "github")
    if lane not in (None, *get_args(Lane)) or forge not in get_args(Forge):
        raise ValueError(f"{folder}: lane {lane!r} or forge {forge!r} is not one the catalog knows")
    # An agent's prompts always describe one lane; with no `lane:` it would be ticked on both.
    if lane_required and lane is None:
        raise ValueError(f"{folder}: an agent variant must name its lane")
    return _meta(_read_tree(folder), spec["summary"], lane, forge)


def _variants(section: str, name: str, spec: Any) -> Mapping[str, VariantMeta]:
    """An agent's or a shared skill's variants, read off disk the same way: a variant folder IS
    the target folder, and the summary, lane and forge each carries come from `CATALOG.yaml`
    alone.

    `spec` stays `Any`, same as the `yaml.safe_load` result it's sliced from below — it is
    untyped YAML, not a modelled structure, and pretending otherwise here is what actually
    triggers a checker error `entries()`'s own `Any` chain never did.
    """
    return {
        variant: _variant(
            TEMPLATE_ROOT / section / name / variant, body, lane_required=section == "agents"
        )
        for variant, body in (spec.get("variants") or {}).items()
    }


@cache
def entries() -> Mapping[tuple[Kind, str], CatalogEntry]:
    meta = yaml.safe_load((TEMPLATE_ROOT / "CATALOG.yaml").read_text())
    found: dict[tuple[Kind, str], CatalogEntry] = {}

    for name, spec in (meta.get("agents") or {}).items():
        found[("agent", name)] = CatalogEntry(
            name, "agent", spec["version"], _variants("agents", name, spec)
        )

    # A shared skill can name a lane just as an agent does — its naming table names branches
    # too — so it is modelled the same way, with real variant folders. One that names no lane
    # fact has no variants, and its folder is the single unnamed one an MCP server gets.
    for name, spec in (meta.get("skills") or {}).items():
        variants = _variants("skills", name, spec) or {
            "": _variant(TEMPLATE_ROOT / "skills" / name, {"summary": ""})
        }
        found[("skill", name)] = CatalogEntry(
            name,
            "skill",
            spec["version"],
            variants,
            offered_with=tuple(spec.get("offered_with") or ()),
        )
    for name, spec in (meta.get("mcp") or {}).items():
        file = f"{name}{MCP_EXAMPLE_SUFFIX if spec.get('example') else MCP_SUFFIX}"
        files = {file: (TEMPLATE_ROOT / "mcp" / file).read_text()}
        found[("mcp", name)] = CatalogEntry(
            name,
            "mcp",
            spec["version"],
            {"": _meta(files, "")},
            offered_with=tuple(spec.get("offered_with") or ()),
        )
    return found


def variant_fit(
    entry: CatalogEntry,
    variant: str,
    facts: Mapping[str, str],
    config: ProcessConfig,
) -> tuple[bool, bool, str | None]:
    """`(offered, default_on, withheld_reason)`.

    Two independent questions. Whether the facts a variant names all read out of the config
    decides whether it may be offered at all; whether its lane matches the config's own
    topology decides only whether it is ticked to begin with — but that second question also
    decides, in practice, whether the variant contributes a body at all: `propose.py`'s
    `offers_for` calls `add()` only for the lane-matching variant (see `add`'s docstring for
    why — two variants of one agent render to the same target path). An offered variant whose
    lane does not match therefore writes nothing and carries no reason either, since a reason is
    produced only when markers are missing. It reaches neither a tree node nor a withheld row in
    the explorer, which filters on `offered` alone — the operator meets it only once the pull
    request is assembled, as the same comments-only stub an uncatalogued agent gets.

    The lane-match question applies to a shared skill exactly as it does to an agent: a
    skill can carry a `stacked` and a `base-target` variant too (its naming table names branches
    the same way a prompt does), so `kind` never enters this decision — only the lane the
    variant declares. One that declares none (spec-page's forge variants, an MCP server's lone
    `""`) fits every lane. The forge is not asked here: a variant of another forge is never
    offered at all (`propose.offers_for`).
    """
    meta = entry.variants[variant]
    missing = sorted(meta.markers - set(facts))
    if missing:
        return False, False, stub_reason(missing, config)
    default_on = meta.lane is None or (meta.lane == "stacked") == is_stacked(config)
    return True, default_on, None
