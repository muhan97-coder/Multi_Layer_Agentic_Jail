"""FailureLogger — lightweight failure tracker.

Ported from agi_v7.1/agent_system/utils/failure_logger.py with the v7.1
dependency on `agent_system.utils` rewired to local agi_v8_1.utils helpers.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agi_v8_1.utils.time_ids import utc_now
from agi_v8_1.utils.io_helpers import ensure_directory

__all__ = ["FailureLogger"]


class FailureLogger:
    """Lightweight failure tracker that persists counts and timestamps."""

    def __init__(self, state_dir: str | Path) -> None:
        self._log_path = ensure_directory(state_dir) / "failure_log.json"
        self._entries: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if self._log_path.exists():
            try:
                self._entries = json.loads(self._log_path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001 - corrupt logs reset to empty
                self._entries = {}

    def _save(self) -> None:
        self._log_path.write_text(
            json.dumps(self._entries, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def record(self, signature: str, task_id: str = "", run_id: str = "") -> None:
        """Record a failure occurrence."""
        ts = utc_now()
        entry = self._entries.setdefault(
            signature, {"count": 0, "last_seen": ts, "tasks": []}
        )
        entry["count"] += 1
        entry["last_seen"] = ts
        if task_id:
            entry["tasks"].append(task_id)
            if len(entry["tasks"]) > 20:
                entry["tasks"] = entry["tasks"][-20:]
        self._save()

    def repeated_signatures(self, threshold: int = 3) -> list[str]:
        """Return signatures that have occurred at least *threshold* times."""
        return [
            sig
            for sig, info in self._entries.items()
            if info.get("count", 0) >= threshold
        ]

    def reset(self) -> None:
        """Clear in-memory entries and persisted file (test/SI use)."""
        self._entries = {}
        if self._log_path.exists():
            try:
                self._log_path.unlink()
            except OSError:
                pass
