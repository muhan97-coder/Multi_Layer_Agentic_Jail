"""Public read-only campaign registry contract, without T9 feeding policy.

Reading a registry does not register, launch, settle or feed a campaign. The
canonical state.store reader retains its existing missing/malformed-row behavior.
This module never discovers state roots, reads environment values, or writes.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from agi_v8_1.state.store import read_jsonl

ENV_ENABLED = "AGI_V8_GOAL_CAMPAIGN_TICK_FEED_ENABLED"
_REGISTRY_REL = ("goal_campaign", "campaign_registry.jsonl")


def registry_path(state_dir: "str | Path") -> Path:
    return Path(state_dir).joinpath(*_REGISTRY_REL)


def registry_rows(state_dir: "str | Path") -> "list[dict[str, Any]]":
    return read_jsonl(registry_path(state_dir))


__all__ = ["ENV_ENABLED", "registry_path", "registry_rows"]
