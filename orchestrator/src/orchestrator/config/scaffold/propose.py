"""Every file the catalog could contribute to this config, with why and at what cost.

A pure function of the parsed config, the project's forge and the target repo's current contents.
A variant of another forge is skipped outright, with no offer: its prompts call a CLI that cannot
deliver on this project, so there is nothing to explain or tick. Only the variant
whose lane matches contributes bodies, because two variants of one agent render to identical
target paths (`add`). An offered variant whose lane does not match therefore contributes
nothing, and the operator never meets it: `variant_fit` gives it no reason (a reason exists only
when markers are missing), and the explorer's withheld list filters on `offered`, which it still
is. Its agent is stubbed at assembly time and named only in the pull request body.

The one thing it must never do is propose a path over a file the repo already has. Those files
are proposed with `present_in_repo` set instead of omitted, so the explorer can show the
operator that their hand-edited prompt is safe rather than leaving its absence unexplained — the
same rule that lets one endpoint serve both onboarding and later version bumps.

A shared skill or MCP server rides with the agent variant that declares it (`declares_skills` /
`declares_mcp`, read back out of the manifest rather than listed a second time here): an
unreferenced skill file is one the runtime never loads, and a declared-but-missing one is a 404
at spawn, so the manifest must be the only source or the two can drift apart. The one exception
is a component `offered_with` an agent: a skill or an inert MCP example that ships undeclared,
waiting for a feature the operator turns on by declaring it.

A variant whose lane facts do not read from the config is withheld rather than guessed at
(`catalog.variant_fit`): the prompts name concrete branches, and copying one anyway would ship a
prompt that contradicts the config — the one failure with no mechanical signal, because the
resulting pull request satisfies no postcondition while CI reports green
(docs/bootstrap.md).
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass

from orchestrator.config.loader import AGENTS_DIR, MCP_DIR, SKILLS_DIR
from orchestrator.config.models import AgentStep, ProcessConfig
from orchestrator.config.scaffold import CONFIG_DIR
from orchestrator.config.scaffold.catalog import (
    CatalogEntry,
    Forge,
    Kind,
    entries,
    variant_fit,
)
from orchestrator.config.scaffold.facts import MARKERS, derive_agent_facts, derive_lane_facts

__all__ = [
    "ComponentProposal",
    "FileProposal",
    "Proposal",
    "VariantOffer",
    "propose",
    "substitute",
]


@dataclass(frozen=True, slots=True)
class FileProposal:
    path: str
    body: str
    component: str
    kind: Kind
    variant: str | None
    version: int
    #: The markers THIS file used, and their values — the staleness comparison's whole input.
    facts: Mapping[str, str]
    present_in_repo: bool


@dataclass(frozen=True, slots=True)
class VariantOffer:
    variant: str
    summary: str
    offered: bool
    default_on: bool
    reason: str | None
    paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ComponentProposal:
    name: str
    kind: Kind
    variants: tuple[VariantOffer, ...]
    present_in_repo: bool
    #: True when the config names this agent. A shared skill or MCP server is here because a
    #: manifest declares it, which is a different reason and must read differently in the UI.
    referenced: bool


@dataclass(frozen=True, slots=True)
class Proposal:
    files: Mapping[str, FileProposal]
    components: tuple[ComponentProposal, ...]


def substitute(text: str, values: Mapping[str, str]) -> tuple[str, dict[str, str]]:
    """Fill the markers, and report which ones this text actually used."""
    used: dict[str, str] = {}
    for marker in MARKERS:
        if marker in text and marker in values:
            used[marker] = values[marker]
            text = text.replace(marker, values[marker])
    return text, used


def _target(kind: Kind, name: str, relative: str) -> str:
    if kind == "agent":
        return f"{CONFIG_DIR}/{AGENTS_DIR}/{name}/{relative}"
    if kind == "skill":
        return f"{CONFIG_DIR}/{SKILLS_DIR}/{name}/{relative}"
    return f"{CONFIG_DIR}/{MCP_DIR}/{relative}"


def propose(
    config: ProcessConfig,
    *,
    forge: Forge = "github",
    present_agents: Collection[str] = (),
    present_skills: Collection[str] = (),
    present_mcp: Collection[str] = (),
) -> Proposal:
    available = entries()
    files: dict[str, FileProposal] = {}
    components: list[ComponentProposal] = []
    lane = derive_lane_facts(config)

    def add(entry: CatalogEntry, variant: str, values: Mapping[str, str], present: bool):
        """Write one variant's files into `files`, keyed by TARGET path.

        Two variants of one agent render to the same target paths (`_target` drops the variant
        segment), so a second `add()` for the same entry silently overwrites the first here. That
        is why the caller below must never call this for more than one offered variant per agent:
        only the variant whose lane matches the config may contribute a body. `files` stays keyed
        by target path regardless — it is what the explorer draws and what the legal-path set is
        computed from — and a withheld variant's own `paths` (recorded on its `VariantOffer`,
        never written here) is what the UI reads to explain the road not taken.
        """
        meta = entry.variants[variant]
        paths: list[str] = []
        for relative, body in meta.files.items():
            path = _target(entry.kind, entry.name, relative)
            filled, used = substitute(body, values)
            files[path] = FileProposal(
                path=path,
                body=filled,
                component=entry.name,
                kind=entry.kind,
                variant=variant or None,
                version=entry.version,
                facts=used,
                present_in_repo=present,
            )
            paths.append(path)
        return tuple(paths)

    def offers_for(
        entry: CatalogEntry, facts: Mapping[str, str], present: bool
    ) -> tuple[tuple[VariantOffer, ...], set[str], set[str]]:
        """Every variant's offer for one entry, contributing bodies from the lane-matching one
        only. Shared exactly as written between agents and shared components below: a shared
        skill can carry `stacked`/`base-target` variants too (project-constitution's naming
        table names branches the same way a prompt does), which collide on target path exactly
        as two agent variants do — `variant_fit` no longer special-cases `kind` for this reason,
        and neither does this.
        """
        offers: list[VariantOffer] = []
        declared_skills: set[str] = set()
        declared_mcp: set[str] = set()
        for variant, meta in entry.variants.items():
            if meta.forge not in (None, forge):
                continue
            offered, default_on, reason = variant_fit(entry, variant, facts, config)
            # Only the lane-matching variant may write bodies (see `add`'s docstring); taking a
            # second one here would let it silently clobber the variant the UI shows as selected.
            # A non-matching variant keeps `paths=()` and reaches the operator nowhere: it has no
            # reason, and the explorer's withheld list filters on `offered`, which it still is.
            paths = add(entry, variant, facts, present) if (offered and default_on) else ()
            if offered and default_on:
                declared_skills.update(meta.declares_skills)
                declared_mcp.update(meta.declares_mcp)
            offers.append(VariantOffer(variant, meta.summary, offered, default_on, reason, paths))
        return tuple(offers), declared_skills, declared_mcp

    wanted_skills: set[str] = set()
    wanted_mcp: set[str] = set()
    referenced = {s.agent for s in config.steps if isinstance(s, AgentStep)}

    for name in sorted(referenced):
        entry = available.get(("agent", name))
        present = name in present_agents
        if entry is None:
            components.append(ComponentProposal(name, "agent", (), present, True))
            continue
        offers, declared_skills, declared_mcp = offers_for(
            entry, derive_agent_facts(config, name), present
        )
        wanted_skills.update(declared_skills)
        wanted_mcp.update(declared_mcp)
        components.append(ComponentProposal(name, "agent", offers, present, True))

    # A component `offered_with` an agent is offered, undeclared (`CATALOG.yaml` says why),
    # wherever the config names that agent — whether or not the repo already owns the agent's
    # own files — so every folder holds it by the time an operator turns its feature on. One the
    # repo already holds is proposed locked, like any other.
    for (kind, name), entry in available.items():
        if referenced & set(entry.offered_with):
            (wanted_skills if kind == "skill" else wanted_mcp).add(name)

    # A shared component is offered because a manifest declares it, read back out of that
    # manifest so the declaration and the file cannot drift apart. Its facts are lane-wide only
    # (`lane`, never `derive_agent_facts`) — it belongs to no single agent step.
    groups: tuple[tuple[Kind, set[str], Collection[str]], ...] = (
        ("skill", wanted_skills, present_skills),
        ("mcp", wanted_mcp, present_mcp),
    )
    for kind, wanted, present_set in groups:
        for name in sorted(wanted):
            entry = available.get((kind, name))
            if entry is None:
                continue
            present = name in present_set
            offers, _, _ = offers_for(entry, lane, present)
            components.append(ComponentProposal(name, kind, offers, present, False))

    return Proposal(files=files, components=tuple(components))
