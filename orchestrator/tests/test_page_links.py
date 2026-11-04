"""Signed page links (tech-stack decision 58): what lets a frame and a new tab, which cannot send
the dashboard's bearer token, load one run's page."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from orchestrator.api.sessions import (
    _PAGE_LINK_KEY,
    _SESSION_KEY,
    _sign,
    sign_page_link,
    verify_page_link,
)

TOKEN = "t" * 32
NOW = datetime(2026, 10, 2, 12, 20, tzinfo=UTC)
BLOB = "b" * 40


def test_a_link_verifies_for_its_run_and_blob_until_it_expires() -> None:
    expires, signature = sign_page_link(7, BLOB, api_token=TOKEN, now=NOW)

    assert expires == int(datetime(2026, 10, 2, 14, 0, tzinfo=UTC).timestamp())
    assert verify_page_link(7, BLOB, expires, signature, api_token=TOKEN, now=NOW)
    later = NOW + timedelta(hours=1, minutes=39)
    assert verify_page_link(7, BLOB, expires, signature, api_token=TOKEN, now=later)
    expired = NOW + timedelta(hours=1, minutes=41)
    assert not verify_page_link(7, BLOB, expires, signature, api_token=TOKEN, now=expired)


def test_a_link_fails_for_anything_it_was_not_signed_for() -> None:
    expires, signature = sign_page_link(7, BLOB, api_token=TOKEN, now=NOW)

    assert not verify_page_link(8, BLOB, expires, signature, api_token=TOKEN, now=NOW)
    assert not verify_page_link(7, "c" * 40, expires, signature, api_token=TOKEN, now=NOW)
    assert not verify_page_link(7, BLOB, expires + 3600, signature, api_token=TOKEN, now=NOW)
    assert not verify_page_link(7, BLOB, expires, signature, api_token="other", now=NOW)
    assert not verify_page_link(7, BLOB, expires, signature, api_token="", now=NOW)


def test_links_minted_within_one_hour_are_the_same_link() -> None:
    """So the drawer's polls do not hand the frame a new `src` every few seconds."""
    first = sign_page_link(7, BLOB, api_token=TOKEN, now=NOW)

    assert sign_page_link(7, BLOB, api_token=TOKEN, now=NOW + timedelta(minutes=39)) == first
    assert sign_page_link(7, BLOB, api_token=TOKEN, now=NOW + timedelta(minutes=41)) != first


def test_a_session_signature_never_doubles_as_a_page_link() -> None:
    assert _sign(b"7.x.1", TOKEN, _SESSION_KEY) != _sign(b"7.x.1", TOKEN, _PAGE_LINK_KEY)


def test_a_link_with_non_ascii_signature_returns_false() -> None:
    """Non-ASCII signatures from untrusted sources must not raise TypeError."""
    expires, _signature = sign_page_link(7, BLOB, api_token=TOKEN, now=NOW)

    assert not verify_page_link(7, BLOB, expires, "é" * 64, api_token=TOKEN, now=NOW)


def test_a_link_signed_with_an_empty_token_is_refused() -> None:
    """The unset token guard must reject a link even if signed with that empty token,
    because sha256(empty) is publicly derivable."""
    expires, signature = sign_page_link(7, BLOB, api_token="", now=NOW)

    assert not verify_page_link(7, BLOB, expires, signature, api_token="", now=NOW)
