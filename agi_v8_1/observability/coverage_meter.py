"""coverage_meter — data-driven stage coverage registry shim.

MUT-5 (META-SI): pure-additive new module. No callsites yet.

Provides a tiny registry surface so later mutations can register
"stage coverage" entries without re-architecting. Strictly data-driven
(no enum branching, no hardcoded prompt knobs) per the
``feedback_no_mode_enums_2026_05_28`` policy: callers register their
stage by string key + payload mapping; the meter only stores and
exposes counters.

Invariants:
- Pure-python, stdlib-only; no provider/network/file I/O.
- Thread-safe via a single RLock.
- No env gate (registry presence is harmless; callsites decide gating).
- ``registered_stages()`` returns a sorted tuple — deterministic output
  for tests.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Mapping

__all__ = [
    "STAGE_COVERAGE_REGISTRY",
    "CoverageEntry",
    "register_stage",
    "registered_stages",
    "get_stage",
    "increment_stage",
    "reset_registry",
    "LOGGER_DROP_TOTAL_STAGE",
    "increment_logger_drop_total",
]


# META-SI round 2 (MUT-3, site :181 cycle_start): a single well-known
# stage_id used as the destination for typed-except logger faults. Kept
# as a module constant so callsites stay data-driven (no enum branching,
# no per-call string literal drift).
LOGGER_DROP_TOTAL_STAGE = "si.cycle.logger_drop_total"


@dataclass(frozen=True, slots=True)
class CoverageEntry:
    """A single registered stage coverage entry.

    ``count`` is the number of times the stage has been incremented.
    ``payload`` is an optional immutable mapping snapshot the caller
    supplied at registration time.
    """

    stage_id: str
    count: int = 0
    payload: Mapping[str, Any] = field(default_factory=dict)


class _CoverageRegistry:
    """Thread-safe registry mapping ``stage_id`` → :class:`CoverageEntry`."""

    def __init__(self) -> None:
        self._entries: dict[str, CoverageEntry] = {}
        self._lock = threading.RLock()

    def register(
        self,
        stage_id: str,
        *,
        payload: Mapping[str, Any] | None = None,
    ) -> CoverageEntry:
        """Register ``stage_id`` if absent; return current entry.

        Re-registering an existing ``stage_id`` is a no-op for the count
        but refreshes the payload snapshot (last-writer-wins). This keeps
        the registry resilient to module reloads without losing prior
        increments.
        """
        sid = str(stage_id)
        if not sid:
            raise ValueError("stage_id must be a non-empty string")
        with self._lock:
            existing = self._entries.get(sid)
            if existing is None:
                entry = CoverageEntry(
                    stage_id=sid,
                    count=0,
                    payload=dict(payload or {}),
                )
            else:
                entry = CoverageEntry(
                    stage_id=sid,
                    count=existing.count,
                    payload=dict(payload or {}) if payload is not None else dict(existing.payload),
                )
            self._entries[sid] = entry
            return entry

    def increment(self, stage_id: str, *, delta: int = 1) -> CoverageEntry:
        """Increment the count for ``stage_id`` (auto-registers if absent)."""
        sid = str(stage_id)
        if not sid:
            raise ValueError("stage_id must be a non-empty string")
        if not isinstance(delta, int):
            raise TypeError("delta must be int")
        with self._lock:
            existing = self._entries.get(sid)
            new_count = (existing.count if existing is not None else 0) + delta
            payload = dict(existing.payload) if existing is not None else {}
            entry = CoverageEntry(stage_id=sid, count=new_count, payload=payload)
            self._entries[sid] = entry
            return entry

    def get(self, stage_id: str) -> CoverageEntry | None:
        with self._lock:
            return self._entries.get(str(stage_id))

    def stages(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._entries.keys()))

    def reset(self) -> None:
        with self._lock:
            self._entries.clear()


# Process-wide singleton; harmless if unused.
STAGE_COVERAGE_REGISTRY = _CoverageRegistry()


def register_stage(stage_id: str, *, payload: Mapping[str, Any] | None = None) -> CoverageEntry:
    """Module-level convenience wrapper around the singleton."""
    return STAGE_COVERAGE_REGISTRY.register(stage_id, payload=payload)


def increment_stage(stage_id: str, *, delta: int = 1) -> CoverageEntry:
    """Module-level convenience wrapper around the singleton."""
    return STAGE_COVERAGE_REGISTRY.increment(stage_id, delta=delta)


def get_stage(stage_id: str) -> CoverageEntry | None:
    """Module-level convenience wrapper around the singleton."""
    return STAGE_COVERAGE_REGISTRY.get(stage_id)


def registered_stages() -> tuple[str, ...]:
    """Return the sorted tuple of registered stage_ids (may be empty)."""
    return STAGE_COVERAGE_REGISTRY.stages()


def reset_registry() -> None:
    """Reset the singleton — intended for tests only."""
    STAGE_COVERAGE_REGISTRY.reset()


def increment_logger_drop_total(
    *,
    delta: int = 1,
    payload: Mapping[str, Any] | None = None,
) -> CoverageEntry:
    """Increment the well-known ``LOGGER_DROP_TOTAL_STAGE`` counter.

    Convenience wrapper for typed-except handlers around
    :mod:`logging.Handler` calls: callers can record that a logger fault
    was swallowed without re-deriving the stage_id at every site. Pure
    delegation to :func:`increment_stage`; behaviour identical apart from
    the fixed stage_id.

    META-SI round 3 additive extension (AP-11 preserved): optional
    ``payload`` kwarg lets callsites tag the swallowed fault with a
    discriminator (e.g. ``{"site": "critic_evidence_emit"}``) so audits
    can split the aggregate counter by origin without losing the round-2
    aggregate contract. When ``payload`` is supplied, it is merged into a
    per-site sub-counter dict stored under the canonical entry's payload
    (key ``"per_site"``: ``{site_name: count}``); the top-level
    ``count`` field remains the aggregate total. Round-2 callers that
    pass no payload are unaffected (the wrapper returns the same
    CoverageEntry shape as before, just with an empty per-site map).
    """
    with STAGE_COVERAGE_REGISTRY._lock:  # noqa: SLF001 — single-module invariant
        entry = STAGE_COVERAGE_REGISTRY.increment(
            LOGGER_DROP_TOTAL_STAGE, delta=delta
        )
        if payload is not None:
            site = str(payload.get("site", "")).strip()
            if site:
                existing_payload = dict(entry.payload)
                per_site = dict(existing_payload.get("per_site", {}) or {})
                per_site[site] = int(per_site.get(site, 0)) + int(delta)
                existing_payload["per_site"] = per_site
                # Re-register to refresh payload (last-writer-wins on
                # payload, preserves count per `_CoverageRegistry.register`).
                entry = STAGE_COVERAGE_REGISTRY.register(
                    LOGGER_DROP_TOTAL_STAGE, payload=existing_payload
                )
        return entry
