"""agi_v8_1 core — Pod A/B/Meta distributed primitives ported from
agi_v7.1/agent_system/swarm (R15+R16 검증된 base).

R17: 이식 + import path 조정. R18~R20 에서 V8 SI 코어 위에 결합.

Dependency graph (all intra-package):
  roles ← schema ← manifest ← scheduler ← backpressure
  axis_scorer ← final_integrator
  sandbox_runner, lean_adapter, prompt_topology,
  difficulty_router, streaming_meta (standalone)
"""

from importlib import import_module
from agi_v8_1.capabilities import PayloadPort, resolve_payload

# Preserve the public facade without eager optional-subsystem imports.
_EXPORTS = {'AxisEvidence': ('.axis_scorer', 'AxisEvidence'),
 'AXIS_KEYS': ('.axis_scorer', 'AXIS_KEYS'),
 'extract_axis_evidence': ('.axis_scorer', 'extract_axis_evidence'),
 'vote_per_axis': ('.axis_scorer', 'vote_per_axis'),
 'vote_axes_with_evidence': ('.axis_scorer', 'vote_axes_with_evidence'),
 'is_axis_vote_enabled': ('.axis_scorer', 'is_axis_vote_enabled'),
 'SandboxTelemetry': ('.sandbox_runner', 'SandboxTelemetry'),
 'sandbox_run': ('.sandbox_runner', 'run'),
 'sandbox_run_async': ('.sandbox_runner', 'run_async'),
 'is_sandbox_enabled': ('.sandbox_runner', 'is_sandbox_enabled'),
 'PromptBlock': ('.prompt_topology', 'PromptBlock'),
 'split_static_dynamic': ('.prompt_topology', 'split_static_dynamic'),
 'assert_prefix_stable': ('.prompt_topology', 'assert_prefix_stable'),
 'measure_cache_hit_potential': ('.prompt_topology', 'measure_cache_hit_potential'),
 'DifficultyVerdict': ('.difficulty_router', 'DifficultyVerdict'),
 'estimate_difficulty': ('.difficulty_router', 'estimate_difficulty'),
 'router_scale_for': ('.difficulty_router', 'router_scale_for'),
 'write_pruning_advisory': ('.difficulty_router', 'write_pruning_advisory'),
 'estimate_cost_usd_for_scale': ('.difficulty_router', 'estimate_cost_usd_for_scale'),
 'CycleLogger': ('.cycle_logger', 'CycleLogger'),
 'CYCLE_LOGGER_EVENT_TYPES': ('.cycle_logger', 'EVENT_TYPES'),
 'ContinuationPlanner': ('.continuation', 'ContinuationPlanner'),
 'MAX_CONTINUATION_CYCLES': ('.continuation', 'MAX_CONTINUATION_CYCLES'),
 'EvidenceState': ('.evidence_gate', 'EvidenceState'),
 'can_promote': ('.evidence_gate', 'can_promote'),
 'promote': ('.evidence_gate', 'promote'),
 'promote_with_audit': ('.evidence_gate', 'promote_with_audit'),
 'collect_manifest': ('.cycle_artifacts', 'collect_manifest'),
 'manifest_to_dict': ('.cycle_artifacts', 'manifest_to_dict'),
 'manifest_to_dict_with_eval': ('.cycle_artifacts', 'manifest_to_dict_with_eval')}


# Keep optional operations at their actual tier. Literal finite coordinates
# let the release checker prove this boundary without following live imports.
_PAYLOAD_EXPORTS = {
    'synthesize_audit_third_proposal': PayloadPort(4, 'agi_v8_1.core.final_integrator', 'synthesize_audit_third_proposal'),
    'synthesize_generation_third_proposal': PayloadPort(4, 'agi_v8_1.core.final_integrator', 'synthesize_generation_third_proposal'),
    'pareto_frontier_select': PayloadPort(4, 'agi_v8_1.core.final_integrator', 'pareto_frontier_select'),
    'is_final_integrator_enabled': PayloadPort(4, 'agi_v8_1.core.final_integrator', 'is_final_integrator_enabled'),
    'LeanCheckResult': PayloadPort(5, 'agi_v8_1.core.lean_adapter', 'LeanCheckResult'),
    'lean_check': PayloadPort(5, 'agi_v8_1.core.lean_adapter', 'lean_check'),
    'is_lean_enabled': PayloadPort(5, 'agi_v8_1.core.lean_adapter', 'is_lean_enabled'),
    'is_lean_installed': PayloadPort(5, 'agi_v8_1.core.lean_adapter', 'is_lean_installed'),
    'TaskMatchEvent': PayloadPort(4, 'agi_v8_1.core.streaming_meta', 'TaskMatchEvent'),
    'StreamingMetaCoordinator': PayloadPort(4, 'agi_v8_1.core.streaming_meta', 'StreamingMetaCoordinator'),
    'streaming_meta_entry': PayloadPort(4, 'agi_v8_1.core.scheduler', 'streaming_meta_entry'),
    'SUPPORTED_SCALES': PayloadPort(4, 'agi_v8_1.core.roles', 'SUPPORTED_SCALES', callable_only=False),
    'POD_A_SIZE': PayloadPort(4, 'agi_v8_1.core.roles', 'POD_A_SIZE', callable_only=False),
    'POD_B_SIZE': PayloadPort(4, 'agi_v8_1.core.roles', 'POD_B_SIZE', callable_only=False),
    'META_REVIEW_180': PayloadPort(4, 'agi_v8_1.core.roles', 'META_REVIEW_180', callable_only=False),
    'META_REVIEW_192': PayloadPort(4, 'agi_v8_1.core.roles', 'META_REVIEW_192', callable_only=False),
    'TOTAL_LANES_180': PayloadPort(4, 'agi_v8_1.core.roles', 'TOTAL_LANES_180', callable_only=False),
    'TOTAL_LANES_192': PayloadPort(4, 'agi_v8_1.core.roles', 'TOTAL_LANES_192', callable_only=False),
    'resolve_scale_layout': PayloadPort(4, 'agi_v8_1.core.roles', 'resolve_scale_layout'),
    'LaneRole': PayloadPort(4, 'agi_v8_1.core.roles', 'LaneRole'),
}


def __getattr__(name):
    port = _PAYLOAD_EXPORTS.get(name)
    if port is not None:
        return resolve_payload(port)
    if name not in _EXPORTS:
        raise AttributeError("unknown core facade export")
    module, symbol = _EXPORTS[name]
    return getattr(import_module(module, __name__), symbol)


__all__ = [
    # axis_scorer
    "AxisEvidence",
    "AXIS_KEYS",
    "extract_axis_evidence",
    "vote_per_axis",
    "vote_axes_with_evidence",
    "is_axis_vote_enabled",
    # final_integrator
    "synthesize_audit_third_proposal",
    "synthesize_generation_third_proposal",
    "pareto_frontier_select",
    "is_final_integrator_enabled",
    # sandbox_runner
    "SandboxTelemetry",
    "sandbox_run",
    "sandbox_run_async",
    "is_sandbox_enabled",
    # lean_adapter
    "LeanCheckResult",
    "lean_check",
    "is_lean_enabled",
    "is_lean_installed",
    # prompt_topology
    "PromptBlock",
    "split_static_dynamic",
    "assert_prefix_stable",
    "measure_cache_hit_potential",
    # difficulty_router
    "DifficultyVerdict",
    "estimate_difficulty",
    "router_scale_for",
    "write_pruning_advisory",
    "estimate_cost_usd_for_scale",
    # streaming_meta
    "TaskMatchEvent",
    "StreamingMetaCoordinator",
    # scheduler
    "streaming_meta_entry",
    # roles
    "SUPPORTED_SCALES",
    "POD_A_SIZE",
    "POD_B_SIZE",
    "META_REVIEW_180",
    "META_REVIEW_192",
    "TOTAL_LANES_180",
    "TOTAL_LANES_192",
    "resolve_scale_layout",
    "LaneRole",
    # R19 cycle_logger + continuation
    "CycleLogger",
    "CYCLE_LOGGER_EVENT_TYPES",
    "ContinuationPlanner",
    "MAX_CONTINUATION_CYCLES",
    # R18 evidence_gate + R26 promote_with_audit
    "EvidenceState",
    "can_promote",
    "promote",
    "promote_with_audit",
    # R20 cycle_artifacts + R26 manifest_to_dict_with_eval
    "collect_manifest",
    "manifest_to_dict",
    "manifest_to_dict_with_eval",
]
