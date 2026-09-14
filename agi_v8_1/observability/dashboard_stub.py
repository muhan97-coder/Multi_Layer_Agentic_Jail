"""ObservabilityDashboard stub — read-only, fail-open.

Full v7.1 ``dashboard.py`` is 724 LoC of runtime-session-scanning code that
exceeds R27's per-module LoC budget and references live filesystem layouts
not yet present in agi_v8_1. R27 ports the *interface* (dataclass + builder
function) returning structured stubs, so callers can wire integration code
now and the real scanner can be dropped in later.

# R27: deferred for later round  (live runtime-session scanner)
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agi_v8_1.utils.time_ids import utc_now

__all__ = [
    "ObservabilityDashboard",
    "build_dashboard_stub",
    "recent_proposal_events_stub",
]


@dataclass(frozen=True)
class ObservabilityDashboard:
    """Frozen dataclass mirror of v7.1 ObservabilityDashboard.

    ``collect()`` returns a stub payload — real scanning is deferred.
    """

    runtime_sessions_dir: str | Path
    recent_event_limit: int = 25
    log_issue_limit: int = 25

    def collect(self) -> dict[str, Any]:
        return build_dashboard_stub(
            self.runtime_sessions_dir,
            recent_event_limit=self.recent_event_limit,
            log_issue_limit=self.log_issue_limit,
        )


def build_dashboard_stub(
    runtime_sessions_dir: str | Path,
    *,
    recent_event_limit: int = 25,
    log_issue_limit: int = 25,
) -> dict[str, Any]:
    """Return a stub dashboard payload with the same top-level shape as v7.1.

    Real implementation pending — see module docstring.
    # R27: deferred for later round
    """
    return {
        "ok": True,
        "stub": True,
        "generated_at": utc_now(),
        "runtime_sessions_dir": str(runtime_sessions_dir),
        "recent_event_limit": int(recent_event_limit),
        "log_issue_limit": int(log_issue_limit),
        "sessions": [],
        "recent_events": [],
        "log_issues": [],
        "summary": {
            "session_count": 0,
            "event_count": 0,
            "issue_count": 0,
        },
        "deferred": "approved_runtime",
    }


def recent_proposal_events_stub(
    runtime_sessions_root: str | Path | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Stub for v7.1 ``recent_proposal_events`` — returns []."""
    return []
