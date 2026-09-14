"""Abstract interface for structured model providers (agi_v8_1 — R20 W2).

Ported from agi_v7.1/agent_system/providers/base.py with R20 V8 modifications:
  - ProvidersDisabledError + helper providers_enabled() / require_providers_enabled()
  - ALLOWED_ENV_KEYS allowlist (R11.b cost & env containment)
  - DisabledProviderBase retained for dead-provider fail-closed (R11.a)

Module-top imports are stdlib only — SDK imports MUST happen inside
provider __init__/method bodies, after the env-knob check.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import logging
import os
import threading
import time
from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, Mapping

# __SLOT_FAIL_FAST_2026_07_25__ Swallowed failures route through one choke
# point: counted + named always, re-raised under AGI_V8_STRICT_FAIL_FAST.
from agi_v8_1.policy.fail_fast import swallowed as _swallowed

logger = logging.getLogger(__name__)


# __R20_W2_SLOT__ default-OFF env knob name (single source of truth)
_PROVIDERS_ENABLED_ENV = "AGI_V8_PROVIDERS_ENABLED"


# __R20_W2_SLOT__ R11.b env allowlist — providers must not read arbitrary env vars.
# Per-provider keys follow the pattern <PROVIDER>_<SUFFIX>. The suffix portion
# is what's allowed; the prefix is the provider's own name (ANTHROPIC, OPENAI,
# GEMINI, DEEPSEEK, etc.). Validation is by suffix membership.
ALLOWED_ENV_KEYS: frozenset[str] = frozenset(
    {
        "API_KEY",
        "ENDPOINT",
        "TIMEOUT",
        "REGION",
        "MODEL",
        "MAX_TOKENS",
        "TEMPERATURE",
        "ORG_ID",
        "PROJECT_ID",
    }
)


class ProvidersDisabledError(RuntimeError):
    """Raised when a provider class is instantiated while the V8 providers
    knob (``AGI_V8_PROVIDERS_ENABLED``) is unset/false.

    The advisory-chain default for agi_v8_1 keeps providers OFF; orchestrator
    and SI cycle must not touch real LLM endpoints unless explicitly enabled.
    """


class RemoteEndpointPolicyError(ValueError):
    """Raised when a configured provider endpoint is outside policy."""


class ProviderExecutionMode(str, Enum):
    """Explicit provider boundary mode."""

    ADVISORY = "advisory"
    MOCK = "mock"
    LOCAL = "local"
    REMOTE = "remote"


def providers_enabled() -> bool:
    """Return True iff AGI_V8_PROVIDERS_ENABLED is set to 'true' (case-insensitive)."""
    return os.environ.get(_PROVIDERS_ENABLED_ENV, "false").strip().lower() == "true"


def provider_execution_mode(
    provider_label: str,
    *,
    is_mock: bool = False,
    is_local: bool = False,
) -> ProviderExecutionMode:
    """Classify provider boundary mode without instantiating or calling it."""

    label = (provider_label or "").strip().lower()
    if is_mock or "mock" in label:
        return ProviderExecutionMode.MOCK
    if is_local or label in {"local", "local_llm", "filesystem", "in_process"}:
        return ProviderExecutionMode.LOCAL
    if not providers_enabled():
        return ProviderExecutionMode.ADVISORY
    return ProviderExecutionMode.REMOTE


def provider_boundary_metadata(
    provider_label: str,
    *,
    is_mock: bool = False,
    is_local: bool = False,
) -> dict[str, Any]:
    """Return explicit boundary metadata for provider dispatch surfaces."""

    mode = provider_execution_mode(
        provider_label,
        is_mock=is_mock,
        is_local=is_local,
    )
    return {
        "provider": str(provider_label),
        "mode": mode.value,
        "providers_enabled": providers_enabled(),
        "network_allowed": mode is ProviderExecutionMode.REMOTE,
        "remote_execution_succeeded": False,
    }


def _try_parse_ip(text: str) -> "Any | None":
    """``ip_address`` probe as a value: the IP, or None for a hostname.

    EXPECTED negative probe, not a swallow: ``ip_address`` raising ValueError
    is the normal "this is a hostname, not an IP literal" branch. Routing it
    through ``swallowed`` made strict fail-fast abort EVERY provider call to a
    hostname URL (first-run live abort, 2026-07-31) — control-flow probes must
    not be strict-raisable. The ONE handler here serves both host-check sites.
    """
    import ipaddress

    try:
        return ipaddress.ip_address(text)
    except ValueError:
        return None


def _normalize_url_host(host: str) -> str:
    ip = _try_parse_ip(host.strip())
    if ip is not None:
        return str(ip)
    return host.strip().lower().rstrip(".")


# __SLOT_W1A6__ Cloud-metadata SSRF deny list (W3.L6 audit).
# AWS uses 169.254.169.254 (already caught by is_link_local), but Alibaba and
# Oracle use globally-routable IPs that bypass the is_link_local/is_private
# checks. Audit also flagged IPv4-mapped IPv6 bypass risk (older Python or
# IPv4-compatible "::169.254.169.254" forms where is_link_local is False).
_CLOUD_METADATA_IPV4_DENY: frozenset[str] = frozenset(
    {
        "169.254.169.254",  # AWS / Azure / GCP (also is_link_local)
        "100.100.100.200",  # Alibaba Cloud metadata
        "192.0.0.192",      # Oracle Cloud metadata
    }
)


def _host_is_unsafe(host: str) -> bool:
    if not host:
        return True
    lowered = host.strip().lower().rstrip(".")
    if lowered in {"localhost", "localhost.localdomain"}:
        return True
    import ipaddress

    ip = _try_parse_ip(lowered)
    if ip is None:
        # Hostname, not an IP literal (see _try_parse_ip) — the IP deny logic
        # below does not apply; hostname-level checks already ran above.
        return False
    # __SLOT_W1A6__ Normalize IPv4-mapped IPv6 forms (::ffff:X.X.X.X and
    # ::X.X.X.X "IPv4-compatible") down to the wrapped IPv4 so the IPv4 deny
    # path runs on the underlying address. Otherwise SSRF can slip through by
    # encoding the metadata IP as IPv6.
    # IMPORTANT: we evaluate BOTH views (original IPv6 AND unwrapped IPv4) —
    # ::1 has 12 leading zero bytes but is loopback, NOT an IPv4-compat form;
    # unwrapping naively would demote it to 0.0.0.1. So the IPv6 properties
    # must still be honored.
    ipv4_view = None
    if ip.version == 6:
        ipv4_view = getattr(ip, "ipv4_mapped", None)
        if ipv4_view is None:
            # IPv4-compatible IPv6 form ::X.X.X.X — low 32 bits hold IPv4
            # but ipv4_mapped is None per RFC 4291. Reconstruct from bytes,
            # but ONLY use the unwrapped IPv4 as an ADDITIONAL deny signal
            # (original IPv6 still evaluated).
            try:
                packed = ip.packed
                if packed[:12] == b"\x00" * 12 and packed[12:] != b"\x00\x00\x00\x00":
                    ipv4_view = ipaddress.IPv4Address(packed[12:])
            except (ValueError, AttributeError) as _ff_exc:
                _swallowed(_ff_exc, site="providers.base._host_is_unsafe:172", category="provider")
                ipv4_view = None

    def _ip_unsafe(addr: Any) -> bool:
        return bool(
            addr.is_loopback
            or addr.is_link_local
            or addr.is_multicast
            or addr.is_unspecified
            or (addr.version == 4 and str(addr) in _CLOUD_METADATA_IPV4_DENY)
        )

    if _ip_unsafe(ip):
        return True
    if ipv4_view is not None and _ip_unsafe(ipv4_view):
        return True
    return False


# __SLOT_W2A3__ Explicit production-endpoint allowlist (defense-in-depth on top
# of W1.A5 host validation).
#
# Why this layer exists:
#   W1.A5 added per-provider host allowlists, which already block most attacks.
#   But each provider's host allowlist is local to that module — a future
#   refactor that widens the host tuple (e.g., to add staging) would silently
#   re-open the surface. ALLOWED_BASE_URLS pins the EXACT canonical URL the
#   project is allowed to talk to. Any deviation (path, scheme, port, even
#   trailing slash differences after normalization) requires an explicit
#   operator override via AGI_V8_<PROVIDER>_ALLOWED_URLS env (opt-in).
#
# Sources audited from W1.A5 module constants and SDK documentation:
#   - DeepSeek:  https://api.deepseek.com/v1   (deepseek_provider._DEFAULT_API_BASE)
#   - Anthropic: https://api.anthropic.com     (anthropic_provider._DEFAULT_BASE_URL)
#   - OpenAI:    https://api.openai.com/v1     (openai_provider._DEFAULT_BASE_URL)
#   - Gemini:    https://generativelanguage.googleapis.com (gemini_provider._DEFAULT_BASE_URL)
#   - Phone-backed endpoints are not a public provider capability.
#     No phone allowlist entry exists; operator overrides cannot enable it.
ALLOWED_BASE_URLS: Mapping[str, frozenset[str]] = {
    "deepseek": frozenset({"https://api.deepseek.com/v1"}),
    "anthropic": frozenset({"https://api.anthropic.com"}),
    "openai": frozenset({"https://api.openai.com/v1"}),
    "gemini": frozenset({"https://generativelanguage.googleapis.com"}),
}


def _normalize_base_url(url: str) -> str:
    """Canonicalize a base URL for allowlist comparison.

    Strips trailing slash and lowercases scheme + host. Path/query/fragment
    are preserved (case-sensitive) — providers like DeepSeek encode the API
    version in the path (``/v1``) and that MUST match exactly.
    """
    raw = (url or "").strip().rstrip("/")
    if not raw:
        return raw
    try:
        from urllib.parse import urlsplit, urlunsplit  # lazy, no network
    except Exception as _ff_exc:
        _swallowed(_ff_exc, site="providers.base._normalize_base_url:urllib_import",
                   category="provider")
        return raw
    try:
        parts = urlsplit(raw)
    except Exception as _ff_exc:
        _swallowed(_ff_exc, site="providers.base._normalize_base_url:235", category="provider")
        return raw
    # Lowercase scheme + netloc; preserve path/query/fragment exactly.
    netloc = (parts.hostname or "").lower()
    if parts.port is not None:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit((parts.scheme.lower(), netloc, parts.path, parts.query, parts.fragment))


def _override_urls_from_env(provider_name: str) -> frozenset[str]:
    """Read the operator's AGI_V8_<PROVIDER>_ALLOWED_URLS opt-in override.

    Empty / unset => empty frozenset (no override; project defaults stand).
    Otherwise, parse comma-separated URLs and canonicalize each.

    NOTE: the override is OPT-IN. It does NOT widen the host allowlist used
    by W1.A5's ``validate_remote_endpoint_url`` — that one still enforces
    the per-provider host tuple. The override only lets operators substitute
    a different exact base URL (e.g., a corporate proxy, staging endpoint,
    AWS Bedrock gateway) that ALSO matches the per-provider host allowlist.
    For broader host overrides operators must edit the provider module —
    this is intentional friction.
    """
    name = (provider_name or "").strip().lower()
    if not name:
        return frozenset()
    env_key = f"AGI_V8_{name.upper()}_ALLOWED_URLS"
    raw = os.environ.get(env_key, "").strip()
    if not raw:
        return frozenset()
    parts = [_normalize_base_url(p) for p in raw.split(",") if p.strip()]
    return frozenset(p for p in parts if p)


def validate_base_url_allowlist(provider_name: str, base_url: str) -> str:
    """Reject base URLs that aren't in the project-pinned allowlist.

    Runs AFTER ``validate_remote_endpoint_url`` (which enforces scheme/host/
    port/credential/SSRF guards). This is a tighter second layer: even if a
    future refactor accidentally widens the per-provider host tuple, the
    canonical project URL set holds.

    Operators who need a different exact URL (corporate proxy, staging,
    Bedrock gateway) opt in via ``AGI_V8_<PROVIDER>_ALLOWED_URLS``.

    Returns the input ``base_url`` (verbatim — does NOT mutate) on success.
    Raises ``RemoteEndpointPolicyError`` on rejection.
    """
    name = (provider_name or "").strip().lower()
    if not name:
        raise RemoteEndpointPolicyError("provider_name required for allowlist check")
    project_defaults = ALLOWED_BASE_URLS.get(name)
    if project_defaults is None:
        raise RemoteEndpointPolicyError(
            f"no ALLOWED_BASE_URLS entry for provider {name!r}"
        )
    override = _override_urls_from_env(name)
    allowed_normalized = {_normalize_base_url(u) for u in project_defaults}
    if override:
        allowed_normalized |= override
    candidate = _normalize_base_url(base_url)
    if not candidate:
        raise RemoteEndpointPolicyError(
            f"{name} base_url empty; expected one of {sorted(allowed_normalized)}"
        )
    if candidate not in allowed_normalized:
        raise RemoteEndpointPolicyError(
            f"{name} base_url {candidate!r} not in allowlist; "
            f"set AGI_V8_{name.upper()}_ALLOWED_URLS to opt-in additional URLs"
        )
    return base_url


def validate_remote_endpoint_url(
    url: str,
    *,
    allowed_hosts: tuple[str, ...],
    allowed_ports: tuple[int, ...] = (),
    allow_http: bool = False,
    label: str = "remote endpoint",
) -> str:
    """Validate a provider base URL before any token or payload can be sent.

    The helper is pure and performs no network I/O. Callers must provide an
    explicit host allowlist; otherwise arbitrary custom endpoints are rejected.
    """

    raw = (url or "").strip().rstrip("/")
    if not raw:
        raise RemoteEndpointPolicyError(f"{label} URL is empty")
    if not allowed_hosts:
        raise RemoteEndpointPolicyError(f"{label} requires an explicit host allowlist")
    try:
        from urllib.parse import urlsplit  # lazy, no network

        parts = urlsplit(raw)
    except Exception as exc:
        raise RemoteEndpointPolicyError(f"{label} URL is malformed") from exc

    allowed_schemes = {"https", "http"} if allow_http else {"https"}
    if parts.scheme not in allowed_schemes:
        raise RemoteEndpointPolicyError(f"{label} URL scheme is not allowed")
    if parts.username or parts.password:
        raise RemoteEndpointPolicyError(f"{label} URL must not contain credentials")
    host = parts.hostname or ""
    if _host_is_unsafe(host):
        raise RemoteEndpointPolicyError(f"{label} host is unsafe")
    normalized_host = _normalize_url_host(host)
    allowed = {_normalize_url_host(h) for h in allowed_hosts}
    if normalized_host not in allowed:
        raise RemoteEndpointPolicyError(f"{label} host is not explicitly allowed")
    try:
        port = parts.port
    except ValueError as exc:
        raise RemoteEndpointPolicyError(f"{label} URL port is invalid") from exc
    effective_port = port or (443 if parts.scheme == "https" else 80)
    if allowed_ports and effective_port not in set(int(p) for p in allowed_ports):
        raise RemoteEndpointPolicyError(f"{label} port is not explicitly allowed")
    return raw


def require_providers_enabled(provider_label: str) -> None:
    """Raise ProvidersDisabledError if the V8 providers knob is OFF."""
    if not providers_enabled():
        raise ProvidersDisabledError(
            f"providers default OFF for V8 — set {_PROVIDERS_ENABLED_ENV}=true "
            f"to enable provider '{provider_label}'."
        )


def assert_env_key_allowed(key: str, provider_prefix: str) -> None:
    """Validate that ``key`` follows the allowlisted pattern for ``provider_prefix``.

    Accepts keys of form ``{PROVIDER_PREFIX}_{SUFFIX}`` where SUFFIX is in
    ALLOWED_ENV_KEYS. Also accepts ``{PROVIDER_PREFIX}_{AGENT}_{SUFFIX}`` for
    per-agent overrides (e.g. ANTHROPIC_BUILDER_MAX_TOKENS).
    """
    prefix = provider_prefix.strip().upper()
    if not key or not key.startswith(prefix + "_"):
        raise ProvidersDisabledError(
            f"env key {key!r} not in allowlist for provider {provider_prefix!r}"
        )
    tail = key[len(prefix) + 1:]
    if tail in ALLOWED_ENV_KEYS:
        return
    # per-agent override: <AGENT>_<SUFFIX>
    parts = tail.rsplit("_", 1)
    if len(parts) == 2 and parts[1] in ALLOWED_ENV_KEYS:
        return
    # multi-token suffixes (MAX_TOKENS etc.) — strip last two tokens
    parts2 = tail.rsplit("_", 2)
    if len(parts2) == 3 and "_".join(parts2[-2:]) in ALLOWED_ENV_KEYS:
        return
    raise ProvidersDisabledError(
        f"env key {key!r} not in allowlist {sorted(ALLOWED_ENV_KEYS)} "
        f"for provider {provider_prefix!r}"
    )


# __SLOT_R9_T6_2026_08_17__ provider 키 회전 전파 — R9 감사 T6.
#
# 문제: DeepSeek/OpenAI provider 가 __init__ 시점에 env 를 한 번 읽어
# self.api_key 에 스냅샷하고, 요청마다 그 스냅샷을 그대로 재사용한다
# (deepseek_provider.py:266, openai_provider.py:66 — 이 함수를 추가하기
# 전 상태). 키를 유출로 **폐기**해도 살아 있는 provider 객체는 구 키를
# 계속 쏜다. 모듈 수준 캐시/싱글턴은 없어서 노출 범위는 "그 객체의
# 수명"으로 한정되지만, ``dispatch_swarm()`` 한 번이 여러 레인에 걸쳐
# 그 수명 동안 도는 구간이 있어 짧지 않다.
#
# 기본 ON — house rule #1(새 방어는 기본 ON) + 이미 확인된 결함이라 OFF 가
# 중립이 아니라 결함 유지다(retry.py 의
# __SLOT_RETRY_FAILOVER_DISCIPLINE_2026_08_17__ 와 동일 논리, 그 파일의
# "raw not in _FALSY" 관례를 그대로 따른다).
# OFF(AGI_V8_PROVIDER_KEY_REFRESH=false 또는 0) ⇒ 생성자 스냅샷 그대로
# 재사용 — 패치 이전과 byte-identical.
PROVIDER_KEY_REFRESH_ENV = "AGI_V8_PROVIDER_KEY_REFRESH"
_KEY_REFRESH_FALSY = ("false", "0")


def provider_key_refresh_enabled() -> bool:
    """Default ON — 요청 조립 시점에 API 키를 다시 해석할지 여부.

    끄면(OFF) 생성자에서 스냅샷한 ``self.api_key`` 를 계속 쓴다(패치 이전
    동작, byte-identical).
    """
    raw = os.environ.get(PROVIDER_KEY_REFRESH_ENV, "").strip().lower()  # tier: T3
    return raw not in _KEY_REFRESH_FALSY


def key_fingerprint(key: str) -> str:
    """로그/원장에 안전하게 남길 수 있는 키 식별자.

    값 자체·접두·접미는 절대 반환하지 않는다 — sha256 지문 앞 8자만.
    """
    if not key:
        return ""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]


def resolve_provider_api_key(
    *,
    cached_key: str,
    explicit_key_given: bool,
    env_name: str,
) -> str:
    """요청 조립 시점 API 키 재해석.

    - 게이트 OFF → ``cached_key``(생성자 스냅샷) 그대로.
    - 생성자에 ``api_key`` 가 **명시적으로** 전달됐으면(``explicit_key_given``)
      그 값을 항상 존중한다 — 테스트가 주입한 키가 env 회전에 흔들리면 안
      된다.
    - 그 외에는 매 요청마다 env 를 다시 읽는다. env 가 비어 있으면 빈
      문자열을 반환한다 — 호출부가 구 키로 계속 쏘지 말고 fail-closed
      거부하도록. **이게 이 트랙의 핵심**: 폐기가 실제로 폐기여야 한다.
    """
    if not provider_key_refresh_enabled():
        return cached_key
    if explicit_key_given:
        return cached_key
    return os.environ.get(env_name, "").strip()


# Global API concurrency semaphore — shared across all providers.
_api_semaphore: threading.Semaphore | None = None

# Async semaphore for async code paths (mirrors _api_semaphore).
_async_api_semaphore: asyncio.Semaphore | None = None

# Optional rate-limiter: must have a wait_if_needed(provider: str) method.
_rate_limiter: Any = None

# __SLOT_B3_PERCALL_USAGE_2026_07_25__ Thread-local per-call usage capture.
# Audit B3: every swarm lane shares ONE provider instance (cli.py registers a
# single {agent_name: provider}), and ``async_generate`` runs the sync
# ``generate`` in a worker thread via ``asyncio.to_thread``. So concurrent lanes
# clobber the shared ``last_usage`` dict, and the post-hoc reader
# (executor cost/reasoning + budget reconcile) attributes another lane's usage
# to this one. When a thread sets ``_CALL_USAGE.sink`` to a fresh dict (done by
# the ``*_with_usage`` helpers around exactly one ``generate`` in that thread),
# the ``last_usage`` property routes ALL reads/writes to that per-thread dict, so
# the capture reflects THIS call only. With no sink active (every legacy/sync
# caller) reads/writes route to the shared instance dict — byte-identical.
_CALL_USAGE = threading.local()

# __SLOT_PARTIAL_USAGE_ON_ERROR_2026_08_02__ Recover the tokens of a call that
# FAILED after the API already answered.
#
# ``async_generate_with_usage`` used to drop the per-call sink in a bare
# ``finally``, so every exception discarded the usage collected so far. But the
# most common failure of a structured-output call is NOT "the request never
# left": it is "the API answered, we were billed, and then JSON/schema
# post-processing rejected the body". Dropping that snapshot deletes real,
# already-charged tokens from the spend ledger — i.e. it shrinks the daily cost
# cap's NUMERATOR, so the cap silently gets looser exactly when calls are
# failing. An honest ledger must recover those tokens.
#
# The snapshot rides on the exception OBJECT as an attribute. Deliberately not a
# new exception type and not a changed signature: every existing caller catches
# these by type and unpacks a 2-tuple, and all of them keep working untouched —
# only a caller that asks for the attribute sees anything new.
PARTIAL_USAGE_ATTR = "agi_v8_1_partial_usage"

# __SLOT_RETRY_BILLED_TALLY_2026_08_02__ One ``generate()`` can be billed MANY
# times, and the per-call sink only remembers the last of them.
#
# ``providers.retry.make_api_retry`` wraps the INNER ``_call_api`` of every
# provider with tenacity (default ``max_attempts=20``, W7A10), and
# ``is_rate_limit_or_transient`` classifies the strings "empty response" and
# "no choices" as transient. DeepSeek raises exactly those two AFTER
# ``_record_usage`` — i.e. after the API answered and charged us. So a single
# ``generate()`` can issue N paid HTTP requests. Each of them assigns
# ``self.last_usage = {...}``, and the sink setter rebinds in place with
# ``clear() + update()``, so attempts 1..N-1 were silently erased and the
# recovered snapshot was 1/N of the real bill — while still being reported as
# fully "measured". Measured 2026-08-02 with a real DeepSeekProvider and real
# tenacity at ``max_attempts=3``: 3 paid POSTs, 1500/15000 tokens really billed,
# 500/5000 recovered.
#
# Fix: a per-call TALLY, thread-local like the sink and installed by the same
# ``*_with_usage`` helpers. Each new assignment folds the attempt it is about to
# overwrite into the tally, so the sealed snapshot carries the tokens of EVERY
# billed attempt plus ``billed_attempts``. An attempt that never reached the API
# records no tokens and is not counted — "billed" means billed.
#
# ``last_usage`` itself keeps its exact old meaning (the LAST attempt), and a
# call with a single attempt seals to a byte-identical snapshot, so no existing
# reader changes behaviour.
BILLED_ATTEMPTS_KEY = "billed_attempts"
# __SLOT_UNMEASURED_ATTEMPTS_2026_08_23__ 백로그 #11(a): billed marker 가 없는
# 시도(왕복은 있었으나 토큰이 안 붙음)를 "콜 없었음"과 구분하려고 센다.
# billed 집계에는 절대 안 섞인다 — "세되, 과금 안 함". 0이면 스냅샷에 키 자체가
# 안 생겨 기존 모든 시나리오가 바이트 동일하다.
UNMEASURED_ATTEMPTS_KEY = "unmeasured_attempts"
#: 적대검증(2026-08-23, HIGH) 회수: 위 "0이면 바이트 동일"은 **미측정이 실제로
#: 발생한 실행**에서는 참이 아니다 — 그때는 sealed 스냅샷(=원장 행)에 없던 키가
#: 생긴다. 원장 행 스키마 변경은 게이트 뒤여야 한다는 이 레포의 계약을 따라
#: default-OFF 로 내린다(ox 설계와 수렴). OFF = 이 판 이전과 완전 동일.
USAGE_HONESTY_ENV = "AGI_V8_USAGE_HONESTY_ENABLED"  # tier: T9


def usage_honesty_enabled() -> bool:
    """미측정 왕복을 스냅샷에 이름 붙여 실을지. strict true/1, default-OFF."""
    return os.environ.get(USAGE_HONESTY_ENV, "") in ("true", "1")
_TALLY_TOKEN_KEYS: tuple[str, ...] = (
    "input_tokens", "output_tokens", "reasoning_tokens",
    "prompt_cache_hit_tokens", "prompt_cache_miss_tokens",
)
# Presence of one of these is what makes an attempt a BILLED attempt.
_TALLY_BILLED_MARKERS: tuple[str, ...] = ("input_tokens", "output_tokens")
# 마지막 **과금된** 시도의 신원. provider 가 기록 후 last_usage 를 비워도
# 봉인 스냅샷이 model/agent 를 잃지 않게 tally 에 함께 보관한다.
_TALLY_IDENTITY_KEYS: tuple[str, ...] = ("agent_name", "provider", "model")
_TALLY_IDENTITY_SLOT = "_identity"


def _is_count(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _fold_attempt_into_tally(attempt: "Mapping[str, Any]") -> None:
    """Add one already-billed attempt's token counts to this call's tally.

    __SLOT_TALLY_COUNTS_ROUND_TRIPS_2026_08_03__ 호출자는 :meth:`_record_usage`
    와 :func:`_seal_call_usage` **둘뿐**이다 — 즉 세는 단위가 "API 왕복"이다.
    예전에는 ``last_usage`` **setter** 가 대입마다 접었는데, 그러면
    ``FailoverProvider`` / ``CircuitBreakerProvider`` 가 하는
    ``self.last_usage = dict(inner_usage)`` 라는 단순 **복사**가 새 시도로 세어져
    유료 POST 1회짜리 호출이 원장에 2~3배로 올라갔다(2026-08-02 실측:
    bare $1.1 → CircuitBreaker $2.2 → Failover(CircuitBreaker) $3.3). 과대계상은
    캡을 일찍 잠그므로 과소계상만큼이나 실동작을 망가뜨린다.

    "billed" 은 **토큰이 실제로 붙은** 시도만 뜻한다. ``_record_usage(a, 0, 0)``
    (gemini 의 usage_metadata 부재 분기)은 돈이 안 나갔으므로 세지 않는다 —
    ``billed_attempts`` 는 인보이스와 대조하라고 있는 숫자다.
    """
    tally = getattr(_CALL_USAGE, "tally", None)
    if tally is None:
        return
    if not any(_is_count(attempt.get(k)) and attempt.get(k) > 0
               for k in _TALLY_BILLED_MARKERS):
        # A provider may prove that the raw response explicitly carried exact
        # integer 0/0 counts.  That is measured-free, not missing usage.  Only
        # the explicit marker plus both exact zeros earns this narrow path;
        # legacy providers that synthesize 0/0 retain the old unmeasured rule.
        if (
            attempt.get("usage_measured") is True
            and all(type(attempt.get(key)) is int and attempt[key] == 0
                    for key in _TALLY_BILLED_MARKERS)
        ):
            return
        # Nothing was charged ⇒ this attempt is not a billed attempt. But if a
        # round trip actually happened (``_record_usage`` always seeds
        # agent_name/provider/model/input_tokens/output_tokens, so a non-empty
        # ``attempt`` here reliably means "a call was made"), count it as
        # unmeasured rather than letting it vanish — "no call" and "call with
        # no usage" must stay distinguishable (백로그 #11a).
        if attempt:
            tally[UNMEASURED_ATTEMPTS_KEY] = int(tally.get(UNMEASURED_ATTEMPTS_KEY, 0)) + 1
        return
    for key in _TALLY_TOKEN_KEYS:
        v = attempt.get(key)
        if _is_count(v):
            tally[key] = tally.get(key, 0) + v
    identity = {k: attempt[k] for k in _TALLY_IDENTITY_KEYS if k in attempt}
    if identity:
        tally[_TALLY_IDENTITY_SLOT] = identity
    tally[BILLED_ATTEMPTS_KEY] = int(tally.get(BILLED_ATTEMPTS_KEY, 0)) + 1


def _seal_call_usage() -> dict[str, Any]:
    """Close this call's usage capture and return the FULL billed snapshot.

    Called exactly once per ``*_with_usage`` invocation (success or failure).
    With one billed attempt the result is ``dict(sink)`` — byte-identical to the
    pre-tally behaviour. With more, the token fields are the sum over every
    billed attempt and ``billed_attempts`` says how many, so a reader can never
    mistake "the last of 3 paid tries" for "the whole bill".
    """
    sink = getattr(_CALL_USAGE, "sink", None)
    snap = dict(sink) if sink else {}
    tally = getattr(_CALL_USAGE, "tally", None)
    if tally is None:
        return snap
    # 마지막 시도는 아직 tally 에 없다(대입 시점이 아니라 다음 왕복 때 접히므로).
    # 보통은 sink 가 그 시도이고, provider 가 기록 후 sink 를 비운 경우에만
    # ``pending`` 사본이 대신한다 — 지불한 토큰이 조용히 사라지지 않게.
    last_attempt = snap
    if (
        snap.get("usage_measured") is not True
        and not any(_is_count(snap.get(k)) and snap.get(k) > 0
                    for k in _TALLY_BILLED_MARKERS)
    ):
        last_attempt = getattr(_CALL_USAGE, "pending", None) or {}
    _fold_attempt_into_tally(last_attempt)

    unmeasured = int(tally.get(UNMEASURED_ATTEMPTS_KEY, 0) or 0)
    attempts = int(tally.get(BILLED_ATTEMPTS_KEY, 0) or 0)
    if attempts == 0:
        # 과금된 왕복이 없었다 — sink 를 그대로 돌려준다. 다만 왕복 자체가
        # 있었는데 전부 미측정이었다면(백로그 #11a) 비용이 진짜 미지수이지
        # 검증된 $0 이 아니므로 그 사실을 표시한다.
        if unmeasured and usage_honesty_enabled():
            snap[UNMEASURED_ATTEMPTS_KEY] = unmeasured
            snap["usage_measured"] = False
        return snap

    # __SLOT_SEAL_KEEPS_PAID_TOKENS_2026_08_03__ 과금된 왕복이 하나라도 있으면
    # **토큰의 진실은 tally** 다. 시도가 1회면 tally 합계 == sink 값이라 결과가
    # 바이트 동일하고, 여러 회거나 sink 가 오염/소실된 경우에만 값이 달라진다
    # (E3: 기록 후 last_usage={} / E4: 0/0 기록이 실제 시도를 덮어씀).
    for key in _TALLY_TOKEN_KEYS:
        if key in tally:
            snap[key] = tally[key]
    # 신원 필드도 마지막 과금 시도의 것으로 복원 — 없으면 원장 행의 model 이
    # None 이 되어 "어느 모델에 썼는지 모르는 지출" 이 된다.
    for key, value in (tally.get(_TALLY_IDENTITY_SLOT) or {}).items():
        snap.setdefault(key, value)
    if attempts > 1:
        snap[BILLED_ATTEMPTS_KEY] = attempts
    if unmeasured and usage_honesty_enabled():
        # 혼합 재시도(예: 미측정 2회 후 과금 1회) — billed 합계는 정확하므로
        # ``usage_measured`` 는 건드리지 않는다(과잉 경보 방지), 낭비된 왕복
        # 횟수만 새 정보로 얹는다. 게이트 OFF 면 키 자체가 없다(행 스키마 불변).
        snap[UNMEASURED_ATTEMPTS_KEY] = unmeasured
    return snap


def _stamp_partial_usage(exc: BaseException, snap: "Mapping[str, Any]") -> None:
    """실패한 호출의 회수 usage 를 예외에 붙인다(없으면 묵은 도장을 지운다).

    ``__dict__`` 를 직접 확인하는 이유는 중첩 try/except 로 감싸면 이국적인
    ``__slots__`` 예외가 진짜 오류를 가릴 수 있기 때문이다 — 그 경우엔 그냥
    스냅샷 없이 도착하고, 호출자는 그 지출을 UNMEASURED 로 기록한다
    (fail-closed: "모른다"이지 "$0 공짜"가 아니다).
    """
    holder = getattr(exc, "__dict__", None)
    if not isinstance(holder, dict):
        return
    if snap:
        holder[PARTIAL_USAGE_ATTR] = dict(snap)
    else:
        # __SLOT_PARTIAL_USAGE_ONE_SHOT_2026_08_02__ 이 예외 객체는 **이전**
        # 호출이 raise 하며 도장을 찍어둔 것일 수 있다(예외 인스턴스 재사용은
        # 합법이다). 한 푼도 안 쓴 호출에 묵은 도장이 살아남으면 유령 지출이
        # 되므로, 과금 없는 실패는 적극적으로 지운다.
        holder.pop(PARTIAL_USAGE_ATTR, None)


def _fill_usage_box(box: Any, snap: "Mapping[str, Any]", exc: "BaseException | None") -> None:
    """호출자 소유 box 를 봉인 스냅샷으로 채운다(실패해도 토큰을 잃지 않게).

    box 는 호출자가 준 임의의 매핑이라 ``clear``/``update`` 가 raise 할 수 있다.
    그때 그냥 터지면 **이미 지불한 토큰이 아무 데도 안 남는다** — box 도 비었고
    예외에도 도장이 없다. 그래서 채우기가 실패하면 그 예외에 도장을 찍는다.
    """
    try:
        box.clear()
        box.update(snap)
    except BaseException as box_exc:
        _stamp_partial_usage(box_exc, snap)
        raise
    if exc is not None:
        _stamp_partial_usage(exc, snap)


def _billing_boundary(fn):
    """``generate()`` 한 번 = **과금 한 건**. 그 수명 동안 재시도 tally 를 켠다.

    __SLOT_RETRY_TALLY_AT_THE_CALL_BOUNDARY_2026_08_03__ 2026-08-02 판은 tally 를
    ``async_generate_with_usage._run`` **한 곳에만** 설치했다. 그런데 기본 게이트
    상태에서 실제로 도는 과금 경로 셋 — ``self_improvement_v8._propose_fn``(순차),
    ``runtime.tick_runner._fn``(tick_review), ``async_throttled_generate``(swarm
    기본) — 은 전부 ``generate()`` 를 직접 부르고 공유 ``last_usage`` 를 읽는다.
    그래서 재시도 붕괴가 거기서는 그대로 살아 있었다. 실측(실 DeepSeekProvider +
    실 tenacity, 유료 POST 5회 = in 500 / out 5000):

        _propose_fn 순차           → 원장 in=100 out=1000, ``usage_measured: true``
        async_throttled_generate   → $1.1  (진실 $5.5)
        async_generate_with_usage  → $5.5  ✅ (수리된 유일한 경로)

    repo 기본 ``max_attempts=20`` 이므로 최악 20배 과소계상이고, 같은 커밋이
    추가한 정직성 표식이 그 1/N 짜리 행에 "쟀다"를 보증했다.

    수리 방향: 경로마다 sink 를 다는 대신 **경계 자체를 옮긴다**. tenacity 재시도는
    ``generate()`` 안에서 일어나므로 자연스러운 과금 경계는 ``generate()`` 다.
    :meth:`BaseModelProvider.__init_subclass__` 가 모든 하위 클래스의 ``generate``
    에 이걸 자동으로 씌우므로, 앞으로 추가되는 provider 도 잊을 수 없다
    (2026-08-02 의 교훈: 검체가 아니라 클래스를 닫는다).

    재진입 안전: ``FailoverProvider`` / ``CircuitBreakerProvider`` 는 자기
    ``generate()`` 안에서 inner 의 ``generate()`` 를 부른다. 안쪽은 경계를 새로
    깔지 않고 **바깥 경계의 sink 에 그대로 기록**한다 — 한 호출은 한 건이다.
    (그러지 않으면 안쪽이 봉인한 값을 바깥이 ``self.last_usage = dict(...)`` 로
    다시 대입하면서 같은 시도가 두 번 접힌다.)

    시도가 1회면 봉인 결과는 ``dict(sink)`` 라 종전과 **바이트 동일**하다.
    """
    @functools.wraps(fn)
    def _wrapped(self, *args, **kwargs):
        if os.environ.get("AGI_V8_PUBLIC_TIER_GATE_ENABLED", "false").strip().lower() == "true":
            from agi_v8_1.providers.mock import MockModelProvider
            if type(self) is not MockModelProvider:
                from agi_v8_1.policy.tier_gate import require_tier
                require_tier(3)
        if getattr(_CALL_USAGE, "depth", 0):
            return fn(self, *args, **kwargs)   # 바깥 경계가 봉인한다
        _CALL_USAGE.depth = 1
        _CALL_USAGE.sink = {}
        _CALL_USAGE.tally = {}
        _CALL_USAGE.pending = None
        try:
            try:
                return fn(self, *args, **kwargs)
            finally:
                # 성공·실패 어느 쪽이든 **먼저** 봉인한다. 실패 경로에서 usage 를
                # 버리면 API 가 이미 청구한 토큰이 증발한다.
                snap = _seal_call_usage()
                _CALL_USAGE.last_sealed = snap
                # 공유 속성에 전체 청구서를 publish — sink 없는 기존 독자
                # (_propose_fn / tick_review / swarm) 가 이걸 읽는다.
                self.__dict__["_last_usage_shared"] = dict(snap)
        except BaseException as exc:
            _stamp_partial_usage(exc, getattr(_CALL_USAGE, "last_sealed", {}) or {})
            raise
        finally:
            _CALL_USAGE.depth = 0
            _CALL_USAGE.sink = None
            _CALL_USAGE.tally = None
            _CALL_USAGE.pending = None
    _wrapped.__wrapped_by_billing_boundary__ = True
    return _wrapped


def partial_usage_from_exc(
    exc: BaseException, *, consume: bool = True,
) -> dict[str, Any]:
    """Per-call usage recovered from a failed ``*_with_usage`` call.

    Returns ``{}`` when nothing was recovered — which means the spend of that
    call is genuinely **UNKNOWN**, not zero. Callers must not record an
    unrecovered failure as a free turn; mark it unmeasured instead (see
    ``enforcement.si_spend_ledger.record_llm_call(usage_measured=...)``).

    __SLOT_PARTIAL_USAGE_ONE_SHOT_2026_08_02__ The snapshot is CONSUMED by
    default: it is popped off the exception so the same tokens can be billed
    exactly once. Exception objects are not guaranteed to be fresh — a caller
    can re-raise one instance for several candidates, and a module-level or
    cached exception can outlive the cycle that stamped it. Without the pop,
    reading it twice double-counts real spend, and reading it in a later cycle
    that made zero API calls invents PHANTOM spend. Both were reproduced
    2026-08-02. A second read fails closed ("unknown"), never "free" and never a
    re-bill. Pass ``consume=False`` only to inspect without billing.
    """
    holder = getattr(exc, "__dict__", None)
    if not isinstance(holder, dict) or PARTIAL_USAGE_ATTR not in holder:
        return {}
    snap = holder.pop(PARTIAL_USAGE_ATTR) if consume else holder[PARTIAL_USAGE_ATTR]
    return dict(snap) if isinstance(snap, Mapping) and snap else {}


JSON_ONLY_SYSTEM_PROMPT = (
    "You are a structured-output agent. "
    "Return ONLY valid JSON — no markdown fences, no commentary, no trailing text."
)

# __SLOT_SCHEMA_OPTIONAL_KEYS_HINT_2026_08_02__ default-OFF gate.
#
# ``_build_system_prompt`` ends the WIRE SYSTEM message on the strongest
# sentence either message carries: "CRITICAL: Your JSON output MUST include ALL
# of these top-level keys: <required>. Missing any one of them will cause
# validation failure." In JSON-Schema, ``required`` is a MINIMUM. In English,
# that sentence reads as an exclusive inventory — "these are your output keys".
#
# Measured 2026-08-02 (si_lanes/command_channel, best-of-N=8 live): a genuinely
# useful OPTIONAL property, correctly added to ``properties`` and correctly kept
# out of ``required``, was emitted 0/8 while the trailing line named only
# ``"files"``. The provider's own docstring says the structural enforcement
# "trails and wins on conflict" — so an optional field advertised anywhere else
# is arguing against the last thing the system role says.
#
# This clause states the true rule (required = minimum) and names the schema's
# optional properties. It is SCHEMA-DRIVEN — no key names, no agent names, no
# mode enum — so it works for any schema and adds nothing when every property is
# required. Gate OFF (the default) ⇒ the system prompt is byte-identical.
_OPTIONAL_KEYS_HINT_ENV = "AGI_V8_SCHEMA_OPTIONAL_KEYS_HINT_ENABLED"


def _optional_keys_hint_enabled() -> bool:
    """Strict ``"true"``/``"1"`` gate for the optional-keys clause."""
    return os.environ.get(_OPTIONAL_KEYS_HINT_ENV, "") in ("true", "1")  # tier: T3


def _optional_keys_clause(schema: Mapping[str, Any], required: list[Any]) -> str:
    """Return the trailing clause naming a schema's OPTIONAL properties.

    ``""`` whenever the gate is off, the schema has no ``properties`` mapping,
    or every declared property is already required — so the common case is
    byte-identical to the pre-clause prompt even with the gate on.
    """
    if not _optional_keys_hint_enabled():
        return ""
    props = schema.get("properties") if isinstance(schema, Mapping) else None
    if not isinstance(props, Mapping):
        return ""
    req = {k for k in required if isinstance(k, str)}
    optional = [k for k in props if isinstance(k, str) and k not in req]
    if not optional:
        return ""
    names = ", ".join(f'"{k}"' for k in optional)
    is_one = len(optional) == 1
    return (
        " That list is the REQUIRED MINIMUM, not the whole of your output: the "
        f"schema also declares {'the optional property' if is_one else 'these optional properties'} "
        f"{names}, which you MAY include when the instructions for "
        f"{'it' if is_one else 'them'} apply. Including "
        f"{'it' if is_one else 'one'} is never a validation failure, and "
        "omitting is never one either."
    )

# Models known to support extended context windows (8192+ tokens).
_LARGE_CONTEXT_MODEL_SUBSTRINGS: tuple[str, ...] = (
    "gpt-4",
    "gpt-5",
    "claude-3",
    "claude-sonnet",
    "claude-opus",
    "gemini-1.5",
    "gemini-2",
    "gemini-3",
)


def get_default_max_tokens(model_name: str) -> int:
    """Return a sensible default max_tokens value for the given model."""
    normalized = model_name.strip().lower()
    for substring in _LARGE_CONTEXT_MODEL_SUBSTRINGS:
        if substring in normalized:
            return 8192
    return 4096


def set_global_api_semaphore(sem: threading.Semaphore) -> None:
    """Install a global API-call semaphore for concurrency throttling."""
    global _api_semaphore, _async_api_semaphore
    _api_semaphore = sem
    _async_api_semaphore = asyncio.Semaphore(sem._value)


def set_global_rate_limiter(limiter: Any) -> None:
    """Install a rate limiter that gates every provider call."""
    global _rate_limiter
    _rate_limiter = limiter


class BaseModelProvider(ABC):
    """Abstract interface for structured model providers (V8 default-OFF gated).

    Subclasses must call ``require_providers_enabled(<label>)`` at the start
    of __init__ to honour the V8 advisory-chain default. The MockModelProvider
    intentionally bypasses this gate — it makes no network calls.
    """

    # Optional callback: called after each generate() with last_usage dict.
    _telemetry_callback: Any = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """모든 하위 provider 의 ``generate`` 를 과금 경계로 감싼다.

        __SLOT_RETRY_TALLY_AT_THE_CALL_BOUNDARY_2026_08_03__ 여기서 자동으로
        거는 이유는 provider 구현이 17개이고 앞으로도 늘기 때문이다. 호출부마다
        sink 를 다는 방식은 새 경로가 생길 때마다 **조용히** 빠진다 — 바로
        2026-08-02 수리가 실제 도는 경로 셋을 놓친 방식이다. 근거와 실측은
        :func:`_billing_boundary` 참조.

        추상 선언만 있고 구현이 없는 중간 클래스는 건너뛴다(감쌀 게 없다).
        이미 감싼 함수를 물려받았으면 다시 감싸지 않는다(중복 경계 금지).
        """
        super().__init_subclass__(**kwargs)
        fn = cls.__dict__.get("generate")
        if fn is None or getattr(fn, "__isabstractmethod__", False):
            return
        if getattr(fn, "__wrapped_by_billing_boundary__", False):
            return
        cls.generate = _billing_boundary(fn)  # type: ignore[assignment]

    # __SLOT_B3_PERCALL_USAGE_2026_07_25__ ``last_usage`` was a plain dict
    # attribute (class-level mutable default). It is now a property that routes
    # to a thread-local per-call sink when one is active (``*_with_usage`` path),
    # else to a per-instance shared dict. This makes concurrent-lane usage
    # capture race-free without changing behaviour for any legacy/sync reader
    # (no sink active → shared dict, byte-identical). Providers keep writing
    # ``self.last_usage = {...}`` / ``self.last_usage.update(...)`` verbatim.
    @property
    def last_usage(self) -> dict[str, Any]:
        sink = getattr(_CALL_USAGE, "sink", None)
        if sink is not None:
            return sink
        shared = self.__dict__.get("_last_usage_shared")
        if shared is None:
            shared = {}
            self.__dict__["_last_usage_shared"] = shared
        return shared

    @last_usage.setter
    def last_usage(self, value: dict[str, Any]) -> None:
        sink = getattr(_CALL_USAGE, "sink", None)
        if sink is not None:
            # __SLOT_TALLY_COUNTS_ROUND_TRIPS_2026_08_03__ 여기서는 접지 않는다.
            # 2026-08-02 판은 대입마다 접었는데, 그러면 래퍼 provider 의 단순
            # 복사 대입(``self.last_usage = dict(inner_usage)``)이 새 시도로
            # 세어져 원장이 2~3배로 부풀었다. 접기는 실제 API 왕복 지점인
            # :meth:`_record_usage` 와 봉인 시점에서만 한다.
            #
            # Rebind the per-call sink IN PLACE so the reference the capturing
            # thread holds stays valid (providers do ``self.last_usage = {...}``
            # then ``.update(...)`` — both must land on the same sink object).
            sink.clear()
            if value:
                sink.update(value)
        else:
            self.__dict__["_last_usage_shared"] = dict(value) if value else {}

    def _record_usage(self, agent_name: str, input_tokens: int, output_tokens: int) -> None:
        """Store token usage from last API call and invoke telemetry callback."""
        provider_name = self._provider_name()
        # __SLOT_TALLY_COUNTS_ROUND_TRIPS_2026_08_03__ 여기가 "API 왕복 한 번"의
        # 유일한 지점이다. 지금 sink 에 들어 있는 것은 **직전 시도**(tenacity
        # 재시도는 하나의 generate() 안에서 일어난다)이므로, 덮어쓰기 전에 접는다.
        # 안 접으면 이미 지불한 토큰이 그대로 사라진다.
        _sink = getattr(_CALL_USAGE, "sink", None)
        if _sink:
            _fold_attempt_into_tally(_sink)
        _attempt = {
            "agent_name": agent_name,
            "provider": provider_name,
            "model": getattr(self, "model", "unknown"),
            "input_tokens": max(0, int(input_tokens)),
            "output_tokens": max(0, int(output_tokens)),
        }
        # provider 가 기록 직후 last_usage 를 비워도 이 왕복을 잃지 않도록
        # 봉인 시점까지 사본을 들고 있는다(봉인이 sink 대신 이걸 쓴다).
        _CALL_USAGE.pending = dict(_attempt)
        self.last_usage = _attempt
        if self._telemetry_callback is not None:
            try:
                self._telemetry_callback(**self.last_usage)
            except Exception as e:
                try:
                    from ..enforcement.cost_limiter import CostBudgetExceeded
                except Exception as _cbe_exc:
                    _swallowed(_cbe_exc,
                               site="providers.base._record_usage:cost_limiter_import",
                               category="provider")
                    CostBudgetExceeded = ()  # type: ignore[assignment]
                if isinstance(e, CostBudgetExceeded):
                    raise
                # __SLOT_CENSUS_UNCONDITIONAL_RECORD_2026_08_03__ logger.warning
                # 은 초크포인트가 아니다 — 세지도, 이름 붙지도, strict 에서
                # 재-raise 되지도 않는다. 그래서 이 핸들러의 유일한 "실명" 이
                # 조건부 raise 뿐이었고, fail-closed census 가 침묵으로 셌다.
                # 무조건 라우팅으로 바꾸면 셋 다 얻는다: 계수·이름·strict 승격.
                _swallowed(e, site="providers.base._record_usage.telemetry",
                           category="telemetry")

    @abstractmethod
    def generate(
        self,
        agent_name: str,
        prompt: str,
        payload: Mapping[str, Any],
        schema: Mapping[str, Any],
    ) -> str:
        """Generate a JSON string for a given agent call."""

    async def async_generate(
        self,
        agent_name: str,
        prompt: str,
        payload: Mapping[str, Any],
        schema: Mapping[str, Any],
    ) -> str:
        """Async version of generate. Wraps sync generate via asyncio.to_thread."""
        return await asyncio.to_thread(
            self.generate, agent_name, prompt, payload, schema,
        )

    async def async_generate_with_usage(
        self,
        agent_name: str,
        prompt: str,
        payload: Mapping[str, Any],
        schema: Mapping[str, Any],
        *,
        usage_sink: "dict[str, Any] | None" = None,
    ) -> tuple[str, dict[str, Any]]:
        """__SLOT_B3_PERCALL_USAGE_2026_07_25__ Like ``async_generate`` but ALSO
        returns a race-free per-call usage snapshot.

        The sync ``generate`` runs in its own worker thread (as in
        ``async_generate``). Before it runs, this thread installs a fresh
        ``_CALL_USAGE.sink`` dict; the ``last_usage`` property then routes every
        usage read/write inside ``generate`` (``_record_usage`` + the provider's
        ``.update(...)`` calls) to THAT per-thread dict. So the returned snapshot
        reflects only this call, even when many concurrent lanes share ONE
        provider instance. The shared ``last_usage`` dict is left untouched on
        this path (the sink intercepts it), so no other reader changes behaviour.

        __SLOT_PARTIAL_USAGE_ON_ERROR_2026_08_02__ On failure the sink is no
        longer thrown away: whatever the provider had already recorded when the
        call died is attached to the exception (see ``PARTIAL_USAGE_ATTR`` /
        :func:`partial_usage_from_exc`) and the exception is re-raised
        unchanged — same type, same message, same traceback. This is how tokens
        billed by an API that answered before a post-processing failure stay
        visible to the spend ledger instead of evaporating.

        __SLOT_RETRY_BILLED_TALLY_2026_08_02__ The snapshot covers EVERY paid
        attempt of this call, not just the last one — provider-internal tenacity
        retries can pay for the same ``generate`` up to ``max_attempts`` times
        (see the tally notes at ``BILLED_ATTEMPTS_KEY``).

        __SLOT_PRIVATE_USAGE_BOX_2026_08_02__ ``usage_sink`` is the RELIABLE way
        to learn a failed call's spend. Pass a fresh dict and it is filled with
        this call's sealed usage on both paths, success and failure. Unlike the
        exception stamp it belongs to exactly one invocation, so it cannot be
        clobbered, shared or left over: two concurrent candidates that happen to
        raise the SAME exception object each still get their own tokens, and a
        stamp from an older cycle can never leak in. The stamp remains for
        callers that catch without a box (see :func:`partial_usage_from_exc`),
        but a caller that bills money should own a box.
        """
        def _run() -> tuple[str, dict[str, Any]]:
            # __SLOT_RETRY_TALLY_AT_THE_CALL_BOUNDARY_2026_08_03__ sink/tally 설치와
            # 봉인, 예외 도장은 이제 ``generate()`` 를 감싼 과금 경계가 한다
            # (:func:`_billing_boundary`). 여기 남는 일은 봉인 결과를 호출자가
            # 소유한 box 로 넘겨주는 것뿐이다 — 경계가 두 겹이면 같은 시도를
            # 두 번 접게 되므로 **여기서 다시 설치하면 안 된다**.
            _CALL_USAGE.last_sealed = {}
            try:
                content = self.generate(agent_name, prompt, payload, schema)
            except BaseException as exc:
                _sealed = getattr(_CALL_USAGE, "last_sealed", {}) or {}
                if usage_sink is not None:
                    _fill_usage_box(usage_sink, _sealed, exc)
                raise
            snap = dict(getattr(_CALL_USAGE, "last_sealed", {}) or {})
            if usage_sink is not None:
                # box 는 1회용 계약이지만, 재사용해도 앞 호출 값이 남지 않도록
                # 비우고 채운다("모름"을 남은 값으로 덮지 않는다).
                # box 채우기가 실패하면 그 예외에 도장을 찍어 이미 지불한 토큰이
                # 호출자에게서 사라지지 않게 한다(E2 경계).
                _fill_usage_box(usage_sink, snap, None)
            return content, snap

        return await asyncio.to_thread(_run)

    # __SLOT_W_SYS_PROMPT_WIRE_2026_06_12__ Payload key carrying a
    # caller-composed system prompt (e.g. swarm_v8 lane role prompt +
    # distilled-rules sidecar). Every provider's generate() folds this into
    # the WIRE system-role message via _build_system_prompt, and
    # _build_user_content strips it so it does not double-appear in the user
    # blob. Single source of truth so the executor (which sets the key) and
    # the providers (which consume it) cannot drift on the spelling.
    SYSTEM_PROMPT_PAYLOAD_KEY = "system_prompt"

    @classmethod
    def _caller_system_prompt(cls, payload: Mapping[str, Any] | None) -> str:
        """Extract the caller-supplied system prompt from ``payload``.

        Returns the stripped string when ``payload[SYSTEM_PROMPT_PAYLOAD_KEY]``
        is a non-empty string; ``""`` otherwise (including ``payload is None``).
        Empty result ⇒ providers behave byte-identically to the pre-bridge
        path (no caller prompt was supplied).
        """
        if not payload:
            return ""
        raw = payload.get(cls.SYSTEM_PROMPT_PAYLOAD_KEY)
        return raw.strip() if isinstance(raw, str) else ""

    def _build_system_prompt(
        self,
        agent_name: str,
        schema: Mapping[str, Any],
        markdown_passthrough: bool = False,
        caller_system_prompt: str = "",
    ) -> str:
        """Build a strict system prompt that reinforces JSON-only output.

        __SLOT_W7A10_2026_06_06__ — when ``markdown_passthrough=True`` the
        JSON-only prefix + schema reinforcement are replaced by a markdown
        directive, so long-form prose lanes do not get told "JSON only".

        __SLOT_W_SYS_PROMPT_WIRE_2026_06_12__ — when ``caller_system_prompt``
        is non-empty, it is placed FIRST in the wire system-role message
        (ahead of the JSON-only / schema reinforcement, so the structural
        enforcement still trails and wins on conflict). This is the bridge
        that lands a swarm_v8 lane's role prompt + distilled-rules sidecar in
        the SYSTEM role instead of the serialized user-payload blob. Empty
        string ⇒ byte-identical to the pre-bridge behavior.
        """
        caller = (caller_system_prompt or "").strip()
        if markdown_passthrough:
            base = (
                f"Agent name: {agent_name}.\n\n"
                "Produce markdown output as specified by the user prompt. "
                "Do NOT wrap output in a JSON object. Do NOT add ```json fences. "
                "The harness preserves your output verbatim as a markdown string."
            )
            return f"{caller}\n\n{base}" if caller else base
        parts: list[str] = []
        if caller:
            parts.append(caller)
        parts.extend([JSON_ONLY_SYSTEM_PROMPT, f"Agent name: {agent_name}."])
        if schema:
            # A schema is also serialized into text on providers without a
            # native schema field. Guard it while its structure still exists.
            from .egress import mask_request_content

            schema = mask_request_content(dict(schema), schema=True)
            parts.append(
                "Target JSON schema:\n"
                + json.dumps(dict(schema), ensure_ascii=False, indent=2, sort_keys=True)
            )
            required = schema.get("required") if isinstance(schema, Mapping) else None
            if isinstance(required, list) and required:
                line = (
                    "CRITICAL: Your JSON output MUST include ALL of these top-level keys: "
                    + ", ".join(f'"{k}"' for k in required)
                    + ". Missing any one of them will cause validation failure."
                )
                line += _optional_keys_clause(schema, required)
                parts.append(line)
        return "\n\n".join(parts)

    def _build_user_content(self, prompt: str, payload: Mapping[str, Any]) -> str:
        """Build the user message content with a serialized payload.

        __SLOT_W7A10_2026_06_06__ — keys prefixed with ``_`` are
        executor-internal flags (e.g. ``_markdown_passthrough``) and are
        filtered out so they do not leak into the LLM's user prompt.

        __SLOT_W_SYS_PROMPT_WIRE_2026_06_12__ — ``system_prompt`` is consumed
        by _build_system_prompt (placed in the WIRE system role), so it is
        also stripped here to avoid a near-duplicate copy in the user blob.
        """
        filtered = {
            k: v
            for k, v in payload.items()
            if not k.startswith("_") and k != self.SYSTEM_PROMPT_PAYLOAD_KEY
        }
        serialized_payload = json.dumps(filtered, ensure_ascii=False, indent=2, default=str)
        # Preserve default=str compatibility, then inspect the actual JSON
        # representation. Key-context credentials must not disappear into a
        # string before the final text-only send-boundary masking sees them.
        from .egress import mask_request_content

        native = json.loads(serialized_payload)
        protected = mask_request_content(native)
        if protected != native:
            serialized_payload = json.dumps(protected, ensure_ascii=False, indent=2)
        return f"{prompt}\n\nStructured payload:\n{serialized_payload}"

    @staticmethod
    def _strip_code_fences(text: str) -> str:
        """Remove accidental markdown fences around a JSON response."""
        stripped = text.strip()
        if not stripped.startswith("```"):
            return stripped
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        return "\n".join(lines).strip()

    def _provider_name(self) -> str:
        """Return the declared provider identity, with a safe class fallback."""
        declared = getattr(self, "provider_name", None)
        if isinstance(declared, str) and declared.strip():
            return declared.strip().lower()
        # Preserve the historical fallback byte-for-byte for undeclared test
        # doubles and legacy providers; only an explicit class identity opts
        # into the canonical path above.
        return type(self).__name__.replace("ModelProvider", "").lower()

    async def async_throttled_generate(
        self,
        agent_name: str,
        prompt: str,
        payload: Mapping[str, Any],
        schema: Mapping[str, Any],
    ) -> str:
        """Async generate with concurrency throttle and rate limiting."""
        if _rate_limiter is not None:
            await asyncio.to_thread(_rate_limiter.wait_if_needed, self._provider_name())
        if _async_api_semaphore is not None:
            wait_start = time.monotonic()
            async with _async_api_semaphore:
                wait_time = time.monotonic() - wait_start
                if wait_time > 0.1:
                    logger.info(
                        "Async API semaphore wait for agent '%s': %.3fs",
                        agent_name, wait_time,
                    )
                return await self.async_generate(agent_name, prompt, payload, schema)
        return await self.async_generate(agent_name, prompt, payload, schema)

    async def async_throttled_generate_with_usage(
        self,
        agent_name: str,
        prompt: str,
        payload: Mapping[str, Any],
        schema: Mapping[str, Any],
        *,
        usage_sink: "dict[str, Any] | None" = None,
    ) -> tuple[str, dict[str, Any]]:
        """__SLOT_B3_PERCALL_USAGE_2026_07_25__ ``async_throttled_generate`` +
        race-free per-call usage capture (delegates to
        ``async_generate_with_usage``). Same throttle/rate-limit semantics.

        ``usage_sink`` is forwarded unchanged — see
        ``async_generate_with_usage``."""
        if _rate_limiter is not None:
            await asyncio.to_thread(_rate_limiter.wait_if_needed, self._provider_name())
        if _async_api_semaphore is not None:
            wait_start = time.monotonic()
            async with _async_api_semaphore:
                wait_time = time.monotonic() - wait_start
                if wait_time > 0.1:
                    logger.info(
                        "Async API semaphore wait for agent '%s': %.3fs",
                        agent_name, wait_time,
                    )
                return await self.async_generate_with_usage(
                    agent_name, prompt, payload, schema, usage_sink=usage_sink,
                )
        return await self.async_generate_with_usage(
            agent_name, prompt, payload, schema, usage_sink=usage_sink,
        )

    def throttled_generate(
        self,
        agent_name: str,
        prompt: str,
        payload: Mapping[str, Any],
        schema: Mapping[str, Any],
    ) -> str:
        """Generate with global concurrency throttle and rate limiting."""
        if _rate_limiter is not None:
            _rate_limiter.wait_if_needed(self._provider_name())
        if _api_semaphore is not None:
            wait_start = time.monotonic()
            _api_semaphore.acquire()
            wait_time = time.monotonic() - wait_start
            if wait_time > 0.1:
                logger.info(
                    "API semaphore wait for agent '%s': %.3fs",
                    agent_name, wait_time,
                )
            try:
                return self.generate(agent_name, prompt, payload, schema)
            finally:
                _api_semaphore.release()
        return self.generate(agent_name, prompt, payload, schema)


class DisabledProviderBase(BaseModelProvider):
    """Fail-fast provider base for deprecated/dead backends (R11.a).

    Used by cohere/groq/mistral/ollama/openrouter/perplexity/xai which were
    proven dead in v7.1 R11. They must remain importable + subclass of
    BaseModelProvider, but raise ``no_capacity`` on construction so the
    failover chain can fast-skip without wasted retries.
    """

    provider_name = "disabled_provider"

    def _no_capacity_message(self) -> str:
        provider_name = getattr(self, "provider_name", "")
        if not provider_name or provider_name == "disabled_provider":
            provider_name = type(self).__name__.replace("Provider", "").lower()
        return (
            f"no_capacity: {provider_name} is deprecated; "
            "use anthropic/openai/gemini/deepseek — fail-closed per R11.a"
        )

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError(self._no_capacity_message())

    def generate(
        self,
        agent_name: str,
        prompt: str,
        payload: Mapping[str, Any],
        schema: Mapping[str, Any],
    ) -> str:
        raise RuntimeError(self._no_capacity_message())


def _mask_secret(text: str, *, mask_opaque_tokens: bool = True) -> str:
    """Minimal in-tree secret masker (V8 has no v7 policy module).

    Masks anything that looks like an API key: long alphanumeric tokens,
    sk- / xai- / AIza prefixes, Authorization headers. Used in failover
    event logs to keep secrets out of error reports.
    """
    import re

    if not text:
        return text
    patterns = [
        # Bearer / Authorization tokens
        (re.compile(r"(Authorization\s*[:=]\s*Bearer\s+)[A-Za-z0-9._\-]{8,}", re.IGNORECASE),
         r"\1<MASKED>"),
        # Common API-key prefixes.
        # __SLOT_BOUNDARY_FIX_2026_06_16__ — leading anchor is ``(?<![A-Za-z])``
        # not ``\b``: a ``\b`` misses a key glued to a word char (``lane_sk-``,
        # ``token_sk-``, ``1sk-``) so the raw key leaked. ``(?<![A-Za-z])`` fires
        # after ``_``/digit/separator/start but NOT mid-English-word, so
        # ``risk-management`` / ``task-scheduler`` stay intact. (Mirrors the fix
        # in policy/secret_masker._SECRET_PATTERNS; the two layers compose.)
        (re.compile(r"(?<![A-Za-z])(sk-[A-Za-z0-9_\-]{6,})"), "<MASKED-SK>"),
        (re.compile(r"(?<![A-Za-z])(xai-[A-Za-z0-9_\-]{6,})"), "<MASKED-XAI>"),
        (re.compile(r"(?<![A-Za-z])(AIza[A-Za-z0-9_\-]{20,})"), "<MASKED-GOOGLE>"),
        # __SLOT_BOUNDARY_FIX_2026_06_16__ — parity patterns so this layer is not
        # a silent single-point-of-failure when called WITHOUT the mask_text
        # prepass (AWS / Slack / GitHub were previously only caught by mask_text).
        (re.compile(r"(?<![A-Za-z])AKIA[0-9A-Z]{16}\b"), "<MASKED-AWS>"),
        (re.compile(r"(?<![A-Za-z])xox[baprs]-[A-Za-z0-9-]{10,}"), "<MASKED-SLACK>"),
        (re.compile(r"(?<![A-Za-z])gh[pousra]_[A-Za-z0-9]{20,}\b"), "<MASKED-GH>"),
    ]
    # Logging keeps its historical conservative heuristic. Request content
    # opts out: a long source identifier/hash is not evidence of a credential.
    if mask_opaque_tokens:
        patterns.append((re.compile(r"\b[A-Za-z0-9_\-]{40,}\b"), "<MASKED-TOKEN>"))
    out = text
    for pat, repl in patterns:
        out = pat.sub(repl, out)
    return out
