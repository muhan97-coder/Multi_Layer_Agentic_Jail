"""Source coordinates for the tested Stage-C bundles, not a release allowlist.

No gate values, authorization, personal memory, runtime state, or credentials
live here. Reader-side tier annotations remain the gate SSOT. A future release
must separately approve source/config/docs packaging and unlock enforcement.
"""
from types import MappingProxyType


PAYLOAD_SOURCES = MappingProxyType({
    3: (
        "providers/deepseek_provider.py", "providers/openai_provider.py",
        "providers/openrouter_provider.py", "providers/anthropic_provider.py",
        "providers/gemini_provider.py", "providers/phone_llm_provider.py",
        "providers/cohere_provider.py", "providers/groq_provider.py",
        "providers/perplexity_provider.py", "providers/xai_provider.py",
        "providers/mistral_provider.py", "providers/ollama_provider.py",
        "providers/provider_manager.py", "providers/prompt_cache.py", "providers/retry.py",
        "providers/empty_response_retry.py",
    ),
    4: (
        "bridge/si_evidence_prompts_payload.py",
        "bridge/si_swarm_access_payload.py",
        "providers/circuit_breaker.py", "providers/failover_provider.py",
        "agent_system/swarm_v8/kernel.py", "agent_system/swarm_v8/executors.py",
        "agent_system/swarm_v8/topology.py", "agent_system/swarm_v8/synthesizer.py",
        "agent_system/swarm_v8/sandbox_matrix.py", "agent_system/swarm_v8/role_prompt_packet.py",
        "bridge/swarm_dispatch_payload.py", "bridge/hetero_pod_payload.py",
        "bridge/feedback.py", "bridge/si_swarm_cycle.py", "bridge/swarm_dispatch.py",
        "core/final_integrator.py", "core/manifest.py", "core/roles.py",
        "core/scheduler.py", "core/streaming_meta.py", "agent_system/swarm_v8/task_input_loader.py",
    ),
    5: ("core/sandbox_payload.py", "si_lanes/verify_access_payload.py",
        "si_lanes/verify_gate.py", "core/phase_manager.py", "core/lean_adapter.py",
        "eval/__init__.py", "hallucination_audit/__init__.py"),
    6: (
        "verifier/cross_model_adversary.py", "verifier/cross_model_verifier.py",
        "verifier/reviewer_provider_payload.py",
    ),
    7: (
        "bus/cycle_wire_payload.py", "runtime/progress_stall_payload.py",
        "bus/consumers/predicted_delta.py", "bus/producers/pnl_surprise_bridge.py",
        "multimodal/si_evidence_source.py", "multimodal/si_integration.py",
        "si_lanes/multimodal_observer.py", "si_lanes/multimodal_proposer.py",
    ),
    8: ("tools/promote_run_payload.py", "si_lanes/promote_driver_payload.py"),
    9: (
        "bridge/si_distilled_recall_payload.py", "si_lanes/si_autonomy_payload.py",
        "runtime/autonomous_tick_payload.py", "runtime/work_feeder.py",
        "runtime/goal_campaign_feed.py", "runtime/tick_review.py",
        "memory/external_tmi_payload.py",
        "runtime/auto_apply_payload.py",
        "si_lanes/hetero_lanes_payload.py", "si_lanes/hetero_executor_payload.py",
        "si_lanes/breadth_select_payload.py", "memory/distilled_search_payload.py",
        "memory/distilled_embed_payload.py",
    ),
})


def payload_source_paths() -> frozenset[str]:
    return frozenset(path for paths in PAYLOAD_SOURCES.values() for path in paths)
