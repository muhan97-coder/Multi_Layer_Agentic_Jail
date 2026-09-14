"""V8 cycle logger — structured event stream over state_store.

Wraps :func:`agi_v8_1.state.store.atomic_append_jsonl` for cycle-scoped event
logging. The logger validates ``event_type`` against an allow-listed set
and injects a deterministic ``timestamp_unix`` field (overridable for tests
via the ``time_fn`` constructor argument).

This module is advisory: it only reads/writes its own JSONL log file. No
provider calls, no subprocess, no network. R18.5 invariant pattern extended.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Final, Mapping

from agi_v8_1.policy.secret_masker import mask_obj  # __SLOT_W1A4__
from agi_v8_1.state.store import atomic_append_jsonl, read_jsonl

# __SLOT_FAIL_FAST_2026_07_25__ Swallowed failures route through one choke
# point: counted + named always, re-raised under AGI_V8_STRICT_FAIL_FAST.
from agi_v8_1.policy.fail_fast import swallowed as _swallowed


EVENT_TYPES: Final[frozenset[str]] = frozenset(
    {
        "cycle_start",
        "agent_step",
        "evidence_emit",
        "decision_record",
        "handoff",
        "si_policy",
        # __SLOT_VERIFY_RESULT_EVENT_2026_08_22__ 관문의 판정을 **이벤트 스트림**에
        # 싣는다. cycle_log.jsonl 에 적는 것으로는 부족했다 — 되먹임(관측 번들)은
        # `read_since` 로 **이 스트림만** 읽고, 그래서 루프는 자기가 지난 사이클에
        # 무엇을 깨뜨렸는지 볼 수단이 없었다(2026-08-22 실측: 3사이클이
        # 30/1 → 30/1 → 24/7 로 나빠지는 동안 사이클 기억에는 3번 다 PASS).
        "verify_result",
        "cycle_end",
        # R4 (C3/C12 반증) — SI↔swarm bridge events. ``dispatch_swarm`` brackets
        # each swarm dispatch with start/end, and ``emit_swarm_observation``
        # records the sanitized pod summary as the swarm→SI feedback loopback.
        # Registered here because ``log_event`` raises ValueError for any
        # event_type outside this whitelist (see :meth:`CycleLogger.log_event`).
        "swarm_dispatch_start",
        "swarm_dispatch_end",
        # __SLOT_FIRST_RUN_STRICT_2026_07_31__ R20 phase-chain transitions.
        # The emitter (orchestrator_v8._run_r20_phase_chain) has existed since
        # R20 but this type was never registered, so EVERY emission failed the
        # whitelist and was silently swallowed — dead telemetry the first
        # autonomous run exposed when strict fail-fast promoted the swallow to
        # an abort. Registered additively; the emitter is unchanged.
        "phase_chain",
        "swarm_observation",
        # __SLOT_PROGRESS_ORACLE_2026_08_22__ bench backlog #2 — the LLM
        # progress-ledger judge's verdict (is_progress_being_made /
        # is_in_loop, adapted from Magentic-One's progress ledger). A
        # DELIBERATELY SEPARATE row from "verify_result"/"cycle_end" (those
        # are completion claims about THIS cycle's changes; this is a
        # trajectory judgment about the trailing N cycles) — folding it into
        # either would mean a reader can no longer tell which of two
        # unrelated verdicts a "status"-shaped key belongs to. Record-only:
        # nothing in self_improvement_v8.run_one_si_cycle reads this event's
        # payload back into a control-flow decision (see
        # runtime/progress_oracle.py module docstring).
        "progress_verdict",
        # __SLOT_DURABLE_GOAL_2026_08_22__ bench backlog #3 — the fact (and
        # content) of a durable-objective reinjection into the S1
        # observation bundle. A DELIBERATELY SEPARATE row from
        # "progress_verdict"/"verify_result"/"cycle_end": those judge or
        # report on cycle OUTCOME; this records an INPUT-side action (what
        # was carried forward into this cycle and why) that happens before
        # any of those verdicts exist. Emitted every armed cycle — including
        # cycles where nothing was reinjected — so "no active goal this
        # cycle" is a named row, never a silent absence (see
        # runtime/durable_goal.py module docstring).
        "goal_reinject",
    }
)


SCHEMA_VERSION: Final[str] = "agi_v8_cycle_logger_v1"


class CycleLogger:
    """Append-only structured event logger for V8 cycles.

    All writes are atomic JSONL lines (via state_store.atomic_append_jsonl).
    Reads return the materialised list of entries (state_store.read_jsonl).
    """

    def __init__(
        self,
        log_path: Path,
        *,
        time_fn: Callable[[], float] | None = None,
    ) -> None:
        self.log_path = Path(log_path)
        self._time_fn: Callable[[], float] = time_fn or time.time

    def log_event(
        self,
        *,
        event_type: str,
        payload: Mapping[str, Any],
        cycle_id: str,
    ) -> None:
        """Append one structured event line.

        Args:
          event_type: must be in :data:`EVENT_TYPES`.
          payload: JSON-serializable mapping. Stored verbatim under
            ``"payload"``.
          cycle_id: stable id tying this event to a cycle.

        Raises:
          ValueError: if ``event_type`` is unknown.
        """

        if event_type not in EVENT_TYPES:
            raise ValueError(
                f"unknown event_type {event_type!r}; "
                f"allowed = {sorted(EVENT_TYPES)!r}"
            )
        # __SLOT_W1A4__: mask secrets in payload before JSONL serialization.
        # Closes leak surfaces: orchestrator_v8 hook_error, apply_chain
        # records, dashboard tail. mask_obj returns a new structure; the
        # caller's payload mapping is never mutated.
        raw_payload = dict(payload) if payload is not None else {}
        masked_payload = mask_obj(raw_payload)
        entry: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "timestamp_unix": float(self._time_fn()),
            "event_type": event_type,
            "cycle_id": str(cycle_id),
            "payload": masked_payload,
        }
        atomic_append_jsonl(self.log_path, entry)

    def read_recent(
        self,
        *,
        since_cycle_id: str | None = None,
        limit: int = 100,
    ) -> list[Mapping[str, Any]]:
        """Return up to ``limit`` most-recent events.

        If ``since_cycle_id`` is provided, only events whose ``cycle_id``
        matches that id are returned. The result preserves write order.
        """

        if limit <= 0:
            return []
        entries: list[Mapping[str, Any]] = list(read_jsonl(self.log_path))
        if since_cycle_id is not None:
            sid = str(since_cycle_id)
            entries = [e for e in entries if str(e.get("cycle_id", "")) == sid]
        return entries[-int(limit):]

    def read_since(
        self,
        cursor: float,
        *,
        event_type: str | None = None,
        cycle_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return events with ``timestamp_unix`` strictly greater than *cursor*.

        The result is sorted by ``timestamp_unix`` ascending (write order is
        preserved when timestamps are equal — JSONL scan order is stable).

        Args:
          cursor: Unix timestamp (float). Events at exactly this timestamp are
            excluded (strictly-after semantics, matching v7.1 read_since).
          event_type: Optional filter; only events whose ``event_type`` matches
            this string are included.
          cycle_id: Optional filter; only events belonging to this cycle are
            included.
          limit: Optional cap on returned entries.  ``None`` means unlimited.

        Returns:
          Filtered, sorted list of raw event dicts. Callers own the list but
          should not mutate the individual dicts (they are live references to
          the parsed JSONL values).
        """
        threshold = float(cursor)
        entries: list[dict[str, Any]] = read_jsonl(self.log_path)
        result: list[dict[str, Any]] = []
        for entry in entries:
            ts = entry.get("timestamp_unix")
            try:
                ts_val = float(ts)  # type: ignore[arg-type]
            except (TypeError, ValueError) as _ff_exc:
                _swallowed(_ff_exc, site="core.cycle_logger.read_since:155", category="telemetry")
                continue
            if ts_val <= threshold:
                continue
            if event_type is not None and entry.get("event_type") != event_type:
                continue
            if cycle_id is not None and str(entry.get("cycle_id", "")) != str(cycle_id):
                continue
            result.append(entry)
        # Stable ascending sort; JSONL write order preserved for equal ts.
        result.sort(key=lambda e: float(e.get("timestamp_unix", 0.0)))
        if limit is not None:
            result = result[: int(limit)]
        return result

    def head_timestamp(self) -> float | None:
        """Return the ``timestamp_unix`` of the most-recent event, or None.

        Scans the whole log; suitable for use as a cursor seed when the caller
        wants to receive only events written *after* the current head.
        """
        entries: list[dict[str, Any]] = read_jsonl(self.log_path)
        latest: float | None = None
        for entry in entries:
            ts = entry.get("timestamp_unix")
            try:
                ts_val = float(ts)  # type: ignore[arg-type]
            except (TypeError, ValueError) as _ff_exc:
                _swallowed(_ff_exc, site="core.cycle_logger.head_timestamp:182", category="telemetry")
                continue
            if latest is None or ts_val > latest:
                latest = ts_val
        return latest

    def __repr__(self) -> str:
        return f"CycleLogger(log_path={self.log_path!s})"


__all__ = [
    "CycleLogger",
    "EVENT_TYPES",
    "SCHEMA_VERSION",
    # __SLOT_W8_CYCLE_LOGGER_READ_SINCE_2026_05_31__
]
