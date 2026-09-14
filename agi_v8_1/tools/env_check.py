"""EnvCheckTool — agi_v8_1 module import probe.

v7.1 had a tiny script that did ``import agent_system`` + wrote OK to a fixed
path. v8 generalises to an importability probe wrapped in BaseTool. The
filesystem write is removed (no implicit side effects).

# R27: stub - provider/shell deferred to approved_runtime path
(but pure import probing is permitted because importlib is stdlib + side-effect
free for a well-behaved target module.)
"""
from __future__ import annotations

import importlib
from typing import Any, ClassVar

from agi_v8_1.policy.fail_fast import format_exception_for_sink
from agi_v8_1.tools.base import BaseTool, ToolKind, ToolResult, register_tool

__all__ = ["EnvCheckTool"]


@register_tool
class EnvCheckTool(BaseTool):
    """Try to import the requested module; report success/failure."""

    name: ClassVar[str] = "env_check"
    kind: ClassVar[ToolKind] = ToolKind.PURE
    description: ClassVar[str] = "Probe that a python module is importable."

    def run(self, module: str = "agi_v8_1", **kwargs: Any) -> ToolResult:
        try:
            mod = importlib.import_module(module)
        except Exception as exc:  # noqa: BLE001 - probe must not raise
            return ToolResult(
                ok=False,
                payload={"module": module},
                error=format_exception_for_sink(exc, max_chars=500),
            )
        return ToolResult(
            ok=True,
            payload={
                "module": module,
                "module_file": getattr(mod, "__file__", ""),
            },
        )
