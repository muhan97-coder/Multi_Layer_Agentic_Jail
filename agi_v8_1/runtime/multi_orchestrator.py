"""V8 runtime — multi-slot orchestrator workspace isolation.

Provides 5 isolated slot directories for parallel orchestrator dispatch
(R-Demo-2: 5 orchestrators × 10 viewports each = 50 viewports total).

Each slot is file-lock isolated via ``fcntl.flock`` so concurrent processes
cannot corrupt each other's ``manifest.jsonl`` or ``cost_meter.json``.

Key constraints:
  - No subprocess, no network, no provider calls.
  - Slot directories live under ``agi_v8_1/runtime/orchestrator_slot_{0..4}/``.
  - Each slot has: ``.lock``, ``manifest.jsonl``, ``cost_meter.json``.
  - ``slot_lock`` is a context manager (enter=acquire, exit=release).
"""

# __R_DEMO_ENV_FIX_MULTI_ORCHESTRATOR_SLOT__

from __future__ import annotations

import fcntl
import json
import os
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Generator

# __SLOT_FAIL_FAST_2026_07_25__ Swallowed failures route through one choke
# point: counted + named always, re-raised under AGI_V8_STRICT_FAIL_FAST.
from agi_v8_1.policy.fail_fast import swallowed as _swallowed


# Default base directory for slot workspaces
_DEFAULT_SLOT_BASE = Path(__file__).resolve().parent

# Number of orchestrator slots
NUM_SLOTS: int = 5


@dataclass(frozen=True, slots=True)
class OrchestratorSlot:
    """Descriptor for one isolated orchestrator workspace.

    Fields:
      slot_index: integer 0..4 identifying the slot.
      workspace_dir: resolved Path to the slot directory.
      cycle_id_prefix: string prefix for cycle ids (e.g. 's0_').
      dispatch_id_prefix: string prefix for dispatch ids (e.g. 's0_disp_').
    """

    slot_index: int
    workspace_dir: Path
    cycle_id_prefix: str
    dispatch_id_prefix: str

    @property
    def lock_path(self) -> Path:
        """Path to the exclusive lock file for this slot."""
        return self.workspace_dir / ".lock"

    @property
    def manifest_path(self) -> Path:
        """Path to the append-only manifest JSONL for this slot."""
        return self.workspace_dir / "manifest.jsonl"

    @property
    def cost_meter_path(self) -> Path:
        """Path to the cost meter JSON for this slot."""
        return self.workspace_dir / "cost_meter.json"


def allocate_slot(
    slot_index: int,
    *,
    base_dir: Path | None = None,
) -> OrchestratorSlot:
    """Create and return an OrchestratorSlot.

    Creates ``agi_v8_1/runtime/orchestrator_slot_{slot_index}/`` if absent.
    Initialises ``.lock``, ``manifest.jsonl``, ``cost_meter.json`` if missing.

    Args:
        slot_index: integer 0..4.
        base_dir: override for the parent of slot directories. Defaults to
            the directory containing this module (``agi_v8_1/runtime/``).

    Returns:
        A frozen :class:`OrchestratorSlot` descriptor.

    Raises:
        ValueError: if slot_index is outside [0, NUM_SLOTS).
    """
    if not (0 <= slot_index < NUM_SLOTS):
        raise ValueError(
            f"slot_index {slot_index} out of range [0, {NUM_SLOTS})"
        )

    base = (base_dir or _DEFAULT_SLOT_BASE).resolve()
    slot_dir = base / f"orchestrator_slot_{slot_index}"
    slot_dir.mkdir(parents=True, exist_ok=True)

    # Initialise .lock file (empty, used only for fcntl)
    lock_path = slot_dir / ".lock"
    if not lock_path.exists():
        lock_path.touch()

    # Initialise manifest.jsonl (empty)
    manifest_path = slot_dir / "manifest.jsonl"
    if not manifest_path.exists():
        manifest_path.touch()

    # Initialise cost_meter.json
    cost_meter_path = slot_dir / "cost_meter.json"
    if not cost_meter_path.exists():
        _write_cost_meter(cost_meter_path, {
            "slot_index": slot_index,
            "total_cost_usd": 0.0,
            "call_count": 0,
            "tokens_in_total": 0,
            "tokens_out_total": 0,
            "created_at_unix": time.time(),
            "updated_at_unix": time.time(),
        })

    return OrchestratorSlot(
        slot_index=slot_index,
        workspace_dir=slot_dir,
        cycle_id_prefix=f"s{slot_index}_",
        dispatch_id_prefix=f"s{slot_index}_disp_",
    )


@contextmanager
def slot_lock(slot: OrchestratorSlot) -> Generator[None, None, None]:
    """Exclusive flock-based context manager for a slot.

    Acquires an exclusive ``fcntl.flock`` on the slot's ``.lock`` file.
    Releases on exit. Safe across multiple processes on the same host.

    Usage::

        with slot_lock(my_slot):
            # safe to write manifest / cost_meter

    Raises:
        OSError: if the lock file cannot be opened or the flock fails.
    """
    lock_path = slot.lock_path
    lock_path.touch(exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def append_manifest_entry(
    slot: OrchestratorSlot,
    entry: dict,
    *,
    locked: bool = False,
) -> None:
    """Append a JSON entry to the slot's manifest.jsonl.

    Args:
        slot: the orchestrator slot.
        entry: dict to serialise and append.
        locked: if True, caller already holds the slot lock (avoids re-lock).
    """
    if locked:
        _do_append(slot.manifest_path, entry)
    else:
        with slot_lock(slot):
            _do_append(slot.manifest_path, entry)


def update_cost_meter(
    slot: OrchestratorSlot,
    *,
    cost_usd: float = 0.0,
    tokens_in: int = 0,
    tokens_out: int = 0,
    call_count: int = 1,
) -> None:
    """Update the slot's cost_meter.json atomically under slot lock.

    Args:
        slot: the orchestrator slot.
        cost_usd: incremental USD cost to add.
        tokens_in: incremental input tokens.
        tokens_out: incremental output tokens.
        call_count: number of provider calls in this update.
    """
    with slot_lock(slot):
        path = slot.cost_meter_path
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except Exception as _ff_exc:
            _swallowed(_ff_exc, site="runtime.multi_orchestrator.update_cost_meter:200", category="config")
            existing = {
                "slot_index": slot.slot_index,
                "total_cost_usd": 0.0,
                "call_count": 0,
                "tokens_in_total": 0,
                "tokens_out_total": 0,
                "created_at_unix": time.time(),
            }
        existing["total_cost_usd"] = float(existing.get("total_cost_usd", 0.0)) + cost_usd
        existing["call_count"] = int(existing.get("call_count", 0)) + call_count
        existing["tokens_in_total"] = int(existing.get("tokens_in_total", 0)) + tokens_in
        existing["tokens_out_total"] = int(existing.get("tokens_out_total", 0)) + tokens_out
        existing["updated_at_unix"] = time.time()
        _write_cost_meter(path, existing)


def read_cost_meter(slot: OrchestratorSlot) -> dict:
    """Read the slot's cost_meter.json. Returns empty dict on error."""
    try:
        return json.loads(slot.cost_meter_path.read_text(encoding="utf-8"))
    except Exception as _ff_exc:
        _swallowed(_ff_exc, site="runtime.multi_orchestrator.read_cost_meter:221", category="config")
        return {}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _do_append(path: Path, entry: dict) -> None:
    """Append one JSON line to path."""
    line = json.dumps(entry, sort_keys=True, separators=(",", ":"), default=str)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _write_cost_meter(path: Path, data: dict) -> None:
    """Write cost_meter.json atomically (write to tmp then rename)."""
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(data, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    tmp.replace(path)


__all__ = [
    "NUM_SLOTS",
    "OrchestratorSlot",
    "allocate_slot",
    "append_manifest_entry",
    "read_cost_meter",
    "slot_lock",
    "update_cost_meter",
]
