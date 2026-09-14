"""V8 evidence promotion gate — 10-state transition contract.

Ported from agi_v7.1/agent_system/swarm/v2/evidence_gate.py, simplified to the
canonical 10-state v2 README spec and with a single :func:`can_promote`
transition table. The blocker set (draft, hypothesis, executed_unverified,
contradicted, infeasible_recorded) cannot be promoted to ``accepted`` without
a :class:`DecisionRecord`.

This module is advisory only — it does not write state. Callers use
:func:`promote` to obtain a new immutable :class:`EvidenceCard` with the
updated state.
"""

from __future__ import annotations

from dataclasses import replace
from enum import StrEnum
from typing import Final

from agi_v8_1.core.messages import DecisionRecord, EvidenceCard


class EvidenceState(StrEnum):
    DRAFT = "draft"
    HYPOTHESIS = "hypothesis"
    EXECUTED_UNVERIFIED = "executed_unverified"
    VERIFIED_ONCE = "verified_once"
    VERIFIED_TWICE = "verified_twice"
    CONTRADICTED = "contradicted"
    INFEASIBLE_RECORDED = "infeasible_recorded"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    SKIPPED_WITH_REASON = "skipped_with_reason"


# Evidence in these states cannot be promoted to ``accepted`` without a
# DecisionRecord. See README §"Evidence Promotion Gate".
PROMOTION_BLOCKERS: Final[frozenset[EvidenceState]] = frozenset(
    {
        EvidenceState.DRAFT,
        EvidenceState.HYPOTHESIS,
        EvidenceState.EXECUTED_UNVERIFIED,
        EvidenceState.CONTRADICTED,
        EvidenceState.INFEASIBLE_RECORDED,
    }
)


_TRANSITIONS: Final[dict[EvidenceState, frozenset[EvidenceState]]] = {
    EvidenceState.DRAFT: frozenset(
        {EvidenceState.HYPOTHESIS, EvidenceState.SKIPPED_WITH_REASON}
    ),
    EvidenceState.HYPOTHESIS: frozenset(
        {EvidenceState.EXECUTED_UNVERIFIED, EvidenceState.SKIPPED_WITH_REASON}
    ),
    EvidenceState.EXECUTED_UNVERIFIED: frozenset(
        {
            EvidenceState.VERIFIED_ONCE,
            EvidenceState.CONTRADICTED,
            EvidenceState.INFEASIBLE_RECORDED,
        }
    ),
    EvidenceState.VERIFIED_ONCE: frozenset(
        {EvidenceState.VERIFIED_TWICE, EvidenceState.CONTRADICTED}
    ),
    EvidenceState.VERIFIED_TWICE: frozenset(
        {EvidenceState.ACCEPTED, EvidenceState.REJECTED}
    ),
    EvidenceState.CONTRADICTED: frozenset({EvidenceState.REJECTED}),
    EvidenceState.INFEASIBLE_RECORDED: frozenset(
        {EvidenceState.REJECTED, EvidenceState.SKIPPED_WITH_REASON}
    ),
    # Terminal — no outgoing transitions
    EvidenceState.ACCEPTED: frozenset(),
    EvidenceState.REJECTED: frozenset(),
    EvidenceState.SKIPPED_WITH_REASON: frozenset(),
}


def can_promote(from_state: EvidenceState, to_state: EvidenceState) -> bool:
    """Return True iff ``from_state → to_state`` is in the legal table.

    Terminal states (accepted/rejected/skipped_with_reason) have no outgoing
    edges. All transitions not explicitly listed return False.
    """

    return EvidenceState(to_state) in _TRANSITIONS[EvidenceState(from_state)]


def promote(
    evidence: EvidenceCard,
    to_state: EvidenceState,
    decision: DecisionRecord | None = None,
) -> EvidenceCard:
    """Return a new EvidenceCard transitioned to ``to_state``.

    Raises:
      ValueError: if the transition is illegal per :func:`can_promote`, or if
        the source state is a promotion blocker and ``decision`` is None.
    """

    current = EvidenceState(evidence.state)
    target = EvidenceState(to_state)
    if not can_promote(current, target):
        raise ValueError(
            f"illegal evidence transition: {current.value} -> {target.value}"
        )
    if current in PROMOTION_BLOCKERS and decision is None:
        raise ValueError(
            f"evidence in blocker state {current.value} requires a DecisionRecord to promote"
        )
    return replace(evidence, state=target.value)


# __R26_SLOT__ — optional hallucination-audit sub-gate.
# Default OFF (``AGI_V8_HALLUCINATION_AUDIT_ENABLED=false``); when enabled,
# callers may run :func:`promote_with_audit` which blocks contradicted /
# unsupported audited claims while returning an audit-record dict. Legacy
# ``promote`` is untouched so all R17-R21 tests stay green.
def promote_with_audit(
    evidence: EvidenceCard,
    to_state: EvidenceState,
    *,
    decision: DecisionRecord | None = None,
    evidence_snippets=None,
    enabled: bool | None = None,
) -> tuple[EvidenceCard, dict]:
    """Promote + optionally run a hallucination audit on the claim.

    Returns ``(new_card, audit_info)`` where ``audit_info`` carries:
      - ``enabled``: bool — whether the sub-gate ran
      - ``hallucination_check``: bool — True iff audit ran AND no contradicted
        / unsupported (high-risk) claims were found
      - ``record``: dict | None — full audit_record_v1 when audit ran
      - ``reason``: str — explanation when ``hallucination_check`` is False

    When the audit sub-gate is enabled, contradicted / unsupported factual
    claims block the transition: the original card is returned with
    ``promotion_blocked=True`` in the audit info. The legacy :func:`promote`
    API remains unchanged.
    """

    new_card = promote(evidence, to_state, decision)

    if enabled is None:
        import os

        enabled = (
            os.getenv("AGI_V8_HALLUCINATION_AUDIT_ENABLED", "false").strip().lower()  # tier: T5
            == "true"
        )

    if not enabled:
        return new_card, {
            "enabled": False,
            "hallucination_check": True,
            "promotion_blocked": False,
            "record": None,
            "reason": "audit_disabled",
        }

    # Lazy import to avoid circular dep + keep evidence_gate import-light.
    from agi_v8_1.capabilities import PayloadPort, resolve_payload
    audit_answer = resolve_payload(PayloadPort(5, "agi_v8_1.hallucination_audit", "audit_answer"))

    record = audit_answer(
        evidence.claim,
        evidence_snippets or [],
        source_ref=str(evidence.evidence_id or ""),
    )
    metrics = record.get("metrics", {})
    counts = metrics.get("counts", {})
    contradicted_n = int(counts.get("contradicted", 0))
    unsupported_n = int(counts.get("unsupported", 0))
    zero_claim_factual = bool(metrics.get("zero_claim_factual_text"))

    if contradicted_n > 0:
        return evidence, {
            "enabled": True,
            "hallucination_check": False,
            "promotion_blocked": True,
            "record": record,
            "reason": f"contradicted_claims={contradicted_n}",
        }
    if unsupported_n > 0:
        return evidence, {
            "enabled": True,
            "hallucination_check": False,
            "promotion_blocked": True,
            "record": record,
            "reason": f"unsupported_claims={unsupported_n}",
        }
    if zero_claim_factual:
        return evidence, {
            "enabled": True,
            "hallucination_check": False,
            "promotion_blocked": True,
            "record": record,
            "reason": "zero_claim_factual_text",
        }

    return new_card, {
        "enabled": True,
        "hallucination_check": True,
        "promotion_blocked": False,
        "record": record,
        "reason": "",
    }


__all__ = [
    "EvidenceState",
    "PROMOTION_BLOCKERS",
    "can_promote",
    "promote",
    "promote_with_audit",
]
