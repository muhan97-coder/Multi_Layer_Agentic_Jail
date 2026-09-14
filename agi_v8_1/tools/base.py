"""Tool framework base classes & registry.

Ported from the implicit v7.1 tool surface (agent_system/tools/) — v7.1 keeps
tools as plain modules; agi_v8_1 wraps them in a typed BaseTool + Registry so
the swarm pod can route tool calls uniformly.

R27 contract:
- BaseTool subclasses MUST be pure / safe at module top.
- Tools that need subprocess or network override ``run`` with a stub returning
  ``ToolResult(ok=False, ...)`` plus a deferred marker — actual execution is
  routed via the approved-runtime path.
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, ClassVar, Iterator

__all__ = [
    "ToolKind",
    "ToolResult",
    "BaseTool",
    "ToolRegistry",
    "register_tool",
    "get_default_registry",
]


class ToolKind(str, Enum):
    """Tool categorisation — affects which lanes/agents may invoke it."""

    PURE = "pure"             # filesystem-only or in-memory, always safe
    SHELL = "shell"           # invokes subprocess  (# R27: stub)
    PROVIDER = "provider"     # calls an LLM provider (# R27: stub)
    NETWORK = "network"       # http/grpc           (# R27: stub)
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ToolResult:
    """Uniform return shape for tool invocations.

    ``ok`` is True only when the tool ran to completion AND produced semantic
    success. ``payload`` carries structured data; ``error`` is non-empty when
    ``ok=False``. ``stub`` flags deferred runtime paths.
    """

    ok: bool
    payload: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    stub: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "payload": dict(self.payload),
            "error": self.error,
            "stub": self.stub,
        }


class BaseTool(ABC):
    """Abstract base — concrete tools override ``name``, ``kind``, ``run``."""

    name: ClassVar[str] = ""
    kind: ClassVar[ToolKind] = ToolKind.UNKNOWN
    description: ClassVar[str] = ""

    @abstractmethod
    def run(self, **kwargs: Any) -> ToolResult:  # pragma: no cover - abstract
        ...

    # Convenience for sub-classes: build a stub result that won't be mistaken
    # for a successful run. Used by SHELL / PROVIDER / NETWORK tools.
    @classmethod
    def stub_result(cls, reason: str) -> ToolResult:
        return ToolResult(
            ok=False,
            payload={"stub": True, "reason": reason},
            error="deferred to approved_runtime",
            stub=True,
        )


class ToolRegistry:
    """Catalogue of tool classes (not instances).

    Tools register a *class* and the registry instantiates lazily on first
    lookup — that gives callers a single instance per tool name without
    racing on import-time construction.
    """

    def __init__(self) -> None:
        self._classes: dict[str, type[BaseTool]] = {}
        self._instances: dict[str, BaseTool] = {}

    def register(self, tool_cls: type[BaseTool]) -> type[BaseTool]:
        if not getattr(tool_cls, "name", ""):
            raise ValueError(
                f"Tool class {tool_cls.__name__} missing class-attr 'name'"
            )
        if not isinstance(tool_cls.kind, ToolKind):
            raise ValueError(
                f"Tool class {tool_cls.__name__} has non-ToolKind 'kind'"
            )
        self._classes[tool_cls.name] = tool_cls
        return tool_cls

    def unregister(self, name: str) -> bool:
        removed = self._classes.pop(name, None) is not None
        self._instances.pop(name, None)
        return removed

    def get(self, name: str) -> BaseTool | None:
        if name in self._instances:
            return self._instances[name]
        cls = self._classes.get(name)
        if cls is None:
            return None
        inst = cls()
        self._instances[name] = inst
        return inst

    def names(self) -> list[str]:
        return sorted(self._classes.keys())

    def by_kind(self, kind: ToolKind) -> list[str]:
        return sorted(n for n, c in self._classes.items() if c.kind == kind)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._classes

    def __len__(self) -> int:
        return len(self._classes)

    def __iter__(self) -> Iterator[type[BaseTool]]:
        return iter(self._classes.values())

    @staticmethod
    def is_enabled() -> bool:
        """Master env gate for tools subsystem — default OFF (R27)."""
        return os.getenv("AGI_V8_TOOLS_ENABLED", "false").lower() == "true"  # tier: T2


# Process-wide default registry. Modules can opt-in via ``register_tool``.
_DEFAULT_REGISTRY = ToolRegistry()


def get_default_registry() -> ToolRegistry:
    """Return the process-wide default registry instance."""
    return _DEFAULT_REGISTRY


def register_tool(tool_cls: type[BaseTool]) -> type[BaseTool]:
    """Decorator / function that adds a tool class to the default registry."""
    return _DEFAULT_REGISTRY.register(tool_cls)
