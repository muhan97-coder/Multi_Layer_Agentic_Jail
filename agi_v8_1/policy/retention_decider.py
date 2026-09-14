# R25 W4 (ported from v7.1 v7/policy/retention_decider.py)
"""Fail-closed retention decisions for V8 privacy/safety policy.

Ported from v7.1's v7/policy/retention_decider.py with R9.1 + R9.S7
invariants preserved:

  - Secrets are never retained raw (``contains_secret=True`` always yields
    ``allow_store=False``, regardless of legal_hold).
  - Direct callers cannot bypass the secret classifier: when
    ``contains_secret=False`` is supplied with a non-None record, the
    record is re-classified via ``secret_masker.contains_secret`` and
    treated as secret-bearing on any exception (fail-closed).
  - ``user_requested_delete`` always wins unless ``legal_hold=True``.
  - ``contains_secret`` precedence > ``legal_hold`` (legal_hold cannot
    re-authorize raw secret storage).
"""

from __future__ import annotations

import re
from typing import Any

# __SLOT_FAIL_FAST_2026_07_25__ Swallowed failures route through one choke
# point: counted + named always, re-raised under AGI_V8_STRICT_FAIL_FAST.
from agi_v8_1.policy.fail_fast import swallowed as _swallowed

DEFAULT_RETENTION_DAYS: dict[str, int] = {
    "audit_record": 365,
    "derived_metric": 365,
    "operational_log": 30,
    "user_content": 90,
    "raw_media": 7,
    "secret": 0,
    "unknown": 30,
}

_EMAIL_RE = re.compile(
    r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE
)
_PHONE_RE = re.compile(
    r"\b(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)\d{3}[-.\s]?\d{4}\b"
)
_MAX_SCAN_CHARS = 64 * 1024
_MAX_SCAN_DEPTH = 8
_MAX_CONTAINER_ITEMS = 1000


def _contains_pii(record: Any) -> bool:
    """Bounded PII scan used for direct retention callers."""
    remaining = _MAX_SCAN_CHARS
    seen: set[int] = set()
    stack: list[tuple[Any, int]] = [(record, 0)]
    while stack:
        node, depth = stack.pop()
        if remaining <= 0 or depth > _MAX_SCAN_DEPTH:
            return True
        if isinstance(node, str):
            chunk = node[:remaining]
            if _EMAIL_RE.search(chunk) or _PHONE_RE.search(chunk):
                return True
            remaining -= len(chunk)
            continue
        if isinstance(node, (dict, list, tuple, set)):
            oid = id(node)
            if oid in seen:
                continue
            seen.add(oid)
        if isinstance(node, dict):
            for i, (k, v) in enumerate(node.items()):
                if i >= _MAX_CONTAINER_ITEMS:
                    return True
                stack.append((k, depth + 1))
                stack.append((v, depth + 1))
        elif isinstance(node, (list, tuple, set)):
            for i, v in enumerate(node):
                if i >= _MAX_CONTAINER_ITEMS:
                    return True
                stack.append((v, depth + 1))
    return False


def decide_retention(
    record: Any = None,
    *,
    data_class: str = "unknown",
    contains_secret: bool = False,
    contains_pii: bool = False,
    contains_raw_media: bool = False,
    user_requested_delete: bool = False,
    legal_hold: bool = False,
) -> dict[str, Any]:
    """Fail-closed retention decision.

    Precedence:
      1. user_requested_delete (no legal_hold) → delete, 0d
      2. contains_secret AND legal_hold → hold_masked, allow_store=False
      3. contains_secret → mask_then_ephemeral, allow_store=False, 0d
      4. legal_hold → hold, allow_store=True, no expiry
      5. user_requested_delete (with legal_hold) → still delete
      6. contains_raw_media → short_term, 7d
      7. default by data_class
    """
    # R9.1: direct callers cannot bypass the privacy classifier. If a
    # non-None record is provided with contains_secret=False, re-check.
    if not contains_secret and record is not None:
        try:
            from .secret_masker import contains_secret as _contains_secret
            contains_secret = bool(_contains_secret(record))
        except Exception as _ff_exc:
            # Fail-closed: classifier crash treats record as secret-bearing.
            _swallowed(_ff_exc, site="policy.retention_decider.decide_retention:105", category="config")
            contains_secret = True
    if not contains_pii and record is not None:
        try:
            contains_pii = _contains_pii(record)
        except Exception as _ff_exc:
            _swallowed(_ff_exc, site="policy.retention_decider.decide_retention:111", category="config")
            contains_pii = True

    if user_requested_delete and not legal_hold:
        return {
            "action": "delete",
            "allow_store": False,
            "retention_days": 0,
            "reason": "user_requested_delete",
        }
    # R9.S7: secret precedes legal_hold so a legal_hold flag can never
    # re-authorize raw-secret storage. The combined case stores a masked
    # record only (raw payload allow_store=False).
    if contains_secret and legal_hold:
        return {
            "action": "hold_masked",
            "allow_store": False,
            "retention_days": None,
            "reason": "secret_under_legal_hold",
        }
    if contains_secret:
        return {
            "action": "mask_then_ephemeral",
            "allow_store": False,
            "retention_days": 0,
            "reason": "raw secrets are not retained",
        }
    if legal_hold:
        return {
            "action": "hold",
            "allow_store": True,
            "retention_days": None,
            "reason": "legal_hold",
        }
    if user_requested_delete:
        return {
            "action": "delete",
            "allow_store": False,
            "retention_days": 0,
            "reason": "user_requested_delete",
        }
    if contains_raw_media:
        return {
            "action": "short_term",
            "allow_store": True,
            "retention_days": DEFAULT_RETENTION_DAYS["raw_media"],
            "reason": "raw media short retention",
        }
    if contains_pii:
        klass = str(data_class or "unknown")
        default_days = DEFAULT_RETENTION_DAYS.get(
            klass, DEFAULT_RETENTION_DAYS["unknown"]
        )
        return {
            "action": "mask_then_retain",
            "allow_store": True,
            "retention_days": min(default_days, DEFAULT_RETENTION_DAYS["unknown"]),
            "reason": "pii masked retention policy",
        }
    klass = str(data_class or "unknown")
    return {
        "action": "retain",
        "allow_store": True,
        "retention_days": DEFAULT_RETENTION_DAYS.get(
            klass, DEFAULT_RETENTION_DAYS["unknown"]
        ),
        "reason": f"{klass} retention policy",
    }


__all__ = ["decide_retention", "DEFAULT_RETENTION_DAYS"]
