"""V8 acceptance gate (R20 W1 — ported from v7.1 control_tower.AcceptanceJudge).

v7.1 lives in agent_system/core/control_tower.py:169-270. Logic ported:
  - acceptance criteria extracted from goal_card (acceptance_criteria /
    success_criteria fields)
  - per-criterion path inventory check
  - environment-blocker classification (permission/dependency/timeout/...)
  - verdict reduction (pass / fail / blocked / unknown)

The V8 gate then *wraps* :func:`agi_v8_1.core.evidence_gate.promote` so that
acceptance translates into an EvidenceCard transition through the legal
10-state contract, instead of writing free-form output as v7.1 did.

Default OFF: when ``AGI_V8_ACCEPTANCE_GATE_ENABLED`` is unset/false, the
gate runs in *observe-only* mode — judgements are computed but no
``promote`` call is made. This matches the R10 envelope pattern (γ-safe).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from agi_v8_1.core.evidence_gate import (
    EvidenceState,
    PROMOTION_BLOCKERS,
    can_promote,
    promote,
)
from agi_v8_1.core.messages import DecisionRecord, EvidenceCard
from agi_v8_1.core.orchestrator_schema import AcceptanceVerdict


def _acceptance_gate_enabled() -> bool:
    return (
        os.getenv("AGI_V8_ACCEPTANCE_GATE_ENABLED", "false").strip().lower() == "true"  # tier: T5
    )


# v7.1 control_tower.py:172 — _ABS_PATH_RE.
_ABS_PATH_RE = re.compile(r"/home/[A-Za-z0-9_./\\-]+")


# v7.1 control_tower.py:246-259 — environment-blocker markers.
_ENV_BLOCKER_MARKERS: tuple[str, ...] = (
    "permission",
    "dependency",
    "module not found",
    "modulenotfounderror",
    "timeout",
    "rate limit",
    "connection refused",
)


@dataclass(frozen=True, slots=True)
class AcceptanceJudgement:
    """Pre-promotion verdict. Matches v7.1 control_tower.AcceptanceJudgement.

    ``verdict`` ∈ {"pass", "fail", "blocked", "unknown"}.
    """

    verdict: str
    satisfied: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    blocked: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    # ADV-R6-009/010: criteria that were recognized but not mechanically
    # checkable (no supported absolute path match). Non-empty ⇒ verdict cannot be
    # PASS — see judge_acceptance's verdict-reduction step below.
    review_required: tuple[str, ...] = ()


def _criteria_from_goal(goal_card: Mapping[str, Any]) -> list[str]:
    """v7.1 control_tower.py:237-243 — extract criteria, accept list or str."""
    raw = goal_card.get("acceptance_criteria") or goal_card.get("success_criteria") or []
    if isinstance(raw, list):
        return [str(item) for item in raw if str(item).strip()]
    if isinstance(raw, str) and raw.strip():
        return [raw.strip()]
    return []


def _looks_environment_blocker(text: str) -> bool:
    """v7.1 control_tower.py:246-259."""
    lowered = text.lower()
    return any(marker in lowered for marker in _ENV_BLOCKER_MARKERS)


def _dedupe(values: Sequence[str]) -> tuple[str, ...]:
    """v7.1 control_tower.py:262-270."""
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        clean = str(value).strip()
        if clean and clean not in seen:
            seen.add(clean)
            result.append(clean)
    return tuple(result)


def judge_acceptance(
    *,
    goal_card: Mapping[str, Any],
    tester_output: Mapping[str, Any],
    builder_output: Mapping[str, Any] | None = None,
    ground_truth_inventory: Sequence[Mapping[str, Any]] = (),
    evaluator_available: bool = True,
) -> AcceptanceJudgement:
    """Deterministic acceptance judgement.

    Verbatim port of v7.1 control_tower.AcceptanceJudge.judge (control_tower.py:174-234),
    hardened per ADV-R6-009/010 (Round 6 track GG): a criterion existing is
    not proof it was satisfied.

    ``evaluator_available=False`` is an explicit opt-in for callers that know
    no grader ran this cycle at all (e.g. acceptance invoked before the
    tester lane executes) — it short-circuits straight to UNMEASURED instead
    of guessing from empty inputs. Default True preserves prior behavior for
    every existing caller byte-for-byte.
    """

    if not evaluator_available:
        return AcceptanceJudgement(verdict=AcceptanceVerdict.UNMEASURED.value)

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
    review_required: list[str] = []

    # v7.1 control_tower.py:194-198
    tester_verdict = str(
        tester_output.get("verdict", tester_output.get("status", ""))
    ).lower()
    if tester_verdict in {"fail", "failed", "error"}:
        missing.append("tester_output.verdict indicates failure")
    elif tester_verdict in {"pass", "passed", "ok", "success"}:
        satisfied.append("tester_output.verdict indicates pass")

    # v7.1 control_tower.py:200-203
    builder = dict(builder_output or {})
    builder_status = str(
        builder.get("overall_status", builder.get("status", ""))
    ).lower()
    if builder_status in {"failure", "failed", "error"}:
        missing.append("builder_output indicates failure")

    # v7.1 control_tower.py:205-216 — per-criterion path check.
    for criterion in criteria:
        text = str(criterion)
        paths = [
            match.rstrip(".,;:)\"'") for match in _ABS_PATH_RE.findall(text)
        ]
        if not paths:
            note = f"criterion requires judge review: {text[:160]}"
            notes.append(note)
            # ADV-R6-009: this criterion was never mechanically checked —
            # it must not be able to reach PASS just by existing.
            review_required.append(note)
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

    # v7.1 control_tower.py:218-219
    if any(_looks_environment_blocker(v) for v in list(missing) + list(notes)):
        blocked.extend(item for item in missing if _looks_environment_blocker(item))

    # v7.1 control_tower.py:221-226, hardened by ADV-R6-009/010:
    # a criterion existing (and landing only in `notes`/`review_required`
    # because it couldn't be mechanically checked) must not be able to
    # stand in for "satisfied". REVIEW_REQUIRED sits strictly below PASS in
    # this ordering — checked before the old `criteria or satisfied` test —
    # so unverifiable criteria can no longer piggyback on an unrelated
    # tester "pass" signal or on each other to reach PASS.
    if missing:
        verdict = (
            AcceptanceVerdict.BLOCKED.value
            if blocked and len(blocked) == len(missing)
            else AcceptanceVerdict.FAIL.value
        )
    elif review_required:
        verdict = AcceptanceVerdict.REVIEW_REQUIRED.value
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
        review_required=_dedupe(review_required),
    )


def gated_promote(
    *,
    evidence: EvidenceCard,
    to_state: EvidenceState,
    decision: DecisionRecord | None,
    acceptance: AcceptanceJudgement,
) -> EvidenceCard:
    """Promote ``evidence`` only when the gate is enabled AND acceptance passes.

    Behavior matrix:
      gate OFF                         → return evidence unchanged (observe-only)
      gate ON + verdict != "pass"      → raise ValueError (caller stays in
                                          retry_chain instead of advancing)
      gate ON + verdict == "pass"      → call evidence_gate.promote (R12-safe)

    R10 envelope: when acceptance is "unknown" the gate also returns evidence
    unchanged (observe-only) so cycles without explicit criteria stay safe.
    """

    if not _acceptance_gate_enabled():
        return evidence  # observe-only — R10 envelope default OFF

    if acceptance.verdict == AcceptanceVerdict.UNKNOWN.value:
        return evidence  # no criteria → nothing to gate

    if acceptance.verdict != AcceptanceVerdict.PASS.value:
        raise ValueError(
            f"acceptance gate refused promotion: verdict={acceptance.verdict} "
            f"missing={len(acceptance.missing)} blocked={len(acceptance.blocked)}"
        )

    # Acceptance passed — delegate to evidence_gate.promote (handles the
    # 10-state contract + blocker-requires-decision rule).
    return promote(evidence, EvidenceState(to_state), decision=decision)


__all__ = [
    "AcceptanceJudgement",
    "_acceptance_gate_enabled",
    "gated_promote",
    "judge_acceptance",
]
