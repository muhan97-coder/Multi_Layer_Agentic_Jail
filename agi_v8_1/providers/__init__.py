"""agi_v8_1.providers — model provider abstractions (default OFF).

Ported from the earlier agent-system provider package for R20 W2.

CRITICAL — default OFF invariant:
  All provider classes (except MockModelProvider) raise
  ProvidersDisabledError on __init__ unless the env knob
  AGI_V8_PROVIDERS_ENABLED is set to "true".

  This preserves V8's advisory-chain default: orchestrator/SI/agents
  never trigger network calls unless explicitly enabled.

Lazy SDK imports: anthropic / openai / google.genai / requests are imported
inside __init__ AFTER the env check — module-import of agi_v8_1.providers
never touches optional SDKs and is safe even when they are absent.

R11.1 hardening preserved verbatim:
  - circuit_breaker.HALF_OPEN owner-pid tracking + lock release rules
  - failover_provider case-insensitive no_capacity fast-skip + secret masking
  - gemini_provider usage_metadata fabrication REMOVED (R11.c)
  - dead-provider fail-closed (cohere/groq/mistral/ollama/openrouter/perplexity/xai)
"""

from .base import (
    BaseModelProvider,
    DisabledProviderBase,
    ProvidersDisabledError,
    providers_enabled,
    require_providers_enabled,
    ALLOWED_ENV_KEYS,
)
from .mock import MockModelProvider

# Public protocol/mock/safety imports must work with every payload absent.
# Compatibility exports resolve to the SAME legacy objects when requested,
# without making a bare facade or secret-masker import depend on T3/T4 code.
from .plugins import ProviderPayloadUnavailable, load_provider_class


def __getattr__(name):
    from .plugins import load_compat_export

    return load_compat_export(name)


def __dir__():
    return sorted(set(globals()) | set(__all__))

__all__ = [
    # base
    "BaseModelProvider",
    "DisabledProviderBase",
    "ProvidersDisabledError",
    "providers_enabled",
    "require_providers_enabled",
    "ALLOWED_ENV_KEYS",
    # mock (always available — test/dev only)
    "MockModelProvider",
    "ProviderPayloadUnavailable",
    "load_provider_class",
    # breaker / failover / manager / cache
    "CircuitBreaker",
    "CircuitBreakerProvider",
    "CircuitOpenError",
    "CircuitState",
    "FailoverProvider",
    "ProviderManager",
    "PromptCache",
    # retry
    "api_retry",
    "is_rate_limit_or_transient",
    "make_api_retry",
]

# Lazy imports for optional providers — kept out of __all__ to avoid
# import errors when the backing SDK is not installed. Each provider
# performs the env-knob check at construction time.
# Use:  from agi_v8_1.providers.anthropic_provider import AnthropicModelProvider
