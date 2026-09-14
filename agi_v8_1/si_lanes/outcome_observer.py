"""SI outcome_observer lane logic (R20 W1 — port of v7.1 outcomes reader).

v7.1 self_improvement_agent.py reads recent session outcomes to drive
proposal generation. The V8 port distils the file-read + tail computation
into a single :func:`observe_outcomes` function.

The observer is *read-only* — it never writes to the log it reads. No
fabrication: when the log is missing or empty, returns ().

Hard rules:
  - read-only access to outcome_log_path via state_store.read_jsonl
  - no provider calls, no shell, no network
  - deterministic: same input → same output
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, Sequence

from agi_v8_1.state.store import read_jsonl

# __SLOT_FAIL_FAST_2026_07_25__ Swallowed failures route through one choke
# point: counted + named always, re-raised under AGI_V8_STRICT_FAIL_FAST.
from agi_v8_1.policy.fail_fast import swallowed as _swallowed


# v7.1 SI reads the last N entries — chosen so the observer never loads
# more than a bounded slice into memory.
DEFAULT_TAIL_SIZE: int = 10
MAX_TAIL_SIZE: int = 100


def observe_outcomes(
    outcome_log_path: Path | str,
    *,
    tail_size: int = DEFAULT_TAIL_SIZE,
) -> tuple[Mapping[str, object], ...]:
    """Return the last ``tail_size`` outcomes from ``outcome_log_path``.

    Behaviour:
      - missing file → ()
      - empty file → ()
      - tail_size <= 0 → ()
      - tail_size > MAX_TAIL_SIZE → clamped to MAX_TAIL_SIZE
      - returned entries are dict-like (Mapping); callers may rely on
        ``.get("status", ...)`` etc.
    """

    if tail_size <= 0:
        return ()
    capped = min(int(tail_size), MAX_TAIL_SIZE)

    try:
        all_entries = read_jsonl(outcome_log_path)
    except Exception as _ff_exc:
        # state_store.read_jsonl tolerates missing files but bubbles up I/O
        # errors. The observer fail-closes on any exception (no fabrication).
        _swallowed(_ff_exc, site="si_lanes.outcome_observer.observe_outcomes:52", category="verify")
        return ()

    if not all_entries:
        return ()

    return tuple(all_entries[-capped:])


def summarize_outcomes(
    outcomes: Sequence[Mapping[str, object]],
) -> dict[str, int]:
    """Bucketize outcomes by ``status`` field.

    Useful for the proposer lane to detect repeated failure patterns
    without scanning the raw entries. Unknown / missing statuses are
    bucketed under ``"unknown"``.
    """

    buckets: dict[str, int] = {}
    for entry in outcomes:
        if not isinstance(entry, Mapping):
            continue
        status = str(entry.get("status", "unknown")) or "unknown"
        buckets[status] = buckets.get(status, 0) + 1
    return buckets


__all__ = [
    "DEFAULT_TAIL_SIZE",
    "MAX_TAIL_SIZE",
    "observe_outcomes",
    "summarize_outcomes",
]
