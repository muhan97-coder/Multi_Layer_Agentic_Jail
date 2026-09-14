"""Lazy compatibility facade: public contracts do not load optional operations."""

from __future__ import annotations

from importlib import import_module
from agi_v8_1.capabilities import PayloadPort, resolve_payload

_PUBLIC_EXPORTS = {
    "SwarmConfig": "agi_v8_1.agent_system.swarm_v8.config",
    "load_config": "agi_v8_1.agent_system.swarm_v8.config",
    "LaneLayer": "agi_v8_1.agent_system.swarm_v8.schemas",
    "TaskBundle": "agi_v8_1.agent_system.swarm_v8.schemas",
}

_PAYLOAD_EXPORTS = {
    "TaskBundleLoadError": "agi_v8_1.agent_system.swarm_v8.task_input_loader",
    "compare_bundle_against_config": "agi_v8_1.agent_system.swarm_v8.task_input_loader",
    "load_task_bundle": "agi_v8_1.agent_system.swarm_v8.task_input_loader",
    "DeterministicMockExecutor": "agi_v8_1.agent_system.swarm_v8.executors",
    "run_swarm": "agi_v8_1.agent_system.swarm_v8.kernel",
    "serialize_decision": "agi_v8_1.agent_system.swarm_v8.synthesizer",
    "synthesize": "agi_v8_1.agent_system.swarm_v8.synthesizer",
    "build_topology": "agi_v8_1.agent_system.swarm_v8.topology",
    "ROLE_PROMPT_PACKET_SCHEMA_VERSION": "agi_v8_1.agent_system.swarm_v8.role_prompt_packet",
    "RolePromptPacket": "agi_v8_1.agent_system.swarm_v8.role_prompt_packet",
    "RolePromptPacketBundle": "agi_v8_1.agent_system.swarm_v8.role_prompt_packet",
    "build_role_prompt_packet": "agi_v8_1.agent_system.swarm_v8.role_prompt_packet",
}

__all__ = [
    "SwarmConfig",
    "load_config",
    "build_topology",
    "run_swarm",
    "DeterministicMockExecutor",
    "synthesize",
    "serialize_decision",
    "TaskBundle",
    "TaskBundleLoadError",
    "load_task_bundle",
    "compare_bundle_against_config",
    "LaneLayer",
    "RolePromptPacket",
    "RolePromptPacketBundle",
    "build_role_prompt_packet",
    "ROLE_PROMPT_PACKET_SCHEMA_VERSION",
]


def __getattr__(name: str):
    module = _PUBLIC_EXPORTS.get(name)
    if module is not None:
        return getattr(import_module(module), name)
    module = _PAYLOAD_EXPORTS.get(name)
    if module is not None:
        return resolve_payload(
            PayloadPort(
                4,
                module,
                name,
                callable_only=name != "ROLE_PROMPT_PACKET_SCHEMA_VERSION",
            )
        )
    raise AttributeError(name)


def __dir__():
    return sorted(set(globals()) | set(__all__))
