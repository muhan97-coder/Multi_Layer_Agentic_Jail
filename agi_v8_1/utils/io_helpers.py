"""I/O helpers — JSON / JSONL / atomic writes.

Ported from agi_v7.1/agent_system/utils.py public API. R27 keeps only the
filesystem-bound helpers (no subprocess, no network).
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable

from agi_v8_1.utils.exceptions import StorageError

__all__ = [
    "ensure_directory",
    "load_json",
    "load_jsonl",
    "save_json",
    "atomic_write_text",
]


def ensure_directory(path: str | Path) -> Path:
    """Create *path* (and parents) if missing, return Path."""
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def load_json(path: str | Path, default: Any = None) -> Any:
    """Load JSON safely; return *default* if file missing or unparseable."""
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def load_jsonl(path: str | Path, max_lines: int | None = None) -> list[Any]:
    """Load JSONL records; tolerate malformed lines.

    Returns at most *max_lines* records (None = no cap). Malformed lines are
    skipped silently — callers handling untrusted logs must check counts.
    """
    p = Path(path)
    if not p.exists():
        return []
    records: list[Any] = []
    try:
        with p.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
                if max_lines is not None and len(records) >= max_lines:
                    break
    except OSError:
        return records
    return records


def save_json(path: str | Path, data: Any, *, indent: int = 2) -> None:
    """Atomic JSON write — write to tmp + os.replace to *path*."""
    p = Path(path)
    ensure_directory(p.parent)
    payload = json.dumps(data, ensure_ascii=False, indent=indent) + "\n"
    atomic_write_text(p, payload)


def atomic_write_text(path: str | Path, text: str) -> None:
    """Write *text* to *path* atomically via os.replace.

    Any OSError encountered while preparing the parent dir, creating the tmp
    file, writing, or renaming is wrapped in ``StorageError``.
    """
    p = Path(path)
    tmp_name: str | None = None
    try:
        ensure_directory(p.parent)
        fd, tmp_name = tempfile.mkstemp(
            prefix=p.name + ".",
            suffix=".tmp",
            dir=str(p.parent),
        )
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp_name, p)
        tmp_name = None  # successful rename; nothing to clean
    except OSError as exc:
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
        raise StorageError(f"atomic_write_text failed for {path}: {exc}") from exc
