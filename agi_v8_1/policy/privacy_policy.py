# R25 W4 (ported from v7.1 v7/policy/privacy_policy.py)
"""Minimal V8 privacy policy composition.

Composes ``secret_masker`` + ``raw_media_policy`` + ``retention_decider``
into a single ``apply_privacy_policy`` decision.

R9.S7 + R9.1 invariants preserved:
  - Bounded flatten (64 KiB total scan budget, depth 8, 1000 items/container)
  - Truncation flag treats payload as potentially-PII (more restrictive)
  - Cycle detection via id() set
  - Fail-open canonical entry never returns raw payload on policy error
    (masked_payload becomes ``"<MASKED:policy_error>"``)
"""

from __future__ import annotations

import re
from typing import Any

from .raw_media_policy import decide_raw_media, is_raw_media
from .retention_decider import decide_retention
from .secret_masker import contains_secret, mask_payload, mask_text

# __SLOT_FAIL_FAST_2026_07_25__ Swallowed failures route through one choke
# point: counted + named always, re-raised under AGI_V8_STRICT_FAIL_FAST.
from agi_v8_1.policy.fail_fast import swallowed as _swallowed

_EMAIL_RE = re.compile(
    r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE
)
_PHONE_RE = re.compile(
    r"\b(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)\d{3}[-.\s]?\d{4}\b"
)
_EMAIL_MASK = "***EMAIL_MASKED***"
_PHONE_MASK = "***PHONE_MASKED***"

# R9.S7 scan limits
_MAX_FLATTEN_CHARS = 64 * 1024
_MAX_FLATTEN_DEPTH = 8
_MAX_CONTAINER_ITEMS = 1000


def _flatten_strings(
    obj: Any, budget: int = _MAX_FLATTEN_CHARS
) -> tuple[list[str], bool]:
    """Bounded iterative flatten. Returns (strings, truncated).

    When ``truncated`` is True the classifier treats the payload as
    MORE-restrictive (potentially-PII). Iterative (stack) form avoids
    recursion-limit issues on deep nesting.
    """
    out: list[str] = []
    remaining = budget
    truncated = False
    seen: set[int] = set()
    stack: list[tuple[Any, int]] = [(obj, 0)]
    while stack:
        node, depth = stack.pop()
        if remaining <= 0:
            truncated = True
            break
        if depth > _MAX_FLATTEN_DEPTH:
            truncated = True
            continue
        if isinstance(node, str):
            if len(node) > remaining:
                out.append(node[:remaining])
                remaining = 0
                truncated = True
            else:
                out.append(node)
                remaining -= len(node)
            continue
        if isinstance(node, (dict, list, tuple, set)):
            nid = id(node)
            if nid in seen:
                continue
            seen.add(nid)
        if isinstance(node, dict):
            count = 0
            for k, v in node.items():
                if count >= _MAX_CONTAINER_ITEMS:
                    truncated = True
                    break
                stack.append((k, depth + 1))
                stack.append((v, depth + 1))
                count += 1
        elif isinstance(node, (list, tuple, set)):
            count = 0
            for v in node:
                if count >= _MAX_CONTAINER_ITEMS:
                    truncated = True
                    break
                stack.append((v, depth + 1))
                count += 1
        # scalars (None/bool/int/float/bytes) ignored
    return out, truncated


def _mask_pii_text(text: Any) -> Any:
    if not isinstance(text, str) or not text:
        return text
    return _PHONE_RE.sub(_PHONE_MASK, _EMAIL_RE.sub(_EMAIL_MASK, text))


def _mask_pii_payload(
    payload: Any, _depth: int = 0, _seen: set[int] | None = None
) -> Any:
    """Recursively mask PII in strings with the same bounded shape policy."""
    if _seen is None:
        _seen = set()
    if _depth > _MAX_FLATTEN_DEPTH:
        return "<MASKED:max_depth>"
    if isinstance(payload, str):
        return _mask_pii_text(payload)
    if isinstance(payload, (dict, list, tuple, set)):
        oid = id(payload)
        if oid in _seen:
            return "<MASKED:cycle>"
        _seen = _seen | {oid}
    if isinstance(payload, dict):
        out: dict[Any, Any] = {}
        for i, (k, v) in enumerate(payload.items()):
            if i >= _MAX_CONTAINER_ITEMS:
                out["<MASKED:truncated>"] = True
                break
            masked_key = _mask_pii_payload(k, _depth + 1, _seen)
            try:
                out[masked_key] = _mask_pii_payload(v, _depth + 1, _seen)
            except TypeError as _ff_exc:
                _swallowed(_ff_exc, site="policy.privacy_policy._mask_pii_payload:126", category="config")
                out[str(masked_key)] = _mask_pii_payload(v, _depth + 1, _seen)
        return out
    if isinstance(payload, list):
        return [
            _mask_pii_payload(v, _depth + 1, _seen)
            for v in payload[:_MAX_CONTAINER_ITEMS]
        ]
    if isinstance(payload, tuple):
        return tuple(
            _mask_pii_payload(v, _depth + 1, _seen)
            for v in payload[:_MAX_CONTAINER_ITEMS]
        )
    if isinstance(payload, set):
        return {
            _mask_pii_payload(v, _depth + 1, _seen)
            for v in list(payload)[:_MAX_CONTAINER_ITEMS]
        }
    return payload


def classify_payload(payload: Any) -> dict[str, Any]:
    """Classify *payload* into {secret, pii, raw_media, standard}.

    Truncation during flatten promotes the classification to ``pii`` since
    we could not inspect the full payload (fail-closed direction).
    """
    strings, truncated = _flatten_strings(payload)
    blob = "\n".join(strings)
    classes: list[str] = []
    if contains_secret(payload):
        classes.append("secret")
    pii_hit = bool(_EMAIL_RE.search(blob) or _PHONE_RE.search(blob))
    if pii_hit or truncated:
        classes.append("pii")
    if is_raw_media(payload):
        classes.append("raw_media")
    if not classes:
        classes.append("standard")
    return {
        "classes": classes,
        "contains_secret": "secret" in classes,
        "contains_pii": "pii" in classes,
        "contains_raw_media": "raw_media" in classes,
        "truncated": truncated,
    }


def apply_privacy_policy(
    payload: Any,
    *,
    data_class: str = "unknown",
    consent: bool = False,
    purpose: str = "",
    user_requested_delete: bool = False,
    legal_hold: bool = False,
) -> dict[str, Any]:
    """Compose classify + mask + raw-media + retention.

    Fail-open canonical entry for downstream callers. If any of classify /
    mask raises we return ``allowed=False`` with ``masked_payload``
    overwritten to ``"<MASKED:policy_error>"`` (NEVER the raw payload).
    """
    try:
        classification = classify_payload(payload)
        masked = mask_payload(payload)
        if classification["contains_pii"]:
            masked = _mask_pii_payload(masked)
        raw_media = decide_raw_media(payload, consent=consent, purpose=purpose)
        retention = decide_retention(
            payload,
            data_class=data_class,
            contains_secret=classification["contains_secret"],
            contains_pii=classification["contains_pii"],
            contains_raw_media=classification["contains_raw_media"],
            user_requested_delete=user_requested_delete,
            legal_hold=legal_hold,
        )
        allowed = retention["allow_store"] and raw_media["allow_store"]
        return {
            "allowed": allowed,
            "masked_payload": masked,
            "classification": classification,
            "retention": retention,
            "raw_media": raw_media,
        }
    except Exception as exc:  # noqa: BLE001
        _swallowed(exc, site="policy.privacy_policy.apply_privacy_policy:212", category="config")
        try:
            error_text = str(_mask_pii_text(mask_text(str(exc))))
        except Exception as _ff_exc:
            _swallowed(_ff_exc, site="policy.privacy_policy.apply_privacy_policy:215", category="config")
            error_text = "<MASKED:policy_error>"
        return {
            "allowed": False,
            # R9.1: never return raw payload on policy/mask failure.
            "masked_payload": "<MASKED:policy_error>",
            "classification": {
                "error": error_text,
                "error_type": type(exc).__name__,
            },
            "retention": {"allow_store": False, "reason": "policy_error"},
            "raw_media": {"allow_store": False, "reason": "policy_error"},
        }


def evaluate_privacy_policy(payload: Any, **kwargs: Any) -> dict[str, Any]:
    """Alias for :func:`apply_privacy_policy` (v7.1 API compat)."""
    return apply_privacy_policy(payload, **kwargs)


__all__ = [
    "classify_payload",
    "apply_privacy_policy",
    "evaluate_privacy_policy",
]
