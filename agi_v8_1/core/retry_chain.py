"""V8 retry chain (R20 W1 — ported from v7.1 control_tower.RecoveryPolicy).

v7.1 control_tower.py:56-152 ``RecoveryPolicy`` maps repeated failures into
a bounded recovery hierarchy: retry → replan → decompose → escalate.
Defaults preserved verbatim:
  - repeated_failure_threshold = 3   (control_tower.py:62, 117)
  - decompose_failure_threshold = 5  (control_tower.py:63, 91)
  - decompose_after_s = 3*60*60      (control_tower.py:64, 104) = 10800

The V8 port collapses the policy into a free :func:`decide_recovery`
function that returns a :class:`RetryChainDecision`. Bounded — at most one
escalate per ``max_retry`` budget. No infinite loops by construction (tests
assert termination after a fixed number of iterations).

Default OFF: when ``AGI_V8_RETRY_CHAIN_ENABLED`` is unset/false, the chain
short-circuits to RETRY at iteration 0 and never escalates. This R10
envelope keeps R17-R19 tests unaffected by R20's new chain behaviour.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from agi_v8_1.core.orchestrator_schema import (
    DECOMPOSE_AFTER_S,
    DECOMPOSE_FAILURE_THRESHOLD,
    REPEATED_FAILURE_THRESHOLD,
    RecoveryAction,
    RetryChainDecision,
)


def _retry_chain_enabled() -> bool:
    return os.getenv("AGI_V8_RETRY_CHAIN_ENABLED", "false").strip().lower() == "true"  # tier: T5


# Iteration safety bound — tests assert the loop body returns within this
# many iterations even under adversarial inputs.
MAX_RETRY_CHAIN_ITERATIONS: int = 64


@dataclass(frozen=True, slots=True)
class RetryChainState:
    """Mutable counters for one retry-chain conversation.

    ``open_task_count`` mirrors v7.1's queue-length signal; controls whether
    retry budget exhaustion escalates or replans.
    """

    failure_signature: str = ""
    repeat_count: int = 0
    retry_count: int = 0
    max_retry: int = 3
    elapsed_s: float = 0.0
    open_task_count: int = 0


def decide_recovery(state: RetryChainState) -> RetryChainDecision:
    """Return the cheapest safe recovery action.

    Verbatim port of v7.1 control_tower.RecoveryPolicy.decide
    (control_tower.py:73-152). Counters are clamped to ≥0.
    """

    repeat_count = max(0, int(state.repeat_count))
    retry_count = max(0, int(state.retry_count))
    max_retry = max(0, int(state.max_retry))
    elapsed_s = max(0.0, float(state.elapsed_s))
    signature = str(state.failure_signature or "").strip()
    open_task_count = max(0, int(state.open_task_count))

    # Disabled → never escalate, always retry at iteration 0.
    if not _retry_chain_enabled():
        return RetryChainDecision(
            action=RecoveryAction.RETRY.value,
            reason="retry_chain_disabled — default observe-only retry",
            repeat_count=repeat_count,
            retry_count=retry_count,
            max_retry=max_retry,
            elapsed_s=elapsed_s,
        )

    # v7.1 control_tower.py:91-102 — decompose threshold
    if repeat_count >= DECOMPOSE_FAILURE_THRESHOLD:
        return RetryChainDecision(
            action=RecoveryAction.DECOMPOSE.value,
            reason=(
                "Failure signature exceeded decompose threshold; split the "
                "task into smaller independently verifiable subtasks."
            ),
            repeat_count=repeat_count,
            retry_count=retry_count,
            max_retry=max_retry,
            elapsed_s=elapsed_s,
        )

    # v7.1 control_tower.py:104-115 — decompose-by-time
    if DECOMPOSE_AFTER_S and elapsed_s >= DECOMPOSE_AFTER_S and open_task_count:
        return RetryChainDecision(
            action=RecoveryAction.DECOMPOSE.value,
            reason=(
                "Open work has persisted beyond the decompose time budget; "
                "use phase/task decomposition rather than another flat retry."
            ),
            repeat_count=repeat_count,
            retry_count=retry_count,
            max_retry=max_retry,
            elapsed_s=elapsed_s,
        )

    # v7.1 control_tower.py:117-128 — replan on repeated failure pattern
    if repeat_count >= REPEATED_FAILURE_THRESHOLD:
        return RetryChainDecision(
            action=RecoveryAction.REPLAN.value,
            reason=(
                "Repeated failure pattern requires head-layer replanning "
                "before any further worker retry."
            ),
            repeat_count=repeat_count,
            retry_count=retry_count,
            max_retry=max_retry,
            elapsed_s=elapsed_s,
        )

    # v7.1 control_tower.py:130-139 — retry budget exhausted
    if max_retry and retry_count >= max_retry:
        action = (
            RecoveryAction.REPLAN.value
            if open_task_count
            else RecoveryAction.ESCALATE.value
        )
        return RetryChainDecision(
            action=action,
            reason="Local retry budget is exhausted.",
            repeat_count=repeat_count,
            retry_count=retry_count,
            max_retry=max_retry,
            elapsed_s=elapsed_s,
        )

    # v7.1 control_tower.py:141-152 — default retry
    if signature:
        reason = "Failure appears bounded; retry locally with the known signature."
    else:
        reason = "No repeated failure evidence; retry locally if the critic agrees."
    return RetryChainDecision(
        action=RecoveryAction.RETRY.value,
        reason=reason,
        repeat_count=repeat_count,
        retry_count=retry_count,
        max_retry=max_retry,
        elapsed_s=elapsed_s,
    )


def chain_to_termination(
    initial_state: RetryChainState,
    *,
    max_iterations: int = MAX_RETRY_CHAIN_ITERATIONS,
) -> tuple[RetryChainDecision, ...]:
    """Run the chain until it produces ESCALATE/DECOMPOSE/REPLAN or the bound.

    Each iteration increments retry_count by 1 (simulating a real retry).
    Terminates when ``action != RETRY`` or after ``max_iterations`` —
    whichever comes first. Returns the full decision trail.

    This function is the *bounded* version: tests assert it never loops
    indefinitely, even with adversarial counters.
    """

    iterations = max(1, int(max_iterations))
    trail: list[RetryChainDecision] = []
    state = initial_state
    for _ in range(iterations):
        decision = decide_recovery(state)
        trail.append(decision)
        if decision.action != RecoveryAction.RETRY.value:
            break
        # Increment retry_count to simulate a retry attempt.
        state = RetryChainState(
            failure_signature=state.failure_signature,
            repeat_count=state.repeat_count,
            retry_count=state.retry_count + 1,
            max_retry=state.max_retry,
            elapsed_s=state.elapsed_s,
            open_task_count=state.open_task_count,
        )
    return tuple(trail)


__all__ = [
    "MAX_RETRY_CHAIN_ITERATIONS",
    "RetryChainState",
    "_retry_chain_enabled",
    "chain_to_termination",
    "decide_recovery",
]
