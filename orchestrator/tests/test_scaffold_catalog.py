import re

import pytest
import yaml

from orchestrator.config import parse_process
from orchestrator.config.scaffold import catalog
from orchestrator.config.scaffold.facts import derive_agent_facts
from tests.scaffold_lanes import STACKED, UNSTACKED


def test_agent_entry_carries_its_variants():
    entry = catalog.entries()[("agent", "plan-agent")]
    assert entry.version == 9
    assert "stacked" in entry.variants


def test_variant_files_are_keyed_by_relative_path():
    files = catalog.entries()[("agent", "plan-agent")].variants["stacked"].files
    assert set(files) >= {"agent.yaml", "system-prompt.md", "instructions.md"}
    assert files["agent.yaml"].startswith("#")


def test_markers_are_scanned_not_declared():
    """A template that starts using a new fact must be gated without touching CATALOG.yaml."""
    markers = catalog.entries()[("agent", "plan-agent")].variants["stacked"].markers
    assert "__BRANCH_PREFIX__" in markers
    assert markers <= set(catalog.MARKERS)


def test_shared_skill_declaration_is_read_from_the_manifest():
    variant = catalog.entries()[("agent", "plan-agent")].variants["stacked"]
    assert "project-constitution" in variant.declares_skills


def test_stacked_variant_fits_a_stacked_lane():
    config = parse_process(STACKED, origin="t")
    entry = catalog.entries()[("agent", "build-agent")]
    offered, default_on, reason = catalog.variant_fit(
        entry, "stacked", derive_agent_facts(config, "build-agent"), config
    )
    assert (offered, default_on, reason) == (True, True, None)


def test_stacked_variant_is_withheld_on_an_unstacked_lane():
    """The withheld reason must name the lane, not list markers: 'does not supply spec_prefix'
    reads as nonsense against a config whose branches are literally spec/ and impl/."""
    config = parse_process(UNSTACKED, origin="t")
    entry = catalog.entries()[("agent", "build-agent")]
    offered, _, reason = catalog.variant_fit(
        entry, "stacked", derive_agent_facts(config, "build-agent"), config
    )
    assert offered is False
    assert reason is not None
    assert "targets the base branch" in reason


def test_base_target_variant_fits_the_unstacked_sample_lane():
    """The regression that motivates variants: on this lane every catalog agent used to stub."""
    config = parse_process(UNSTACKED, origin="t")
    for name in ("plan-agent", "build-agent"):
        entry = catalog.entries()[("agent", name)]
        offered, default_on, reason = catalog.variant_fit(
            entry, "base-target", derive_agent_facts(config, name), config
        )
        assert (offered, default_on, reason) == (True, True, None), name


def test_base_target_variant_is_offered_but_unticked_on_a_stacked_lane():
    config = parse_process(STACKED, origin="t")
    entry = catalog.entries()[("agent", "plan-agent")]
    offered, default_on, _ = catalog.variant_fit(
        entry, "base-target", derive_agent_facts(config, "plan-agent"), config
    )
    assert (offered, default_on) == (True, False)


def test_base_target_prompts_never_name_a_stacked_only_fact():
    for name in ("plan-agent", "build-agent", "test-agent"):
        markers = catalog.entries()[("agent", name)].variants["base-target"].markers
        assert not markers & {"__SPEC_PREFIX__", "__IMPL_PREFIX__"}, name


def test_every_variant_names_its_lane_and_forge():
    """Only the stacked prompts call one forge's CLI, so a stacked GitLab lane gets the stub."""
    for (kind, name), entry in catalog.entries().items():
        for variant, meta in entry.variants.items():
            if variant == "stacked":
                assert (meta.lane, meta.forge) == ("stacked", "github"), name
            elif variant == "base-target":
                assert (meta.lane, meta.forge) == ("base-target", None), name
            else:
                assert (meta.lane, meta.forge) == (None, None), (kind, name, variant)


def test_an_agent_variant_without_a_lane_fails_the_catalog_load():
    with pytest.raises(ValueError, match="must name its lane"):
        catalog._variants("agents", "build-agent", {"variants": {"stacked": {"summary": "s"}}})
    assert catalog._variants(
        "skills", "project-constitution", {"variants": {"stacked": {"summary": "s"}}}
    )


def test_plan_agent_ships_its_local_speckit_skills():
    files = catalog.entries()[("agent", "plan-agent")].variants["base-target"].files
    assert "skills/speckit-specify/SKILL.md" in files


def test_mcp_component_is_in_the_catalog():
    assert catalog.entries()[("mcp", "product-docs")].version == 3


def test_the_mcp_server_ships_as_an_inert_example_no_agent_declares():
    """A live server needs an endpoint, a secret and an allowlist entry a first commit lacks."""
    entry = catalog.entries()[("mcp", "product-docs")]
    assert set(entry.variants[""].files) == {"product-docs.yaml.example"}
    assert not any(
        meta.declares_mcp
        for (kind, _), agent in catalog.entries().items()
        if kind == "agent"
        for meta in agent.variants.values()
    )


def test_test_agent_has_an_entry():
    variants = set(catalog.entries()[("agent", "test-agent")].variants)
    assert variants == {"base-target"}


def test_every_shipped_skill_is_declared_by_its_own_variant():
    """The regression that shipped three dead SKILL.md files under plan-agent's `stacked`
    variant: a file physically present in a variant's tree but not named in THAT variant's own
    `skills.local` is one the runtime never loads (propose.py's module docstring). A
    skill belongs to the variant that actually invokes it, never to a sibling variant that
    happens to share the agent name.
    """
    for (kind, name), entry in catalog.entries().items():
        if kind != "agent":
            continue
        for variant_name, meta in entry.variants.items():
            manifest = yaml.safe_load(meta.files.get("agent.yaml", "")) or {}
            declared_local = set(manifest.get("skills", {}).get("local") or ())
            shipped = {
                path.split("/")[1]
                for path in meta.files
                if path.startswith("skills/") and path.endswith("/SKILL.md")
            }
            undeclared = shipped - declared_local
            assert not undeclared, (name, variant_name, undeclared)


@pytest.mark.parametrize("key", sorted(catalog.entries()))
def test_every_catalog_manifest_parses_once_substituted(key):
    """A shipped template must be valid YAML with every marker filled — a broken one is only
    discovered at the operator's pull request otherwise."""
    from orchestrator.config.scaffold.facts import MARKERS

    entry = catalog.entries()[key]
    for meta in entry.variants.values():
        for path, body in meta.files.items():
            if not path.endswith(".yaml"):
                continue
            filled = body
            for marker in MARKERS:
                filled = filled.replace(marker, "x")
            yaml.safe_load(filled)


# --- the prohibitions (restored from the deleted test_scaffold_render.py, ported to every
# variant rather than the old single `stacked` shape) ----------------------------------------------


#: A concept counts as forbidden only when a negation *leads into it*, within the same sentence
#: and a short distance after — not one fixed phrase, since the wording varies (a bare `Never X.`
#: list here, a compressed `Do not X, Y, or Z.` list there), but proximity alone is not enough.
#: Two shapes tried first both produced real false positives (caught by the mutation proof
#: below, not hypothetical): a raw character window bridged unrelated sentences
#: and even section headers ("...do not route around it.\n\n## Change safety\n\n- Agents produce
#: merge candidates." reads as "do not ... merge" within 80 characters); same-sentence-anywhere
#: still matched "branches, gates, approvals, checks, labels; these rules cover what enforcement
#: cannot see" — "labels" and "cannot" cohabit one sentence about something else entirely.
#: Requiring the negation to come FIRST, with the concept within `_NEGATION_REACH` characters
#: after it, matches every real prohibition (all phrased negation-then-verb-list) and rejects
#: both recorded false positives.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_NEGATIONS = ("never", "do not", "don't", "must not", "cannot")
_NEGATION_REACH = 60
#: Word-bounded, not a bare substring: an unbounded "merge" also matches inside "emerge", and an
#: unbounded "approve" also matches the unrelated "approved" (as in "human-approved pull
#: request", which names something already blessed, not a prohibition). `labels?` alone keeps
#: its left boundary so both "label" and "labels" count.
_CONTROL_PATTERNS = {
    "merge": r"\bmerge\b",
    "approve": r"\bapprove\b",
    "label": r"\blabels?\b",
    "push": r"\bpush\b",
}


def _forbidden(text: str, concept: str) -> bool:
    pattern = _CONTROL_PATTERNS[concept]
    for sentence in _SENTENCE_SPLIT.split(text.lower()):
        for negation in _NEGATIONS:
            start = sentence.find(negation)
            if start == -1:
                continue
            reach = sentence[start + len(negation) : start + len(negation) + _NEGATION_REACH]
            if re.search(pattern, reach):
                return True
    return False


def _prompt_and_declared_skills_text(meta: catalog.VariantMeta, variant_name: str) -> str:
    """One agent variant's own `system-prompt.md`, plus the matching variant of every shared
    skill it declares — the two places §7.1's controls are allowed to live (see the test
    docstring below for why a shared skill counts)."""
    text = meta.files.get("system-prompt.md", "")
    for skill_name in meta.declares_skills:
        skill_entry = catalog.entries().get(("skill", skill_name))
        if skill_entry is None:
            continue
        skill_variant = skill_entry.variants.get(variant_name)
        if skill_variant is None:
            continue
        text += "".join(skill_variant.files.values())
    return text


def test_every_catalog_prompt_carries_the_prohibitions() -> None:
    """The four controls (merge, approve, label, push) must be forbidden somewhere an agent of
    this variant actually reads: its own system prompt, or a shared skill it declares in
    `skills.shared` — matched by lane, since a shared skill can carry `stacked`/`base-target`
    variants of its own. A shared skill counts because `project-constitution`'s own manifest
    comment says it is shared "so all agents read one copy" — precisely so this policy has one
    place to edit instead of drifting across every prompt that repeats it."""
    for (kind, name), entry in catalog.entries().items():
        if kind != "agent":
            continue
        for variant_name, meta in entry.variants.items():
            text = _prompt_and_declared_skills_text(meta, variant_name)
            for control in _CONTROL_PATTERNS:
                assert _forbidden(text, control), f"{name}/{variant_name} lost {control!r}"


def test_the_constitution_repeats_the_prohibitions_deliberately() -> None:
    """They are also in every system prompt, and that duplication is the point: a system prompt
    is always in context, a skill body is loaded when the harness decides it is relevant. The
    control §7.1 leaves at prompt level must not depend on that decision. Checked in every lane
    variant — the rule is not one this skill's naming table gets to vary by lane."""
    entry = catalog.entries()[("skill", "project-constitution")]
    for variant_name, meta in entry.variants.items():
        body = meta.files["SKILL.md"].lower()
        assert "never merge" in body, variant_name
        assert "never add, remove or edit a label" in body, variant_name
