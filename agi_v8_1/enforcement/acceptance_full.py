# __R25_5_SLOT__ acceptance_full — enriched superset of agi_v8_1/core/acceptance_gate.py
"""V8 acceptance enforcement — full port of v7.1 control_tower AcceptanceJudge.

The short R20 W1 version in :mod:`agi_v8_1.core.acceptance_gate` is preserved
byte-untouched. This module is a **superset** that adds:

  * :class:`RecoveryAction` / :class:`RecoveryDecision` / :class:`RecoveryPolicy`
    — the v7.1 control_tower recovery hierarchy (retry → replan → decompose
    → escalate) so the V8 retry_chain can consult a policy when an
    acceptance fail repeats.
  * :class:`AcceptanceJudge` (class API, kept frozen-dataclass output via
    :class:`AcceptanceJudgement`) for callers that want a stateful judge
    instance — the short version only exposes a free function.
  * :func:`judge_acceptance_full` — the same function-call entry as
    ``judge_acceptance`` but returning the v7.1 ``list``-typed judgement
    fields (the short version returns tuples for frozen-slots safety).
  * environment-blocker tagging expanded to cover network / DNS / disk-full /
    rate-limit categories beyond the short version's 7 markers.

All env knobs default OFF. No subprocess / requests / urllib / socket /
http at module top.

# __R25_5_SLOT_ACCEPTANCE_FULL__
"""

from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence

from agi_v8_1.core.orchestrator_schema import AcceptanceVerdict


def _acceptance_full_enabled() -> bool:
    return (
        os.getenv("AGI_V8_ACCEPTANCE_FULL_ENABLED", "false").strip().lower()  # tier: T5
        == "true"
    )


# v7.1 control_tower.py:172.
_ABS_PATH_RE = re.compile(r"/home/[A-Za-z0-9_./\\-]+")


# Expanded env-blocker markers: v7.1 had 7, V8 R25.5 adds 5 more so blocker
# classification covers DNS / disk-full / quota / SSL / no-route categories
# the v5/v7 SI lanes have reported in tickets. Pure-marker list — no
# fabrication, only string containment.
_ENV_BLOCKER_MARKERS: tuple[str, ...] = (
    "permission",
    "dependency",
    "module not found",
    "modulenotfounderror",
    "timeout",
    "rate limit",
    "connection refused",
    # R25.5 additions
    "name or service not known",
    "no space left on device",
    "quota exceeded",
    "ssl",
    "no route to host",
)


# v7.1 control_tower.RecoveryAction port.
class RecoveryAction(str, Enum):
    """Canonical failure recovery action."""

    RETRY = "retry"
    REPLAN = "replan"
    DECOMPOSE = "decompose"
    ESCALATE = "escalate"


@dataclass(frozen=True)
class RecoveryDecision:
    """Structured retry/replan/decompose recommendation (v7.1 port)."""

    action: RecoveryAction
    reason: str
    repeat_count: int = 0
    retry_count: int = 0
    max_retry: int = 0
    elapsed_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["action"] = self.action.value
        return data


class RecoveryPolicy:
    """Map repeated failures into a bounded recovery hierarchy.

    Verbatim port of v7.1 ``control_tower.RecoveryPolicy``. Thresholds and
    time budget are constructor knobs; :meth:`decide` is deterministic — no
    env reads, no I/O.
    """

    def __init__(
        self,
        *,
        repeated_failure_threshold: int = 3,
        decompose_failure_threshold: int = 5,
        decompose_after_s: float = 3 * 60 * 60,
    ) -> None:
        self.repeated_failure_threshold = max(1, int(repeated_failure_threshold))
        self.decompose_failure_threshold = max(
            self.repeated_failure_threshold,
            int(decompose_failure_threshold),
        )
        self.decompose_after_s = max(0.0, float(decompose_after_s))

    def decide(
        self,
        *,
        failure_signature: str = "",
        repeat_count: int = 0,
        retry_count: int = 0,
        max_retry: int = 0,
        elapsed_s: float = 0.0,
        open_task_count: int = 0,
    ) -> RecoveryDecision:
        """Return the cheapest safe recovery action."""

        repeat_count = max(0, int(repeat_count))
        retry_count = max(0, int(retry_count))
        max_retry = max(0, int(max_retry))
        elapsed_s = max(0.0, float(elapsed_s))
        signature = str(failure_signature or "").strip()

        if repeat_count >= self.decompose_failure_threshold:
            return RecoveryDecision(
                action=RecoveryAction.DECOMPOSE,
                reason=(
                    "Failure signature exceeded decompose threshold; split the "
                    "task into smaller independently verifiable subtasks."
                ),
                repeat_count=repeat_count,
                retry_count=retry_count,
                max_retry=max_retry,
                elapsed_s=elapsed_s,
            )

        if (
            self.decompose_after_s
            and elapsed_s >= self.decompose_after_s
            and open_task_count
        ):
            return RecoveryDecision(
                action=RecoveryAction.DECOMPOSE,
                reason=(
                    "Open work has persisted beyond the decompose time budget; "
                    "use phase/task decomposition rather than another flat retry."
                ),
                repeat_count=repeat_count,
                retry_count=retry_count,
                max_retry=max_retry,
                elapsed_s=elapsed_s,
            )

        if repeat_count >= self.repeated_failure_threshold:
            return RecoveryDecision(
                action=RecoveryAction.REPLAN,
                reason=(
                    "Repeated failure pattern requires head-layer replanning "
                    "before any further worker retry."
                ),
                repeat_count=repeat_count,
                retry_count=retry_count,
                max_retry=max_retry,
                elapsed_s=elapsed_s,
            )

        if max_retry and retry_count >= max_retry:
            action = (
                RecoveryAction.REPLAN if open_task_count else RecoveryAction.ESCALATE
            )
            return RecoveryDecision(
                action=action,
                reason="Local retry budget is exhausted.",
                repeat_count=repeat_count,
                retry_count=retry_count,
                max_retry=max_retry,
                elapsed_s=elapsed_s,
            )

        if signature:
            reason = (
                "Failure appears bounded; retry locally with the known signature."
            )
        else:
            reason = (
                "No repeated failure evidence; retry locally if the critic agrees."
            )
        return RecoveryDecision(
            action=RecoveryAction.RETRY,
            reason=reason,
            repeat_count=repeat_count,
            retry_count=retry_count,
            max_retry=max_retry,
            elapsed_s=elapsed_s,
        )


@dataclass(frozen=True)
class AcceptanceJudgement:
    """Deterministic pre-critic acceptance evidence (v7.1-shape, list fields).

    Different from the R20 W1 short version (which uses tuple fields for
    slots safety): the full version returns the v7.1-compatible ``list``
    fields and a ``to_dict()`` method so callers built against the v7.1
    surface drop in without modification.
    """

    verdict: str
    satisfied: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _criteria_from_goal(goal_card: Mapping[str, Any]) -> list[str]:
    raw = (
        goal_card.get("acceptance_criteria")
        or goal_card.get("success_criteria")
        or []
    )
    if isinstance(raw, list):
        return [str(item) for item in raw if str(item).strip()]
    if isinstance(raw, str) and raw.strip():
        return [raw.strip()]
    return []


def _looks_environment_blocker(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _ENV_BLOCKER_MARKERS)


def _dedupe(values: Sequence[str]) -> list[str]:
    """v7.1 control_tower._dedupe — preserves order, drops blanks."""

    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        clean = str(value).strip()
        if clean and clean not in seen:
            seen.add(clean)
            result.append(clean)
    return result


class AcceptanceJudge:
    """Check explicit acceptance criteria against local evidence.

    Verbatim port of v7.1 ``control_tower.AcceptanceJudge``. Stateless — the
    class form exists purely for API symmetry with the v7.1 surface so
    callers that injected an AcceptanceJudge instance can be re-pointed at
    the V8 implementation without code changes.
    """

    _ABS_PATH_RE = _ABS_PATH_RE

    def judge(
        self,
        *,
        goal_card: Mapping[str, Any],
        tester_output: Mapping[str, Any],
        builder_output: Mapping[str, Any] | None = None,
        ground_truth_inventory: Sequence[Mapping[str, Any]] = (),
    ) -> AcceptanceJudgement:
        return judge_acceptance_full(
            goal_card=goal_card,
            tester_output=tester_output,
            builder_output=builder_output,
            ground_truth_inventory=ground_truth_inventory,
        )


def judge_acceptance_full(
    *,
    goal_card: Mapping[str, Any],
    tester_output: Mapping[str, Any],
    builder_output: Mapping[str, Any] | None = None,
    ground_truth_inventory: Sequence[Mapping[str, Any]] = (),
) -> AcceptanceJudgement:
    """Deterministic acceptance judgement (v7.1-compatible list fields).

    Same logic shape as :func:`agi_v8_1.core.acceptance_gate.judge_acceptance`
    but:
      * returns the v7.1 ``AcceptanceJudgement`` (list fields, ``to_dict``)
      * uses the **expanded** env-blocker marker set
      * accepts the same goal_card / tester_output / builder_output /
        ground_truth_inventory inputs as v7.1.

    The full version is intentionally a parallel surface so the R20 W1
    short version's frozen-tuple contract is not perturbed; callers wire to
    whichever variant fits their evidence pipeline.
    """

    criteria = _criteria_from_goal(goal_card)
    inventory = {
        str(item.get("path", "")): item
        for item in ground_truth_inventory
        if isinstance(item, Mapping) and item.get("path")
    }

    satisfied: list[str] = []
    missing: list[str] = []
    blocked: list[str] = []
    notes: list[str] = []

    tester_verdict = str(
        tester_output.get("verdict", tester_output.get("status", ""))
    ).lower()
    if tester_verdict in {"fail", "failed", "error"}:
        missing.append("tester_output.verdict indicates failure")
    elif tester_verdict in {"pass", "passed", "ok", "success"}:
        satisfied.append("tester_output.verdict indicates pass")

    builder = dict(builder_output or {})
    builder_status = str(
        builder.get("overall_status", builder.get("status", ""))
    ).lower()
    if builder_status in {"failure", "failed", "error"}:
        missing.append("builder_output indicates failure")

    for criterion in criteria:
        text = str(criterion)
        paths = [
            match.rstrip(".,;:)\"'") for match in _ABS_PATH_RE.findall(text)
        ]
        if not paths:
            notes.append(f"criterion requires judge review: {text[:160]}")
            continue
        for path in paths:
            item = inventory.get(path)
            if (
                item
                and bool(item.get("exists"))
                and bool(item.get("is_file"))
                and int(item.get("size") or 0) > 0
            ):
                satisfied.append(f"{path} exists and is non-empty")
            else:
                missing.append(f"{path} missing or empty")

    if any(_looks_environment_blocker(v) for v in list(missing) + list(notes)):
        blocked.extend(item for item in missing if _looks_environment_blocker(item))

    if missing:
        verdict = (
            AcceptanceVerdict.BLOCKED.value
            if blocked and len(blocked) == len(missing)
            else AcceptanceVerdict.FAIL.value
        )
    elif criteria or satisfied:
        verdict = AcceptanceVerdict.PASS.value
    else:
        verdict = AcceptanceVerdict.UNKNOWN.value

    return AcceptanceJudgement(
        verdict=verdict,
        satisfied=_dedupe(satisfied),
        missing=_dedupe(missing),
        blocked=_dedupe(blocked),
        notes=_dedupe(notes),
    )


__all__ = [
    "AcceptanceJudge",
    "AcceptanceJudgement",
    "RecoveryAction",
    "RecoveryDecision",
    "RecoveryPolicy",
    "_acceptance_full_enabled",
    "judge_acceptance_full",
]
