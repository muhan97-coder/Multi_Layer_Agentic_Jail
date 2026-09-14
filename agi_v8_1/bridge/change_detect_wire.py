"""bridge/change_detect_wire.py — external_change_detect hook wiring (PART2 §3).

Reverses audit claim **C5** ("external_change_detection이 dead hook —
``run_external_change_detect_hook`` is __all__-exported but called 0 times").

The detector module itself (``external_change_detector.py``, ~424 LOC) already
ships; only the *call site* was missing. This wrapper adds a ``phase`` tag and a
thin pass-through to :func:`run_external_change_detect_hook` so the bridge can
fire it at the 3 designed points (PART2 §3 H1/H2/H3):

  * ``"si_start"``      — SI cycle entry (caller-side; here for completeness).
  * ``"pre_dispatch"``  — before a swarm dispatch (baseline fingerprint).
  * ``"post_dispatch"`` — after a swarm dispatch (reproducibility check).

Gate semantics are inherited verbatim from the hook: env knob
``AGI_V8_EXTERNAL_CHANGE_DETECT_ENABLED`` unset/non-strict → returns None WITHOUT
importing the detector (gate-OFF == byte-identical no-op). The ``phase`` tag is
advisory metadata only — it does not change the hook's behavior.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Mapping

from agi_v8_1.agent_system.swarm_v8.router_freeze import run_external_change_detect_hook


ChangeDetectPhase = Literal["si_start", "pre_dispatch", "post_dispatch"]

_VALID_PHASES: frozenset[str] = frozenset(
    {"si_start", "pre_dispatch", "post_dispatch"}
)


def run_change_detect(
    phase: ChangeDetectPhase,
    *,
    env: Mapping[str, str] | None = None,
    ledger_path: Path | str | None = None,
) -> Any | None:
    """Fire the external-change-detect hook tagged with ``phase``.

    Returns the hook's ChangeReport (or None when the gate is OFF). Invalid
    phase strings raise ValueError so a typo cannot silently disable the wire.
    The ``phase`` is purely a call-site label; the gate + detector logic lives
    in :func:`run_external_change_detect_hook` (AP-5 ledger reuse).
    """
    if phase not in _VALID_PHASES:
        raise ValueError(
            f"unknown change-detect phase {phase!r}; "
            f"allowed = {sorted(_VALID_PHASES)!r}"
        )
    return run_external_change_detect_hook(env=env, ledger_path=ledger_path)


__all__ = ["run_change_detect", "ChangeDetectPhase"]
