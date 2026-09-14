"""V8 orchestrator schema (R20 W1 port).

Frozen dataclasses + StrEnum constants extracted from the v7.1
orchestrator.py monolith (6,160 LoC) into a single small schema module so
the rest of V8 can import phase / arm / recovery enums without dragging in
the monolith's imports.

References to v7.1 line numbers are noted inline for the load-bearing
heuristics so future audit can trace each constant back to its source.

R20 invariants (enforced by tests/v8/test_r20_w1_invariants.py):
  - no ``subprocess``, no ``requests``/``urllib``/``socket``/``http``
  - import-clean: only stdlib + agi_v8_1.core.messages
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final, Mapping


# ── Phase ordering ────────────────────────────────────────────────────────
# v7.1 orchestrator runs cycles through a fixed phase sequence; the V8 port
# preserves the *ordering* (planning → execution → acceptance → judge →
# continuation) as a deterministic StrEnum so phase transitions can be
# verified by tests without depending on v7.1's stateful machinery.


class CyclePhase(StrEnum):
    PLANNING = "planning"
    EXECUTION = "execution"
    ACCEPTANCE = "acceptance"
    JUDGE = "judge"
    RETRY_CHAIN = "retry_chain"
    CONTINUATION = "continuation"
    COMPLETED = "completed"


# Forward ordering only — tests/test_r20_w1_phase_manager.py asserts that
# transitions never go backward.
PHASE_ORDER: Final[tuple[CyclePhase, ...]] = (
    CyclePhase.PLANNING,
    CyclePhase.EXECUTION,
    CyclePhase.ACCEPTANCE,
    CyclePhase.JUDGE,
    CyclePhase.RETRY_CHAIN,
    CyclePhase.CONTINUATION,
    CyclePhase.COMPLETED,
)


# ── Arm status (digestive contract) ──────────────────────────────────────
# v7.1 digestive_agent.py:1114-1131 — R10 fail-closed rule:
#   avg<0.35 → status="escalate"; parse exception → status="escalate".
# Ported as ArmStatus StrEnum so the SI port + acceptance gate can reason
# about digest outcomes without importing the digestive module.


class ArmStatus(StrEnum):
    OK = "ok"
    ESCALATE = "escalate"  # v7.1 digestive_agent.py:1122 — fail-closed
    DEFERRED = "deferred"
    INFEASIBLE = "infeasible"


# v7.1 digestive_scorer.py:12 — GOAL_RELEVANCE_WEIGHT = 0.35. This same
# threshold is the avg<0.35 → escalate fail-closed boundary in
# digestive_agent.py:1118-1122.
ARM_DIGEST_FAIL_CLOSED_THRESHOLD: Final[float] = 0.35


# ── Recovery action (retry/replan/decompose) ─────────────────────────────
# v7.1 control_tower.py:30-36 — RecoveryAction enum. Ported verbatim so the
# V8 retry_chain produces records compatible with v7.1 telemetry consumers.


class RecoveryAction(StrEnum):
    RETRY = "retry"
    REPLAN = "replan"
    DECOMPOSE = "decompose"
    ESCALATE = "escalate"


# ── Recovery thresholds (v7.1 control_tower.RecoveryPolicy defaults) ─────
# v7.1 control_tower.py:62-71 defaults — repeated_failure_threshold=3,
# decompose_failure_threshold=5, decompose_after_s=3*60*60 = 10800.
REPEATED_FAILURE_THRESHOLD: Final[int] = 3
DECOMPOSE_FAILURE_THRESHOLD: Final[int] = 5
DECOMPOSE_AFTER_S: Final[float] = 3.0 * 60.0 * 60.0


# v7.1 orchestrator.py:254-256 — ESCALATE_FAIL_THRESHOLD default 3.
ESCALATE_FAIL_THRESHOLD_DEFAULT: Final[int] = 3


# ── Cycle status (terminal outcome envelope) ─────────────────────────────


class CycleStatus(StrEnum):
    STUB = "stub"            # R17 seed
    CONSENSUS = "consensus"
    SPLIT = "split"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    DEFERRED = "deferred"
    ESCALATED = "escalated"


# ── Runtime summary status vocabulary ────────────────────────────────────
# These are operator/reporting words, not raw cycle internals. Ambiguous raw
# tokens like "ok", "dispatch_started", and stub-derived "consensus" must be
# translated before they appear in runtime/eval summaries.


class RuntimeSummaryStatus(StrEnum):
    CONSENSUS = "runtime_consensus"
    SPLIT = "runtime_split"
    COMPLETED = "runtime_completed"
    BLOCKED = "runtime_blocked"
    DEFERRED = "runtime_deferred"
    ESCALATED = "runtime_escalated"
    ERROR = "runtime_error"
    IMPORT_ONLY = "runtime_import_only"
    DRY_RUN = "runtime_dry_run"
    ADVISORY_STUB = "runtime_advisory_stub"
    NOT_EXECUTED = "runtime_not_executed"
    UNKNOWN = "runtime_unknown"


RUNTIME_SUMMARY_STATUS_VALUES: Final[frozenset[str]] = frozenset(
    status.value for status in RuntimeSummaryStatus
)


# ── Acceptance verdict (v7.1 control_tower.AcceptanceJudgement) ──────────
# v7.1 control_tower.py:156-234 — verdicts pass / fail / blocked / unknown.


class AcceptanceVerdict(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"
    # ADV-R6-009/010 (Round 6 track GG): a criterion string existing is not
    # the same as it being verified. REVIEW_REQUIRED marks criteria that
    # judge_acceptance recognized but could not mechanically check (no
    # supported absolute path match) — these must never fall through to PASS.
    # UNMEASURED marks the case where the caller explicitly signals no
    # evaluator ran at all (judge_acceptance(..., evaluator_available=False)).
    REVIEW_REQUIRED = "review_required"
    UNMEASURED = "unmeasured"


# ── Frozen records ───────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PhaseTransition:
    """Record of one phase transition. Immutable, deterministic."""

    cycle_id: str
    from_phase: str
    to_phase: str
    timestamp: float
    reason: str


@dataclass(frozen=True, slots=True)
class RetryChainDecision:
    """Recovery action selected by retry_chain.

    Mirrors v7.1 control_tower.RecoveryDecision (control_tower.py:39-53)
    but is a free dataclass — no v7.1 import dependency.
    """

    action: str            # RecoveryAction.value
    reason: str
    repeat_count: int = 0
    retry_count: int = 0
    max_retry: int = 0
    elapsed_s: float = 0.0


@dataclass(frozen=True, slots=True)
class CycleArtifactManifest:
    """Aggregate of EvidenceCard ids collected during one cycle."""

    cycle_id: str
    evidence_ids: tuple[str, ...]
    ticket_ids: tuple[str, ...]
    decision_ids: tuple[str, ...]
    handoff_id: str
    completed_phase: str


# ── SI namespace allow-list ──────────────────────────────────────────────
# v7.1 self_improvement_agent.py:28-35 — _FORBIDDEN_TARGETS (config keys
# the SI agent is forbidden from touching). Ported as a frozenset so the
# V8 SI proposer / rollback_guard share the exact same blocklist.

SI_FORBIDDEN_TARGETS: Final[frozenset[str]] = frozenset(
    {
        "goal_card",
        "immutable_rules",
        "emergency_stop",
        "memory_write_gate",
        "max_continuation_cycles",
        "security_constraints",
    }
)

# v7.1 SI agent operates over named config "namespaces". The V8 SI port
# uses these as the proposer / rollback_guard allow-list. They mirror the
# 6 v7.1 RL config namespaces (project_rl_architecture).
SI_NAMESPACE_ALLOWLIST: Final[frozenset[str]] = frozenset(
    {
        "agi_v8_1.si",
        "agi_v8_1.thresholds",
        "agi_v8_1.weights",
        "agi_v8_1.calibrator",
        "agi_v8_1.router",
        "agi_v8_1.cost",
    }
)


# v7.1 self_improvement_agent.py:38-40 — warmup limits.
SI_WARMUP_PHASE_1_LIMIT: Final[int] = 1
SI_WARMUP_PHASE_2_LIMIT: Final[int] = 10
SI_REPEATED_FAILURE_THRESHOLD: Final[int] = 2  # v7.1 default for the env var


__all__ = [
    "ArmStatus",
    "ARM_DIGEST_FAIL_CLOSED_THRESHOLD",
    "AcceptanceVerdict",
    "CycleArtifactManifest",
    "CyclePhase",
    "CycleStatus",
    "DECOMPOSE_AFTER_S",
    "DECOMPOSE_FAILURE_THRESHOLD",
    "ESCALATE_FAIL_THRESHOLD_DEFAULT",
    "PHASE_ORDER",
    "PhaseTransition",
    "REPEATED_FAILURE_THRESHOLD",
    "RecoveryAction",
    "RetryChainDecision",
    "RuntimeSummaryStatus",
    "RUNTIME_SUMMARY_STATUS_VALUES",
    "SI_FORBIDDEN_TARGETS",
    "SI_NAMESPACE_ALLOWLIST",
    "SI_REPEATED_FAILURE_THRESHOLD",
    "SI_WARMUP_PHASE_1_LIMIT",
    "SI_WARMUP_PHASE_2_LIMIT",
]
