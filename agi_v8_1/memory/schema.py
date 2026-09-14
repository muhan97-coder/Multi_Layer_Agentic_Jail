"""memory_event_v1 schema, masking, deterministic ids — v8 port.

Ported from agi_v7.1/agent_system/v7/memory_schema.py and the relevant mask
functions from causal_trace.py. Self-contained (no agent_system imports).
Pure functions; safe to import without side effects.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any

from agi_v8_1.policy.secret_masker import (
    mask_obj as _canonical_mask_obj,
    mask_text as _canonical_mask_text,
)

# __SLOT_FAIL_FAST_2026_07_25__ Swallowed failures route through one choke
# point: counted + named always, re-raised under AGI_V8_STRICT_FAIL_FAST.
from agi_v8_1.policy.fail_fast import swallowed as _swallowed

SCHEMA_VERSION = "memory_event_v1"

REQUIRED_FIELDS: tuple[str, ...] = (
    "memory_id",
    "schema_version",
    "created_at",
    "kind",
    "scope",
    "level",
    "parent_id",
    "summary",
    "text",
    "salience",
    "retention",
    "source",
    "pointers",
    "tags",
    "dedup_key",
    "supersedes",
    "status",
    "metadata",
)

RETENTION_CLASSES = {"ephemeral", "short", "medium", "long"}
LEVELS = {"event", "summary", "rule", "preference", "procedure", "episode"}
MEMORY_STATUSES = {"active", "superseded", "deprecated", "archived", "draft"}
SOURCE_STATUSES = {"available", "missing", "stale", "imported", "unknown"}
_WS_RE = re.compile(r"\s+")


def mask_secrets(text: Any) -> Any:
    """Mask API keys / passwords / private keys in *text*."""
    return _canonical_mask_text(text)


def mask_obj(obj: Any) -> Any:
    """Recursively mask secrets in every string leaf."""
    return _canonical_mask_obj(obj)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_text(text: Any) -> str:
    return _WS_RE.sub(" ", str(text or "").strip().lower())


def make_memory_id(scope: str, dedup_key: str) -> str:
    h = hashlib.sha256(f"{scope}|{dedup_key}".encode("utf-8")).hexdigest()
    return f"mem_{h[:24]}"


def dedup_key_for(*, scope: str = "global", kind: str = "note", text: str = "", summary: str = "") -> str:
    base = normalize_text(text or summary)
    h = hashlib.sha256(base.encode("utf-8")).hexdigest()[:24]
    return f"{scope}|{kind}|{h}"


def _coerce_retention(retention: Any) -> dict:
    if isinstance(retention, dict):
        cls = str(retention.get("class") or retention.get("retention") or "short")
        out = dict(retention)
    else:
        cls = str(retention or "short")
        out = {"class": cls}
    if cls not in RETENTION_CLASSES:
        cls = "short"
    out["class"] = cls
    return out


def _coerce_salience(salience: Any) -> dict:
    if isinstance(salience, dict):
        out = dict(salience)
        raw = out.get("score", 0.0)
    else:
        out = {}
        raw = salience if salience is not None else 0.0
    try:
        score = float(raw)
    except (TypeError, ValueError) as _ff_exc:
        _swallowed(_ff_exc, site="memory.schema._coerce_salience:101", category="telemetry")
        score = 0.0
    out["score"] = max(0.0, min(1.0, score))
    labels = out.get("labels", [])
    out["labels"] = [str(x) for x in labels] if isinstance(labels, list) else []
    return out


def _coerce_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _coerce_status(status: Any) -> str:
    text = str(status or "active").strip().lower()
    return text if text in MEMORY_STATUSES else "active"


def _coerce_source(source: Any) -> dict:
    if isinstance(source, dict):
        out = dict(source)
    elif source is None:
        out = {}
    else:
        out = {"label": str(source)}
    out.setdefault("kind", str(out.get("source_kind") or "unspecified"))
    out.setdefault("status", "unknown")
    status = str(out.get("status") or "unknown").strip().lower()
    out["status"] = status if status in SOURCE_STATUSES else "unknown"
    return mask_obj(out)


def build_memory_event(
    *,
    kind: str = "note",
    scope: str = "global",
    level: str = "event",
    parent_id: str = "",
    summary: str = "",
    text: str = "",
    salience: Any = None,
    retention: Any = None,
    source: Any = None,
    pointers: list | None = None,
    tags: list | None = None,
    dedup_key: str | None = None,
    supersedes: list | str | None = None,
    status: str = "active",
    metadata: dict | None = None,
    created_at: str | None = None,
    memory_id: str | None = None,
) -> dict:
    """Build a normalized, secret-masked memory_event_v1 record."""
    masked_summary = mask_secrets(str(summary or text or ""))
    masked_text = mask_secrets(str(text or summary or ""))
    key = dedup_key or dedup_key_for(scope=scope, kind=kind, text=masked_text, summary=masked_summary)
    supersede_list = sorted({str(x).strip() for x in _coerce_list(supersedes) if str(x).strip()})
    id_key = key
    if supersede_list and memory_id is None:
        payload = "|".join([key, masked_summary, *supersede_list])
        id_key = f"{key}|supersede|{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:16]}"
    mid = memory_id or make_memory_id(scope, id_key)
    lvl = str(level or "event")
    if lvl not in LEVELS:
        lvl = "event"
    rec = {
        "memory_id": str(mid),
        "schema_version": SCHEMA_VERSION,
        "created_at": created_at or now_iso(),
        "kind": str(kind or "note"),
        "scope": str(scope or "global"),
        "level": lvl,
        "parent_id": str(parent_id or ""),
        "summary": masked_summary,
        "text": masked_text,
        "salience": _coerce_salience(salience),
        "retention": _coerce_retention(retention),
        "source": _coerce_source(source),
        "pointers": mask_obj(_coerce_list(pointers)),
        "tags": [str(t) for t in _coerce_list(tags)],
        "dedup_key": str(key),
        "supersedes": supersede_list,
        "status": _coerce_status(status),
        "metadata": mask_obj(dict(metadata or {})),
    }
    return mask_memory_event(rec)


def mask_memory_event(event: Any) -> Any:
    return mask_obj(event)


def validate_memory_event(event: Any) -> list[str]:
    """Return schema problems. Empty list means valid."""
    problems: list[str] = []
    if not isinstance(event, dict):
        return ["not a dict"]
    for field in REQUIRED_FIELDS:
        if field not in event:
            problems.append(f"missing field: {field}")
    if event.get("schema_version") != SCHEMA_VERSION:
        problems.append(f"schema_version != {SCHEMA_VERSION!r}")
    if not isinstance(event.get("memory_id"), str) or not event.get("memory_id"):
        problems.append("memory_id empty/non-str")
    if not isinstance(event.get("summary"), str):
        problems.append("summary not a string")
    if not isinstance(event.get("text"), str):
        problems.append("text not a string")
    if not isinstance(event.get("tags"), list):
        problems.append("tags not a list")
    if not isinstance(event.get("pointers"), list):
        problems.append("pointers not a list")
    if not isinstance(event.get("supersedes"), list):
        problems.append("supersedes not a list")
    elif any(not isinstance(item, str) or not item for item in event.get("supersedes", [])):
        problems.append("supersedes contains empty/non-str item")
    if event.get("status") not in MEMORY_STATUSES:
        problems.append("status invalid")
    source = event.get("source")
    if not isinstance(source, dict):
        problems.append("source not an object")
    elif str(source.get("status") or "") not in SOURCE_STATUSES:
        problems.append("source.status invalid")
    sal = event.get("salience")
    if not isinstance(sal, dict) or not isinstance(sal.get("score"), (int, float)):
        problems.append("salience.score missing/non-numeric")
    elif isinstance(sal.get("score"), bool) or not 0.0 <= float(sal.get("score")) <= 1.0:
        problems.append("salience.score out of [0,1]")
    ret = event.get("retention")
    if not isinstance(ret, dict) or ret.get("class") not in RETENTION_CLASSES:
        problems.append("retention.class invalid")
    if event.get("level") not in LEVELS:
        problems.append("level invalid")
    return problems


validate = validate_memory_event
mask = mask_memory_event
validate_event = validate_memory_event
mask_event = mask_memory_event
