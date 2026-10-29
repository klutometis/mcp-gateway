"""Finding binary payloads in a tool result, and naming them.

MCP servers hide bytes in four different places, and we have one of each
in the house:

============================  ==========================================
shape                         who does it
============================  ==========================================
``image`` content block       WhatsApp ``download_media`` (images),
                              Roblox ``get_asset_thumbnail``
``resource`` block, ``blob``  WhatsApp ``download_media`` (non-images)
base64 inside a JSON field    iMessage ``imessage_get_attachment``,
of a ``text`` block           LinkedIn ``fetch_attachment``
a bare base64 / data: URI     small servers that just dump the string
``text`` block
============================  ==========================================

This module only *finds and describes* payloads; the middleware decides
what to do with them. Keeping the two apart is what makes the detector
testable without a store, a network, or a gateway.
"""

from __future__ import annotations

import base64
import binascii
import re

__all__ = [
    "decode_b64",
    "ext_for",
    "guess_filename",
    "looks_like_base64",
    "sniff_mime",
    "split_data_uri",
]

_B64_RE = re.compile(rb"^[A-Za-z0-9+/\s]+={0,2}\s*$")
_DATA_URI_RE = re.compile(r"^data:([\w.+-]+/[\w.+-]+)?(;charset=[\w-]+)?;base64,", re.I)

# Magic numbers, longest-prefix first. Deliberately small: these are the
# types that actually come through messaging attachments.
_MAGIC: list[tuple[bytes, str]] = [
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
    (b"%PDF-", "application/pdf"),
    (b"OggS", "audio/ogg"),
    (b"ID3", "audio/mpeg"),
    (b"\xff\xfb", "audio/mpeg"),
    (b"fLaC", "audio/flac"),
    (b"PK\x03\x04", "application/zip"),
    (b"\x1f\x8b", "application/gzip"),
    (b"\x00\x00\x00 ftyp", "video/mp4"),
]

_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
    "image/svg+xml": ".svg",
    "application/pdf": ".pdf",
    "application/zip": ".zip",
    "application/gzip": ".gz",
    "application/json": ".json",
    "text/plain": ".txt",
    "text/html": ".html",
    "audio/ogg": ".ogg",
    "audio/mpeg": ".mp3",
    "audio/flac": ".flac",
    "audio/mp4": ".m4a",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
}


def split_data_uri(text: str) -> tuple[str | None, str] | None:
    """``data:image/png;base64,AAA`` → ``("image/png", "AAA")``; else None."""
    m = _DATA_URI_RE.match(text)
    if not m:
        return None
    return m.group(1), text[m.end() :]


def looks_like_base64(text: str, *, min_chars: int) -> bool:
    """Cheap test before paying for a decode.

    Rejects anything short, anything with a character base64 can't
    contain, and anything whose length isn't a multiple of 4 once
    whitespace is stripped.
    """
    if len(text) < min_chars:
        return False
    raw = text.strip().encode("ascii", "ignore")
    if len(raw) < min_chars:
        return False
    if not _B64_RE.match(raw):
        return False
    compact = b"".join(raw.split())
    return len(compact) % 4 == 0


def decode_b64(text: str) -> bytes | None:
    """Strict base64 decode, or ``None``. Never raises."""
    try:
        return base64.b64decode(text, validate=False)
    except (binascii.Error, ValueError):
        return None


def sniff_mime(data: bytes, declared: str | None = None) -> str:
    """Trust the magic number over the declaration, but keep subtypes.

    A server that says ``application/octet-stream`` for a PNG is common;
    a server that says ``image/heic`` for something whose magic we don't
    know is also common. So: sniff first, fall back to the declaration,
    fall back to octet-stream.
    """
    head = data[:16]
    for magic, mime in _MAGIC:
        if head.startswith(magic) or (
            magic == b"\x00\x00\x00 ftyp" and data[4:8] == b"ftyp"
        ):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if declared and declared != "application/octet-stream":
        return declared
    return "application/octet-stream"


def ext_for(mime: str) -> str:
    return _EXT.get(mime.split(";")[0].strip().lower(), ".bin")


def guess_filename(
    *, declared: str | None, mime: str, sha256: str
) -> str:
    """Prefer the server's filename; otherwise a short content-addressed one."""
    if declared:
        name = declared.strip().replace("/", "_").replace("\\", "_")
        if name and name not in (".", ".."):
            return name
    return f"{sha256[:12]}{ext_for(mime)}"


#: JSON keys that carry bytes, and the sibling keys that name them.
BLOB_KEYS = (
    "data",
    "base64",
    "b64",
    "b64_json",
    "blob",
    "bytes",
    "content",
    "image",
    "image_base64",
    "file",
    "file_data",
    "attachment",
    "attachment_data",
    "body",
    "payload",
    "thumbnail",
)
NAME_KEYS = ("filename", "file_name", "name", "title")
MIME_KEYS = ("mime", "mime_type", "mimeType", "content_type", "contentType", "type")
