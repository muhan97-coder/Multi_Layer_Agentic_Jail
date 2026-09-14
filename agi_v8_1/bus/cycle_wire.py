"""cycle_wire — the SI cycle's single touch-point on the falsifier bus.

# __SLOT_BUS_AUTOWIRE_2026_06_16__

This is the orchestration step that makes ``run_one_si_cycle`` DRIVE the
external-signal loop end to end, rather than leaving the bus to be filled and
read only by hand:

  produce  — run the read-only PnL-surprise bridge (C) to refresh the bus from
             the live trader outcome log.
  register — if the cycle produced a change, pre-register ONE falsifiable
             forward claim ("this change will not let the trader's wrong-signal
             rate rise"), to be graded later against the bus.
  grade    — grade every prediction whose horizon has passed (B), against the
             bus's realized events — NOT the loop's own cycle log.
  observe  — surface a small digest (recent wrong-signals + Brier calibration)
             back to the cycle outcome.

GATED default-OFF (``AGI_V8_BUS_AUTOWIRE_ENABLED``). OBSERVATION-ONLY: it never
blocks, reverts, or changes the cycle verdict — a failure is swallowed and the
cycle proceeds. Everything is contained under the cycle's ``state_dir`` (the
same jail as SafeAutoApply), so a test cycle in a tempdir never writes to the
shared default ledger.

The deliberate, honest payoff: SI proposals today are advisory artifacts that do
not touch the live trader, so these forward claims should grade out near
coin-flip — and the accumulated Brier MEASURES that disconnect instead of
assuming it.
"""

from __future__ import annotations

# Public safety/admission remains importable without optional implementation.
from agi_v8_1.capabilities import PayloadPort, PayloadUnavailable, resolve_payload
import sys as _payload_sys

import logging
import os
from pathlib import Path
from typing import Any

# __SLOT_FAIL_FAST_2026_07_25__ Swallowed failures route through one choke
# point: counted + named always, re-raised under AGI_V8_STRICT_FAIL_FAST.
from agi_v8_1.policy.fail_fast import (
    record_critical_failure,
    format_exception_for_log,
    swallowed as _swallowed,
)

logger = logging.getLogger(__name__)

_ENV_ENABLED = "AGI_V8_BUS_AUTOWIRE_ENABLED"
_ENV_HORIZON_S = "AGI_V8_BUS_PREDICTION_HORIZON_S"
_DEFAULT_HORIZON_S = 86_400.0  # 1 day — the trader settles trades daily
_RECENT_WRONG_N = 5


def bus_autowire_enabled() -> bool:
    """Default OFF — the cycle is byte-equivalent to pre-wire unless turned on."""
    raw = os.environ.get(_ENV_ENABLED)  # tier: T7
    if raw is None:
        return False
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _horizon_s() -> float:
    raw = os.environ.get(_ENV_HORIZON_S)  # tier: T7
    if raw is None:
        return _DEFAULT_HORIZON_S
    try:
        v = float(raw.strip())
    except (TypeError, ValueError) as _ff_exc:
        _swallowed(_ff_exc, site="bus.cycle_wire._horizon_s:60", category="persist")
        return _DEFAULT_HORIZON_S
    return v if v > 0 else _DEFAULT_HORIZON_S


def _paths(state_dir: str | Path):
    base = Path(state_dir) / "bus"
    return {
        "bus": base / "falsifier_events.jsonl",
        "cursor": base / "pnl_bridge_cursor.json",
        "predictions": base / "predicted_deltas.jsonl",
        "grades": base / "predicted_delta_grades.jsonl",
    }


def run_bus_cycle_step(
    *,
    cycle_id: str,
    now_ts: float,
    state_dir: str | Path,
    change_count: int = 0,
    applied: bool = False,
    trader_source: str = "trader_real",
) -> dict:
    """Dispatch through the optional T7 implementation; safety lives here."""
    try:
        step = resolve_payload(PayloadPort(
            7, 'agi_v8_1.bus.cycle_wire_payload', 'run_bus_cycle_step'))
    except PayloadUnavailable as exc:
        record_critical_failure(exc, site="bus.cycle_wire.payload_unavailable",
                                category="telemetry")
        return {"enabled": True, "cycle_id": cycle_id, "measured": False,
                "reason": "payload_unavailable", "tier": 7,
                "doc_pointer": "Plz_ReadMe.md §T7"}
    return step(
        _payload_sys.modules[__name__], cycle_id=cycle_id, now_ts=now_ts, state_dir=state_dir, change_count=change_count, applied=applied, trader_source=trader_source
    )
