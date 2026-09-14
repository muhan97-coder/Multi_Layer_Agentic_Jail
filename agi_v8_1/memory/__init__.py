"""agi_v8_1 memory layer.

R23 W2: ported from v7.1 (memory_panel/retriever/schema/lineage_retriever/
bulk_reembed/dreaming_bridge). Self-contained — no agent_system imports.
All env knobs default OFF (see AGI_V8_MEMORY_ENABLED in .env.example).

Wires to agi_v8_1.state.store atomic IO via import (store.py untouched).
"""

from __future__ import annotations

from agi_v8_1.memory.schema import (
    SCHEMA_VERSION,
    REQUIRED_FIELDS,
    RETENTION_CLASSES,
    LEVELS,
    build_memory_event,
    validate_memory_event,
    mask_memory_event,
    make_memory_id,
    dedup_key_for,
    now_iso,
    normalize_text,
    mask_secrets,
    mask_obj,
)
# Runtime helpers are lazy: importing public memory/vector contracts must not
# load retrievers, TMI model dependencies, or optional implementation code.
from importlib import import_module as _import_module

_LAZY_EXPORTS = {
    'MemoryRetriever': ('agi_v8_1.memory.retriever', 'MemoryRetriever'),
    'pointer_for': ('agi_v8_1.memory.retriever', 'pointer_for'),
    'pointer_record': ('agi_v8_1.memory.retriever', 'pointer_record'),
    'retrieve_pointers': ('agi_v8_1.memory.retriever', 'retrieve_pointers'),
    'token_jaccard': ('agi_v8_1.memory.retriever', 'token_jaccard'),
    'render_memory_index': ('agi_v8_1.memory.panel', 'render_memory_index'),
    'render_index_hook': ('agi_v8_1.memory.panel', 'render_index_hook'),
    'lineage_retrieve': ('agi_v8_1.memory.lineage_retriever', 'retrieve'),
    'as_context_block': ('agi_v8_1.memory.lineage_retriever', 'as_context_block'),
    'summarize_lineage_learning': ('agi_v8_1.memory.lineage_retriever', 'summarize_lineage_learning'),
    'lineage_learning_dir': ('agi_v8_1.memory.lineage_retriever', 'lineage_learning_dir'),
    'bulk_reembed_run': ('agi_v8_1.memory.bulk_reembed', 'run'),
    'FEATURE': ('agi_v8_1.memory.bulk_reembed', 'FEATURE'),
    'FALLBACK': ('agi_v8_1.memory.bulk_reembed', 'FALLBACK'),
    'build_dreaming_inputs': ('agi_v8_1.memory.dreaming_bridge', 'build_dreaming_inputs'),
    'MemoryStore': ('agi_v8_1.memory.store_bridge', 'MemoryStore'),
    'memory_enabled': ('agi_v8_1.memory.store_bridge', 'memory_enabled'),
    'append_events': ('agi_v8_1.memory.store_bridge', 'append_events'),
    'iter_jsonl': ('agi_v8_1.memory.store_bridge', 'iter_jsonl'),
    'tmi_chain': ('agi_v8_1.memory.tmi_chain', None),
    'tmi_types': ('agi_v8_1.memory.tmi_types', None),
}


def __getattr__(name):
    entry = _LAZY_EXPORTS.get(name)
    if entry is None:
        raise AttributeError("unknown memory export")
    module, symbol = entry
    loaded = _import_module(module)
    return loaded if symbol is None else getattr(loaded, symbol)

__all__ = [
    "SCHEMA_VERSION",
    "REQUIRED_FIELDS",
    "RETENTION_CLASSES",
    "LEVELS",
    "build_memory_event",
    "validate_memory_event",
    "mask_memory_event",
    "make_memory_id",
    "dedup_key_for",
    "now_iso",
    "normalize_text",
    "mask_secrets",
    "mask_obj",
    "MemoryRetriever",
    "pointer_for",
    "pointer_record",
    "retrieve_pointers",
    "token_jaccard",
    "render_memory_index",
    "render_index_hook",
    "lineage_retrieve",
    "as_context_block",
    "summarize_lineage_learning",
    "lineage_learning_dir",
    "bulk_reembed_run",
    "FEATURE",
    "FALLBACK",
    "build_dreaming_inputs",
    "MemoryStore",
    "memory_enabled",
    "append_events",
    "iter_jsonl",
    "tmi_chain",
    "tmi_types",
]
