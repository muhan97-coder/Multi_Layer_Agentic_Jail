"""V8 runtime — multi-slot orchestrator workspace isolation.

Provides 5 isolated slot directories for parallel orchestrator dispatch.
Each slot has exclusive file-lock isolation via ``fcntl.flock``.

Public surface:
  - :class:`OrchestratorSlot` — frozen slot descriptor
  - :func:`allocate_slot` — create/open slot workspace
  - :func:`slot_lock` — exclusive context manager for a slot
  - :func:`append_manifest_entry` — append to slot manifest.jsonl
  - :func:`update_cost_meter` — update slot cost_meter.json
  - :func:`read_cost_meter` — read slot cost_meter.json
  - :data:`NUM_SLOTS` — 5
"""

from .multi_orchestrator import (
    NUM_SLOTS,
    OrchestratorSlot,
    allocate_slot,
    append_manifest_entry,
    read_cost_meter,
    slot_lock,
    update_cost_meter,
)

__all__ = [
    "NUM_SLOTS",
    "OrchestratorSlot",
    "allocate_slot",
    "append_manifest_entry",
    "read_cost_meter",
    "slot_lock",
    "update_cost_meter",
]
