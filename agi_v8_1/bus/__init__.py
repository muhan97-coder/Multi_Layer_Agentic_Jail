"""agi_v8_1.bus — decoupled, append-only event ledgers that bridge the
idea-starved SI loop to the external "you were wrong" scoreboards Doha already
owns (live trader PnL, git history, Seoul open-data, robot telemetry).

# __SLOT_FALSIFIER_BUS_2026_06_16__

The seam is a FILE, not an import: producers (trader-side, git-side, …) append
observation-only events; SI-side consumers read them. agi_v8_1 never imports
the trader package and vice-versa — the two islands only ever touch a JSONL.
"""

from __future__ import annotations

from agi_v8_1.bus.falsifier_bus import (
    BUS_SCHEMA_VERSION,
    FalsifierEvent,
    append_event,
    read_events,
    resolve_bus_path,
    validate_event,
)

__all__ = [
    "BUS_SCHEMA_VERSION",
    "FalsifierEvent",
    "append_event",
    "read_events",
    "resolve_bus_path",
    "validate_event",
]
