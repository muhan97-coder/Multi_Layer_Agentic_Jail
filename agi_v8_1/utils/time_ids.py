"""Time & ID helpers — ported from agi_v7.1/agent_system/utils.py public API.

ID helpers are deterministic where they can be (counter-based) and uuid-based
otherwise. All time helpers return UTC.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from uuid import uuid4

__all__ = [
    "utc_now",
    "utc_now_dt",
    "generate_run_id",
    "generate_task_id",
    "monotonic_ns",
]


def utc_now() -> str:
    """Return current UTC time as ISO-8601 string (matches v7.1 behavior)."""
    return datetime.now(timezone.utc).isoformat()


def utc_now_dt() -> datetime:
    """Return current UTC time as timezone-aware datetime."""
    return datetime.now(timezone.utc)


def generate_run_id(prefix: str = "run") -> str:
    """Generate a globally-unique run identifier.

    Format: ``{prefix}_{utc_compact}_{short_uuid}``.
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    short = uuid4().hex[:8]
    return f"{prefix}_{ts}_{short}"


def generate_task_id(prefix: str = "task") -> str:
    """Generate a per-cycle task identifier (UUID-based)."""
    return f"{prefix}_{uuid4().hex[:12]}"


def monotonic_ns() -> int:
    """Return monotonic clock value in nanoseconds (latency probes)."""
    return time.monotonic_ns()
