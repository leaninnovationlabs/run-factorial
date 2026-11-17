"""A file attached to a message: what its bytes say it is decides what happens to it, never its
name or what a browser called it."""

from __future__ import annotations

import base64

import pytest

from orchestrator.assistant.attachments import (
    MAX_IMAGE,
    MAX_TEXT,
    AttachmentRefused,
    classify,
    safe_name,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16
GIF = b"GIF89a" + b"\x00" * 16
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 16


@pytest.mark.parametrize(
    ("name", "content", "media_type"),
    [
        ("shot.png", PNG, "image/png"),
        ("photo.JPG", JPEG, "image/jpeg"),
        ("loop.gif", GIF, "image/gif"),
        ("modern.webp", WEBP, "image/webp"),
        # The bytes are the authority: a PNG under another name is still a PNG.
        ("screenshot", PNG, "image/png"),
        ("worker.log", b"12:00 started\n12:01 timed out\n", "text/plain"),
        ("config.yaml", "name: caf\u00e9\n".encode(), "text/plain"),
    ],
)
def test_a_file_is_what_its_bytes_are(name: str, content: bytes, media_type: str) -> None:
    assert classify(name, content) == media_type


@pytest.mark.parametrize(
    ("name", "content", "says"),
    [
        ("empty.txt", b"", "`empty.txt` is empty."),
        # Named as a picture, and not one: the person meant the model to see it.
        ("page.png", b"<html><body>hi</body></html>", "`page.png` is not a PNG"),
        ("logo.svg", b"<svg xmlns='http://www.w3.org/2000/svg'/>", "`logo.svg` is not a PNG"),
        ("big.png", PNG + b"\x00" * MAX_IMAGE, "`big.png` is larger than 3.75 MB."),
        ("big.log", b"x" * (MAX_TEXT + 1), "`big.log` is larger than 100 KB"),
        # Neither an image nor text: its size is not what is wrong with it.
        ("logs.zip", b"PK\x03\x04" + b"\x00" * (MAX_TEXT + 1), "`logs.zip` is not an image or"),
        ("tool.bin", b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 32, "`tool.bin` is not an image or"),
        ("notes.txt", b"caf\xe9 in latin-1", "`notes.txt` is not an image or"),
    ],
)
def test_a_file_that_is_neither_a_small_text_nor_one_of_four_images_is_refused(
    name: str, content: bytes, says: str
) -> None:
    with pytest.raises(AttachmentRefused) as refused:
        classify(name, content)
    assert says in str(refused.value)


def test_an_image_at_the_limit_and_a_text_at_the_limit_are_taken() -> None:
    assert classify("edge.png", PNG + b"\x00" * (MAX_IMAGE - len(PNG))) == "image/png"
    assert classify("edge.log", b"x" * MAX_TEXT) == "text/plain"


@pytest.mark.parametrize(
    ("given", "kept"),
    [
        ("worker.log", "worker.log"),
        ("../../etc/passwd", "passwd"),
        ("C:\\Users\\me\\shot one.png", "shot one.png"),
        ("a\nb`c>>>.txt", "abc.txt"),
        ("   ", "file"),
        ("x" * 200 + ".png", "x" * 76 + ".png"),
    ],
)
def test_a_name_is_kept_as_a_name_and_nothing_more(given: str, kept: str) -> None:
    assert safe_name(given) == kept


def test_the_largest_image_fits_what_a_provider_takes_once_it_is_encoded() -> None:
    """A model's API measures an image as it is sent, in base64, which is a third larger than the
    file: 5 MB there is 3.75 MB here. A file taken at the send must not fail its turn for size."""
    assert MAX_IMAGE == 3_750_000
    assert len(base64.b64encode(b"\x00" * MAX_IMAGE)) <= 5 * 1024 * 1024
