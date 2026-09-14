"""SI rollback_guard lane logic (R20 W1 — port of v7.1 SI rollback math).

v7.1 self_improvement_agent.py + ProposalLedger track *recent reverts* to
flag risky proposals. The V8 port distils this into :func:`score_rollback_risk`
which computes a deterministic risk score in [0, 1] from:

  - proposed_changes complexity (more keys → more risk)
  - history of prior reverts on the same source_decision_id
  - whether the ticket touches an allowed config namespace

The score is then attached to a *new* :class:`SIPolicyTicket` (rollback_guard
lane), preserving the proposer's identity via source_decision_id.

Hard rules:
  - no provider calls, no shell, no network
  - import-clean: only stdlib + agi_v8_1 messages + orchestrator_schema
"""

from __future__ import annotations

import time
from typing import Mapping, Sequence

from agi_v8_1.core.messages import SIPolicyTicket, make_message_id
from agi_v8_1.core.orchestrator_schema import SI_NAMESPACE_ALLOWLIST


# Risk knobs — chosen so the score has interpretable thresholds:
#   < 0.3 → low risk (auto-apply candidate)
#   < 0.6 → medium risk (review)
#   ≥ 0.6 → high risk (log-only / fail-closed)
_BASE_RISK: float = 0.2
_RISK_PER_CHANGE: float = 0.05
_RISK_PER_PRIOR_REVERT: float = 0.15
_RISK_NAMESPACE_VIOLATION: float = 1.0  # forces fail-closed


def _count_prior_reverts(
    source_decision_id: str,
    history: Sequence[Mapping[str, object]],
) -> int:
    """Count prior rollback events for the same source_decision_id."""
    if not history:
        return 0
    count = 0
    for entry in history:
        if not isinstance(entry, Mapping):
            continue
        if str(entry.get("source_decision_id", "")) != source_decision_id:
            continue
        if str(entry.get("event", "")) == "revert":
            count += 1
    return count


def _compute_risk(
    *,
    proposed_changes_count: int,
    prior_reverts: int,
    namespace_allowed: bool,
) -> float:
    """Deterministic risk computation. Clamped to [0, 1]."""

    if not namespace_allowed:
        return _RISK_NAMESPACE_VIOLATION  # 1.0 — fail-closed

    risk = (
        _BASE_RISK
        + (_RISK_PER_CHANGE * max(0, int(proposed_changes_count)))
        + (_RISK_PER_PRIOR_REVERT * max(0, int(prior_reverts)))
    )
    return max(0.0, min(1.0, float(risk)))


def score_rollback_risk(
    *,
    proposer_tickets: Sequence[SIPolicyTicket],
    history: Sequence[Mapping[str, object]] = (),
    lane_index: int = 1,
    now_unix: float | None = None,
) -> tuple[SIPolicyTicket, ...]:
    """Emit one rollback_guard ticket per proposer ticket with computed risk.

    Each emitted ticket preserves the proposer's source_decision_id,
    config_namespace, and proposed_changes — only ``rollback_risk`` and
    ``lane_kind`` are updated.
    """

    ts = float(now_unix) if now_unix is not None else time.time()
    out: list[SIPolicyTicket] = []
    for src in proposer_tickets:
        prior_reverts = _count_prior_reverts(src.source_decision_id, history)
        namespace_allowed = src.config_namespace in SI_NAMESPACE_ALLOWLIST
        risk = _compute_risk(
            proposed_changes_count=len(src.proposed_changes),
            prior_reverts=prior_reverts,
            namespace_allowed=namespace_allowed,
        )
        out.append(
            SIPolicyTicket(
                message_id=make_message_id("si_guard"),
                created_at_unix=ts,
                ticket_id=f"si_guard_{lane_index:02d}_{src.ticket_id}",
                source_decision_id=src.source_decision_id,
                config_namespace=src.config_namespace,
                proposed_changes=src.proposed_changes,
                rollback_risk=risk,
                lane_kind="rollback_guard",
            )
        )
    return tuple(out)


__all__ = ["score_rollback_risk"]
