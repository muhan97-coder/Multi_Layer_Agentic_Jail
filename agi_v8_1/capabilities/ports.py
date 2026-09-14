"""Lazy optional-code attachment, with bounded, non-secret refusal reasons."""
from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
import os
from typing import Any


@dataclass(frozen=True)
class PayloadPort:
    """A source-owned import coordinate, never a user-supplied module selector."""

    tier: int
    module: str
    symbol: str
    callable_only: bool = True


class PayloadUnavailable(ImportError):
    def __init__(self, tier: int, reason: str) -> None:
        self.tier = tier
        self.reason = reason
        self.doc_pointer = f"Plz_ReadMe.md §T{tier}"
        super().__init__(f"T{tier} 미개방: capability payload unavailable ({reason}); "
                         f"read {self.doc_pointer}")


def resolve_payload(port: PayloadPort) -> Any:
    """Import only when an admitted caller needs a capability; never cache failures.

    Unknown/missing dependencies are not mock success. Exceptions produced by
    an available implementation during execution are not caught by this port.
    """
    if type(port) is not PayloadPort or type(port.tier) is not int or not 3 <= port.tier <= 9:
        raise ValueError("invalid capability attachment")
    if (type(port.module) is not str or not port.module.startswith("agi_v8_1.")
            or type(port.symbol) is not str or not port.symbol.isidentifier()
            or type(port.callable_only) is not bool):
        raise ValueError("invalid capability attachment")
    if os.environ.get("AGI_V8_PUBLIC_TIER_GATE_ENABLED", "false").strip().lower() == "true":
        from agi_v8_1.policy.tier_gate import require_tier
        require_tier(port.tier)
    try:
        module = import_module(port.module)
    except ModuleNotFoundError as exc:
        if exc.name == port.module or (exc.name and port.module.startswith(exc.name + ".")):
            raise PayloadUnavailable(port.tier, "payload_missing") from None
        raise PayloadUnavailable(port.tier, "dependency_missing") from None
    except ImportError:
        raise PayloadUnavailable(port.tier, "dependency_missing") from None
    value = getattr(module, port.symbol, None)
    if value is None or (port.callable_only and not callable(value)):
        raise PayloadUnavailable(port.tier, "invalid_export")
    return value
