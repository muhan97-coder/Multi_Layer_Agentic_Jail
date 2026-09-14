"""R6 — escalation policy revival: ``build_cycle_policy_v81`` (audit claim C4).

Reverses audit claim **C4 CONFIRMED** ("v8 has NO ``escalate_streak`` /
``force_replan`` / ``auto_head_reset`` symbols at all — the v7.1 escalation
policy that turned 3 consecutive escalations into a forced replan + head-state
reset was dropped entirely during the v8 refactor").

v7.1 reference: ``agent_system/core/cycle_policy.py``:
  - :203-224 ``_auto_reset_head_state`` (backup-then-reset the head state file)
  - :265-271 escalate_streak (reverse-scan ``previous_state_status_history`` for
             trailing consecutive ``'escalated'`` entries)
  - :272-276 ``force_replan = streak >= 3`` + ``head_auto_reset`` opt-out env

v8.1 deltas vs v7.1 (PART1 §3):
  - The SSOT for escalation history is now :class:`StatusHistoryRing`
    (``core/continuation_ring.py``) — the policy consumes
    ``ring.trailing_escalated_count()`` instead of a raw status list. The ring's
    ``escalated_count()`` returns the TOTAL escalated entries, but the v7.1
    semantics require the trailing *consecutive* run (reset by any non-escalated
    terminal entry), so R6 adds ``trailing_escalated_count()`` to the ring and
    consumes that here (PART1 §3.1).
  - v7.1's RuntimeManifest / SystemConfig / provider-route machinery is replaced
    by the v8 swarm topology, so this port drops it. The preserved core is the
    escalation arithmetic + the ``emergency_regression_threshold`` /
    ``self_improvement_max_proposals`` env-derived cadence knobs (PART1 §3.4).

Design principles honoured:
  - NO mode enum / hardcoded branch — the policy is a frozen *data* class, every
    field is derived from data (ring counts + env strings)
    (``feedback_no_mode_enums_2026_05_28``).
  - The ``head_auto_reset`` *signal* is computed here; the actual destructive
    head-state reset is performed by the SI loop only when the operator gate
    permits, and always backs up first (``feedback_no_git_backup_switching``).
  - Default-ON read path: when ``AGI_V81_CYCLE_POLICY_ENABLED`` is ON the policy
    consumes the ring; the ring's OWN env gate
    (``AGI_V8_CONTINUATION_RING_ENABLED``) still governs whether it has any
    history to read, so a gate-off ring yields ``streak == 0`` (no forced
    replan) — exactly the pre-R6 behaviour.
"""

from __future__ import annotations

import datetime
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

# Consumed for the trailing-escalated count (the v8 SSOT for escalation
# history). Imported at module load so the wiring grep (anti-amnesia test)
# can assert the dependency edge.
from agi_v8_1.core.continuation_ring import StatusHistoryRing

# __SLOT_FAIL_FAST_2026_07_25__ Swallowed failures route through one choke
# point: counted + named always, re-raised under AGI_V8_STRICT_FAIL_FAST.
from agi_v8_1.policy.fail_fast import swallowed as _swallowed

SCHEMA_VERSION: str = "agi_v8_cycle_policy_v81_v1"

# --- env gate names -----------------------------------------------------------
# Master read gate for the escalation policy. Default-ON (the learning read loop
# is the point — PART1 §0.4 / §5.1). When OFF the policy short-circuits to a
# neutral snapshot (streak=0, force_replan=False) so the cycle behaves exactly
# as it did before R6.
_ENV_POLICY_ENABLED: str = "AGI_V81_CYCLE_POLICY_ENABLED"
# Opt-out for the auto head-state reset SIGNAL (the actual reset is still gated
# by the SI loop + always backs up first). Default-ON, opt-out via "false".
_ENV_HEAD_AUTO_RESET: str = "AGI_V81_HEAD_AUTO_RESET_ENABLED"

# v7.1 :272 — the forced-replan threshold. 3 consecutive escalations → replan.
FORCE_REPLAN_THRESHOLD: int = 3


def _env_get(env: Mapping[str, str] | None, key: str, default: str = "") -> str:
    """Read an env key from the supplied mapping or ``os.environ``."""
    source = env if env is not None else os.environ
    return str(source.get(key, default))


def _policy_enabled(env: Mapping[str, str] | None) -> bool:
    """Default-ON master gate. True unless explicitly set to a false-y value.

    Mirrors the v7.1 opt-out convention: unset == enabled. Any of
    ``0/false/no/off/never/disabled`` (case-insensitive) disables it.
    """
    text = _env_get(env, _ENV_POLICY_ENABLED, "").strip().lower()
    if not text:
        return True
    return text not in {"0", "false", "no", "off", "never", "disabled"}


def _head_auto_reset_enabled(env: Mapping[str, str] | None) -> bool:
    """Default-ON opt-out for the head-auto-reset signal (v7.1 :273 parity)."""
    text = _env_get(env, _ENV_HEAD_AUTO_RESET, "").strip().lower()
    return text != "false"


def _safe_int(value: Any, default: int) -> int:
    """Parse *value* as an int, falling back to *default*.

    An absent/empty value is a NORMAL "not configured" case, so it takes the
    default WITHOUT raising — the exception path is reserved for a value that
    was actually supplied and is malformed. Before this, an unset env var made
    ``int("")`` raise twice per cycle and the handler absorbed it, which meant
    the swallow census could never distinguish "nothing configured" (fine) from
    "someone typed a bad number" (worth knowing).
    """
    text = "" if value is None else str(value).strip()
    if not text:
        return default
    try:
        return int(text)
    except (TypeError, ValueError) as _ff_exc:
        _swallowed(_ff_exc, site="core.cycle_policy_v81._safe_int:99", category="telemetry")
        return default


@dataclass(frozen=True, slots=True)
class CyclePolicyV81:
    """One frozen escalation-policy snapshot for a single SI cycle.

    Pure data — no methods branch on a mode enum. Every field is derived from
    the ring's escalation history + env-derived knobs.

    Fields:
      schema_version       stable schema tag (audit/replay).
      cycle_n              1-based cycle ordinal (prev_cycle_log length + 1).
      escalate_streak      trailing consecutive ``escalated`` entries in the
                           ring (0 when the policy gate or the ring gate is OFF).
      force_replan         ``escalate_streak >= FORCE_REPLAN_THRESHOLD`` (v7.1
                           :272). The SI loop consumes this as a real branch
                           (PART1 §3.3 — fixes v7.1's DEAD-OUTPUT defect).
      head_auto_reset      ``force_replan AND head_auto_reset_enabled`` — the
                           SIGNAL to back-up-then-reset the head state file. The
                           actual reset is performed by the SI loop, not here.
      emergency_regression_threshold  env-derived BLOCK threshold (v7.1 :257).
      self_improvement_max_proposals  env-derived proposal cap (v7.1 :311).
      policy_enabled       whether the master read gate was ON this cycle.
    """

    schema_version: str = SCHEMA_VERSION
    cycle_n: int = 1
    escalate_streak: int = 0
    force_replan: bool = False
    head_auto_reset: bool = False
    emergency_regression_threshold: int = 3
    self_improvement_max_proposals: int = 10
    policy_enabled: bool = True

    def as_dict(self) -> dict[str, Any]:
        """JSON-serialisable snapshot for cycle_log payloads / audits."""
        return {
            "schema_version": self.schema_version,
            "cycle_n": self.cycle_n,
            "escalate_streak": self.escalate_streak,
            "force_replan": self.force_replan,
            "head_auto_reset": self.head_auto_reset,
            "emergency_regression_threshold": self.emergency_regression_threshold,
            "self_improvement_max_proposals": self.self_improvement_max_proposals,
            "policy_enabled": self.policy_enabled,
        }


def _prev_cycle_count(prev_cycle_log: Any) -> int:
    """Best-effort count of prior cycles from a cycle_log tail.

    Accepts a list of event dicts (the ``read_since`` / ``read_recent`` shape)
    or an int already-counted value. Counts DISTINCT ``cycle_id`` values so a
    multi-event cycle (cycle_start + si_policy + cycle_end) counts once. Returns
    0 for None / empty / unparseable input — never raises.
    """
    if prev_cycle_log is None:
        return 0
    if isinstance(prev_cycle_log, int):
        return max(0, prev_cycle_log)
    if isinstance(prev_cycle_log, Mapping):
        return 0
    try:
        seen: set[str] = set()
        for ev in prev_cycle_log:
            if isinstance(ev, Mapping):
                cid = str(ev.get("cycle_id", ""))
                if cid:
                    seen.add(cid)
        return len(seen)
    except TypeError as _ff_exc:
        _swallowed(_ff_exc, site="core.cycle_policy_v81._prev_cycle_count:171", category="telemetry")
        return 0


def build_cycle_policy_v81(
    *,
    ring: StatusHistoryRing | None,
    prev_cycle_log: Any = None,
    env: Mapping[str, str] | None = None,
) -> CyclePolicyV81:
    """Build one escalation-policy snapshot for the upcoming SI cycle (PART1 §3).

    Args:
      ring:           the :class:`StatusHistoryRing` SSOT for escalation history.
                      ``None`` (or a gate-off ring) yields ``escalate_streak=0``.
      prev_cycle_log: optional cycle_log tail (list of event dicts) or an int
                      cycle count — used only to seed ``cycle_n``.
      env:            optional env mapping override (defaults to ``os.environ``).

    Returns:
      A frozen :class:`CyclePolicyV81`. When the master gate
      (``AGI_V81_CYCLE_POLICY_ENABLED``) is OFF, returns a neutral snapshot
      (streak=0, force_replan=False, head_auto_reset=False) so the cycle behaves
      byte-equivalently to the pre-R6 path.

    Escalation arithmetic (v7.1 :265-276 port):
      ``streak = ring.trailing_escalated_count()`` — trailing CONSECUTIVE
      escalated entries (reset by any non-escalated terminal entry). This is the
      v8 SSOT replacement for v7.1's reverse-scan over
      ``previous_state_status_history``. ``force_replan = streak >= 3``;
      ``head_auto_reset = force_replan AND head_auto_reset_enabled``.
    """
    enabled = _policy_enabled(env)
    cycle_n = max(1, _prev_cycle_count(prev_cycle_log) + 1)

    emergency_threshold = _safe_int(
        _env_get(env, "EMERGENCY_REGRESSION_THRESHOLD")
        or _env_get(env, "CRITIC_EMERGENCY_REGRESSION_THRESHOLD"),
        default=3,
    )
    max_proposals = _safe_int(
        _env_get(env, "SELF_IMPROVEMENT_MAX_PROPOSALS"),
        default=10,
    )

    if not enabled:
        # Gate OFF → neutral snapshot. No ring consumption, no replan.
        return CyclePolicyV81(
            cycle_n=cycle_n,
            escalate_streak=0,
            force_replan=False,
            head_auto_reset=False,
            emergency_regression_threshold=emergency_threshold,
            self_improvement_max_proposals=max_proposals,
            policy_enabled=False,
        )

    # --- escalate_streak (v7.1 :265-271 → ring trailing count) ---
    # The ring's OWN env gate still governs whether it has any history; a
    # gate-off ring's trailing_escalated_count() returns 0, so streak stays 0.
    streak = 0
    if ring is not None:
        try:
            streak = int(ring.trailing_escalated_count())
        except (AttributeError, TypeError, ValueError) as _ff_exc:
            _swallowed(_ff_exc, site="core.cycle_policy_v81.build_cycle_policy_v81:235", category="telemetry")
            streak = 0

    force_replan = streak >= FORCE_REPLAN_THRESHOLD
    head_auto_reset = force_replan and _head_auto_reset_enabled(env)

    return CyclePolicyV81(
        cycle_n=cycle_n,
        escalate_streak=streak,
        force_replan=force_replan,
        head_auto_reset=head_auto_reset,
        emergency_regression_threshold=emergency_threshold,
        self_improvement_max_proposals=max_proposals,
        policy_enabled=True,
    )


def auto_reset_head_state(state_path: Path) -> bool:
    """Back-up-then-reset the head-state file (v7.1 :203-224 port).

    Performed by the SI loop only when ``policy.head_auto_reset`` is True. Backs
    up the existing state to ``current_state_auto.backup_<ts>.json`` BEFORE
    overwriting it with a fresh planning-stage head (``feedback_no_git_backup_
    switching`` — destructive reset always backs up first).

    Returns True when a reset was performed, False when the file did not exist
    (nothing to reset) — never raises on a missing file.
    """
    state_path = Path(state_path)
    if not state_path.exists():
        return False
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = state_path.parent / f"current_state_auto.backup_{ts}.json"
    shutil.copy2(state_path, backup_path)
    try:
        existing = json.loads(state_path.read_text(encoding="utf-8"))
        session_id = existing.get("session_id", "")
    except (OSError, ValueError, AttributeError) as _ff_exc:
        _swallowed(_ff_exc, site="core.cycle_policy_v81.auto_reset_head_state:272", category="telemetry")
        session_id = ""
    reset_state = {
        "session_id": session_id,
        "status": "planning",
        "current_stage": "strategist_replan",
        "open_tasks": [],
        "completed_tasks": [],
        "blocked_tasks": [],
        "notes": ["auto_head_reset after 3+ escalations"],
    }
    state_path.write_text(
        json.dumps(reset_state, indent=2) + "\n", encoding="utf-8"
    )
    return True


__all__ = [
    "CyclePolicyV81",
    "build_cycle_policy_v81",
    "auto_reset_head_state",
    "SCHEMA_VERSION",
    "FORCE_REPLAN_THRESHOLD",
]
