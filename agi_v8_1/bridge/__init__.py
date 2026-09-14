"""Lazy compatibility facade: public contracts do not load optional operations."""

from __future__ import annotations

from importlib import import_module
from agi_v8_1.capabilities import PayloadPort, resolve_payload

_PUBLIC_EXPORTS = {
    "run_change_detect": "agi_v8_1.bridge.change_detect_wire",
    "pod_report_to_si_block": "agi_v8_1.bridge.sanitize",
    "sanitize_swarm_result": "agi_v8_1.bridge.sanitize",
    "SwarmGateDecision": "agi_v8_1.bridge.swarm_gate",
    "estimate_risk_tier": "agi_v8_1.bridge.swarm_gate",
    "evaluate_swarm_gate": "agi_v8_1.bridge.swarm_gate",
}

# Source-owned coordinates: accessing an operation first checks its tier;
# merely importing the facade must not require a higher-tier installation.
_PAYLOAD_EXPORTS = {
    "build_swarm_handoff_packet": PayloadPort(4, "agi_v8_1.bridge.feedback", "build_swarm_handoff_packet"),
    "emit_swarm_arm_digest": PayloadPort(4, "agi_v8_1.bridge.feedback", "emit_swarm_arm_digest"),
    "emit_swarm_observation": PayloadPort(4, "agi_v8_1.bridge.feedback", "emit_swarm_observation"),
    "SICycleWithSwarmOutcome": PayloadPort(4, "agi_v8_1.bridge.si_swarm_cycle", "SICycleWithSwarmOutcome"),
    "run_si_cycle_with_swarm": PayloadPort(4, "agi_v8_1.bridge.si_swarm_cycle", "run_si_cycle_with_swarm"),
    "SwarmDispatchResult": PayloadPort(4, "agi_v8_1.bridge.swarm_dispatch", "SwarmDispatchResult"),
    "dispatch_swarm": PayloadPort(4, "agi_v8_1.bridge.swarm_dispatch", "dispatch_swarm"),
}

__all__ = [
    "dispatch_swarm",
    "SwarmDispatchResult",
    "pod_report_to_si_block",
    "sanitize_swarm_result",
    "evaluate_swarm_gate",
    "SwarmGateDecision",
    "estimate_risk_tier",
    "emit_swarm_observation",
    "emit_swarm_arm_digest",
    "build_swarm_handoff_packet",
    "run_si_cycle_with_swarm",
    "SICycleWithSwarmOutcome",
    "run_change_detect",
]


def __getattr__(name: str):
    module = _PUBLIC_EXPORTS.get(name)
    if module is not None:
        return getattr(import_module(module), name)
    port = _PAYLOAD_EXPORTS.get(name)
    if port is not None:
        return resolve_payload(port)
    raise AttributeError(name)


def __dir__():
    return sorted(set(globals()) | set(__all__))
