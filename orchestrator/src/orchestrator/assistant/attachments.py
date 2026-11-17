"""Files a person attaches to a message to the assistant (tech-stack.md decision 63).

A message takes at most `MAX_FILES`, each one of two things: an image a model can look at (PNG,
JPEG, GIF or WebP, up to `MAX_IMAGE`), or a small text file (UTF-8, up to `MAX_TEXT`) whose
contents go into the message as data. An image's limit is 3.75 MB because a model's API measures
it as it is sent, in base64, where that is the 5 MB such an API takes: a file accepted here must
not fail its turn for its size. What a file is, its bytes decide. Its name and whatever a
browser called it are a person's or a page's word, so the name is only shown, and is cut down to a
name (`safe_name`) before it is stored or told to a model. A file named as a picture that is not
one of the four is refused rather than read as text: the person meant the model to see it.

The bytes are kept in `chat_attachment`, which goes with its conversation, and nothing serves
them back to a browser.
"""

from __future__ import annotations

import os
import re
from typing import Final

__all__ = [
    "IMAGE_TYPES",
    "MAX_FILES",
    "MAX_IMAGE",
    "MAX_TEXT",
    "TEXT_TYPE",
    "AttachmentRefused",
    "classify",
    "safe_name",
]

IMAGE_TYPES: Final = ("image/png", "image/jpeg", "image/gif", "image/webp")
TEXT_TYPE: Final = "text/plain"
MAX_IMAGE: Final = 3_750_000
MAX_TEXT: Final = 100 * 1024
MAX_FILES: Final = 4
_NAME_MAX: Final = 80

_MAGIC: Final = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)
#: Names a person gives a picture. One of these that is not an image the model can take is
#: refused, never read as text.
_PICTURE_NAMES: Final = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".svg",
    ".bmp",
    ".tif",
    ".tiff",
    ".heic",
    ".avif",
    ".ico",
)
#: What a name may not hold: a control character, and the marks a message's text is fenced with.
_NOT_IN_A_NAME: Final = re.compile(r"[\x00-\x1f\x7f`<>]")


class AttachmentRefused(ValueError):
    """A file the assistant will not take. The message is a sentence for the person."""


def _image(content: bytes) -> str | None:
    for magic, media_type in _MAGIC:
        if content.startswith(magic):
            return media_type
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    return None


def classify(name: str, content: bytes) -> str:
    """The media type `content` is: one of `IMAGE_TYPES` or `TEXT_TYPE`. Anything else, and
    anything over its limit, raises `AttachmentRefused`."""
    if not content:
        raise AttachmentRefused(f"`{name}` is empty.")
    media_type = _image(content)
    if media_type is not None:
        if len(content) > MAX_IMAGE:
            raise AttachmentRefused(f"`{name}` is larger than 3.75 MB.")
        return media_type
    if name.lower().endswith(_PICTURE_NAMES):
        raise AttachmentRefused(f"`{name}` is not a PNG, JPEG, GIF or WebP image.")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        text = "\x00"
    # Before its size: an archive or a PDF is not a text file that happens to be too long.
    if "\x00" in text:
        raise AttachmentRefused(f"`{name}` is not an image or a text file in UTF-8.")
    if len(content) > MAX_TEXT:
        raise AttachmentRefused(f"`{name}` is larger than 100 KB, the limit for a text file.")
    return TEXT_TYPE


def safe_name(given: str) -> str:
    """`given` as a file's name and nothing more: no folder, no control character, none of the
    marks a message's text is fenced with, and at most 80 characters with its ending kept."""
    name = _NOT_IN_A_NAME.sub("", re.split(r"[/\\]", given)[-1]).strip()
    if not name:
        return "file"
    if len(name) <= _NAME_MAX:
        return name
    stem, ending = os.path.splitext(name)
    ending = ending[:10]
    return stem[: _NAME_MAX - len(ending)] + ending
