# __R25_5_SLOT__ judge_full — enriched superset of agi_v8_1/core/judge.py
"""V8 judge — full arbitration port from v7.1 orchestrator._run_critic_phase.

The short R20 W1 version in :mod:`agi_v8_1.core.judge` is preserved
byte-untouched. This module is a **superset** that adds:

  * :func:`arbitrate` — multi-input arbitration across strategist / skeptic /
    builder / tester / scribe outputs (the v7.1 critic-phase shape)
  * recovery-policy consultation — the full judge consults a
    :class:`agi_v8_1.enforcement.acceptance_full.RecoveryPolicy` when verdict
    is non-accept and surfaces the resulting :class:`RecoveryDecision` on
    :class:`JudgeOutcome` so the retry_chain can pick the cheapest action.
  * critic-ticket downgrade rules expanded to the full v7.1 critic decision
    surface (``accept`` / ``retry`` / ``escalate`` / ``blocked`` /
    ``infeasible``).

All env knobs default OFF. No subprocess / requests / urllib / socket /
http at module top.

# __R25_5_SLOT_JUDGE_FULL__
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from agi_v8_1.core.messages import (
    CriticTicket,
    DecisionRecord,
    EvidenceCard,
    make_message_id,
)
from agi_v8_1.core.orchestrator_schema import AcceptanceVerdict
from agi_v8_1.enforcement.acceptance_full import (
    AcceptanceJudgement,
    RecoveryAction,
    RecoveryDecision,
    RecoveryPolicy,
    judge_acceptance_full,
)

# __SLOT_FAIL_FAST_2026_07_25__ Swallowed failures route through one choke
# point: counted + named always, re-raised under AGI_V8_STRICT_FAIL_FAST.
from agi_v8_1.policy.fail_fast import swallowed as _swallowed


def _judge_full_enabled() -> bool:
    return os.getenv("AGI_V8_JUDGE_FULL_ENABLED", "false").strip().lower() == "true"  # tier: T6


# v7.1 critic decision surface — full set (vs short version which uses the
# same set but without arbitrate's mid-step "blocked"-via-recovery hook).
JUDGE_DECISIONS_FULL: tuple[str, ...] = (
    "accept",
    "retry",
    "replan",
    "decompose",
    "escalate",
    "blocked",
    "infeasible",
)


@dataclass(frozen=True, slots=True)
class JudgeOutcome:
    """Aggregate of one full judge pass.

    Adds :attr:`recovery` (a :class:`RecoveryDecision`) and :attr:`acceptance`
    (the full v7.1-shape AcceptanceJudgement) on top of the short version's
    ``decision_record`` / ``critic_ticket`` / ``verdict``.
    """

    decision_record: DecisionRecord
    critic_ticket: CriticTicket | None
    verdict: str  # one of JUDGE_DECISIONS_FULL
    recovery: RecoveryDecision | None = None
    acceptance: AcceptanceJudgement | None = None


def _verdict_from_acceptance(acceptance: AcceptanceJudgement) -> str:
    v = acceptance.verdict
    if v == AcceptanceVerdict.PASS.value:
        return "accept"
    if v == AcceptanceVerdict.BLOCKED.value:
        return "blocked"
    if v == AcceptanceVerdict.FAIL.value:
        return "retry"
    return "retry"  # unknown → retry (recovery policy may upgrade)


def _maybe_critic(
    critic: Any | None,
    decision: DecisionRecord,
    evidence: Sequence[EvidenceCard],
) -> CriticTicket | None:
    """Call ``critic.evaluate_decision`` defensively. Returns None on exception."""

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
        _swallowed(_ff_exc, site="enforcement.judge_full._maybe_critic:106", category="apply")
        return None


def _downgrade_with_critic(verdict: str, ticket: CriticTicket | None) -> str:
    """Translate critic severity into a verdict refinement.

    Rules (full v7.1 critic decision surface):
      * critic severity == "reject" → escalate
      * critic severity == "warn"   →
          - verdict == "accept" → retry
          - verdict == "blocked" → escalate
          - otherwise no change
      * critic severity == "info"   → keep verdict
    """

    if ticket is None:
        return verdict
    sev = ticket.severity.lower()
    if sev == "reject":
        return "escalate"
    if sev == "warn":
        if verdict == "accept":
            return "retry"
        if verdict == "blocked":
            return "escalate"
    return verdict


def _apply_recovery(verdict: str, recovery: RecoveryDecision | None) -> str:
    """Upgrade ``verdict`` per recovery policy action.

    When verdict is one of {"retry","blocked"} and the recovery policy
    recommends a stronger action (replan/decompose/escalate), the verdict is
    promoted accordingly. ``accept`` is never downgraded by recovery.
    """

    if recovery is None or verdict == "accept":
        return verdict
    action = recovery.action
    if action == RecoveryAction.RETRY:
        return verdict  # leave as-is
    if action == RecoveryAction.REPLAN:
        return "replan"
    if action == RecoveryAction.DECOMPOSE:
        return "decompose"
    if action == RecoveryAction.ESCALATE:
        return "escalate"
    return verdict


def judge_cycle_full(
    *,
    cycle_id: str,
    acceptance: AcceptanceJudgement,
    evidence: Sequence[EvidenceCard] = (),
    critic: Any | None = None,
    recovery_policy: RecoveryPolicy | None = None,
    failure_signature: str = "",
    repeat_count: int = 0,
    retry_count: int = 0,
    max_retry: int = 0,
    elapsed_s: float = 0.0,
    open_task_count: int = 0,
    rationale_extra: str = "",
) -> JudgeOutcome:
    """Run one full judge pass (multi-input arbitration + recovery policy).

    Always produces a :class:`DecisionRecord`. When ``critic`` is injected
    and exposes :meth:`evaluate_decision`, the resulting ticket may downgrade
    the verdict per :func:`_downgrade_with_critic`. When ``recovery_policy``
    is supplied, a stronger recovery action may further upgrade the verdict
    per :func:`_apply_recovery`.

    The verdict reflects what the retry_chain *would* do — but only when
    :envvar:`AGI_V8_JUDGE_FULL_ENABLED` is true does the caller pull the
    trigger. Otherwise the verdict is advisory (observe-only).
    """

    base_verdict = _verdict_from_acceptance(acceptance)

    recovery: RecoveryDecision | None = None
    if recovery_policy is not None:
        recovery = recovery_policy.decide(
            failure_signature=failure_signature,
            repeat_count=repeat_count,
            retry_count=retry_count,
            max_retry=max_retry,
            elapsed_s=elapsed_s,
            open_task_count=open_task_count,
        )

    decision = DecisionRecord(
        message_id=make_message_id("dec"),
        created_at_unix=time.time(),
        decision_id=f"judge_full_decision_{cycle_id}",
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
    verdict_after_critic = _downgrade_with_critic(base_verdict, ticket)
    final_verdict = _apply_recovery(verdict_after_critic, recovery)

    return JudgeOutcome(
        decision_record=decision,
        critic_ticket=ticket,
        verdict=final_verdict,
        recovery=recovery,
        acceptance=acceptance,
    )


def arbitrate(
    *,
    cycle_id: str,
    goal_card: Mapping[str, Any],
    tester_output: Mapping[str, Any],
    builder_output: Mapping[str, Any] | None = None,
    ground_truth_inventory: Sequence[Mapping[str, Any]] = (),
    evidence: Sequence[EvidenceCard] = (),
    critic: Any | None = None,
    recovery_policy: RecoveryPolicy | None = None,
    failure_signature: str = "",
    repeat_count: int = 0,
    retry_count: int = 0,
    max_retry: int = 0,
    elapsed_s: float = 0.0,
    open_task_count: int = 0,
) -> JudgeOutcome:
    """One-shot arbitration: acceptance → judge → critic → recovery.

    Mirrors the v7.1 ``orchestrator._run_critic_phase`` flow at a high level:
    builds an :class:`AcceptanceJudgement` from goal/tester/builder, then
    runs the full judge cycle with the supplied critic + recovery policy.
    The output is a single :class:`JudgeOutcome` with all four lanes
    (decision_record / critic_ticket / verdict / recovery / acceptance)
    populated.
    """

    judgement = judge_acceptance_full(
        goal_card=goal_card,
        tester_output=tester_output,
        builder_output=builder_output,
        ground_truth_inventory=ground_truth_inventory,
    )
    return judge_cycle_full(
        cycle_id=cycle_id,
        acceptance=judgement,
        evidence=evidence,
        critic=critic,
        recovery_policy=recovery_policy,
        failure_signature=failure_signature,
        repeat_count=repeat_count,
        retry_count=retry_count,
        max_retry=max_retry,
        elapsed_s=elapsed_s,
        open_task_count=open_task_count,
    )


__all__ = [
    "JUDGE_DECISIONS_FULL",
    "JudgeOutcome",
    "_judge_full_enabled",
    "arbitrate",
    "judge_cycle_full",
]
