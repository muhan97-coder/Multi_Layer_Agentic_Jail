"""V8 activation ladder — 6-mode router/pod/meta/SI lane allocation.

Ported from agi_v7.1/agent_system/swarm/v2/activation.py and extended for V8.

V2 baseline:  180 = router(12) + pod_a(72) + pod_b(72) + meta(24)
V8 extension: 192 = 180 + si(12)   — SI layer only activates in ``full`` mode.

R26 extension (opt-in via ``AGI_V8_LANE_SCALING_240_ENABLED=true``):
    base 180 (router+pods+meta) + 36 cross-cutting (SI 12 + digestive 12 +
    handoff 12) = 216 in ``full`` mode. Each cross-cutting layer scales
    proportionally across the 6 modes — see :data:`CROSS_CUTTING_BUDGETS`.
    Default OFF: ``ActivationDecision.full.active_agent_budget`` remains 192
    so all R17-R21 tests stay green. ``digestive_lanes``/``handoff_lanes``
    fields default to 0 for legacy callers.

This module is advisory-only: no provider calls, no shell, no apply. It only
returns budgets that downstream code may use to build a manifest. The R18.5
invariant test asserts no ``subprocess``/``os.system``/network imports here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Mapping


class ActivationMode(StrEnum):
    """Six-rung activation ladder.

    Each mode names a deterministic ``active_agent_budget`` (see
    :data:`MODE_ACTIVE_AGENTS`). Modes are ordered scout → full; ``full`` is
    the only mode that activates the 12 SI lanes.
    """

    SCOUT = "scout"
    LEAN = "lean"
    STANDARD = "standard"
    HIGH = "high"
    DUAL_POD = "dual_pod"
    FULL = "full"


MODE_ORDER: Final[tuple[ActivationMode, ...]] = (
    ActivationMode.SCOUT,
    ActivationMode.LEAN,
    ActivationMode.STANDARD,
    ActivationMode.HIGH,
    ActivationMode.DUAL_POD,
    ActivationMode.FULL,
)


MODE_ACTIVE_AGENTS: Final[Mapping[ActivationMode, int]] = {
    ActivationMode.SCOUT: 24,
    ActivationMode.LEAN: 48,
    ActivationMode.STANDARD: 72,
    ActivationMode.HIGH: 96,
    ActivationMode.DUAL_POD: 144,
    ActivationMode.FULL: 216,  # __R18_5_SLOT_FULL__ 180 v2 baseline + 12 SI + 24 meta (M48 upgrade 2026-06-01)
}


# Per-mode (router, pod_a, pod_b, meta, si) lane allocations. Each row sums to
# its ``MODE_ACTIVE_AGENTS`` value. Modes below ``full`` preserve the v2
# baseline ratio (router 12/180, pod_a 72/180, pod_b 72/180, meta 24/180,
# si 0 — SI is the apex-only enhancement).
LAYER_BUDGETS: Final[Mapping[ActivationMode, Mapping[str, int]]] = {
    # scout: router stays at the v2 minimum (12); pod/meta scaled to 12 budget
    ActivationMode.SCOUT: {"router": 12, "pod_a": 6, "pod_b": 4, "meta": 2, "si": 0},
    ActivationMode.LEAN: {"router": 12, "pod_a": 18, "pod_b": 14, "meta": 4, "si": 0},
    ActivationMode.STANDARD: {"router": 12, "pod_a": 30, "pod_b": 22, "meta": 8, "si": 0},
    ActivationMode.HIGH: {"router": 12, "pod_a": 42, "pod_b": 30, "meta": 12, "si": 0},
    # dual_pod: pod_a == pod_b (independent comparison)
    ActivationMode.DUAL_POD: {"router": 12, "pod_a": 60, "pod_b": 60, "meta": 12, "si": 0},
    # full: 180 v2 baseline + 12 SI lanes + 24 extra meta (M48 upgrade 2026-06-01)
    # meta 24→48 doubles cross-pod reasoning surface for hybrid-comm Stage 4 (Meta Reconcile)
    ActivationMode.FULL: {"router": 12, "pod_a": 72, "pod_b": 72, "meta": 48, "si": 12},
}


# Sanity check at import time. If anyone tweaks the table by hand and breaks
# the 192 invariant, surface it immediately.
for _mode, _budget in LAYER_BUDGETS.items():
    if sum(_budget.values()) != MODE_ACTIVE_AGENTS[_mode]:
        raise AssertionError(
            f"LAYER_BUDGETS[{_mode!r}] sums to {sum(_budget.values())}, "
            f"expected {MODE_ACTIVE_AGENTS[_mode]}"
        )


# __R26_SLOT__ — 3-way matrix (SI / digestive / hand-off), each scales identically.
# Each layer in full mode = 12 lanes split (Pod A 4 / Pod B 4 / Meta 2 / Router 1 /
# SI base 1 / Global 0). Per-mode totals are: scout 1, lean 3, standard 4, high 6,
# dual_pod 9, full 12.
CROSS_CUTTING_TOTALS: Final[Mapping[ActivationMode, int]] = {
    ActivationMode.SCOUT: 1,
    ActivationMode.LEAN: 3,
    ActivationMode.STANDARD: 4,
    ActivationMode.HIGH: 6,
    ActivationMode.DUAL_POD: 9,
    ActivationMode.FULL: 12,
}

# Each cross-cutting layer (SI / digestive / hand-off) shares the same per-mode
# total. So total cross-cutting in full = 36 = 12 * 3.
CROSS_CUTTING_BUDGETS: Final[Mapping[ActivationMode, Mapping[str, int]]] = {
    mode: {"si": total, "digestive": total, "handoff": total}
    for mode, total in CROSS_CUTTING_TOTALS.items()
}

# Canonical 3-way matrix shape — used by manifest builder + tests.
# Pod A / Pod B / Meta / Router / SI base / Global = 4 / 4 / 2 / 1 / 1 / 0 = 12.
THREE_WAY_MATRIX: Final[Mapping[str, int]] = {
    "pod_a": 4,
    "pod_b": 4,
    "meta": 2,
    "router": 1,
    "si_base": 1,
    "global": 0,
}
assert sum(THREE_WAY_MATRIX.values()) == 12, "3-way matrix must sum to 12"


def _is_240_scaling_enabled() -> bool:
    """R26 opt-in env knob. Default OFF — keeps R17-R21 (1425 tests) green.

    When OFF, ``ActivationDecision.full.active_agent_budget`` stays 192 and
    ``digestive_lanes``/``handoff_lanes`` default to 0.
    """

    return os.getenv("AGI_V8_LANE_SCALING_240_ENABLED", "false").strip().lower() == "true"


@dataclass(frozen=True, slots=True)
class ActivationDecision:
    """Result of :func:`select_plan_activation`.

    Carries the chosen mode, the total ``active_agent_budget``, and a per-layer
    lane allocation (router/pod_a/pod_b/meta/si). The lane fields are an
    additive enhancement over v2 — v2 itself stops at meta_review.

    R26 additive fields ``digestive_lanes`` / ``handoff_lanes`` default to 0
    so legacy R17-R21 callers (which only set router/pod/meta/si) keep the
    lane-sum invariant ``router + pod_a + pod_b + meta + si == budget``.
    """

    mode: ActivationMode
    active_agent_budget: int
    reason: str
    router_lanes: int
    pod_a_lanes: int
    pod_b_lanes: int
    meta_lanes: int
    si_lanes: int
    # __R26_SLOT__ — additive, default 0 to preserve R18.5 lane-sum invariant.
    digestive_lanes: int = 0
    handoff_lanes: int = 0


def _budget_for(mode: ActivationMode, reason: str) -> ActivationDecision:
    budget = LAYER_BUDGETS[mode]
    si_lanes = budget["si"]
    digestive_lanes = 0
    handoff_lanes = 0
    total_budget = MODE_ACTIVE_AGENTS[mode]

    if _is_240_scaling_enabled():
        # R26 216-lane scaling — promote SI to cross-cutting and add digestive
        # + handoff layers. base = router + pod_a + pod_b + meta (= 180 in full).
        cc = CROSS_CUTTING_BUDGETS[mode]
        si_lanes = cc["si"]
        digestive_lanes = cc["digestive"]
        handoff_lanes = cc["handoff"]
        base = budget["router"] + budget["pod_a"] + budget["pod_b"] + budget["meta"]
        total_budget = base + si_lanes + digestive_lanes + handoff_lanes

    return ActivationDecision(
        mode=mode,
        active_agent_budget=total_budget,
        reason=reason,
        router_lanes=budget["router"],
        pod_a_lanes=budget["pod_a"],
        pod_b_lanes=budget["pod_b"],
        meta_lanes=budget["meta"],
        si_lanes=si_lanes,
        digestive_lanes=digestive_lanes,
        handoff_lanes=handoff_lanes,
    )


def select_plan_activation(
    *,
    estimated_task_count: int,
    risk_score: float,
    disagreement_score: float,
    explicit_mode: ActivationMode | None = None,
) -> ActivationDecision:
    """Deterministically choose an activation mode.

    Rules (applied in order):
      1. ``explicit_mode`` wins if provided.
      2. Both ``risk_score > 0.7`` AND ``disagreement_score > 0.5`` → ``full``.
      3. ``disagreement_score > 0.5`` → ``dual_pod``.
      4. ``risk_score > 0.7`` → ``high``.
      5. ``estimated_task_count < 10`` → ``scout``.
      6. ``estimated_task_count < 30`` → ``lean``.
      7. ``estimated_task_count < 60`` → ``standard``.
      8. Otherwise → ``high``.
    """

    if explicit_mode is not None:
        return _budget_for(explicit_mode, reason="explicit_mode")

    high_risk = float(risk_score) > 0.7
    high_disagreement = float(disagreement_score) > 0.5

    if high_risk and high_disagreement:
        return _budget_for(ActivationMode.FULL, reason="risk+disagreement→full")
    if high_disagreement:
        return _budget_for(ActivationMode.DUAL_POD, reason="disagreement→dual_pod")
    if high_risk:
        return _budget_for(ActivationMode.HIGH, reason="risk→high")

    tasks = int(estimated_task_count)
    if tasks < 10:
        return _budget_for(ActivationMode.SCOUT, reason=f"tasks<10:{tasks}")
    if tasks < 30:
        return _budget_for(ActivationMode.LEAN, reason=f"tasks<30:{tasks}")
    if tasks < 60:
        return _budget_for(ActivationMode.STANDARD, reason=f"tasks<60:{tasks}")
    return _budget_for(ActivationMode.HIGH, reason=f"tasks>=60:{tasks}")


__all__ = [
    "ActivationMode",
    "ActivationDecision",
    "LAYER_BUDGETS",
    "MODE_ACTIVE_AGENTS",
    "MODE_ORDER",
    "CROSS_CUTTING_BUDGETS",
    "CROSS_CUTTING_TOTALS",
    "THREE_WAY_MATRIX",
    "select_plan_activation",
]
