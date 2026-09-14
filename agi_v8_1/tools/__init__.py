"""agi_v8_1.tools — tool framework.

Ported from agi_v7.1/agent_system/tools/. R27 ships the public registry +
base classes + two reference tools (env_check, gen_inventory). Provider /
shell tools that need subprocess remain STUB until the approved-runtime
path is wired.

R27 invariants:
- env knobs OFF by default (AGI_V8_TOOLS_ENABLED=false)
- no subprocess/network at module top
"""

from agi_v8_1.tools.base import (
    BaseTool,
    ToolResult,
    ToolRegistry,
    ToolKind,
    register_tool,
    get_default_registry,
)
from agi_v8_1.tools.env_check import EnvCheckTool
from agi_v8_1.tools.gen_inventory import GenInventoryTool, generate_inventory

__all__ = [
    "BaseTool",
    "ToolResult",
    "ToolRegistry",
    "ToolKind",
    "register_tool",
    "get_default_registry",
    "EnvCheckTool",
    "GenInventoryTool",
    "generate_inventory",
]
