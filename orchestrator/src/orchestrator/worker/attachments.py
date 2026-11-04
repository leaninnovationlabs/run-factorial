"""Images embedded in an agent's input text, resolved for its run (design.md §7.11).

The worker decides WHICH images a run may receive: only those whose link survives into the final
text of an input the agent is given, attributed to an item that passed the same fences as its
text (`inputs.py`, `interrupts.py`). `MediaSource` fetches them; the adapter places and attaches
them (`RunSpec.attachments`).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from orchestrator.github.media import Fetched, ItemRef, MediaRef, Unavailable, media_refs
from runtime.contract import MissingAttachment, RunAttachment

__all__ = [
    "MediaFactory",
    "MediaSource",
    "TextItem",
    "attachment_details",
    "resolve_attachments",
]


class MediaSource(Protocol):
    """What the worker asks of GitHub for images — `github.media.GitHubMedia`'s surface."""

    async def resolve(
        self, wanted: Sequence[tuple[ItemRef, MediaRef]]
    ) -> tuple[Fetched | Unavailable | None, ...]: ...


MediaFactory = Callable[[str], MediaSource]


@dataclass(frozen=True, slots=True)
class TextItem:
    """One observed item whose text an input carries: the issue body, or one comment or review
    that passed the input's fences."""

    source: str
    item: ItemRef
    text: str


async def resolve_attachments(
    media: MediaFactory | None,
    repo: str,
    inputs: Sequence[tuple[str, Sequence[TextItem]]],
    *,
    prefix: str,
    refs: Callable[[str], Sequence[MediaRef]] = media_refs,
) -> tuple[tuple[RunAttachment, ...], tuple[MissingAttachment, ...]]:
    """The images `inputs` embed — each `(final text, items)` one resolved input — named
    `<prefix>-1`, `<prefix>-2`, ... in input order.

    An image counts only when its link is in the FINAL text (a digest after truncation) and in an
    item's own text, so a truncated-away comment, or one the fences dropped, contributes nothing.
    Nothing is fetched unless some final text names media. `refs` defaults to GitHub's parser.
    """
    if media is None:
        return (), ()
    wanted: list[tuple[ItemRef, MediaRef]] = []
    sources: list[str] = []
    queued: set[str] = set()
    for final_text, items in inputs:
        visible = {ref.asset for ref in refs(final_text)}
        if not visible:
            continue
        for text_item in items:
            for ref in refs(text_item.text):
                if ref.asset in visible and ref.asset not in queued:
                    queued.add(ref.asset)
                    wanted.append((text_item.item, ref))
                    sources.append(text_item.source)
    if not wanted:
        return (), ()
    outcomes = await media(repo).resolve(wanted)
    attachments: list[RunAttachment] = []
    missing: list[MissingAttachment] = []
    digests: set[str] = set()
    for (_, ref), source, outcome in zip(wanted, sources, outcomes, strict=True):
        if outcome is None:
            continue
        if isinstance(outcome, Unavailable):
            missing.append(
                MissingAttachment(source=source, origin=ref.origin, reason=outcome.reason)
            )
        elif outcome.sha256 not in digests:
            digests.add(outcome.sha256)
            attachments.append(
                RunAttachment(
                    name=f"{prefix}-{len(attachments) + 1}",
                    media_type=outcome.media_type,
                    content=outcome.content,
                    source=source,
                    origin=ref.origin,
                    sha256=outcome.sha256,
                )
            )
    return tuple(attachments), tuple(missing)


def attachment_details(
    attachments: Sequence[RunAttachment], missing: Sequence[MissingAttachment]
) -> dict[str, object]:
    """What `agent_spawned` / `agent_resumed` record: references, never bytes. Empty when the run
    got no image and missed none."""
    if not attachments and not missing:
        return {}
    return {
        "attachments": [
            {
                "name": item.name,
                "source": item.source,
                "origin": item.origin,
                "media_type": item.media_type,
                "bytes": len(item.content),
                "sha256": item.sha256,
            }
            for item in attachments
        ],
        "attachments_missing": [
            {"source": item.source, "origin": item.origin, "reason": item.reason}
            for item in missing
        ],
    }
