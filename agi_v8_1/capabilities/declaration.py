"""Capability declaration + registry — ports the meta.json schema from v7.1.

Each capability lives at ``capabilities/<name>/`` with:
  - ``meta.json`` describing the capability (kind, code_path, signature, ...)
  - ``<name>.py`` containing the executable code (NOT auto-imported by registry)

R27 only catalogues capabilities. Execution must be requested via the
approved-runtime path — the registry never imports/runs capability modules.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any, Iterator

from agi_v8_1.utils.io_helpers import load_json
from agi_v8_1.utils.hashing import content_hash

__all__ = [
    "CapabilityKind",
    "CapabilityMeta",
    "CapabilityRegistry",
    "load_meta",
    "iter_capability_dirs",
]


class CapabilityKind(str, Enum):
    """Enumerates capability kinds recognised by the executor.

    Mirrors the ``kind`` field of v7.1 ``meta.json`` (only ``executor_skill``
    is currently used; other kinds reserved for forward compatibility).
    """

    EXECUTOR_SKILL = "executor_skill"
    PROVIDER_TOOL = "provider_tool"
    SHELL_TOOL = "shell_tool"
    UNKNOWN = "unknown"

    @classmethod
    def from_string(cls, value: str | None) -> "CapabilityKind":
        if not value:
            return cls.UNKNOWN
        for member in cls:
            if member.value == value:
                return member
        return cls.UNKNOWN


@dataclass(frozen=True)
class CapabilityMeta:
    """Frozen view of a capability's ``meta.json`` declaration."""

    name: str
    kind: CapabilityKind
    code_path: str
    signature: str
    created_by: str = ""
    origin_cycle: int = 0
    reuse_count: int = 0
    created_at: str = ""
    extras: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "CapabilityMeta":
        known = {"name", "kind", "code_path", "signature",
                 "created_by", "origin_cycle", "reuse_count", "created_at"}
        extras = {k: v for k, v in raw.items() if k not in known}
        return cls(
            name=str(raw.get("name", "")),
            kind=CapabilityKind.from_string(raw.get("kind")),
            code_path=str(raw.get("code_path", "")),
            signature=str(raw.get("signature", "")),
            created_by=str(raw.get("created_by", "")),
            origin_cycle=int(raw.get("origin_cycle", 0) or 0),
            reuse_count=int(raw.get("reuse_count", 0) or 0),
            created_at=str(raw.get("created_at", "")),
            extras=extras,
        )

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["kind"] = self.kind.value
        # flatten extras into top-level (matches v7.1 layout)
        extras = out.pop("extras", {}) or {}
        out.update(extras)
        return out

    def verify_signature(self, source_text: str) -> bool:
        """Recompute SHA-256 of *source_text* and compare with declared sig."""
        if not self.signature:
            return False
        return content_hash(source_text) == self.signature


def load_meta(meta_path: str | Path) -> CapabilityMeta | None:
    """Load a single meta.json and return CapabilityMeta or None on failure."""
    raw = load_json(meta_path, default=None)
    if not isinstance(raw, dict):
        return None
    # If 'name' missing, derive from parent directory
    if not raw.get("name"):
        raw["name"] = Path(meta_path).parent.name
    return CapabilityMeta.from_dict(raw)


def iter_capability_dirs(root: str | Path) -> Iterator[Path]:
    """Yield candidate capability directories beneath *root*.

    A capability dir is any direct child of *root* that contains ``meta.json``.
    Hidden / underscore-prefixed dirs are skipped.
    """
    root_p = Path(root)
    if not root_p.is_dir():
        return
    for child in sorted(root_p.iterdir()):
        if not child.is_dir():
            continue
        if child.name.startswith((".", "_")):
            continue
        if (child / "meta.json").is_file():
            yield child


class CapabilityRegistry:
    """In-memory catalogue of capabilities.

    The registry never auto-imports capability code. It only loads ``meta.json``
    descriptors, validates them, and serves them to higher layers (which then
    decide whether to dispatch via the approved runtime).
    """

    def __init__(self) -> None:
        self._items: dict[str, CapabilityMeta] = {}

    # ------------------------------------------------------------------ load
    def load_root(self, root: str | Path) -> int:
        """Scan *root* and load every capability dir found. Returns count loaded."""
        count = 0
        for cap_dir in iter_capability_dirs(root):
            meta = load_meta(cap_dir / "meta.json")
            if meta is None:
                continue
            self.register(meta)
            count += 1
        return count

    def register(self, meta: CapabilityMeta) -> None:
        """Register or replace a capability by name."""
        self._items[meta.name] = meta

    def unregister(self, name: str) -> bool:
        """Remove *name* from registry; return True if removed."""
        return self._items.pop(name, None) is not None

    # --------------------------------------------------------------- access
    def get(self, name: str) -> CapabilityMeta | None:
        return self._items.get(name)

    def names(self) -> list[str]:
        return sorted(self._items.keys())

    def by_kind(self, kind: CapabilityKind) -> list[CapabilityMeta]:
        return [m for m in self._items.values() if m.kind == kind]

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._items

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self) -> Iterator[CapabilityMeta]:
        return iter(self._items.values())

    # ------------------------------------------------------------- enabled?
    @staticmethod
    def is_enabled() -> bool:
        """Master env gate — default OFF (R27 invariant)."""
        return os.getenv("AGI_V8_CAPABILITIES_ENABLED", "false").lower() == "true"  # tier: T2
