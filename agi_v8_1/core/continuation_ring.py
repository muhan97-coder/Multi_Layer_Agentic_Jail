"""V8 continuation_cycle_count ring buffer — StatusHistoryRing.

Restores the v7.1 orchestrator._status_history list and continuation_cycle_count
cross-cycle state that v8's stateless DeterministicMockExecutor dropped.

Design:
  - StatusHistoryRing is a 60-entry capped ring of (cycle, status, decision,
    timestamp) tuples, keyed by (run_id, session_id, pod).
  - append() adds a new entry, trimming to max_size from the right (newest kept).
  - tail(n) returns the last n entries.
  - to_list() returns all entries as plain dicts (JSON-serialisable).
  - JSONL persistence sidecar under state/continuation_ring/<run_id>.jsonl via
    agi_v8_1.state.store.atomic_append_jsonl.

Env gate: AGI_V8_CONTINUATION_RING_ENABLED — must be set to "true" or "1"
(case-sensitive) for ring writes and persistence to be active. When the env var
is absent or set to any other value the module exports no-op stubs so all callers
are safe to import unconditionally.

Read by: load_previous_run_context (feature #2), cycle_logger_read_since (feature
#5), and review_phase_v8.py when computing recent_cycle_logs/cont_count.

Section 6 STEP1-PLANNED row 25 contract: no-op when env gate is unset.

Slot marker: __SLOT_W8_CONTINUATION_RING_2026_05_31__
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from agi_v8_1.state.store import atomic_append_jsonl, read_jsonl

# __SLOT_FAIL_FAST_2026_07_25__ 삼키는 지점은 이름을 붙여 센다.
from agi_v8_1.policy.fail_fast import swallowed as _swallowed


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION: str = "agi_v8_continuation_ring_v1"
DEFAULT_MAX_SIZE: int = 60
_ENV_GATE: str = "AGI_V8_CONTINUATION_RING_ENABLED"
_SIDECAR_DIR: str = "state/continuation_ring"

# ---------------------------------------------------------------------------
# Status-key alignment: v7.1 wire strings → v8 ring status keys
# ---------------------------------------------------------------------------

# v7.1 orchestrator used TWO separate channels for what v8 collapses into one
# ring struct:
#
#   Channel A — current_state.status (string field set each iteration):
#     'continuation_pending' — ContinuationAgent returned 'continue'
#                              (orchestrator.py:3537/3545/3555)
#     'accepted'             — ContinuationAgent returned completion and
#                              critic accepted; count reset to 0
#                              (orchestrator.py:3571-3572)
#     'escalated'            — ContinuationAgent returned 'escalate' with
#                              no override (orchestrator.py:3550)
#     'rejected'             — auto-goal rejected by ethics checker
#                              (orchestrator.py:3409-3410); rare, not part of
#                              the main continuation loop
#
#   Channel B — _status_history (list[str], capped at 16 entries):
#     Only EVER appended with the literal string 'escalated', at
#     orchestrator.py:5781-5785.  continuation_pending / accepted were never
#     recorded here in v7.1.  The list was passed to build_cycle_policy() as
#     previous_state_status_history at orchestrator.py:3343 so the policy
#     engine could see how many recent escalation events had occurred.
#
# v8 intentional conflation:
#   StatusHistoryRing collapses both channels into one ring, storing the full
#   set of status strings from Channel A.  This is a SUPERSET of v7.1
#   _status_history (Channel B only ever had 'escalated').  The conflation is
#   documented here and in migration_ledger Section 6 P2 so future callers
#   know that ring entries with status='escalated' are the semantic equivalent
#   of v7.1 _status_history entries, while 'continuation_pending' / 'accepted'
#   entries have no v7.1 _status_history equivalent.
#
# Canonical mapping tuple — (v7.1_wire_string, v8_ring_status, channel):
STATUS_ALIASES: tuple[tuple[str, str, str], ...] = (
    # v7.1 channel-A status         v8 ring status          v7.1 channel
    ("continuation_pending",         "continuation_pending",  "current_state.status"),
    ("accepted",                     "accepted",              "current_state.status"),
    ("escalated",                    "escalated",             "current_state.status + _status_history"),
    ("rejected",                     "rejected",              "current_state.status"),
)


def _is_enabled() -> bool:
    """Return True when the env gate is active (exact string "true" or "1")."""
    return os.environ.get(_ENV_GATE, "") in ("true", "1")  # tier: T5


# ---------------------------------------------------------------------------
# Core dataclass
# ---------------------------------------------------------------------------

@dataclass
class RingEntry:
    """One recorded cycle snapshot."""
    cycle: int
    status: str
    decision: str
    timestamp: float


@dataclass
class StatusHistoryRing:
    """60-entry ring buffer for continuation cycle state per (run_id, session_id, pod).

    All mutation methods are no-ops when AGI_V8_CONTINUATION_RING_ENABLED is
    not "true" or "1", so callers can import and call unconditionally.

    Args:
        run_id:     Opaque run identifier (used as JSONL sidecar filename stem).
        session_id: Session scope string (stored per entry for filtering).
        pod:        Pod label (stored per entry for filtering).
        max_size:   Maximum entries to retain (default 60, matching v7.1's
                    16-entry _status_history but extended to 60 for V8 scale).
        state_dir:  Root directory for JSONL sidecars. Defaults to
                    ``state/continuation_ring`` relative to cwd; callers should
                    pass an absolute path for production use.
        time_fn:    Callable returning float epoch seconds (injectable for tests).
        persist:    Whether to write entries to a JSONL sidecar (default True
                    when env gate is active; set False in unit tests to skip I/O).
    """

    run_id: str
    session_id: str
    pod: str
    max_size: int = DEFAULT_MAX_SIZE
    state_dir: Path = field(default_factory=lambda: Path(_SIDECAR_DIR))
    time_fn: Callable[[], float] = field(default_factory=lambda: time.time)
    persist: bool = True

    # Internal ring storage — not part of the public init signature.
    _entries: list[RingEntry] = field(default_factory=list, init=False, repr=False)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def append(
        self,
        *,
        cycle: int,
        status: str,
        decision: str,
    ) -> None:
        """Append a new entry to the ring buffer.

        When the env gate is off this is a no-op. When the buffer reaches
        max_size the oldest entry is discarded (FIFO trim from the left).
        The entry is also appended to the JSONL sidecar when persist=True.
        """
        if not _is_enabled():
            return

        entry = RingEntry(
            cycle=int(cycle),
            status=str(status),
            decision=str(decision),
            timestamp=float(self.time_fn()),
        )
        self._entries.append(entry)
        if len(self._entries) > self.max_size:
            self._entries = self._entries[-self.max_size:]

        if self.persist:
            self._write_sidecar(entry)

    def tail(self, n: int) -> list[RingEntry]:
        """Return the last *n* entries (or fewer if buffer is smaller).

        Returns an empty list when the env gate is off.
        """
        if not _is_enabled():
            return []
        n = max(0, int(n))
        return list(self._entries[-n:]) if n else []

    def to_list(self) -> list[dict[str, Any]]:
        """Return all entries as JSON-serialisable dicts.

        Returns an empty list when the env gate is off.
        """
        if not _is_enabled():
            return []
        return [
            {
                "cycle": e.cycle,
                "status": e.status,
                "decision": e.decision,
                "timestamp": e.timestamp,
                "run_id": self.run_id,
                "session_id": self.session_id,
                "pod": self.pod,
            }
            for e in self._entries
        ]

    def continuation_cycle_count(self) -> int:
        """Return the current in-memory continuation cycle count.

        Mirrors v7.1 current_state.continuation_cycle_count: the number of
        consecutive ``continuation_pending`` entries at the tail of the ring.
        Resets to 0 on the first ``accepted`` or ``escalated`` terminal entry.

        v7.1 cross-reference:
          - Incremented at orchestrator.py:3537 (goal_status='continue'),
            :3545 (escalation override → continue), :3555 (queue still open).
          - Reset to 0 at orchestrator.py:3571 on ``accepted``; also reset at
            :5491 on ``accepted`` or ``escalated`` (auto-clear path).
          - This method counts only ``continuation_pending`` entries, which
            corresponds to Channel A (current_state.status).  It does NOT
            mirror v7.1 _status_history (Channel B), which only ever stored
            the literal string ``'escalated'`` — see STATUS_ALIASES and
            escalated_count() for the Channel B equivalent.

        Returns 0 when the env gate is off.
        """
        if not _is_enabled():
            return 0
        count = 0
        for entry in reversed(self._entries):
            if entry.status in ("continuation_pending",):
                count += 1
            else:
                break
        return count

    def escalated_count(self) -> int:
        """Return the total number of ``escalated`` entries in the ring.

        Mirrors v7.1 _status_history append pattern: orchestrator.py:5781-5785
        appended the literal string ``'escalated'`` to _status_history (capped
        at 16 entries) on every escalation event, and that list was consumed by
        build_cycle_policy() as ``previous_state_status_history``.

        This method is the v8 equivalent: it counts all ring entries whose
        status == ``'escalated'`` (not just trailing ones, matching the v7.1
        behaviour where _status_history accumulated all escalation events
        across the session, not just the last consecutive run).

        Returns 0 when the env gate is off.
        """
        if not _is_enabled():
            return 0
        return sum(1 for e in self._entries if e.status == "escalated")

    def trailing_escalated_count(self) -> int:
        """Return the number of TRAILING consecutive ``escalated`` entries.

        Unlike :meth:`escalated_count` (which counts ALL escalated entries in
        the ring), this counts only the consecutive run of ``escalated`` entries
        at the tail — any non-escalated terminal entry resets the count to 0.

        This is the exact v7.1 ``build_cycle_policy`` escalate_streak semantics
        (``cycle_policy.py``:265-271): the policy engine reverse-scanned
        ``previous_state_status_history`` and stopped at the first non-escalated
        entry, so a single ``accepted`` / ``continuation_pending`` between two
        escalations breaks the streak. Consumed by
        :func:`agi_v8_1.core.cycle_policy_v81.build_cycle_policy_v81` (R6 / C4).

        Returns 0 when the env gate is off.
        """
        if not _is_enabled():
            return 0
        count = 0
        for entry in reversed(self._entries):
            if entry.status == "escalated":
                count += 1
            else:
                break
        return count

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    def _sidecar_path(self) -> Path:
        return Path(self.state_dir) / f"{self.run_id}.jsonl"

    def _write_sidecar(self, entry: RingEntry) -> None:
        record: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "pod": self.pod,
            "cycle": entry.cycle,
            "status": entry.status,
            "decision": entry.decision,
            "timestamp": entry.timestamp,
        }
        # __SLOT_LEDGER_JOIN_2026_08_08__ ⚠️ 위 ``cycle`` 은 런 내 **정수 순번**이지
        # 신원이 아니다 — 그걸 사이클 신원으로 읽으면 **모든 런의 1번 사이클이 한
        # 덩어리로 조인된다**. 순번은 그대로 두고 신원을 **옆에** 붙인다.
        # 게이트 OFF 면 빈 dict 라 행이 종전과 byte-identical.
        try:
            from agi_v8_1.runtime.ledger_join import join_keys

            record.update(join_keys())
        except Exception as _join_exc:  # noqa: BLE001 — 조인키가 링을 죽이면 안 된다
            _swallowed(_join_exc, site="core.continuation_ring._write_sidecar:join_keys",
                       category="telemetry")
        atomic_append_jsonl(self._sidecar_path(), record)

    # ------------------------------------------------------------------
    # Class-level loader (for feature #2 load_previous_run_context)
    # ------------------------------------------------------------------

    @classmethod
    def load_from_sidecar(
        cls,
        *,
        run_id: str,
        session_id: str,
        pod: str,
        state_dir: Path | str = _SIDECAR_DIR,
        max_size: int = DEFAULT_MAX_SIZE,
        time_fn: Callable[[], float] | None = None,
    ) -> "StatusHistoryRing":
        """Reconstruct a StatusHistoryRing from an existing JSONL sidecar.

        Returns an empty ring when the env gate is off or the sidecar does not
        exist.  Only the last ``max_size`` entries from the file are loaded to
        honour the ring contract.
        """
        ring = cls(
            run_id=run_id,
            session_id=session_id,
            pod=pod,
            max_size=max_size,
            state_dir=Path(state_dir),
            time_fn=time_fn or time.time,
            persist=False,  # avoid re-writing already-persisted entries
        )
        if not _is_enabled():
            return ring

        sidecar = ring._sidecar_path()
        rows = read_jsonl(sidecar)
        # Filter to this (run_id, session_id, pod) triple in case the file was
        # shared (future multi-pod sidecar layout).
        matching = [
            r for r in rows
            if r.get("run_id") == run_id
            and r.get("session_id") == session_id
            and r.get("pod") == pod
        ]
        # Honour max_size from the tail of the file.
        for row in matching[-max_size:]:
            ring._entries.append(
                RingEntry(
                    cycle=int(row.get("cycle", 0)),
                    status=str(row.get("status", "")),
                    decision=str(row.get("decision", "")),
                    timestamp=float(row.get("timestamp", 0.0)),
                )
            )
        ring.persist = True  # re-enable persistence for future appends
        return ring


# ---------------------------------------------------------------------------
# Module-level exports
# ---------------------------------------------------------------------------

__all__ = [
    "StatusHistoryRing",
    "RingEntry",
    "DEFAULT_MAX_SIZE",
    "SCHEMA_VERSION",
    "STATUS_ALIASES",
    "_is_enabled",
]
