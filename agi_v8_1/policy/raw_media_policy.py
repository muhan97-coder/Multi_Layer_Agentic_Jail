# R25 W4 (ported from v7.1 v7/policy/raw_media_policy.py - __RPLAN_R9_S7__)
"""Raw-media safety policy: detect + decide storage permission.

Ported from v7.1's v7/policy/raw_media_policy.py with R9.S7 magic-byte
sniffing preserved.

Detection layers (any one is sufficient):
  1. Suffix in {.jpg .jpeg .png .webp .gif .mp4 .mov .wav .mp3}
  2. MIME prefix in {image/, video/, audio/}
  3. Magic bytes (JPEG/PNG/GIF/RIFF/MP3/Ogg/MP4-ftyp)

Storage decision (fail-closed):
  - not raw media → allow_store=True
  - derived_only → allow_store=True, store_raw=False, retain 365d
  - consent AND purpose → allow_store=True, store_raw=True, retain 7d
  - else → allow_store=False (raw media requires explicit consent+purpose)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# __SLOT_FAIL_FAST_2026_07_25__ Swallowed failures route through one choke
# point: counted + named always, re-raised under AGI_V8_STRICT_FAIL_FAST.
from agi_v8_1.policy.fail_fast import swallowed as _swallowed

_RAW_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".mov", ".wav", ".mp3"}
_RAW_MIME_PREFIXES = ("image/", "video/", "audio/")

# Magic-byte prefixes for binary media sniffing.
_MAGIC_PREFIXES = (
    b"\xff\xd8\xff",          # JPEG
    b"\x89PNG\r\n\x1a\n",     # PNG
    b"GIF87a",                # GIF87a
    b"GIF89a",                # GIF89a
    b"RIFF",                  # WAV/WEBP/AVI container
    b"ID3",                   # MP3 (ID3 tag)
    b"\xff\xfb",              # MP3 (no ID3)
    b"\xff\xf3",              # MP3
    b"\xff\xf2",              # MP3
    b"OggS",                  # Ogg
)


def _has_media_magic(content_bytes: bytes | bytearray | memoryview) -> bool:
    """True iff leading bytes match a known media signature."""
    head = bytes(content_bytes[:16])
    if any(head.startswith(p) for p in _MAGIC_PREFIXES):
        return True
    # MP4/QuickTime 'ftyp' atom: bytes 4-8 == b"ftyp"
    if len(head) >= 8 and head[4:8] == b"ftyp":
        return True
    return False


def is_raw_media(item: Any, content_bytes: bytes | None = None) -> bool:
    """True iff *item* refers to raw image/video/audio media.

    Checks: explicit MIME (dict), file suffix (path or URL), and
    finally magic-byte sniffing on any embedded/passed bytes.
    """
    if isinstance(item, dict):
        mime = str(item.get("mime_type") or item.get("mime") or "").lower()
        if mime.startswith(_RAW_MIME_PREFIXES):
            return True
        path = str(item.get("path") or item.get("uri") or item.get("name") or "")
        if content_bytes is None:
            embedded = item.get("bytes") or item.get("content_bytes")
            if isinstance(embedded, (bytes, bytearray, memoryview)):
                content_bytes = bytes(embedded)
    elif isinstance(item, (bytes, bytearray, memoryview)):
        if content_bytes is None:
            content_bytes = bytes(item)
        path = ""
    else:
        path = str(item or "")

    if path:
        parsed = urlparse(path)
        if parsed.scheme in ("http", "https", "file", "data") and parsed.path:
            path_for_suffix = parsed.path
        else:
            path_for_suffix = path.split("?", 1)[0].split("#", 1)[0]
    else:
        path_for_suffix = ""

    if Path(path_for_suffix.lower()).suffix in _RAW_EXTS:
        return True

    if content_bytes:
        try:
            if _has_media_magic(content_bytes):
                return True
        except Exception as _ff_exc:
            _swallowed(_ff_exc, site="policy.raw_media_policy.is_raw_media:92", category="config")
            return False
    return False


def decide_raw_media(
    item: Any,
    *,
    consent: bool = False,
    purpose: str = "",
    derived_only: bool = False,
    content_bytes: bytes | None = None,
) -> dict[str, Any]:
    """Decide raw-media storage permission for *item*.

    Fail-closed default: raw media requires explicit ``consent=True`` AND
    a non-empty ``purpose``. Otherwise ``allow_store=False``.
    """
    raw = is_raw_media(item, content_bytes=content_bytes)
    if not raw:
        return {
            "is_raw_media": False,
            "allow_store": True,
            "store_raw": False,
            "retention_days": None,
            "reason": "not raw media",
        }
    if derived_only:
        return {
            "is_raw_media": True,
            "allow_store": True,
            "store_raw": False,
            "retention_days": 365,
            "reason": "derived features only",
        }
    if consent and purpose:
        return {
            "is_raw_media": True,
            "allow_store": True,
            "store_raw": True,
            "retention_days": 7,
            "reason": "consented short-term raw media use",
        }
    return {
        "is_raw_media": True,
        "allow_store": False,
        "store_raw": False,
        "retention_days": 0,
        "reason": "raw media requires explicit consent and purpose",
    }


__all__ = ["is_raw_media", "decide_raw_media"]
