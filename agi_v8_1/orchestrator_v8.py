"""agi_v8_1 V8 orchestrator seed (R17 → R18).

V8 는 v5/v7.1 의 monolithic 단일 클래스 orchestrator (6,160줄) 대신,
Pod A/B/Meta 분산 합의 위에 얇은 SI cycle 코어를 둠.

R17 은 시드: run_cycle 의 skeleton, Pod dispatch hook stub, state placeholder.
R18: SelfImprovementV8 wire — state_store + apply chain + real axis vote.
R19: real agents (executor/planner/critic) + critic. R20: provider lane.

설계 원칙:
1. Single source of truth: 모든 cycle 결정은 6/7축 vote 합의 → state 기록.
2. Default OFF: AGI_V8_ENABLED=false 시 모든 run_cycle 호출이 stub envelope 반환.
3. Pod-distributed by construction: 단일 worker 가 아닌, Pod A + Pod B + Meta 분산이 default.
4. Sandbox-grounded: 코드 변경 제안은 sandbox 통과해야 apply (R18 의 apply chain 의 입력).

SIA (Lane B addendum, default OFF):
  Self-Improving Agent decomposition flows transitively through ``self.si``
  (``SelfImprovementV8.run_one_si_cycle``). When ``AGI_V8_SIA_ENABLED=true``
  the SI cycle invokes ``SIASelfRewriteController.maybe_run_iteration`` after
  the apply_chain entry is durable; the iteration summary rides on
  ``SICycleOutcome.sia_iteration_summary``. No orchestrator-level branching
  is needed — depth runs deep via the SI hub. Lineage: Gödel Machine, STaR,
  Reflexion, SPIN, Promptbreeder, SICA. Env discoverability:
    * ``AGI_V8_SIA_ENABLED`` — master gate (default OFF).
    * ``AGI_V8_SIA_HARNESS_REWRITE_APPROVED`` — controller harness rewrite
      advertisement gate (default OFF, additionally gated by Lane E).
    * ``AGI_V8_SIA_LEDGER_DIR`` — optional SIA chain dir override.
"""

from __future__ import annotations
import contextlib
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from agi_v8_1.self_improvement_v8 import SelfImprovementV8

# __SLOT_FAIL_FAST_2026_07_25__ Swallowed failures route through one choke
# point: counted + named always, re-raised under AGI_V8_STRICT_FAIL_FAST.
from agi_v8_1.policy.fail_fast import (
    format_exception_for_critical_record as _format_exception_for_critical_record,
    format_text_for_critical_record as _format_text_for_critical_record,
    safe_exception_type_name as _safe_exception_type_name,
    swallowed as _swallowed,
)

logger = logging.getLogger(__name__)


def _activation_ladder_enabled() -> bool:
    """R18.5 gate. Default OFF — orchestrator can compute an ActivationDecision
    without applying it to the cycle outcome."""
    return os.getenv("AGI_V8_ACTIVATION_LADDER_ENABLED", "false").strip().lower() == "true"


def _si_layer_enabled() -> bool:
    """R18.5 gate for the SI 12-lane layer. Default OFF."""
    return os.getenv("AGI_V8_SI_LAYER_ENABLED", "false").strip().lower() == "true"


# R20 W1 — additive gates. Each one stays default OFF so R17-R19 tests are
# unaffected by the new orchestrator decomposition.
def _phase_manager_enabled_env() -> bool:
    return os.getenv("AGI_V8_PHASE_MANAGER_ENABLED", "false").strip().lower() == "true"


def _providers_enabled_env() -> bool:
    """R20 W2's knob — W1 only reads it to gate the provider hook."""
    return os.getenv("AGI_V8_PROVIDERS_ENABLED", "false").strip().lower() == "true"


# __SLOT_EXECUTOR_TICKET_PRODUCE_2026_08_19__ track B — production-caller
# wire for si_lanes.ticket_plan.active_plan_scope (dormant since the
# 2026-08-16 track 4 revival: the consumer side was fully built, but zero
# production callers ever entered the scope). This gate controls only
# whether the ORCHESTRATOR supplies a plan; it is independent of the
# sibling consume gate (si_lanes.ticket_plan.ENV) which still governs
# whether any reader acts on it — double-OFF is automatically a no-op.
def _executor_ticket_produce_enabled() -> bool:
    """Default-OFF gate. Default OFF ⇒ the orchestrator never calls
    ``ticket_plan.active_plan_scope`` and the SI cycle call site stays
    byte-identical to pre-produce behavior."""
    return os.getenv("AGI_V8_EXECUTOR_TICKET_PRODUCE_ENABLED", "false").strip().lower() == "true"  # tier: T9


# __R22_SLOT__ R22 7-hook causal envelope (default OFF master + 7 per-hook).
# See agi_v8_1/causal/__init__.py for ENV_HOOKS canonical names.
#
# __R26_SLOT__ R26 default-ON observe-only envelope (additive):
#   AGI_V8_CAUSAL_DEFAULT_ON_OBSERVE_ONLY=true → all read-only ("observe") hooks
#   default ON even when AGI_V8_CAUSAL_ENABLED is unset. Write gates
#   (POSTACCEPT_WRITE, CYCLELOG_WRITE) stay OFF unless their explicit
#   AGI_V8_CAUSAL_<HOOK>_WRITE env is true. This mirrors the v7.1 R10 pattern.
#   Master escape valve still wins (sets all hooks OFF).
def _causal_master_enabled() -> bool:
    """R22: master gate. Default OFF unlike v7.1 (γ-safe ON)."""
    if os.getenv("AGI_V8_CAUSAL_ESCAPE_VALVE", "false").strip().lower() == "true":
        return False
    return os.getenv("AGI_V8_CAUSAL_ENABLED", "false").strip().lower() == "true"


def _causal_default_on_observe_only() -> bool:
    """R26: observe-only envelope. Default OFF — opt-in."""
    if os.getenv("AGI_V8_CAUSAL_ESCAPE_VALVE", "false").strip().lower() == "true":
        return False
    return (
        os.getenv("AGI_V8_CAUSAL_DEFAULT_ON_OBSERVE_ONLY", "false").strip().lower() == "true"
    )


_R22_HOOK_ENV: dict[str, str] = {
    "planning": "AGI_V8_CAUSAL_PLANNING_ENABLED",
    "trace": "AGI_V8_CAUSAL_TRACE_ENABLED",
    "failure": "AGI_V8_CAUSAL_FAILURE_ENABLED",
    "sirepair": "AGI_V8_CAUSAL_SIREPAIR_ENABLED",
    "digest": "AGI_V8_CAUSAL_DIGEST_ENABLED",
    "postaccept": "AGI_V8_CAUSAL_POSTACCEPT_ENABLED",
    "cyclelog": "AGI_V8_CAUSAL_CYCLELOG_ENABLED",
}

# R26: write-gate hooks — these ALWAYS require their own env regardless of
# the observe-only envelope. Default OFF.
_R26_WRITE_GATE_HOOKS: frozenset[str] = frozenset({"postaccept", "cyclelog"})
_R26_WRITE_GATE_ENV: dict[str, str] = {
    "postaccept": "AGI_V8_CAUSAL_POSTACCEPT_WRITE",
    "cyclelog": "AGI_V8_CAUSAL_CYCLELOG_WRITE",
}


def _causal_write_gate_enabled(hook: str) -> bool:
    """R26: write-gate gate. Default OFF — even observe-only envelope ignores."""
    if os.getenv("AGI_V8_CAUSAL_ESCAPE_VALVE", "false").strip().lower() == "true":
        return False
    env = _R26_WRITE_GATE_ENV.get(hook)
    if not env:
        return False
    return os.getenv(env, "false").strip().lower() == "true"


def _causal_hook_enabled(hook: str) -> bool:
    """R22: per-hook gate. Fires only when master AND per-hook env are ON.

    R26 additive: when ``AGI_V8_CAUSAL_DEFAULT_ON_OBSERVE_ONLY=true``, the
    five **observe-only** hooks (planning/trace/failure/sirepair/digest)
    default to ON. The two **write-gate** hooks (postaccept/cyclelog) stay
    OFF unless their explicit ``AGI_V8_CAUSAL_<HOOK>_WRITE`` env is true.
    """
    # R22 master path — wins if it allows the hook through the per-hook ENV.
    if _causal_master_enabled():
        env = _R22_HOOK_ENV.get(hook)
        if env and os.getenv(env, "false").strip().lower() == "true":
            return True

    # R26 observe-only envelope — only for non-write hooks.
    if _causal_default_on_observe_only() and hook not in _R26_WRITE_GATE_HOOKS:
        return True

    # Write-gate hooks (postaccept/cyclelog) only fire when their dedicated
    # write env is on (in addition to master+hook env above, if used).
    if hook in _R26_WRITE_GATE_HOOKS and _causal_write_gate_enabled(hook):
        return True

    return False


SCHEMA_VERSION = "agi_v8_orchestrator_cycle_v1"


def _agent_failure_metadata(agent_kind: str, phase: str, exc: Exception) -> dict[str, Any]:
    """Return structured failure metadata without echoing exception text."""
    return {
        "agent_kind": str(agent_kind),
        "phase": str(phase),
        "error_type": _safe_exception_type_name(exc),
        "error_message_redacted": True,
    }


# C2 (2026-07-26) — the multimodal SI wire's own errors DO carry their message.
#
# This deliberately diverges from ``_agent_failure_metadata`` above, and the
# divergence is the point: that helper redacts because an *agent* exception can
# quote LLM output, a prompt, or a provider payload. The wire's two handlers
# cannot. They wrap (a) an import of ``agi_v8_1.multimodal.si_cycle_wire`` and
# (b) ``run_multimodal_si_step``, whose failure modes are import errors, path
# errors and our own ``ValueError``s. Reporting only ``type(exc).__name__``
# there made "No module named agi_v8_1.multimodal" and "cannot import name
# multimodal_si_wire_enabled" — a missing package vs. a broken refactor —
# indistinguishable in the one field an operator reads.
#
# Bounded and whitespace-collapsed because this string lands in a JSONL row and
# on the CLI's stdout.
_WIRE_ERROR_DETAIL_MAX = 300


def _wire_error_detail(exc: BaseException) -> str:
    # This value can be consumed directly from CycleResult before runtime/cli's
    # publish-side sanitizer.  The capture boundary is therefore always-on,
    # non-throwing, and masks the complete exception message before applying
    # the diagnostic cap (P0 #5.5 disk/logger sink closure).
    return _format_text_for_critical_record(
        exc,
        max_chars=_WIRE_ERROR_DETAIL_MAX,
        one_line=True,
        keep="head",
    )


@dataclass(frozen=True)
class _RuntimeOutcomeView:
    status: str
    blocked_reason: str
    axes_aligned_count: int
    apply_chain_entry_hash: str = ""
    apply_chain_current_entry_hash: str = ""


@dataclass(frozen=True)
class CycleSeed:
    """Input seed for run_cycle. R18 에서 풍부해짐."""
    objective: str
    cycle_id: str
    pod_scale: int = 72  # default 72 lean
    sandbox_enabled: bool = False
    axis_vote_enabled: bool = False


@dataclass(frozen=True)
class CycleResult:
    """Cycle 결과 envelope. R18 에서 apply chain + state 통합.

    Status values:
      stub        — R17 시드 경로 (deprecated once SI wired)
      consensus   — R18: axes_aligned >= 5 from caller-supplied Pod evidence
      advisory_stub — default Pod A/B stubs aligned; not proof of real work
      split       — R18: 3 <= axes_aligned < 5 (R19 final_integrator)
      blocked     — env disabled OR axes_aligned < 3
      completed   — reserved for R19+ (sandbox-passing apply)
      deferred    — reserved for R20+ (provider lane backoff)
    """
    schema_version: str = SCHEMA_VERSION
    cycle_id: str = ""
    status: str = "stub"
    blocked_reason: str = ""
    pod_scale_used: int = 72
    axes_aligned_count: int = 0
    sandbox_invoked: bool = False
    lean_invoked: bool = False
    artifacts: tuple[str, ...] = ()
    # R18: SI cycle wire-through
    apply_chain_entry_hash: str = ""
    # Exact hash of the apply row emitted by this cycle.  The older field
    # above intentionally remains the predecessor hash for compatibility.
    apply_chain_current_entry_hash: str = ""
    si_status: str = ""
    # R18.5 additive — None when activation ladder gate is OFF
    activation_mode: str | None = None
    v8_lane_manifest: Mapping[str, Any] | None = None
    si_layer_tickets_count: int = 0
    # R19 additive — None / 0 / () when agents not provided
    agents_invoked: tuple[str, ...] = ()
    dispatch_tickets_planned: int = 0
    handoff_packet_id: str | None = None
    critic_findings_count: int = 0
    # R20 W1 additive — None / "" when R20 path disabled (R19 compat).
    phase_history: tuple[tuple[str, str], ...] = ()
    judge_verdict: str = ""
    judge_decision_id: str = ""
    retry_chain_actions: tuple[str, ...] = ()
    artifact_manifest_evidence_count: int = 0
    artifact_manifest_ticket_count: int = 0
    artifact_manifest_decision_count: int = 0
    provider_hook_invoked: bool = False
    provider_hook_status: str = ""  # skipped_disabled | import_ok | dispatch_attempted | budget_checked | provider_called | blocked | failed
    provider_import_ok: bool = False
    provider_dispatch_attempted: bool = False
    provider_called: bool = False
    provider_budget_checked: bool = False
    provider_blocked: bool = False
    provider_failed: bool = False
    provider_budget_ledger_records: int = 0
    agent_failures: tuple[Mapping[str, Any], ...] = ()
    # __P4B_MMSI_SLOT__ P4-B additive — observation-only digest; never affects
    # status/verdict. Exactly three states, and they are DISTINGUISHABLE:
    #   * None                     -> the double gate was OFF (the default),
    #                                 byte-equivalent to the pre-wire cycle.
    #   * {"enabled": True, ...}   -> the wire ran. ``wire_error`` absent on a
    #                                 normal digest, present (with
    #                                 ``wire_error_stage``) if the step raised.
    #   * {"enabled": None, ...}   -> the gate itself could not be READ (e.g.
    #                                 the wire module failed to import), so we
    #                                 cannot claim the gate was OFF.
    # A broken ON wire used to collapse to None, i.e. it was indistinguishable
    # from a never-configured cycle in every durable artifact.
    multimodal_si_signal: Mapping[str, Any] | None = None


def is_v8_enabled() -> bool:
    return os.getenv("AGI_V8_ENABLED", "false").strip().lower() == "true"


class OrchestratorV8:
    """V8 orchestrator — R17 시드.

    R17 scope: run_cycle 가 호출 가능 + stub envelope 반환 + Pod dispatch hook 정의.
    R18~R20: state_store, apply chain, critic, agents, provider 통합.
    """

    def __init__(self, *, state_dir: Path | None = None):
        self.state_dir = state_dir or (Path(__file__).resolve().parent / "state")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._cycles_run: int = 0
        # R18: SI cycle engine (Pod-distributed axis vote + apply chain).
        self.si = SelfImprovementV8(state_dir=self.state_dir)

    def run_cycle(
        self,
        seed: CycleSeed,
        *,
        pod_a_block: Mapping[str, Any] | None = None,
        pod_b_block: Mapping[str, Any] | None = None,
        planner: "Any | None" = None,
        architect: "Any | None" = None,
        executor: "Any | None" = None,
        critic: "Any | None" = None,
        cycle_logger: "Any | None" = None,
        continuation: "Any | None" = None,
        # __R22_SLOT__ 7 optional hook callables (default None → never fire).
        # When provided AND the matching AGI_V8_CAUSAL_<HOOK>_ENABLED env is ON
        # AND master is ON, the callback is invoked with a frozen
        # {cycle_id, phase, payload} mapping. Hook errors are caught and logged
        # via cycle_logger.log_event(\"hook_error\", ...) — never crash cycle.
        hook_planning: "Any | None" = None,
        hook_trace: "Any | None" = None,
        hook_failure: "Any | None" = None,
        hook_sirepair: "Any | None" = None,
        hook_digest: "Any | None" = None,
        hook_postaccept: "Any | None" = None,
        hook_cyclelog: "Any | None" = None,
    ) -> CycleResult:
        """Run one V8 cognitive cycle.

        Behaviour:
          - is_v8_enabled() == False → status='blocked', reason='AGI_V8_ENABLED_REQUIRED'.
          - True → SelfImprovementV8.run_one_si_cycle (6-axis vote + apply chain).
            CycleResult.status carries the SI status (consensus|split|blocked|stub).

        R19 additive kwargs (all default None — backward-compatible with
        R17/R18/R18.5 callers):
          planner       — PlannerAgent; if set with architect+executor, the
                          full agent pipeline runs and tickets are counted.
          architect     — ArchitectAgent; builds layered DAG.
          executor      — ExecutorAgent; produces DispatchTickets (advisory).
          critic        — CriticAgent; scores the SI cycle decision.
          cycle_logger  — CycleLogger; logs cycle_start/agent_step/cycle_end.
          continuation  — ContinuationPlanner; builds HandoffPacket.
        """
        if not is_v8_enabled():
            return CycleResult(
                cycle_id=seed.cycle_id,
                status="blocked",
                blocked_reason="AGI_V8_ENABLED_REQUIRED",
                pod_scale_used=seed.pod_scale,
            )

        self._cycles_run += 1

        # __R22_SLOT__ collect 7 hooks for unified firing helper.
        _r22_hooks: dict[str, Any] = {
            "planning": hook_planning,
            "trace": hook_trace,
            "failure": hook_failure,
            "sirepair": hook_sirepair,
            "digest": hook_digest,
            "postaccept": hook_postaccept,
            "cyclelog": hook_cyclelog,
        }

        def _fire_r22_hook(phase: str, payload: Mapping[str, Any]) -> None:
            """R22: fire one hook iff master+per-hook env both ON AND callable provided."""
            cb = _r22_hooks.get(phase)
            if cb is None:
                return
            if not _causal_hook_enabled(phase):
                return
            try:
                cb({"cycle_id": seed.cycle_id, "phase": phase, "payload": dict(payload)})
            except Exception as hook_exc:
                # R22: hook errors MUST NOT crash the cycle.
                _swallowed(hook_exc, site="orchestrator_v8._fire_r22_hook:319", category="telemetry")
                if cycle_logger is not None:
                    try:
                        cycle_logger.log_event(
                            event_type="hook_error",
                            payload={
                                "phase": phase,
                                "error": _format_exception_for_critical_record(
                                    hook_exc,
                                    max_chars=1024,
                                    one_line=False,
                                ),
                            },
                            cycle_id=seed.cycle_id,
                        )
                    except Exception as _ff_exc:
                        _swallowed(_ff_exc, site="orchestrator_v8._fire_r22_hook:331", category="telemetry")
                        pass

        # --- R19: optional agent pipeline (planner → architect → executor) ---
        agents_invoked: list[str] = []
        agent_failures: list[Mapping[str, Any]] = []
        dispatch_tickets_planned = 0
        # __SLOT_EXECUTOR_TICKET_PRODUCE_2026_08_19__ declared OUTSIDE the
        # try block below (unlike the local ``tickets`` var, which is try
        # block-scoped and undefined on failure/absent pipeline) so the
        # produce-side wiring after the pipeline always has a defined,
        # possibly-empty tuple to check.
        _planned_tickets: tuple = ()
        if planner is not None and architect is not None and executor is not None:
            stage = "planner"
            try:
                packets = planner.plan_task_packets(
                    objective=seed.objective,
                    estimated_task_count=1,
                    route_default="pod_a",
                )
                # __R22_SLOT__ HOOK_PLANNING — fires after planner produces TaskPackets.
                _fire_r22_hook("planning", {"packets_count": len(packets)})
                if cycle_logger is not None:
                    try:
                        cycle_logger.log_event(
                            event_type="agent_step",
                            payload={
                                "agent_kind": "planner",
                                "packets_count": len(packets),
                            },
                            cycle_id=seed.cycle_id,
                        )
                    except Exception as _ff_exc:
                        _swallowed(_ff_exc, site="orchestrator_v8.run_cycle:358", category="telemetry")
                        pass
                agents_invoked.append("planner")

                stage = "architect"
                dag = architect.build_layered_dag(packets)
                if cycle_logger is not None:
                    try:
                        cycle_logger.log_event(
                            event_type="agent_step",
                            payload={
                                "agent_kind": "architect",
                                "node_count": dag.get("node_count", 0),
                            },
                            cycle_id=seed.cycle_id,
                        )
                    except Exception as _ff_exc:
                        _swallowed(_ff_exc, site="orchestrator_v8.run_cycle:374", category="telemetry")
                        pass
                agents_invoked.append("architect")

                stage = "planner_merge"
                enriched = planner.merge_with_dag(packets, dag)

                stage = "executor"
                tickets = executor.plan_dispatch(enriched, dag)
                dispatch_tickets_planned = len(tickets)
                _planned_tickets = tickets
                # __R22_SLOT__ HOOK_TRACE — fires after each agent step (executor).
                _fire_r22_hook("trace", {"agent": "executor", "tickets": dispatch_tickets_planned})
                if cycle_logger is not None:
                    try:
                        cycle_logger.log_event(
                            event_type="agent_step",
                            payload={
                                "agent_kind": "executor",
                                "tickets": dispatch_tickets_planned,
                            },
                            cycle_id=seed.cycle_id,
                        )
                    except Exception as _ff_exc:
                        _swallowed(_ff_exc, site="orchestrator_v8.run_cycle:396", category="telemetry")
                        pass
                agents_invoked.append("executor")
            except Exception as exc:
                _swallowed(exc, site="orchestrator_v8.run_cycle:399", category="telemetry")
                agent_kind = "planner" if stage == "planner_merge" else stage
                failure = _agent_failure_metadata(agent_kind, stage, exc)
                agent_failures.append(failure)
                if cycle_logger is not None:
                    try:
                        cycle_logger.log_event(
                            event_type="agent_failure",
                            payload=failure,
                            cycle_id=seed.cycle_id,
                        )
                    except Exception as _ff_exc:
                        _swallowed(_ff_exc, site="orchestrator_v8.run_cycle:410", category="telemetry")
                        pass

        # --- R18.5: optional activation-ladder + lane-manifest derivation ---
        activation_decision = None
        activation_mode_val: str | None = None
        v8_lane_manifest: Mapping[str, Any] | None = None
        if _activation_ladder_enabled():
            from agi_v8_1.core.activation import select_plan_activation
            from agi_v8_1.capabilities import PayloadPort, resolve_payload
            build_v8_lane_manifest = resolve_payload(PayloadPort(4, "agi_v8_1.core.manifest", "build_v8_lane_manifest"))
            activation_decision = select_plan_activation(
                estimated_task_count=1,  # stub — R19+ supplies real signals
                risk_score=0.0,
                disagreement_score=0.0,
            )
            activation_mode_val = activation_decision.mode.value
            v8_lane_manifest = build_v8_lane_manifest(activation_decision)

        # __SLOT_B1_SI_SWARM_EVIDENCE_2026_08_07__ B1 — 실 swarm 증거를 6축 투표로.
        #
        # 호출자가 pod 블록을 **안 준 경우에만** 채운다. 주고 온 호출자의 동작은
        # 그대로다(테스트·bridge/si_swarm_cycle 경로 불변). 게이트 OFF 면
        # ``swarm_evidence_blocks`` 가 즉시 None 이라 byte-identical.
        #
        # ⛔ 실패는 전부 None → 아래 스텁 경로 그대로. 증거 수집이 사이클을 못 죽인다.
        if pod_a_block is None and pod_b_block is None:
            from agi_v8_1.bridge import si_evidence as _si_ev

            _blocks = _si_ev.swarm_evidence_blocks(
                cycle_id=str(seed.cycle_id),
                objective=str(getattr(seed, "objective", "") or ""),
                state_dir=self.si.state_dir,
            )
            if _blocks:
                pod_a_block = _blocks["pod_a_block"]
                pod_b_block = _blocks["pod_b_block"]

        # __SLOT_EXECUTOR_TICKET_PRODUCE_2026_08_19__ track B — the ONLY
        # production caller of ticket_plan.active_plan_scope. Gate OFF, or
        # no tickets planned this cycle (pipeline absent/failed) ⇒
        # nullcontext — the si.run_one_si_cycle call below is
        # byte-identical to pre-produce behavior. plan_from_tickets never
        # touches network/state — any failure here is telemetry-shaped and
        # swallowed so it can never block a cycle. The sibling CONSUME gate
        # (si_lanes.ticket_plan.ENV) independently decides whether any
        # reader inside run_one_si_cycle acts on the plan this opens.
        _ticket_plan_cm: "Any" = contextlib.nullcontext()
        if _executor_ticket_produce_enabled() and _planned_tickets:
            try:
                from agi_v8_1.si_lanes import ticket_plan as _ticket_plan_mod

                _plan = _ticket_plan_mod.plan_from_tickets(_planned_tickets)
                _ticket_plan_cm = _ticket_plan_mod.active_plan_scope(_plan)
            except Exception as exc:  # noqa: BLE001 — ticket-plan telemetry never blocks a cycle
                _swallowed(
                    exc,
                    site="orchestrator_v8.run_cycle:ticket_plan_produce",
                    category="telemetry",
                )
                _ticket_plan_cm = contextlib.nullcontext()

        # R18: SI cycle wire. R17 의 _pod_dispatch_stub 는 호환용으로 남겨두고
        # 실 dispatch 는 self.si.run_one_si_cycle 로 위임. R19 forwards the
        # optional critic + cycle_logger so SI emits the same events.
        with _ticket_plan_cm:
            si_outcome = self.si.run_one_si_cycle(
                seed.cycle_id,
                pod_a_block=pod_a_block,
                pod_b_block=pod_b_block,
                activation_decision=activation_decision,
                critic=critic,
                cycle_logger=cycle_logger,
                objective=seed.objective,
            )
        agent_failures.extend(getattr(si_outcome, "agent_failures", ()) or ())

        # __R22_SLOT__ HOOK_FAILURE — fires when critic emits reject/infeasible.
        if si_outcome.status not in ("consensus",):
            _fire_r22_hook(
                "failure",
                {
                    "si_status": si_outcome.status,
                    "blocked_reason": si_outcome.blocked_reason,
                    "axes_aligned_count": si_outcome.axes_aligned_count,
                },
            )

        # __R22_SLOT__ HOOK_SIREPAIR — fires when SI proposes rollback policy.
        if si_outcome.status == "blocked" or "rollback" in (si_outcome.blocked_reason or "").lower():
            _fire_r22_hook(
                "sirepair",
                {"si_status": si_outcome.status, "blocked_reason": si_outcome.blocked_reason},
            )

        # __R22_SLOT__ HOOK_DIGEST — fires after digestive produces digest (advisory).
        _fire_r22_hook(
            "digest",
            {"si_status": si_outcome.status, "objective": seed.objective[:240]},
        )

        # __R22_SLOT__ HOOK_POSTACCEPT — fires after EvidenceState transitions to accepted.
        if si_outcome.status == "consensus" and not agent_failures:
            _fire_r22_hook(
                "postaccept",
                {
                    "si_status": si_outcome.status,
                    "apply_chain_entry_hash": si_outcome.apply_chain_entry_hash,
                },
            )

        # --- R18.5: optional SI 12-lane layer ---
        si_layer_tickets_count = 0
        if (
            _si_layer_enabled()
            and activation_decision is not None
            and activation_decision.si_lanes > 0
        ):
            # R2 (C8 반증): promote the dead ``run_si_layer_v2`` (si_lanes
            # delegation: propose_tickets/score_rollback_risk/observe_outcomes/
            # stable_sort_tickets) to the SOLE production caller. The v2 result
            # contract is identical to v1 (``Sequence[SILaneResult]``); the
            # only signature delta is an optional ``revert_history`` kwarg that
            # defaults to ``()`` — so this is a drop-in promotion, not an adapt.
            from agi_v8_1.self_improvement_v8 import run_si_layer_v2
            from agi_v8_1.core.messages import DecisionRecord, make_message_id
            # Stub DecisionRecord seed — R19+ pulls from real meta review.
            seed_decision = DecisionRecord(
                message_id=make_message_id("dec"),
                created_at_unix=0.0,
                decision_id=f"decision_{seed.cycle_id}",
                evidence_ids=(),
                outcome=si_outcome.status,
                rationale="orchestrator_v8 R18.5 seed",
            )
            lane_results = run_si_layer_v2(
                decision_records=(seed_decision,),
                outcome_log_path=self.si.cycle_log_path,
                activation_decision=activation_decision,
            )
            si_layer_tickets_count = sum(
                len(r.tickets_emitted) for r in lane_results
            )

        # __P4B_MMSI_SLOT__ P4-B: the multimodal SI bundle's FIRST production
        # caller (FEATURE_MAP had wired_by = NONE). Gated on the documented
        # double gate AGI_V8_MULTIMODAL_SI_ENABLED AND
        # AGI_V8_MULTIMODAL_SI_INTEGRATION_ENABLED (both default OFF), so an
        # unconfigured cycle leaves multimodal_si_signal=None → byte-equivalent
        # to the pre-wire cycle. Observation-only: it never touches the verdict.
        # Deliberately OUTSIDE the _si_layer_enabled() block so it does not
        # silently inherit an unrelated gate; it builds its own seed decision
        # exactly as the R19 critic block below does.
        # The gate read and the step call get SEPARATE handlers on purpose. A
        # single blanket try/except around both made a broken ON wire produce
        # exactly the OFF result (signal None, same status, same state_dir
        # files) — the only trace was a WARNING nobody diffs. The wire still
        # never breaks a cycle; it just can no longer fail invisibly.
        multimodal_si_signal: Mapping[str, Any] | None = None
        _mm_wire_enabled = False
        _mm_gate_error = ""
        # C2 (2026-07-26): the class name alone is not a cause. ``ImportError``
        # and ``ImportError`` are two different outages when one says "No module
        # named agi_v8_1.multimodal" and the other says "cannot import name
        # multimodal_si_wire_enabled". The message used to survive only in
        # ``policy.fail_fast._last`` — an in-process dict with no production
        # reader — so it reached nobody. It now rides the signal itself.
        _mm_gate_error_detail = ""
        try:
            from agi_v8_1.multimodal.si_cycle_wire import multimodal_si_wire_enabled

            _mm_wire_enabled = bool(multimodal_si_wire_enabled())
        except Exception as exc:  # noqa: BLE001 — incl. ImportError paths
            _swallowed(
                exc,
                site="orchestrator_v8.run_cycle:mmsi_gate",
                category="telemetry",
                detail=_wire_error_detail(exc),
            )
            _mm_gate_error = _safe_exception_type_name(exc)
            _mm_gate_error_detail = _wire_error_detail(exc)

        if _mm_gate_error:
            # We cannot claim "the gate was OFF" — we could not read it. This is
            # a THIRD state, never produced by a healthy OFF cycle, so the OFF
            # path stays byte-identical in a healthy tree.
            multimodal_si_signal = {
                "enabled": None,
                "cycle_id": str(seed.cycle_id),
                "wire_error": _mm_gate_error,
                "wire_error_detail": _mm_gate_error_detail,
                "wire_error_stage": "gate",
                "degraded": ("wire_error",),
            }
        elif _mm_wire_enabled:
            try:
                from agi_v8_1.multimodal.si_cycle_wire import run_multimodal_si_step
                from agi_v8_1.core.messages import (
                    DecisionRecord as _MMDR,
                    make_message_id as _mm_mid,
                )
                import time as _mm_t

                _mm_now = _mm_t.time()
                multimodal_si_signal = run_multimodal_si_step(
                    cycle_id=seed.cycle_id,
                    si=self.si,
                    decision=_MMDR(
                        message_id=_mm_mid("dec"),
                        created_at_unix=_mm_now,
                        decision_id=f"mmsi_decision_{seed.cycle_id}",
                        evidence_ids=(),
                        outcome=si_outcome.status,
                        rationale="orchestrator_v8 P4-B multimodal SI seed",
                    ),
                    outcome_log_path=self.si.cycle_log_path,
                    state_dir=self.state_dir,
                    now_unix=_mm_now,
                )
            except Exception as exc:  # noqa: BLE001 — the wire never breaks a cycle
                _swallowed(
                    exc,
                    site="orchestrator_v8.run_cycle:mmsi_step",
                    category="telemetry",
                    detail=_wire_error_detail(exc),
                )
                # enabled=True: the gates WERE on. A caller diffing this field
                # against an OFF cycle now sees the difference.
                multimodal_si_signal = {
                    "enabled": True,
                    "cycle_id": str(seed.cycle_id),
                    "wire_error": _safe_exception_type_name(exc),
                    # C2: the class name is the alarm, the message is the cause.
                    "wire_error_detail": _wire_error_detail(exc),
                    "wire_error_stage": "step",
                    "degraded": ("wire_error",),
                }

        # --- R19: critic findings count (decision-level scoring) ---
        critic_findings_count = 0
        critic_invoked_ok = bool(
            critic is not None
            and not any(f.get("agent_kind") == "critic" for f in agent_failures)
        )
        if critic is not None and critic_invoked_ok:
            try:
                from agi_v8_1.core.messages import DecisionRecord as _DR, make_message_id as _mid
                import time as _t
                synth = _DR(
                    message_id=_mid("dec"),
                    created_at_unix=_t.time(),
                    decision_id=f"orch_decision_{seed.cycle_id}",
                    evidence_ids=(),
                    outcome=si_outcome.status,
                    rationale=si_outcome.blocked_reason or "orchestrator_cycle",
                )
                ticket = critic.evaluate_decision(synth, ())
                if ticket is not None:
                    critic_findings_count = 1
                critic_invoked_ok = True
            except Exception as exc:
                _swallowed(exc, site="orchestrator_v8.run_cycle:531", category="telemetry")
                critic_invoked_ok = False
                agent_failures.append(
                    _agent_failure_metadata("critic", "orchestrator_critic_evaluate", exc)
                )
        if critic_invoked_ok and not any(f.get("agent_kind") == "critic" for f in agent_failures):
            agents_invoked.append("critic")

        effective_status = si_outcome.status
        effective_blocked_reason = si_outcome.blocked_reason
        if agent_failures:
            effective_status = "blocked"
            failed_agents = ",".join(
                sorted({str(f.get("agent_kind", "agent")) for f in agent_failures})
            )
            effective_blocked_reason = f"AGENT_FAILURE:{failed_agents}"
        runtime_outcome = _RuntimeOutcomeView(
            status=effective_status,
            blocked_reason=effective_blocked_reason,
            axes_aligned_count=si_outcome.axes_aligned_count,
            apply_chain_entry_hash=si_outcome.apply_chain_entry_hash,
            apply_chain_current_entry_hash=(
                si_outcome.apply_chain_current_entry_hash
            ),
        )

        # --- R19: continuation HandoffPacket build ---
        handoff_packet_id: str | None = None
        if continuation is not None:
            try:
                from agi_v8_1.core.messages import DecisionRecord as _DR, make_message_id as _mid
                import time as _t
                synth_for_handoff = _DR(
                    message_id=_mid("dec"),
                    created_at_unix=_t.time(),
                    decision_id=f"orch_decision_{seed.cycle_id}",
                    evidence_ids=(),
                    outcome=runtime_outcome.status,
                    rationale=runtime_outcome.blocked_reason or "orchestrator_cycle",
                )
                handoff = continuation.build_handoff(
                    decision_records=(synth_for_handoff,),
                    cycle_id=seed.cycle_id,
                )
                handoff_packet_id = handoff.handoff_id
                agents_invoked.append("continuation")
                if cycle_logger is not None:
                    try:
                        cycle_logger.log_event(
                            event_type="handoff",
                            payload={
                                "handoff_id": handoff.handoff_id,
                                "next_action": handoff.next_action,
                                "blockers_count": len(handoff.blockers),
                            },
                            cycle_id=seed.cycle_id,
                        )
                    except Exception as _ff_exc:
                        _swallowed(_ff_exc, site="orchestrator_v8.run_cycle:585", category="telemetry")
                        pass
            except Exception as _ff_exc:
                _swallowed(_ff_exc, site="orchestrator_v8.run_cycle:587", category="telemetry")
                pass

        # --- R20 W1: optional phase manager + judge + retry_chain wiring ---
        phase_history: tuple[tuple[str, str], ...] = ()
        judge_verdict_val = ""
        judge_decision_id = ""
        retry_chain_actions: tuple[str, ...] = ()
        manifest_ev_count = 0
        manifest_tk_count = 0
        manifest_dc_count = 0
        provider_hook_invoked = False
        provider_hook_status = ""
        provider_import_ok = False
        provider_dispatch_attempted = False
        provider_called = False
        provider_budget_checked = False
        provider_blocked = False
        provider_failed = False
        provider_budget_ledger_records = 0

        if _phase_manager_enabled_env():
            try:
                phase_history = self._run_r20_phase_chain(
                    seed=seed,
                    si_outcome=runtime_outcome,
                    critic=critic,
                    cycle_logger=cycle_logger,
                )
            except Exception as _ff_exc:
                # Defensive — R20 path must not break R19 cycle outcome.
                _swallowed(_ff_exc, site="orchestrator_v8.run_cycle:616", category="telemetry")
                phase_history = ()

        # R20 W1 judge runs always when env is set, even without phase
        # manager — produces a DecisionRecord for the cycle log.
        from agi_v8_1.core.judge import _judge_enabled
        if _judge_enabled():
            try:
                judge_outcome = self._run_r20_judge(
                    seed=seed,
                    si_outcome=runtime_outcome,
                    critic=critic,
                )
                judge_verdict_val = judge_outcome.verdict
                judge_decision_id = judge_outcome.decision_record.decision_id
            except Exception as _ff_exc:
                _swallowed(_ff_exc, site="orchestrator_v8.run_cycle:632", category="telemetry")
                pass

        # R20 W1 retry_chain — bounded simulation of recovery decisions.
        from agi_v8_1.core.retry_chain import _retry_chain_enabled
        if _retry_chain_enabled():
            try:
                retry_chain_actions = self._run_r20_retry_chain(
                    seed=seed,
                    si_outcome=runtime_outcome,
                )
            except Exception as _ff_exc:
                _swallowed(_ff_exc, site="orchestrator_v8.run_cycle:643", category="telemetry")
                pass

        # R20 W1 cycle artifact manifest — always produced (cheap aggregate);
        # values are 0 when no upstream artifacts collected.
        try:
            manifest = self._build_r20_manifest(
                seed=seed,
                si_outcome=runtime_outcome,
                handoff_packet_id=handoff_packet_id,
            )
            manifest_ev_count = len(manifest.evidence_ids)
            manifest_tk_count = len(manifest.ticket_ids)
            manifest_dc_count = len(manifest.decision_ids)
        except Exception as _ff_exc:
            _swallowed(_ff_exc, site="orchestrator_v8.run_cycle:657", category="telemetry")
            pass

        # R20 W1 provider hook — only invoked when W2 has shipped providers
        # AND AGI_V8_PROVIDERS_ENABLED is true. Default OFF preserves the
        # advisory invariant (real_dispatch_started=False).
        if _providers_enabled_env():
            provider_hook_invoked = True
            try:
                provider_meta = self._dispatch_to_provider_lane(
                    seed=seed,
                    si_outcome=runtime_outcome,
                )
                provider_import_ok = bool(provider_meta.get("import_ok"))
                provider_dispatch_attempted = bool(provider_meta.get("dispatch_attempted"))
                provider_called = bool(provider_meta.get("provider_called"))
                provider_budget_checked = bool(provider_meta.get("budget_checked"))
                provider_blocked = bool(provider_meta.get("blocked"))
                provider_failed = bool(provider_meta.get("failed"))
                provider_budget_ledger_records = int(
                    provider_meta.get("budget_ledger_records", 0)
                )
                if provider_failed:
                    provider_hook_status = "failed"
                elif provider_blocked:
                    provider_hook_status = "blocked"
                elif provider_called:
                    provider_hook_status = "provider_called"
                elif provider_budget_checked:
                    provider_hook_status = "budget_checked"
                elif provider_dispatch_attempted:
                    provider_hook_status = "dispatch_attempted"
                elif provider_import_ok:
                    provider_hook_status = "import_ok"
                else:
                    provider_hook_status = "failed"
            except NotImplementedError as _ff_exc:
                _swallowed(_ff_exc, site="orchestrator_v8.run_cycle:693", category="telemetry")
                provider_failed = True
                provider_hook_status = "failed"
            except Exception as _ff_exc:
                _swallowed(_ff_exc, site="orchestrator_v8.run_cycle:696", category="telemetry")
                provider_failed = True
                provider_hook_status = "failed"
        else:
            provider_hook_status = "skipped_disabled"

        # __R22_SLOT__ HOOK_CYCLELOG — fires on cycle_end (last advisory fire).
        _fire_r22_hook(
            "cyclelog",
            {
                "si_status": runtime_outcome.status,
                "cycles_run": self._cycles_run,
                "agents_invoked": list(agents_invoked),
                "agent_failures": list(agent_failures),
            },
        )

        return CycleResult(
            cycle_id=seed.cycle_id,
            status=runtime_outcome.status,
            blocked_reason=runtime_outcome.blocked_reason,
            pod_scale_used=seed.pod_scale,
            axes_aligned_count=si_outcome.axes_aligned_count,
            sandbox_invoked=seed.sandbox_enabled,
            lean_invoked=False,
            artifacts=(),
            apply_chain_entry_hash=si_outcome.apply_chain_entry_hash,
            apply_chain_current_entry_hash=(
                si_outcome.apply_chain_current_entry_hash
            ),
            si_status=runtime_outcome.status,
            activation_mode=activation_mode_val,
            v8_lane_manifest=v8_lane_manifest,
            si_layer_tickets_count=si_layer_tickets_count,
            agents_invoked=tuple(agents_invoked),
            dispatch_tickets_planned=dispatch_tickets_planned,
            handoff_packet_id=handoff_packet_id,
            critic_findings_count=critic_findings_count,
            phase_history=phase_history,
            judge_verdict=judge_verdict_val,
            judge_decision_id=judge_decision_id,
            retry_chain_actions=retry_chain_actions,
            artifact_manifest_evidence_count=manifest_ev_count,
            artifact_manifest_ticket_count=manifest_tk_count,
            artifact_manifest_decision_count=manifest_dc_count,
            provider_hook_invoked=provider_hook_invoked,
            provider_hook_status=provider_hook_status,
            provider_import_ok=provider_import_ok,
            provider_dispatch_attempted=provider_dispatch_attempted,
            provider_called=provider_called,
            provider_budget_checked=provider_budget_checked,
            provider_blocked=provider_blocked,
            provider_failed=provider_failed,
            provider_budget_ledger_records=provider_budget_ledger_records,
            agent_failures=tuple(agent_failures),
            multimodal_si_signal=multimodal_si_signal,
        )

    # ── R20 W1 helpers (default OFF — gated by env knobs) ────────────────

    def _run_r20_phase_chain(
        self,
        *,
        seed: CycleSeed,
        si_outcome: Any,
        critic: "Any | None",
        cycle_logger: "Any | None",
    ) -> tuple[tuple[str, str], ...]:
        """Walk the V8 cycle through the deterministic phase machine.

        v7.1 orchestrator.py:5081-5117 advance_phase semantics, ported to V8
        as a free PhaseManager. Records each transition; never regresses.
        """
        from agi_v8_1.capabilities import PayloadPort, resolve_payload
        build_phase_manager = resolve_payload(PayloadPort(5, "agi_v8_1.core.phase_manager", "build_phase_manager"))
        from agi_v8_1.core.orchestrator_schema import CyclePhase

        pm = build_phase_manager(seed.cycle_id)
        if not pm.enabled:
            return ()

        pm.advance_to(CyclePhase.PLANNING, reason="r20_start")
        pm.advance_to(CyclePhase.EXECUTION, reason="post_si_run")
        pm.advance_to(CyclePhase.ACCEPTANCE, reason="advisory_acceptance")
        pm.advance_to(CyclePhase.JUDGE, reason="judge_pass")
        # Move into retry_chain only if SI cycle was non-consensus.
        if si_outcome.status != "consensus":
            pm.advance_to(CyclePhase.RETRY_CHAIN, reason=f"si_status={si_outcome.status}")
        pm.advance_to(CyclePhase.CONTINUATION, reason="continuation_phase")
        if pm.exit_satisfied(axes_aligned=si_outcome.axes_aligned_count):
            pm.advance_to(CyclePhase.COMPLETED, reason="exit_satisfied")

        if cycle_logger is not None:
            try:
                cycle_logger.log_event(
                    event_type="phase_chain",
                    payload={
                        "transitions": list(pm.history_as_tuples()),
                        "terminal": pm.is_terminal(),
                    },
                    cycle_id=seed.cycle_id,
                )
            except Exception as _ff_exc:
                _swallowed(_ff_exc, site="orchestrator_v8._run_r20_phase_chain:793", category="telemetry")
                pass

        return pm.history_as_tuples()

    def _run_r20_judge(
        self,
        *,
        seed: CycleSeed,
        si_outcome: Any,
        critic: "Any | None",
    ) -> "Any":
        """Run the R20 judge over the SI cycle outcome."""
        from agi_v8_1.core.acceptance_gate import judge_acceptance
        from agi_v8_1.core.judge import judge_cycle

        # Synthesize an AcceptanceJudgement from the SI cycle status. This
        # is the V8 analogue of v7.1 orchestrator.py:4724-4796 critic-phase
        # payload: synthesize the verdict from the upstream signal.
        synth_goal = {"acceptance_criteria": []}  # no path criteria — use status
        synth_tester = {"verdict": "pass" if si_outcome.status == "consensus" else "fail"}
        acceptance = judge_acceptance(
            goal_card=synth_goal,
            tester_output=synth_tester,
            ground_truth_inventory=(),
        )
        return judge_cycle(
            cycle_id=seed.cycle_id,
            acceptance=acceptance,
            evidence=(),
            critic=critic,
            rationale_extra=f"si_outcome.status={si_outcome.status}",
        )

    def _run_r20_retry_chain(
        self,
        *,
        seed: CycleSeed,
        si_outcome: Any,
    ) -> tuple[str, ...]:
        """Simulate the bounded retry chain for the SI outcome."""
        from agi_v8_1.core.retry_chain import (
            RetryChainState,
            chain_to_termination,
        )

        # SI consensus → no retries needed. Anything else → simulate.
        if si_outcome.status == "consensus":
            return ()

        repeat = 0 if si_outcome.status == "split" else 1
        trail = chain_to_termination(
            RetryChainState(
                failure_signature=si_outcome.blocked_reason or si_outcome.status,
                repeat_count=repeat,
                retry_count=0,
                max_retry=3,
                elapsed_s=0.0,
                open_task_count=0,
            )
        )
        return tuple(d.action for d in trail)

    def _build_r20_manifest(
        self,
        *,
        seed: CycleSeed,
        si_outcome: Any,
        handoff_packet_id: str | None,
    ) -> "Any":
        """Build a cycle artifact manifest. Always cheap, always produced."""
        from agi_v8_1.core.cycle_artifacts import collect_manifest

        # R20 W1 does not yet collect evidence cards through the cycle (R19
        # agents emit them but the orchestrator does not aggregate them
        # here yet). The manifest is intentionally empty by default; it
        # will fill in once W2 wires real evidence collection.
        return collect_manifest(
            cycle_id=seed.cycle_id,
            evidence=(),
            tickets=(),
            decisions=(),
            handoff=None,
            completed_phase="orchestrator_v8",
        )

    def _dispatch_to_provider_lane(
        self,
        *,
        seed: CycleSeed,
        si_outcome: Any,
    ) -> dict[str, bool | int]:
        """Provider-enabled runtime hook with a no-network mock dispatch.

        Default OFF (AGI_V8_PROVIDERS_ENABLED). Even when enabled, this hook
        MUST not violate the advisory invariant: real_dispatch_started is
        owned by dry_run_dispatch and is always False at the V8 contract
        level. Phase 4 uses MockModelProvider only: it proves import, dispatch
        intent, budget preflight, and provider-call truth without live network
        or SDK calls.
        """
        try:
            from agi_v8_1.enforcement import CostBudgetExceeded
            from agi_v8_1.enforcement.cost_limiter import CostBudgetLedger
            from agi_v8_1.providers import MockModelProvider, ProviderManager
        except Exception as exc:  # ImportError or anything W2 not done yet
            raise NotImplementedError(
                "providers not yet ported — W2 slot. "
                "underlying import error: "
                f"{_format_exception_for_critical_record(exc, max_chars=1024, one_line=False)}"
            ) from None

        def _runtime_expected_output_tokens() -> int:
            try:
                return max(
                    1,
                    int(
                        os.getenv(
                            "AGI_V8_PROVIDER_RUNTIME_EXPECTED_OUTPUT_TOKENS",
                            "64",
                        )
                    ),
                )
            except ValueError as _ff_exc:
                _swallowed(_ff_exc, site="orchestrator_v8._runtime_expected_output_tokens:915", category="telemetry")
                return 64

        class _RuntimeBudgetTruthMockProvider(MockModelProvider):
            provider_id = "orchestrator_v8_phase4_budget_truth_mock"
            model = "mock-provider-budget-truth"
            budget_expected_output_tokens = _runtime_expected_output_tokens()

            def __init__(self) -> None:
                self.calls = 0
                self.last_usage: dict[str, Any] = {}

            def generate(
                self,
                agent_name: str,
                prompt: str,
                payload: Mapping[str, Any],
                schema: Mapping[str, Any],
            ) -> str:
                self.calls += 1
                return super().generate(
                    agent_name=agent_name,
                    prompt=prompt,
                    payload=payload,
                    schema=schema,
                )

        provider = _RuntimeBudgetTruthMockProvider()
        ledger = CostBudgetLedger(raise_on_exceed=True)
        manager = ProviderManager(
            registry={"support_advisor": provider},
            budget_ledger=ledger,
        )
        meta: dict[str, bool | int] = {
            "import_ok": True,
            "dispatch_attempted": False,
            "provider_called": False,
            "budget_checked": False,
            "blocked": False,
            "failed": False,
            "budget_ledger_records": 0,
        }
        try:
            meta["dispatch_attempted"] = True
            manager.request(
                "support_advisor",
                "phase4 provider budget truth smoke",
                {
                    "phase": "provider_budget_truth",
                    "cycle_id": seed.cycle_id,
                    "si_status": getattr(si_outcome, "status", ""),
                    "objective_preview": seed.objective[:120],
                },
                {"type": "object"},
            )
            meta["provider_called"] = provider.calls > 0
            meta["budget_checked"] = any(
                bool(record.get("budget_checked")) for record in ledger.records
            )
        except CostBudgetExceeded as _ff_exc:
            _swallowed(_ff_exc, site="orchestrator_v8._dispatch_to_provider_lane:974", category="telemetry")
            meta["blocked"] = True
            meta["provider_called"] = provider.calls > 0
            meta["budget_checked"] = True
        except Exception as _ff_exc:
            _swallowed(_ff_exc, site="orchestrator_v8._dispatch_to_provider_lane:978", category="telemetry")
            meta["failed"] = True
            meta["provider_called"] = provider.calls > 0
            meta["budget_checked"] = any(
                bool(record.get("budget_checked")) for record in ledger.records
            )

        meta["budget_ledger_records"] = len(ledger.records)
        return meta

    def _pod_dispatch_stub(self, seed: CycleSeed) -> dict[str, Any]:
        """Legacy R17 stub (kept for backward compat with R17 tests).

        R18+ uses :attr:`si` (``SelfImprovementV8``) directly via
        :meth:`run_cycle`.
        """
        return {
            "axes_aligned_count": 0,
            "stub": True,
            "stub_reason": "R17_seed_phase",
        }

    @property
    def cycles_run(self) -> int:
        return self._cycles_run


__all__ = ["OrchestratorV8", "CycleSeed", "CycleResult", "is_v8_enabled", "SCHEMA_VERSION"]
