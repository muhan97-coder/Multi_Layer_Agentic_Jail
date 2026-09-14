"""GenInventoryTool — recursive file listing.

Ported from v7.1 tools/gen_inventory.py. Pure (filesystem-only) so no
approved-runtime gating needed.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from agi_v8_1.policy.fail_fast import format_exception_for_sink
from agi_v8_1.tools.base import BaseTool, ToolKind, ToolResult, register_tool

__all__ = ["GenInventoryTool", "generate_inventory"]


def generate_inventory(project_root: str | Path, output_file: str | Path) -> int:
    """Walk *project_root* and write file paths + sizes to *output_file*.

    Returns the number of files written.
    """
    root = Path(project_root)
    out_path = Path(output_file)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with out_path.open("w", encoding="utf-8") as out:
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            out.write(f"{path.relative_to(root)} {size}\n")
            count += 1
    return count


@register_tool
class GenInventoryTool(BaseTool):
    """Recursive file/size dump under a project root."""

    name: ClassVar[str] = "gen_inventory"
    kind: ClassVar[ToolKind] = ToolKind.PURE
    description: ClassVar[str] = "Write a recursive file/size inventory."

    def run(
        self,
        project_root: str | Path = ".",
        output_file: str | Path = "inventory.txt",
        **kwargs: Any,
    ) -> ToolResult:
        try:
            count = generate_inventory(project_root, output_file)
        except OSError as exc:
            return ToolResult(
                ok=False,
                payload={"project_root": str(project_root)},
                error=format_exception_for_sink(exc, max_chars=500),
            )
        return ToolResult(
            ok=True,
            payload={
                "project_root": str(project_root),
                "output_file": str(output_file),
                "file_count": count,
            },
        )
