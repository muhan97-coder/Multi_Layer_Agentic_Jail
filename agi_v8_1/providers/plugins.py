"""Public provider attachment points; payload code is imported only on demand.

Stage C's first T3 slice, NOT token verification or an unlock service. The
private checkout still resolves its existing implementations unchanged. A
payload-zero public skeleton keeps protocol/mock/safety imports usable.
Descriptors are import coordinates, not a second registry of gate tiers.
"""
from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
import os
from types import MappingProxyType
from typing import Any


@dataclass(frozen=True)
class ProviderPlugin:
    module: str
    symbol: str
    tier: int


class ProviderPayloadUnavailable(ImportError):
    """Named refusal, with no raw import exception or local path in the message."""

    def __init__(self, tier: int, reason: str) -> None:
        self.tier = tier
        self.reason = reason
        self.doc_pointer = f"Plz_ReadMe.md §T{tier}"
        super().__init__(f"T{tier} 미개방: provider payload unavailable ({reason}); "
                         f"read {self.doc_pointer}")


_WORKERS = MappingProxyType({
    "deepseek": ProviderPlugin(".deepseek_provider", "DeepSeekProvider", 3),
    "openai": ProviderPlugin(".openai_provider", "OpenAIModelProvider", 3),
    "openrouter": ProviderPlugin(".openrouter_provider", "OpenRouterProvider", 3),
})
_COMPAT_EXPORTS = MappingProxyType({
    "ProviderManager": ProviderPlugin(".provider_manager", "ProviderManager", 3),
    "PromptCache": ProviderPlugin(".prompt_cache", "PromptCache", 3),
    "api_retry": ProviderPlugin(".retry", "api_retry", 3),
    "is_rate_limit_or_transient": ProviderPlugin(".retry", "is_rate_limit_or_transient", 3),
    "make_api_retry": ProviderPlugin(".retry", "make_api_retry", 3),
    "CircuitBreaker": ProviderPlugin(".circuit_breaker", "CircuitBreaker", 4),
    "CircuitBreakerProvider": ProviderPlugin(".circuit_breaker", "CircuitBreakerProvider", 4),
    "CircuitOpenError": ProviderPlugin(".circuit_breaker", "CircuitOpenError", 4),
    "CircuitState": ProviderPlugin(".circuit_breaker", "CircuitState", 4),
    "FailoverProvider": ProviderPlugin(".failover_provider", "FailoverProvider", 4),
})


def _resolve(descriptor: ProviderPlugin) -> Any:
    if os.environ.get("AGI_V8_PUBLIC_TIER_GATE_ENABLED", "false").strip().lower() == "true":
        from agi_v8_1.policy.tier_gate import require_tier
        require_tier(descriptor.tier)
    try:
        module = import_module(descriptor.module, package=__package__)
    except ModuleNotFoundError as exc:
        expected = f"{__package__}{descriptor.module}"
        if exc.name == expected:
            raise ProviderPayloadUnavailable(descriptor.tier, "payload_missing") from None
        raise ProviderPayloadUnavailable(descriptor.tier, "dependency_missing") from None
    symbol = getattr(module, descriptor.symbol, None)
    if symbol is None or not callable(symbol):
        raise ProviderPayloadUnavailable(descriptor.tier, "invalid_export")
    return symbol


def load_provider_class(provider: str) -> Any:
    """Resolve a known worker factory; never instantiate it or read credentials."""
    if type(provider) is not str or provider not in _WORKERS:
        raise ValueError("unknown provider plugin")
    return _resolve(_WORKERS[provider])


def load_compat_export(name: str) -> Any:
    """Preserve legacy facade symbols without importing their payload eagerly."""
    if name not in _COMPAT_EXPORTS:
        raise AttributeError("unknown provider facade export")
    return _resolve(_COMPAT_EXPORTS[name])
