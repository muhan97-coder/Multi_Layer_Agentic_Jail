"""Public, dependency-free attachment contracts for optional capability code.

These coordinates are not an unlock authority or a duplicate gate registry.
Callers retain their existing admission, accounting and write-safety checks.
The R27 declaration/registry/preflight facade remains available lazily.
"""

from .ports import PayloadPort, PayloadUnavailable, resolve_payload
from importlib import import_module

_LEGACY = {
    "CapabilityMeta": ".declaration", "CapabilityKind": ".declaration",
    "CapabilityRegistry": ".declaration", "load_meta": ".declaration",
    "iter_capability_dirs": ".declaration", "environment_preflight": ".builtins",
    "ENVIRONMENT_PREFLIGHT_META": ".builtins",
}


def __getattr__(name):
    if name not in _LEGACY:
        raise AttributeError("unknown capabilities facade export")
    return getattr(import_module(_LEGACY[name], __name__), name)


__all__ = [*_LEGACY, "PayloadPort", "PayloadUnavailable", "resolve_payload"]
