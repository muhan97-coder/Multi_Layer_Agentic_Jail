"""Hash helpers — stable hashes for signatures and dedup keys.

Ported from agi_v7.1/agent_system/utils.py public API. Uses stdlib hashlib only.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

__all__ = ["stable_hash", "content_hash", "json_signature"]


def stable_hash(value: Any, *, algo: str = "sha256") -> str:
    """Return a hex digest of *value* (anything JSON-serializable)."""
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.new(algo, payload.encode("utf-8")).hexdigest()


def content_hash(text: str, *, algo: str = "sha256") -> str:
    """Hash a raw string (no JSON normalization)."""
    return hashlib.new(algo, text.encode("utf-8")).hexdigest()


def json_signature(value: Any, *, length: int = 16) -> str:
    """Short signature (default 16 chars) for dedup / cache keys."""
    return stable_hash(value)[:length]
