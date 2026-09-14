# R25 W4 (ported subset of v7.1 v7/cost/usage_estimator.py)
"""Token + cost estimation + budget enforcement helper for V8.

Subset of v7.1's v7/cost module: ``estimate_tokens`` + ``estimate_cost`` +
new V8-specific budget enforcement (``CostBudgetExceeded`` exception +
``enforce_budget`` checker).

V8 budget knobs (all default OFF):
  - ``AGI_V8_COST_BUDGET_ENFORCE`` (default "false"): when "true",
    ``enforce_budget`` raises ``CostBudgetExceeded`` instead of returning
    False.
  - ``AGI_V8_COST_BUDGET_USD`` (default "0.0"): hard cap in USD. When 0
    no cap is enforced regardless of the enforce knob.

No subprocess, no network, no provider SDK imports. Pure arithmetic +
env-knob reads.
"""

from __future__ import annotations

import math
import os
import json
import threading
from typing import Any

# __SLOT_FAIL_FAST_2026_07_25__ Swallowed failures route through one choke
# point: counted + named always, re-raised under AGI_V8_STRICT_FAIL_FAST.
from agi_v8_1.policy.fail_fast import swallowed as _swallowed

# Schema markers (mirror v7.1's v7/cost/cost_schema.py for compat)
MODEL_CATALOG_SCHEMA = "model_catalog_v1"
COST_ESTIMATE_SCHEMA = "cost_estimate_v1"
ROUTE_DECISION_SCHEMA = "model_route_decision_v1"
PRE_ORCHESTRATOR_REPORT_SCHEMA = "pre_orchestrator_cost_report_v1"


class CostBudgetExceeded(RuntimeError):
    """Raised by :func:`enforce_budget` when the hard cap is breached."""


class BudgetExceededError(Exception):
    """Raised when a provider call would exceed the configured budget cap.
    Must NOT be caught by executor_v20 fail-safe — see R48."""


def estimate_tokens(
    prompt: str,
    *,
    expected_output_tokens: int = 1200,
    attachment_count: int = 0,
) -> dict[str, Any]:
    """Estimate input + output tokens for a prompt + attachments.

    Input tokens: ``ceil(len(prompt) / 4)`` (rough char→token approximation)
    plus 768 per attachment (image/audio overhead).
    Output tokens: caller-provided ``expected_output_tokens`` (floor 1).
    """
    input_tokens = max(1, math.ceil(len(prompt or "") / 4))
    input_tokens += max(0, int(attachment_count)) * 768
    output_tokens = max(1, int(expected_output_tokens or 1))
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "usage_source": "estimated",
    }


def usage_from_provider(
    reported_usage: dict[str, Any] | None,
    *,
    trusted: bool = False,
    fallback_prompt: str = "",
    expected_output_tokens: int = 1200,
    attachment_count: int = 0,
) -> dict[str, Any]:
    """Prefer provider-reported usage; fall back to estimate when untrusted.

    Untrusted providers (or missing/malformed usage) always re-estimate.
    """
    if trusted and isinstance(reported_usage, dict):
        try:
            inp = int(
                reported_usage.get("input_tokens")
                or reported_usage.get("prompt_tokens")
            )
            out = int(
                reported_usage.get("output_tokens")
                or reported_usage.get("completion_tokens")
            )
        except (TypeError, ValueError) as _ff_exc:
            _swallowed(_ff_exc, site="enforcement.cost_limiter.usage_from_provider:88", category="apply")
            inp = out = 0
        if inp > 0 and out >= 0:
            return {
                "input_tokens": inp,
                "output_tokens": out,
                "total_tokens": inp + out,
                "usage_source": "reported",
            }
    return estimate_tokens(
        fallback_prompt,
        expected_output_tokens=expected_output_tokens,
        attachment_count=attachment_count,
    )


def estimate_cost(
    model: dict[str, Any], usage: dict[str, Any]
) -> dict[str, Any]:
    """Convert (model_pricing, token_usage) → cost record with band label."""
    input_cost = (usage["input_tokens"] / 1000.0) * float(
        model.get("price_per_1k_input", 0.0) or 0.0
    )
    output_cost = (usage["output_tokens"] / 1000.0) * float(
        model.get("price_per_1k_output", 0.0) or 0.0
    )
    total = round(input_cost + output_cost, 8)
    if total == 0:
        band = "free_or_local"
    elif total < 0.01:
        band = "low_estimated"
    elif total < 0.10:
        band = "medium_estimated"
    else:
        band = "high_estimated"
    return {
        "schema_version": COST_ESTIMATE_SCHEMA,
        "model_id": model.get("model_id"),
        "usage_source": usage.get("usage_source", "estimated"),
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "total_tokens": usage["total_tokens"],
        "estimated_cost_usd": total,
        "cost_band": band,
    }


def _env_float(name: str, default: float = 0.0) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as _ff_exc:
        _swallowed(_ff_exc, site="enforcement.cost_limiter._env_float:141", category="apply")
        return default


def _float_from_sources(
    provider: Any,
    attr_names: tuple[str, ...],
    pricing_key: str,
    env_name: str,
) -> float:
    pricing = getattr(provider, "pricing", None)
    if isinstance(pricing, dict):
        try:
            return float(pricing.get(pricing_key) or 0.0)
        except (TypeError, ValueError) as _ff_exc:
            _swallowed(_ff_exc, site="enforcement.cost_limiter._float_from_sources:155", category="apply")
            pass
    for attr_name in attr_names:
        value = getattr(provider, attr_name, None)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError) as _ff_exc:
            _swallowed(_ff_exc, site="enforcement.cost_limiter._float_from_sources:163", category="apply")
            continue
    return _env_float(env_name, 0.0)


def model_pricing_from_provider(provider: Any) -> dict[str, Any]:
    """Build an ``estimate_cost`` model record from provider metadata.

    Prices default to zero so the V8 advisory path remains budget-off unless
    a provider/test explicitly supplies pricing metadata or env prices.
    """
    model_id = (
        getattr(provider, "model_id", None)
        or getattr(provider, "model", None)
        or type(provider).__name__
    )
    return {
        "model_id": str(model_id),
        "price_per_1k_input": _float_from_sources(
            provider,
            ("price_per_1k_input", "input_price_per_1k"),
            "price_per_1k_input",
            "AGI_V8_COST_PRICE_PER_1K_INPUT",
        ),
        "price_per_1k_output": _float_from_sources(
            provider,
            ("price_per_1k_output", "output_price_per_1k"),
            "price_per_1k_output",
            "AGI_V8_COST_PRICE_PER_1K_OUTPUT",
        ),
    }


def expected_output_tokens_from_provider(provider: Any, default: int = 1200) -> int:
    """Return a conservative caller-supplied output estimate for preflight."""
    for attr_name in ("budget_expected_output_tokens", "expected_output_tokens"):
        value = getattr(provider, attr_name, None)
        if value is None:
            continue
        try:
            return max(1, int(value))
        except (TypeError, ValueError) as _ff_exc:
            _swallowed(_ff_exc, site="enforcement.cost_limiter.expected_output_tokens_from_provider:204", category="apply")
            continue
    return max(1, int(default))


def _stable_jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(k): _stable_jsonable(v)
            for k, v in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_stable_jsonable(v) for v in value]
    if isinstance(value, set):
        return sorted((_stable_jsonable(v) for v in value), key=repr)
    return value


def request_text_for_estimate(
    prompt: str,
    payload: dict[str, Any] | Any,
    schema: dict[str, Any] | Any,
) -> str:
    """Serialize request inputs into the same cost-estimate identity space."""
    return json.dumps(
        {
            "prompt": prompt or "",
            "payload": _stable_jsonable(payload),
            "schema": _stable_jsonable(schema),
        },
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )


def estimate_provider_dispatch_cost(
    provider: Any,
    *,
    prompt: str,
    payload: dict[str, Any] | Any,
    schema: dict[str, Any] | Any,
    reported_usage: dict[str, Any] | None = None,
    trusted_usage: bool = True,
    expected_output_tokens: int | None = None,
) -> dict[str, Any]:
    """Estimate or price a provider dispatch using provider metadata."""
    expected = (
        max(1, int(expected_output_tokens))
        if expected_output_tokens is not None
        else expected_output_tokens_from_provider(provider)
    )
    usage = usage_from_provider(
        reported_usage,
        trusted=trusted_usage,
        fallback_prompt=request_text_for_estimate(prompt, payload, schema),
        expected_output_tokens=expected,
    )
    return estimate_cost(model_pricing_from_provider(provider), usage)


class CostBudgetLedger:
    """Thread-safe cumulative budget ledger for provider dispatches."""

    def __init__(
        self,
        *,
        cap_usd: float | None = None,
        raise_on_exceed: bool | None = None,
    ) -> None:
        self._cap_usd = cap_usd
        self._raise_on_exceed = raise_on_exceed
        self._cumulative_cost_usd = 0.0
        self._records: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    @property
    def cumulative_cost_usd(self) -> float:
        with self._lock:
            return self._cumulative_cost_usd

    @property
    def records(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(record) for record in self._records]

    def check_and_reserve(
        self,
        cost_record: dict[str, Any],
        *,
        label: str = "provider_preflight",
        metadata: dict[str, Any] | None = None,
    ) -> float:
        """Reserve an estimated cost before dispatch, raising on cap breach."""
        amount = max(0.0, float(cost_record.get("estimated_cost_usd") or 0.0))
        with self._lock:
            proposed_total = self._cumulative_cost_usd + amount
            allowed = enforce_budget(
                proposed_total,
                cap_usd=self._cap_usd,
                raise_on_exceed=False,
            )
            if not allowed:
                self._records.append(
                    {
                        "label": label,
                        "budget_checked": True,
                        "budget_allowed": False,
                        "blocked": True,
                        "attempted_cost_usd": amount,
                        "cumulative_cost_usd": self._cumulative_cost_usd,
                        "proposed_cumulative_cost_usd": round(proposed_total, 8),
                        "cost": dict(cost_record),
                        "metadata": dict(metadata or {}),
                    }
                )
                raise CostBudgetExceeded(
                    f"cumulative cost ${proposed_total:.4f} exceeds configured cap"
                )
            self._cumulative_cost_usd = round(proposed_total, 8)
            self._records.append(
                {
                    "label": label,
                    "budget_checked": True,
                    "budget_allowed": True,
                    "blocked": False,
                    "reserved_cost_usd": amount,
                    "cumulative_cost_usd": self._cumulative_cost_usd,
                    "proposed_cumulative_cost_usd": round(proposed_total, 8),
                    "cost": dict(cost_record),
                    "metadata": dict(metadata or {}),
                }
            )
            return amount

    def replace_reservation(
        self,
        reserved_cost_usd: float,
        cost_record: dict[str, Any],
        *,
        label: str = "provider_reported_usage",
        metadata: dict[str, Any] | None = None,
    ) -> float:
        """Replace a preflight reservation with reported/fallback usage cost."""
        amount = max(0.0, float(cost_record.get("estimated_cost_usd") or 0.0))
        reserved = max(0.0, float(reserved_cost_usd or 0.0))
        with self._lock:
            base_total = max(0.0, self._cumulative_cost_usd - reserved)
            proposed_total = base_total + amount
            allowed = enforce_budget(
                proposed_total,
                cap_usd=self._cap_usd,
                raise_on_exceed=False,
            )
            if not allowed:
                self._records.append(
                    {
                        "label": label,
                        "budget_checked": True,
                        "budget_allowed": False,
                        "blocked": True,
                        "actual_cost_usd": amount,
                        "released_reserved_cost_usd": reserved,
                        "cumulative_cost_usd": self._cumulative_cost_usd,
                        "proposed_cumulative_cost_usd": round(proposed_total, 8),
                        "cost": dict(cost_record),
                        "metadata": dict(metadata or {}),
                    }
                )
                raise CostBudgetExceeded(
                    f"cumulative cost ${proposed_total:.4f} exceeds configured cap"
                )
            self._cumulative_cost_usd = round(proposed_total, 8)
            self._records.append(
                {
                    "label": label,
                    "budget_checked": True,
                    "budget_allowed": True,
                    "blocked": False,
                    "actual_cost_usd": amount,
                    "released_reserved_cost_usd": reserved,
                    "cumulative_cost_usd": self._cumulative_cost_usd,
                    "proposed_cumulative_cost_usd": round(proposed_total, 8),
                    "cost": dict(cost_record),
                    "metadata": dict(metadata or {}),
                }
            )
            return amount


def _budget_enforce_enabled() -> bool:
    return os.getenv("AGI_V8_COST_BUDGET_ENFORCE", "false").lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _budget_cap_usd() -> float:
    raw = os.getenv("AGI_V8_COST_BUDGET_USD", "0.0").strip()
    try:
        return float(raw)
    except ValueError as _ff_exc:
        _swallowed(_ff_exc, site="enforcement.cost_limiter._budget_cap_usd:407", category="apply")
        return 0.0


def enforce_budget(
    cumulative_cost_usd: float,
    *,
    cap_usd: float | None = None,
    raise_on_exceed: bool | None = None,
) -> bool:
    """Check cumulative cost against the configured cap.

    Returns True iff *cumulative_cost_usd* is within budget. When the cap
    is breached:
      - if ``raise_on_exceed=True`` (or env enforce flag is set) raises
        ``CostBudgetExceeded``;
      - otherwise returns False.

    A cap of 0 (default) disables enforcement entirely — function always
    returns True.
    """
    cap = cap_usd if cap_usd is not None else _budget_cap_usd()
    if cap <= 0:
        return True
    if cumulative_cost_usd <= cap:
        return True
    enforce = (
        raise_on_exceed
        if raise_on_exceed is not None
        else _budget_enforce_enabled()
    )
    if enforce:
        raise CostBudgetExceeded(
            f"cumulative cost ${cumulative_cost_usd:.4f} exceeds cap ${cap:.4f}"
        )
    return False


__all__ = [
    "MODEL_CATALOG_SCHEMA",
    "COST_ESTIMATE_SCHEMA",
    "ROUTE_DECISION_SCHEMA",
    "PRE_ORCHESTRATOR_REPORT_SCHEMA",
    "CostBudgetExceeded",
    "BudgetExceededError",
    "estimate_tokens",
    "usage_from_provider",
    "estimate_cost",
    "model_pricing_from_provider",
    "expected_output_tokens_from_provider",
    "request_text_for_estimate",
    "estimate_provider_dispatch_cost",
    "CostBudgetLedger",
    "enforce_budget",
]
