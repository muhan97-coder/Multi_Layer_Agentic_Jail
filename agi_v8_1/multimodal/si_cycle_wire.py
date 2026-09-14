# __P4B_MMSI_SLOT__
"""P4-B — the production call site for the dormant multimodal SI bundle.

``multimodal/si_integration.py:integrate_multimodal_si`` had wired_by = **NONE**
in FEATURE_MAP: the observer/proposer chain existed, imported, and was tested,
but no production path ever constructed it. This module is that path, modelled
on ``bus/cycle_wire.py`` (lazy imports, observation-only, never perturbs a
cycle verdict, everything contained under the cycle's ``state_dir`` jail).

GATE — deliberately the DOCUMENTED double gate, not a new one:

    ``AGI_V8_MULTIMODAL_SI_ENABLED`` AND ``AGI_V8_MULTIMODAL_SI_INTEGRATION_ENABLED``

i.e. exactly ``multimodal.si_integration.integration_enabled()``. Both default
OFF, so an un-configured cycle never reaches a line of this module. A third
env var ``AGI_V8_MULTIMODAL_SI_WIRE_DISABLED`` is a pure *kill switch*: it can
only subtract (unset == allowed), so it can never turn anything on and can
never create a phantom "documented gates set, nothing runs" gap.

HONEST PAYOFF — read this before believing a green digest:

  * The two gates above populate the bundle's slots, but the observer and the
    proposer each read their OWN sub-gate (``..._OBSERVER_ENABLED`` /
    ``..._PROPOSER_ENABLED``). With those unset the bundle reports
    ``enabled: True`` while the observer DISCARDS the evidence list entirely
    (``multimodal_evidence_count: 0``) and the proposer returns base tickets
    untouched. That is a confident, well-formed, entirely content-free
    success — so this wire names it in ``degraded`` instead of reporting a 0.
  * ``bio_layers/`` does not exist in this tree, so the proposer's
    ``failure.*`` / ``repair.*`` hint keys are structurally unreachable. The
    ONLY keys it can ever add are ``multimodal.modality_count.<modality>`` and
    ``multimodal.evidence_count`` — at most ``len(modality_summary) + 1`` per
    ticket.
  * Those keys are layered on ``si_lanes/proposer.py``'s content-free base
    (``threshold.calibrator.bias_NN = "observe_<status>"``, derived from
    nothing but the last-3 outcome status strings). This is a WIRE, not
    content: it moves a measured signal, it does not create one.

INVARIANT enforced in code and in test: ``multimodal_keys_added == 0`` implies
``degraded`` is non-empty. A silent zero is a defect. If the reason-derivation
below ever misses a case, ``unexplained_zero`` appears and logs at ERROR —
that string is itself a defect marker, and a test forces it to prove the
failsafe branch is live (asserting only its absence would be satisfied by a
DELETED branch).

The invariant extends past the multimodal delta to the BASE signal it is
layered on: ``no_outcomes_observed`` / ``no_base_keys`` fire when the base
proposer itself measured nothing. Without them a corrupt ``cycle_log.jsonl``
(whose JSONDecodeError ``si_lanes/outcome_observer.py`` swallows) produced a
``degraded`` tuple byte-identical to a healthy cycle's while outcome_count and
base_key_count silently fell to 0.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

# __SLOT_FAIL_FAST_2026_07_25__ Swallowed failures route through one choke
# point: counted + named always, re-raised under AGI_V8_STRICT_FAIL_FAST.
from agi_v8_1.policy.fail_fast import (
    format_exception_for_critical_record as _format_exception_for_critical_record,
    swallowed as _swallowed,
)

logger = logging.getLogger(__name__)

ENV_MULTIMODAL_SI_WIRE_DISABLED = "AGI_V8_MULTIMODAL_SI_WIRE_DISABLED"
# Mirrored (not imported) so the OFF predicate stays import-free. Kept honest
# by test_wire_gate_matches_integration_enabled_over_full_matrix.
ENV_MULTIMODAL_SI_MASTER = "AGI_V8_MULTIMODAL_SI_ENABLED"
ENV_MULTIMODAL_SI_INTEGRATION = "AGI_V8_MULTIMODAL_SI_INTEGRATION_ENABLED"

# Named degraded reasons — each is a specific, actionable explanation for why
# this cycle's multimodal signal is content-free.
DEGRADED_MASTER_GATE_OFF = "master_gate_off"
DEGRADED_INTEGRATION_GATE_OFF = "integration_gate_off"
DEGRADED_OBSERVER_SUBGATE_OFF = "observer_subgate_off"
DEGRADED_PROPOSER_SUBGATE_OFF = "proposer_subgate_off"
DEGRADED_BIO_LAYERS_ABSENT = "bio_layers_absent"
# Deliberately defensive, and UNREACHABLE from the orchestrator: that caller
# always passes exactly one decision and ``propose_tickets`` returns one ticket
# per decision. Kept (not deleted) because the wire is a public function whose
# proposer dependency is not frozen — and pinned by an explicit test that
# monkeypatches ``propose_tickets`` to return (), so it is *documented* dead
# rather than *ambiguously* dead.
DEGRADED_NO_BASE_TICKETS = "no_base_tickets"
# The base signal — not the multimodal delta — collapsed. These carry the zero
# that DEGRADED_NO_BASE_TICKETS only appeared to guard: a corrupt or
# wrong-schema cycle_log makes ``observe_outcomes`` swallow the JSONDecodeError
# and return (), so outcome_count and base_key_count fall to 0 while the ticket
# count stays 1 and ``degraded`` looked byte-identical to a healthy cycle.
DEGRADED_NO_OUTCOMES = "no_outcomes_observed"
DEGRADED_NO_BASE_KEYS = "no_base_keys"
# Defect marker: a zero we could not explain. Should be unreachable.
DEGRADED_UNEXPLAINED_ZERO = "unexplained_zero"

_SIGNAL_RELPATH = ("multimodal_si", "cycle_signal.jsonl")

# C3 — named reasons for "this rollup measured nothing". A summariser that can
# only say 0 cannot tell an un-run wire from a healthy one.
SUMMARY_REASON_NO_SIGNAL_LOG = "no_signal_log"
# Set by the CLI, not here: the runner used a throwaway ``--state-dir``, so the
# cross-cycle history this rollup exists to expose is structurally unreachable.
SUMMARY_REASON_EPHEMERAL_STATE_DIR = "ephemeral_state_dir"
# Set by the CLI when the rollup itself blew up. A summary that cannot be
# computed must not read as a summary of zero.
SUMMARY_REASON_SUMMARY_FAILED = "summary_failed"


def _truthy(value: str) -> bool:
    """Match the multimodal/si_lanes family convention (1/true/on/yes)."""
    return (value or "").strip().lower() in ("1", "true", "on", "yes")


def wire_kill_switch_engaged() -> bool:
    """Operator kill switch. Unset == NOT engaged. Can only subtract."""
    return _truthy(os.environ.get(ENV_MULTIMODAL_SI_WIRE_DISABLED, ""))  # tier: T4


def multimodal_si_wire_enabled() -> bool:
    """Default OFF — the documented double gate, minus the kill switch.

    The two env names are read DIRECTLY rather than by calling
    ``si_integration.integration_enabled()``, so an OFF cycle never imports the
    bundle (and transitively the two lanes) at all — that no-op is asserted by
    a sys.modules test. The duplication is guarded: a test cross-checks this
    predicate against ``integration_enabled()`` over the full 2x2 env matrix,
    so the two can never drift apart silently.
    """
    if wire_kill_switch_engaged():
        return False
    if not _truthy(os.environ.get(ENV_MULTIMODAL_SI_MASTER, "")):  # tier: T4
        return False
    return _truthy(os.environ.get(ENV_MULTIMODAL_SI_INTEGRATION, ""))  # tier: T4


def signal_log_path(state_dir: str | Path) -> Path:
    """The jail-local JSONL this wire appends to. Never the source tree."""
    return Path(state_dir).joinpath(*_SIGNAL_RELPATH)


def read_multimodal_si_signals(
    state_dir: str | Path, *, limit: int = 50
) -> tuple[dict[str, Any], ...]:
    """Read back the wire's own JSONL — the missing half of the write.

    The signal log used to have ZERO readers anywhere in the tree: the wire
    appended to it every ON cycle and nothing ever opened it, which is this
    repo's signature "written and never read" defect. :func:`summarize_signal_log`
    and ``runtime.cli.run_orchestrator_cycle`` are the production consumers.

    Returns the LAST ``limit`` records, oldest-first. A missing log is an empty
    tuple — the file legitimately does not exist until an ON cycle runs — but a
    log that exists and cannot be parsed raises, because a corrupt record store
    is a defect and must not be laundered into a clean empty result.
    """
    path = signal_log_path(state_dir)
    if not path.exists():
        return ()
    from agi_v8_1.state.store import read_jsonl

    rows = read_jsonl(path)
    tail = rows[-int(limit):] if limit and limit > 0 else rows
    return tuple(dict(r) for r in tail)


def summarize_signal_log(state_dir: str | Path) -> dict[str, Any]:
    """Operator-facing rollup of the signal log. Counts, not a bare existence.

    ``cycles_recorded`` / ``cycles_with_zero_keys_added`` is the number this
    whole wire exists to expose: "the wire ran N times and added 0 keys N
    times" is the finding an operator needs, and it is invisible from any
    single cycle's digest.

    C3 (2026-07-26) — **a count that counted nothing is not a zero.** This used
    to return ``cycles_recorded: 0, cycles_with_zero_keys_added: 0,
    total_keys_added: 0, degraded_reason_counts: {}`` when the log did not exist
    at all, i.e. an un-run wire reported the same four numbers as a wire that
    ran and was healthy. That is this repo's signature defect (a zero that is
    really "nothing was measured") and it was recoverable only by noticing
    ``exists: false`` — a flag every summariser downstream was free to ignore.

    So: when nothing was measured, the counts are ``None``, ``measured`` is
    ``False``, and ``unmeasured_reason`` names why. ``None`` is deliberately
    NOT castable to 0 by an unwitting consumer — a reader that ignores
    ``measured`` now raises instead of charting a fake zero. The key set is
    identical in both branches, so no consumer KeyErrors on the switch.
    """
    path = signal_log_path(state_dir)
    if not path.exists():
        return {
            "path": str(path),
            "exists": False,
            "measured": False,
            "unmeasured_reason": SUMMARY_REASON_NO_SIGNAL_LOG,
            "cycles_recorded": None,
            "cycles_with_zero_keys_added": None,
            "total_keys_added": None,
            "degraded_reason_counts": None,
            "last_cycle_id": "",
        }
    rows = read_multimodal_si_signals(state_dir, limit=0)
    zero = sum(1 for r in rows if int(r.get("multimodal_keys_added", 0) or 0) <= 0)
    reasons: dict[str, int] = {}
    for row in rows:
        for reason in row.get("degraded", ()) or ():
            key = str(reason)
            reasons[key] = reasons.get(key, 0) + 1
    return {
        "path": str(path),
        "exists": True,
        "measured": True,
        "unmeasured_reason": "",
        "cycles_recorded": len(rows),
        "cycles_with_zero_keys_added": zero,
        "total_keys_added": sum(
            int(r.get("multimodal_keys_added", 0) or 0) for r in rows
        ),
        "degraded_reason_counts": dict(sorted(reasons.items())),
        "last_cycle_id": str(rows[-1].get("cycle_id", "")) if rows else "",
    }


def _ticket_keys(tickets: Any) -> list[str]:
    """Flatten every ticket's proposed_changes into a list of keys."""
    keys: list[str] = []
    for ticket in tickets or ():
        for pair in getattr(ticket, "proposed_changes", ()) or ():
            try:
                keys.append(str(pair[0]))
            except (IndexError, TypeError, KeyError) as exc:
                _swallowed(
                    exc,
                    site="multimodal.si_cycle_wire._ticket_keys:pair_unpack",
                    category="telemetry",
                )
    return keys


def run_multimodal_si_step(
    *,
    cycle_id: str,
    si: Any,
    decision: Any,
    outcome_log_path: str | Path,
    state_dir: str | Path,
    now_unix: float,
    persist: bool = True,
) -> dict:
    """Collect evidence -> observe -> propose -> MEASURE the delta.

    Called ONLY when :func:`multimodal_si_wire_enabled` is True. Contained to
    ``state_dir/multimodal_si/``. The returned digest reports what the
    multimodal path ADDED on top of the plain R20 W1 proposer — not merely
    that it ran.
    """
    # Lazy — an OFF cycle must never import the bundle. Proven by a test that
    # asserts ``agi_v8_1.multimodal.si_integration`` stays out of sys.modules.
    from agi_v8_1.capabilities import PayloadPort, resolve_payload
    collect_multimodal_evidence = resolve_payload(PayloadPort(7, "agi_v8_1.multimodal.si_evidence_source", "collect_multimodal_evidence"))
    integrate_multimodal_si = resolve_payload(PayloadPort(7, "agi_v8_1.multimodal.si_integration", "integrate_multimodal_si"))
    integration_enabled = resolve_payload(PayloadPort(7, "agi_v8_1.multimodal.si_integration", "integration_enabled"))
    MultimodalOutcomeObserver = resolve_payload(PayloadPort(7, "agi_v8_1.si_lanes.multimodal_observer", "MultimodalOutcomeObserver"))
    multimodal_si_enabled = resolve_payload(PayloadPort(7, "agi_v8_1.si_lanes.multimodal_observer", "multimodal_si_enabled"))
    MultimodalPolicyProposer = resolve_payload(PayloadPort(7, "agi_v8_1.si_lanes.multimodal_proposer", "MultimodalPolicyProposer"))
    has_failure_cartographer = resolve_payload(PayloadPort(7, "agi_v8_1.si_lanes.multimodal_proposer", "_HAS_FAILURE_CARTOGRAPHER", callable_only=False))
    has_repair_pattern = resolve_payload(PayloadPort(7, "agi_v8_1.si_lanes.multimodal_proposer", "_HAS_REPAIR_PATTERN", callable_only=False))
    from agi_v8_1.si_lanes.proposer import propose_tickets

    collection = collect_multimodal_evidence()

    # NO explicit enable_multimodal — the components' own env sub-gates govern.
    # Forcing them here would hide exactly the trap this digest exists to name.
    observer = MultimodalOutcomeObserver()
    proposer = MultimodalPolicyProposer()
    bundle = integrate_multimodal_si(
        si=si,
        multimodal_observer=observer,
        multimodal_proposer=proposer,
    )

    mm_outcome = bundle.observe_multimodal(
        outcome_log_path,
        multimodal_evidence=collection.cards,
    )
    mm_tickets = tuple(
        bundle.propose_multimodal(decision, mm_outcome, now_unix=now_unix)
    )

    # Recompute the plain R20 W1 base with identical inputs so the delta is
    # MEASURED, not predicted. ``propose_tickets`` is deterministic for a fixed
    # now_unix apart from message_id, which does not affect proposed_changes.
    base_tickets = propose_tickets(
        decisions=(decision,),
        recent_outcomes=tuple(mm_outcome.get("outcomes", ()) or ()),
        config_namespace=proposer.config_namespace,
        lane_index=proposer.lane_index,
        now_unix=now_unix,
    )

    outcomes = tuple(mm_outcome.get("outcomes", ()) or ())
    base_keys = _ticket_keys(base_tickets)
    mm_keys = _ticket_keys(mm_tickets)
    keys_added = len(mm_keys) - len(base_keys)
    added_key_names = sorted(set(mm_keys) - set(base_keys))

    # --- degraded reasons, derived independently of any counter -------------
    degraded: list[str] = []
    if not multimodal_si_enabled():
        degraded.append(DEGRADED_MASTER_GATE_OFF)
    if not integration_enabled():
        degraded.append(DEGRADED_INTEGRATION_GATE_OFF)
    if not observer.enabled:
        degraded.append(DEGRADED_OBSERVER_SUBGATE_OFF)
    if not proposer.enabled:
        degraded.append(DEGRADED_PROPOSER_SUBGATE_OFF)
    if collection.status != "ok":
        degraded.append(collection.status)
    # NOTE: these are module-level constants evaluated at IMPORT time, so a
    # long-lived process that imported the lane before bio_layers was installed
    # reports absent forever. Correct for this tree — bio_layers/ does not exist.
    if not has_failure_cartographer:
        degraded.append(DEGRADED_BIO_LAYERS_ABSENT)
    if not base_tickets:
        degraded.append(DEGRADED_NO_BASE_TICKETS)
    # The base signal's own zeros. ``observe_outcomes`` swallows a JSONDecodeError
    # and returns (), so a corrupt cycle_log is otherwise indistinguishable from a
    # healthy one here — the ticket still exists, it is merely content-free.
    if not outcomes:
        degraded.append(DEGRADED_NO_OUTCOMES)
    if not base_keys:
        degraded.append(DEGRADED_NO_BASE_KEYS)
    if keys_added == 0 and not degraded:
        # Unreachable by construction; if it fires, the reason-derivation above
        # is wrong and the digest would otherwise be a silent zero.
        degraded.append(DEGRADED_UNEXPLAINED_ZERO)
        logger.error(
            "multimodal SI wire produced an UNEXPLAINED zero for cycle %s — "
            "degraded-reason derivation is incomplete",
            cycle_id,
        )

    digest: dict[str, Any] = {
        "enabled": True,
        "cycle_id": str(cycle_id),
        "bundle_enabled": bool(bundle.enabled),
        "observer_active": bool(observer.enabled),
        "proposer_active": bool(proposer.enabled),
        "has_failure_cartographer": bool(has_failure_cartographer),
        "has_repair_pattern": bool(has_repair_pattern),
        "evidence": collection.as_dict(),
        "multimodal_evidence_count": int(
            mm_outcome.get("multimodal_evidence_count", 0) or 0
        ),
        "modality_summary": dict(mm_outcome.get("modality_summary", {}) or {}),
        "outcome_count": len(outcomes),
        "base_ticket_count": len(base_tickets),
        "multimodal_ticket_count": len(mm_tickets),
        "base_key_count": len(base_keys),
        "multimodal_key_count": len(mm_keys),
        "multimodal_keys_added": keys_added,
        "multimodal_keys_added_names": added_key_names,
        "degraded": tuple(degraded),
    }

    if persist:
        try:
            from agi_v8_1.state.store import atomic_append_jsonl

            record = dict(digest)
            record["degraded"] = list(degraded)
            record["now_unix"] = float(now_unix)
            atomic_append_jsonl(signal_log_path(state_dir), record)
            digest["persisted"] = True
        except Exception as exc:  # noqa: BLE001 — telemetry never breaks a cycle
            _swallowed(
                exc,
                site="multimodal.si_cycle_wire.run_multimodal_si_step:persist",
                category="persist",
                detail=_format_exception_for_critical_record(
                    exc,
                    max_chars=300,
                    one_line=True,
                    keep="head",
                ),
            )
            digest["persisted"] = False
    else:
        digest["persisted"] = False

    return digest


__all__ = [
    "DEGRADED_BIO_LAYERS_ABSENT",
    "DEGRADED_INTEGRATION_GATE_OFF",
    "DEGRADED_MASTER_GATE_OFF",
    "DEGRADED_NO_BASE_KEYS",
    "DEGRADED_NO_BASE_TICKETS",
    "DEGRADED_NO_OUTCOMES",
    "DEGRADED_OBSERVER_SUBGATE_OFF",
    "DEGRADED_PROPOSER_SUBGATE_OFF",
    "DEGRADED_UNEXPLAINED_ZERO",
    "ENV_MULTIMODAL_SI_INTEGRATION",
    "ENV_MULTIMODAL_SI_MASTER",
    "ENV_MULTIMODAL_SI_WIRE_DISABLED",
    "SUMMARY_REASON_EPHEMERAL_STATE_DIR",
    "SUMMARY_REASON_NO_SIGNAL_LOG",
    "SUMMARY_REASON_SUMMARY_FAILED",
    "multimodal_si_wire_enabled",
    "read_multimodal_si_signals",
    "run_multimodal_si_step",
    "signal_log_path",
    "summarize_signal_log",
    "wire_kill_switch_engaged",
]
