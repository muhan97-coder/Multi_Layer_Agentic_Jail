"""V8 judge / arbitration (R20 W1 — decomposed from v7.1 orchestrator.py).

v7.1 orchestrator.py:4724-4796 ``_run_critic_phase`` builds a critic
payload, runs the critic agent, and arbitrates between strategist /
skeptic / builder / tester outputs. The V8 port distils the *deterministic
arbitration core* into a free module that:

  1. produces a :class:`DecisionRecord` (advisory chain card),
  2. optionally consults an injected :class:`CriticAgent` (R19) for a
     :class:`CriticTicket`,
  3. derives a final verdict ∈ {"accept", "retry", "escalate", "blocked",
     "infeasible"} matching v7.1 critic decision categories.

Default OFF: when ``AGI_V8_JUDGE_ENABLED`` is unset/false, callers still
receive a DecisionRecord but the *enforcement* (escalation / retry-chain
trigger) is left to the caller. This R10 envelope keeps R17-R19 tests
unaffected by R20's new arbitration.

Hard rules:
  - no provider calls, no shell, no network
  - import-clean: only stdlib + agi_v8_1 (messages, evidence_gate,
    orchestrator_schema, acceptance_gate)
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from agi_v8_1.core.acceptance_gate import AcceptanceJudgement
from agi_v8_1.core.evidence_gate import EvidenceState
from agi_v8_1.core.messages import (
    CriticTicket,
    DecisionRecord,
    EvidenceCard,
    make_message_id,
)
from agi_v8_1.core.orchestrator_schema import AcceptanceVerdict

# __SLOT_FAIL_FAST_2026_07_25__ Swallowed failures route through one choke
# point: counted + named always, re-raised under AGI_V8_STRICT_FAIL_FAST.
from agi_v8_1.policy.fail_fast import swallowed as _swallowed


def _judge_enabled() -> bool:
    return os.getenv("AGI_V8_JUDGE_ENABLED", "false").strip().lower() == "true"  # tier: T6


# v7.1 critic_agent decision categories (also referenced in
# agi_v8_1/agents/critic.py:56-58).
JUDGE_DECISIONS: tuple[str, ...] = (
    "accept",
    "retry",
    "escalate",
    "blocked",
    "infeasible",
)


@dataclass(frozen=True, slots=True)
class JudgeOutcome:
    """Aggregate of one judge pass.

    ``decision_record`` is the load-bearing advisory artifact; tests assert
    it is always present (even when judge is OFF). ``critic_ticket`` is
    optional (only emitted when a critic was injected and produced one).
    ``verdict`` is the high-level category — used by retry_chain to pick
    its next action.
    """

    decision_record: DecisionRecord
    critic_ticket: CriticTicket | None
    verdict: str  # one of JUDGE_DECISIONS


def _verdict_from_acceptance(acceptance: AcceptanceJudgement) -> str:
    """Map AcceptanceVerdict to a Judge verdict.

    v7.1 orchestrator.py:3540-3604 chains acceptance → critic → handle_*
    branches. The V8 port collapses this into a deterministic table.
    """

    v = acceptance.verdict
    if v == AcceptanceVerdict.PASS.value:
        return "accept"
    if v == AcceptanceVerdict.BLOCKED.value:
        return "blocked"
    if v == AcceptanceVerdict.FAIL.value:
        return "retry"
    return "retry"  # unknown → retry (caller may downgrade via critic)


def _maybe_critic(
    critic: Any | None,
    decision: DecisionRecord,
    evidence: Sequence[EvidenceCard],
) -> CriticTicket | None:
    """Call critic.evaluate_decision defensively. Returns None on exception."""

    if critic is None:
        return None
    if not hasattr(critic, "evaluate_decision"):
        return None
    try:
        ticket = critic.evaluate_decision(decision, tuple(evidence))
        if isinstance(ticket, CriticTicket):
            return ticket
        return None
    except Exception as _ff_exc:
        # Defensive — never block the judge on a critic exception
        # (v7.1 orchestrator.py:262-264 telemetry hook used the same defensive
        # try/except pattern).
        _swallowed(_ff_exc, site="core.judge._maybe_critic:107", category="telemetry")
        return None


def _downgrade_with_critic(verdict: str, ticket: CriticTicket | None) -> str:
    """Translate critic severity into a verdict refinement.

    Rules (v7.1 orchestrator.py:3581-3604 + critic decision categories):
      - critic severity == "reject" → escalate (worst case)
      - critic severity == "warn" + verdict == "accept" → retry
      - critic severity == "info" → keep verdict as-is
    """

    if ticket is None:
        return verdict
    sev = ticket.severity.lower()
    if sev == "reject":
        return "escalate"
    if sev == "warn" and verdict == "accept":
        return "retry"
    return verdict


def judge_cycle(
    *,
    cycle_id: str,
    acceptance: AcceptanceJudgement,
    evidence: Sequence[EvidenceCard] = (),
    critic: Any | None = None,
    rationale_extra: str = "",
) -> JudgeOutcome:
    """Run one judge pass.

    Always produces a :class:`DecisionRecord`. When ``critic`` is injected
    and exposes :meth:`evaluate_decision`, the resulting ticket may downgrade
    the verdict per :func:`_downgrade_with_critic`.

    The verdict reflects what the retry_chain *would* do — but only when
    AGI_V8_JUDGE_ENABLED is true does the caller pull the trigger. Otherwise
    the verdict is advisory (observe-only).
    """

    base_verdict = _verdict_from_acceptance(acceptance)

    decision = DecisionRecord(
        message_id=make_message_id("dec"),
        created_at_unix=time.time(),
        decision_id=f"judge_decision_{cycle_id}",
        evidence_ids=tuple(e.evidence_id for e in evidence),
        outcome=base_verdict,
        rationale=(
            f"acceptance.verdict={acceptance.verdict} "
            f"missing={len(acceptance.missing)} "
            f"blocked={len(acceptance.blocked)} "
            f"{rationale_extra}"
        ).strip(),
    )

    ticket = _maybe_critic(critic, decision, evidence)
    final_verdict = _downgrade_with_critic(base_verdict, ticket)

    # If the judge env knob is OFF, the verdict is still computed (so the
    # caller can log it) but the caller is expected to NOT enforce it —
    # this is the R10 envelope default-ON-but-fail-safe pattern.
    return JudgeOutcome(
        decision_record=decision,
        critic_ticket=ticket,
        verdict=final_verdict,
    )


__all__ = [
    "JUDGE_DECISIONS",
    "JudgeOutcome",
    "_judge_enabled",
    "judge_cycle",
]
