"""MetricLog — in-memory metric/event ring buffer.

This is the R27 minimum observability primitive. The v7.1 stack has
multi-thousand-LoC dashboards / learning pipelines / semantic maps that
require live runtime sessions; those are ported later. For agi_v8_1 R27 the
core requirement is that any subsystem can ``emit`` a structured metric and
tests can inspect what was emitted.

R27 invariants:
- in-memory only (no network, no file writes by default)
- bounded buffer (default 1024 entries — oldest dropped)
- env gate AGI_V8_OBSERVABILITY_ENABLED default OFF (emit becomes no-op)
"""
from __future__ import annotations

import os
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterable

from agi_v8_1.utils.time_ids import utc_now, monotonic_ns

__all__ = ["MetricEvent", "MetricLog", "get_default_metric_log"]

_DEFAULT_CAPACITY = 1024
_ENV_GATE = "AGI_V8_OBSERVABILITY_ENABLED"


def _enabled() -> bool:
    return os.getenv(_ENV_GATE, "false").lower() == "true"  # tier: T2


@dataclass(frozen=True)
class MetricEvent:
    """A single observability event."""

    name: str
    value: float | int | str | bool | None
    ts_iso: str
    ts_ns: int
    tags: dict[str, str] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "ts_iso": self.ts_iso,
            "ts_ns": self.ts_ns,
            "tags": dict(self.tags),
            "extra": dict(self.extra),
        }


class MetricLog:
    """Bounded, thread-safe metric ring buffer.

    Drop policy: oldest first when ``capacity`` exceeded.
    """

    def __init__(self, capacity: int = _DEFAULT_CAPACITY) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be > 0")
        self._capacity = capacity
        self._events: deque[MetricEvent] = deque(maxlen=capacity)
        self._lock = threading.RLock()
        self._dropped = 0
        self._emitted = 0

    @property
    def capacity(self) -> int:
        return self._capacity

    def emit(
        self,
        name: str,
        value: Any = None,
        *,
        tags: dict[str, str] | None = None,
        extra: dict[str, Any] | None = None,
        force: bool = False,
    ) -> MetricEvent | None:
        """Record an event. No-op (returns None) unless env gate ON or force=True.

        ``force=True`` is intended for internal subsystems (e.g. ratchet
        violations) that must observe regardless of the global gate.
        """
        if not force and not _enabled():
            return None
        event = MetricEvent(
            name=str(name),
            value=value,
            ts_iso=utc_now(),
            ts_ns=monotonic_ns(),
            tags=dict(tags or {}),
            extra=dict(extra or {}),
        )
        with self._lock:
            if len(self._events) == self._capacity:
                self._dropped += 1
            self._events.append(event)
            self._emitted += 1
        return event

    def snapshot(self) -> list[MetricEvent]:
        """Return a copy of current events (oldest → newest)."""
        with self._lock:
            return list(self._events)

    def filter(self, *, name: str | None = None, tag: tuple[str, str] | None = None) -> list[MetricEvent]:
        """Return events matching ``name`` and/or a (tag_key, tag_val) pair."""
        out: list[MetricEvent] = []
        with self._lock:
            for ev in self._events:
                if name is not None and ev.name != name:
                    continue
                if tag is not None:
                    k, v = tag
                    if ev.tags.get(k) != v:
                        continue
                out.append(ev)
        return out

    def reset(self) -> None:
        with self._lock:
            self._events.clear()
            self._dropped = 0
            self._emitted = 0

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "emitted": self._emitted,
                "dropped": self._dropped,
                "current": len(self._events),
                "capacity": self._capacity,
            }

    def __len__(self) -> int:
        with self._lock:
            return len(self._events)

    def __iter__(self) -> Iterable[MetricEvent]:
        return iter(self.snapshot())


_DEFAULT_LOG = MetricLog()


def get_default_metric_log() -> MetricLog:
    """Return the process-wide default metric log."""
    return _DEFAULT_LOG
