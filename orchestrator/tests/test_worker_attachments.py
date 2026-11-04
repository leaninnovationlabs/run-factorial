"""Which images a run may see: only links in an input's FINAL text, in an item that passed that
input's fences. No Postgres: the resolution is a function of texts, items and a media source."""

from __future__ import annotations

from collections.abc import Callable

from orchestrator.github.media import ItemRef
from orchestrator.worker.attachments import TextItem, attachment_details, resolve_attachments
from tests.reconcile_world import PNG_BYTES, FakeMedia

UUID_A = "11111111-2222-4333-8444-555555555555"
UUID_B = "66666666-7777-4888-9999-aaaaaaaaaaaa"
ISSUE = ItemRef("issue", 12)
COMMENT = ItemRef("issue_comment", 12, 1001)


def _factory(media: FakeMedia) -> Callable[[str], FakeMedia]:
    return lambda _repo: media


async def test_with_no_media_source_nothing_resolves() -> None:
    assert await resolve_attachments(None, "acme/w", [("x", ())], prefix="image") == ((), ())


async def test_a_run_whose_inputs_name_no_media_costs_no_call() -> None:
    media = FakeMedia()
    body = "please add a widget"
    result = await resolve_attachments(
        _factory(media), "acme/w", [(body, (TextItem("ticket_body", ISSUE, body),))],
        prefix="image",
    )
    assert result == ((), ())
    assert media.calls == []


async def test_an_image_in_the_body_is_attached_under_the_prefix() -> None:
    media = FakeMedia()
    body = f'Extract the text.\n<img src="{media.image(UUID_A)}" />'
    attachments, missing = await resolve_attachments(
        _factory(media), "acme/w", [(body, (TextItem("ticket_body", ISSUE, body),))],
        prefix="image",
    )
    (item,) = attachments
    assert (item.name, item.source, item.content) == ("image-1", "ticket_body", PNG_BYTES)
    assert item.origin == f"https://github.com/user-attachments/assets/{UUID_A}"
    assert missing == ()
    ((wanted_item, _),) = media.calls[0]
    assert wanted_item == ISSUE


async def test_an_image_the_final_text_no_longer_carries_is_never_requested() -> None:
    """Digest truncation cut the comment: its image goes with it."""
    media = FakeMedia()
    comment = f"old note ![x]({media.image(UUID_A)})"
    final = "[... earlier comments truncated ...]\n\nthe newest note"
    result = await resolve_attachments(
        _factory(media), "acme/w", [(final, (TextItem("ticket_comments", COMMENT, comment),))],
        prefix="image",
    )
    assert result == ((), ())
    assert media.calls == []


async def test_an_unavailable_image_is_named_with_its_reason() -> None:
    media = FakeMedia()
    reason = "not accessible from this repository"
    body = f"![x]({media.unavailable(UUID_A, reason)})"
    attachments, (missing,) = await resolve_attachments(
        _factory(media), "acme/w", [(body, (TextItem("ticket_body", ISSUE, body),))],
        prefix="image",
    )
    assert attachments == ()
    assert (missing.source, missing.reason) == ("ticket_body", reason)


async def test_one_image_named_in_two_inputs_is_requested_once() -> None:
    media = FakeMedia()
    link = media.image(UUID_A)
    body, comment = f"![a]({link})", f"again ![a]({link})"
    attachments, _ = await resolve_attachments(
        _factory(media),
        "acme/w",
        [
            (body, (TextItem("ticket_body", ISSUE, body),)),
            (comment, (TextItem("ticket_comments", COMMENT, comment),)),
        ],
        prefix="image",
    )
    assert [a.name for a in attachments] == ["image-1"]
    assert len(media.calls[0]) == 1


async def test_two_links_to_identical_bytes_are_one_attachment() -> None:
    media = FakeMedia()
    body = f"![a]({media.image(UUID_A)}) ![b]({media.image(UUID_B)})"
    attachments, _ = await resolve_attachments(
        _factory(media), "acme/w", [(body, (TextItem("ticket_body", ISSUE, body),))],
        prefix="image",
    )
    assert [a.name for a in attachments] == ["image-1"]


async def test_a_link_github_did_not_embed_is_dropped_silently() -> None:
    media = FakeMedia()
    body = f"`https://github.com/user-attachments/assets/{UUID_A}`"
    assert await resolve_attachments(
        _factory(media), "acme/w", [(body, (TextItem("ticket_body", ISSUE, body),))],
        prefix="image",
    ) == ((), ())


async def test_the_event_record_names_images_without_their_bytes() -> None:
    media = FakeMedia()
    body = f"![a]({media.image(UUID_A)})"
    attachments, missing = await resolve_attachments(
        _factory(media), "acme/w", [(body, (TextItem("ticket_body", ISSUE, body),))],
        prefix="image",
    )
    details = attachment_details(attachments, missing)
    assert details["attachments"] == [
        {
            "name": "image-1",
            "source": "ticket_body",
            "origin": f"https://github.com/user-attachments/assets/{UUID_A}",
            "media_type": "image/png",
            "bytes": len(PNG_BYTES),
            "sha256": attachments[0].sha256,
        }
    ]
    assert details["attachments_missing"] == []
    assert attachment_details((), ()) == {}
