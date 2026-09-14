"""agi_v8_1 state — atomic JSON/JSONL primitives (R18 port).

Pure I/O helpers used by R18 apply_chain + R20 cycle_artifacts + R23 memory.
No provider/network/subprocess imports.
"""

# __R30_SLOT__

from .path_guard import require_under, validate_slug  # __SLOT_W2A1__
from .store import (
    atomic_append_jsonl,
    atomic_write_json,
    atomic_write_text,
    read_json,
    read_jsonl,
)

__all__ = [
    "atomic_append_jsonl",
    "atomic_write_json",
    "atomic_write_text",
    "read_json",
    "read_jsonl",
    "require_under",  # __SLOT_W2A1__
    "validate_slug",  # __SLOT_W2A1__
]
