"""Trivial debug logging utility — ported verbatim from agi_v7.1."""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

__all__ = ["log_debug_event"]


def log_debug_event(event: str, extra: dict | None = None) -> None:
    """Emit a debug-level log for diagnostic markers (no side effects)."""
    logger.debug("Debug event: %s | extra: %s", event, extra)
