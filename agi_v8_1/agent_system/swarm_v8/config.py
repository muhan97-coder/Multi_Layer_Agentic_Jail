"""SwarmConfig and env-based loader for swarm_v8."""
from __future__ import annotations

import os
from typing import Mapping

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


# __SLOT_W3A2__ Master env knob — single source of truth shared with
# ``agi_v8_1.core.activation._is_240_scaling_enabled``. Reading via the same
# env var keeps swarm_v8 and core/activation in lock-step.
_LANE_SCALING_240_ENV: str = "AGI_V8_LANE_SCALING_240_ENABLED"
_CODEGEN_624_PROFILE: dict[str, int] = {
    "scale_total": 624,
    "router": 24,
    "pod_a": 240,
    "pod_b": 240,
    "meta": 48,
    "si": 24,
    "digestive": 24,
    "handoff": 24,
}


class SwarmConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = False
    scale_total: int = 192
    pod_lane_count: int = 84
    meta_lane_count: int = 24  # 192-mode default; 240-mode load_config() override → 48 (__SLOT_M48_2026_06_01__)
    grid_task_count: int = 240
    partial_threshold: float = 0.20
    max_usd: float = 5.00
    tokens_per_grid_estimate: int = 2000
    usd_per_ktoken_estimate: float = 0.003
    meta_depth_multiplier: int = 2
    prune_data_leak: bool = True
    prune_multicollinearity: bool = True
    write_meta_decision: bool = False
    seed: int = 0
    queue_maxsize: int = 96
    # __SLOT_W3A2__ R26 master gate. Default False — 192-mode byte-identical.
    lane_scaling_240_enabled: bool = False
    # __SLOT_W3A2__ R26 cross-cutting lane budgets — default 0 = layer absent.
    router_lane_count: int = 0
    si_lane_count: int = 0
    digestive_lane_count: int = 0
    handoff_lane_count: int = 0
    # __SLOT_W3A2__ R26 per-layer task counts. Cross-cutting layers receive
    # synthetic tasks (not grid cells), so they have their own knob.
    router_task_count: int = 0
    si_task_count: int = 0
    digestive_task_count: int = 0
    handoff_task_count: int = 0

    @model_validator(mode="after")
    def _check_lane_totals(self) -> "SwarmConfig":
        if self.lane_scaling_240_enabled:
            # __SLOT_W3A2__ 240-mode arithmetic:
            # router + pod_a + pod_b + meta + si + digestive + handoff.
            # Default FULL is 240 lanes after the M48 upgrade. Seoul codegen
            # high-scale uses the explicit 624 profile
            # (240 + 240 + 48 + 24*4).
            expected = (
                self.router_lane_count
                + self.pod_lane_count * 2
                + self.meta_lane_count
                + self.si_lane_count
                + self.digestive_lane_count
                + self.handoff_lane_count
            )
            if expected != self.scale_total:
                raise ValueError(
                    f"240-mode total={expected} != scale_total={self.scale_total}"
                )
            # __SLOT_W3A2__ Canonical-source cross-check — the default shape
            # must match agi_v8_1.core.activation FULL budgets. The codegen
            # 624-lane profile is an explicit second SSOT profile rather than
            # arbitrary per-layer drift.
            from agi_v8_1.core.activation import (
                ActivationMode,
                CROSS_CUTTING_BUDGETS,
                LAYER_BUDGETS,
            )

            canonical_full = LAYER_BUDGETS[ActivationMode.FULL]
            cc_full = CROSS_CUTTING_BUDGETS[ActivationMode.FULL]
            active_profile = {
                "scale_total": self.scale_total,
                "router": self.router_lane_count,
                "pod_a": self.pod_lane_count,
                "pod_b": self.pod_lane_count,
                "meta": self.meta_lane_count,
                "si": self.si_lane_count,
                "digestive": self.digestive_lane_count,
                "handoff": self.handoff_lane_count,
            }
            canonical_profile = {
                "scale_total": (
                    canonical_full["router"]
                    + canonical_full["pod_a"]
                    + canonical_full["pod_b"]
                    + canonical_full["meta"]
                    + cc_full["si"]
                    + cc_full["digestive"]
                    + cc_full["handoff"]
                ),
                "router": canonical_full["router"],
                "pod_a": canonical_full["pod_a"],
                "pod_b": canonical_full["pod_b"],
                "meta": canonical_full["meta"],
                "si": cc_full["si"],
                "digestive": cc_full["digestive"],
                "handoff": cc_full["handoff"],
            }
            if active_profile not in (canonical_profile, _CODEGEN_624_PROFILE):
                raise ValueError(
                    "swarm_v8 SwarmConfig diverges from "
                    "agi_v8_1.core.activation canonical FULL budgets "
                    "or explicit codegen-624 profile"
                )
        else:
            # 192-mode legacy invariant — unchanged.
            expected = self.pod_lane_count * 2 + self.meta_lane_count
            if expected != self.scale_total:
                raise ValueError(
                    f"pod_lane_count*2 + meta_lane_count={expected} != scale_total={self.scale_total}"
                )
        return self

    @field_validator("partial_threshold")
    @classmethod
    def _check_threshold(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError(f"partial_threshold must be in [0,1], got {v}")
        return v

    @field_validator("max_usd")
    @classmethod
    def _check_max_usd(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(f"max_usd must be > 0, got {v}")
        return v

    @field_validator("grid_task_count")
    @classmethod
    def _check_grid_task_count(cls, v: int) -> int:
        if v <= 0:
            raise ValueError(f"grid_task_count must be > 0, got {v}")
        return v


def _bool(val: str) -> bool:
    return val.strip().lower() in ("1", "true", "yes")


def load_config(env: Mapping[str, str] | None = None) -> SwarmConfig:
    """Build SwarmConfig from environment (or supplied mapping)."""
    e = env if env is not None else os.environ
    # __SLOT_W3A2__ Read master 216 gate first — every other 216-only knob
    # picks up its default from this single decision so the env surface stays
    # in lock-step with ``agi_v8_1.core.activation``.
    enable_240 = _bool(e.get(_LANE_SCALING_240_ENV, "false"))  # tier: T4
    # __SLOT_M48_2026_06_01__ 240-mode upgraded to 240 (meta 24→48 = +24 lanes)
    default_scale = "240" if enable_240 else "192"
    default_layer = "12" if enable_240 else "0"
    default_task = "24" if enable_240 else "0"
    return SwarmConfig(
        enabled=_bool(e.get("AGI_V8_SWARM_ENABLED", "false")),  # tier: T4
        scale_total=int(e.get("AGI_V8_SWARM_SCALE", default_scale)),  # tier: T4
        pod_lane_count=int(e.get("AGI_V8_SWARM_POD_LANE_COUNT", "84" if not enable_240 else "72")),  # tier: T4
        meta_lane_count=int(e.get("AGI_V8_SWARM_META_LANE_COUNT", "48" if enable_240 else "24")),  # __SLOT_M48_2026_06_01__  # tier: T4
        grid_task_count=int(e.get("AGI_V8_SWARM_GRID_TASK_COUNT", "240")),  # tier: T4
        partial_threshold=float(e.get("AGI_V8_SWARM_PARTIAL_THRESHOLD", "0.20")),  # tier: T4
        max_usd=float(e.get("AGI_V8_SWARM_MAX_USD", "5.00")),  # tier: T4
        tokens_per_grid_estimate=int(e.get("AGI_V8_SWARM_TOKENS_PER_GRID_ESTIMATE", "2000")),  # tier: T4
        usd_per_ktoken_estimate=float(e.get("AGI_V8_SWARM_USD_PER_KTOKEN_ESTIMATE", "0.003")),  # tier: T4
        meta_depth_multiplier=int(e.get("AGI_V8_SWARM_META_DEPTH_MULTIPLIER", "2")),  # tier: T4
        prune_data_leak=_bool(e.get("AGI_V8_SWARM_PRUNE_DATA_LEAK", "true")),  # tier: T4
        prune_multicollinearity=_bool(e.get("AGI_V8_SWARM_PRUNE_MULTICOLLINEARITY", "true")),  # tier: T4
        write_meta_decision=_bool(e.get("AGI_V8_SWARM_WRITE_META_DECISION", "false")),  # tier: T4
        seed=int(e.get("AGI_V8_SWARM_SEED", "0")),  # tier: T4
        queue_maxsize=int(e.get("AGI_V8_SWARM_QUEUE_MAXSIZE", "96")),  # tier: T4
        # __SLOT_W3A2__ R26 — all default OFF when env knob not set.
        lane_scaling_240_enabled=enable_240,
        router_lane_count=int(e.get("AGI_V8_SWARM_ROUTER_LANE_COUNT", default_layer)),  # tier: T4
        si_lane_count=int(e.get("AGI_V8_SWARM_SI_LANE_COUNT", default_layer)),  # tier: T4
        digestive_lane_count=int(e.get("AGI_V8_SWARM_DIGESTIVE_LANE_COUNT", default_layer)),  # tier: T4
        handoff_lane_count=int(e.get("AGI_V8_SWARM_HANDOFF_LANE_COUNT", default_layer)),  # tier: T4
        router_task_count=int(e.get("AGI_V8_SWARM_ROUTER_TASK_COUNT", default_task)),  # tier: T4
        si_task_count=int(e.get("AGI_V8_SWARM_SI_TASK_COUNT", default_task)),  # tier: T4
        digestive_task_count=int(e.get("AGI_V8_SWARM_DIGESTIVE_TASK_COUNT", default_task)),  # tier: T4
        handoff_task_count=int(e.get("AGI_V8_SWARM_HANDOFF_TASK_COUNT", default_task)),  # tier: T4
    )
