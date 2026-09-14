"""SI proposer lane logic (R20 W1 — port of v7.1 SI proposal generation).

v7.1 self_improvement_agent.py:1356-1543 emits *proposals* — config /
prompt / code change suggestions. The V8 port distils this into a single
:func:`propose_tickets` function that produces :class:`SIPolicyTicket`
records keyed off the supplied DecisionRecord(s).

The V8 proposer is *advisory only*: tickets are emitted into the chain,
never applied. Apply happens via apply_chain (R12 Merkle) in a separate
caller after rollback_guard and ratchet have signed off.

Hard rules:
  - no provider calls, no shell, no network
  - import-clean: only stdlib + agi_v8_1 messages + orchestrator_schema
"""

from __future__ import annotations

import time
from typing import Mapping, Sequence

from agi_v8_1.core.messages import (
    DecisionRecord,
    SIPolicyTicket,
    make_message_id,
)
from agi_v8_1.core.orchestrator_schema import (
    SI_FORBIDDEN_TARGETS,
    SI_NAMESPACE_ALLOWLIST,
)


def _derive_proposed_changes(
    decision: DecisionRecord,
    recent_outcomes: Sequence[Mapping[str, object]],
) -> tuple[tuple[str, str], ...]:
    """Derive proposed (key, value_json) pairs from a decision + outcomes.

    v7.1 self_improvement_agent.py:1481-1543 — when repeated failures are
    detected, the SI agent injects default proposals targeting the prompt /
    config namespace. The V8 port keeps the same shape but drops the prompt
    text bodies (those belonged to the v7.1 LLM-driven path).

    Deterministic — empty outcomes → empty changes. With outcomes, emit a
    single threshold tweak keyed on outcome counter so tests can stable-sort.
    """

    if not recent_outcomes:
        return ()

    # v7.1 emitted up to max_proposals=3; we cap at 3 here too.
    capped = list(recent_outcomes)[-3:]
    changes: list[tuple[str, str]] = []
    for idx, outcome in enumerate(capped):
        if not isinstance(outcome, Mapping):
            continue
        # Use the outcome status to suggest a calibrator nudge.
        status = str(outcome.get("status", "unknown"))
        key = f"threshold.calibrator.bias_{idx:02d}"
        # Forbidden-target guard (defense-in-depth — ratchet also checks).
        if any(forbidden in key for forbidden in SI_FORBIDDEN_TARGETS):
            continue
        # Encode the proposed value as JSON-text per SIPolicyTicket contract.
        value_json = f'"observe_{status}"'
        changes.append((key, value_json))

    return tuple(changes)


def propose_tickets(
    *,
    decisions: Sequence[DecisionRecord],
    recent_outcomes: Sequence[Mapping[str, object]] = (),
    config_namespace: str = "agi_v8_1.si",
    lane_index: int = 1,
    now_unix: float | None = None,
) -> tuple[SIPolicyTicket, ...]:
    """Emit one SIPolicyTicket per decision (proposer lane semantics).

    ``config_namespace`` MUST be in SI_NAMESPACE_ALLOWLIST. If not, raises
    ValueError to fail-closed (v7.1's _FORBIDDEN_TARGETS guard).
    """

    if config_namespace not in SI_NAMESPACE_ALLOWLIST:
        raise ValueError(
            f"config_namespace {config_namespace!r} not in allowlist; "
            f"allowed = {sorted(SI_NAMESPACE_ALLOWLIST)}"
        )

    ts = float(now_unix) if now_unix is not None else time.time()
    tickets: list[SIPolicyTicket] = []
    for d in decisions:
        proposed = _derive_proposed_changes(d, recent_outcomes)
        ticket = SIPolicyTicket(
            message_id=make_message_id("si_prop"),
            created_at_unix=ts,
            ticket_id=f"si_prop_{lane_index:02d}_{d.decision_id}",
            source_decision_id=d.decision_id,
            config_namespace=config_namespace,
            proposed_changes=proposed,
            # 0.5 default until rollback_guard scores it.
            rollback_risk=0.5,
            lane_kind="proposer",
        )
        tickets.append(ticket)
    return tuple(tickets)


__all__ = ["propose_tickets"]
