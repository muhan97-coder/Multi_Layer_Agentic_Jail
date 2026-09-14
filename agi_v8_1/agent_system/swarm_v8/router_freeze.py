"""Router-freeze rule lane for swarm_v8 self_architect class tasks.

NOT a new router (there is no router.py — the 240-mode "router" LAYER in
``topology.assign_lanes`` is a DOWNSTREAM consumer of Pod A/B results, NOT
the lane-allocation dispatcher this module freezes). This module is a
freeze-rule lane installed on top of existing ``kernel`` / ``topology`` /
``cli`` dispatch. It rewrites the resolved :class:`SwarmConfig` to a
single-arm shape BEFORE ``build_topology(cfg)`` runs, so the kernel /
topology code paths stay byte-identical when the env gate is unset.

Round 2 evidence chain (3-round adversarial, cross-family band c):
  * v1 n=9 cross-family — NO_CONCLUSION (p=0.656, 12/18 usable lens)
  * v2 lens infra fix 18/18 usable — DS-only band c suggestive p=0.0156
    (6/6 single), cross-family NO_CONCLUSION (heuristic claude rubric
    artifact)
  * v3 free-form Claude restored — Cross-family CONFIRMED band c
    (p_one=0.0020, p_two=0.0039, n=9 non-tie, 9/9 single-wins,
    median_gap +0.116)

CAVEAT (carried per AP-10 / META-SI round 2 safety lever):
  * Spec-strict absolute agreement gate >=7/9 MISSED (4/9). Ties were
    rubric-floor artifacts (filemap 0.18-0.31 band, audit/s2 both-zero)
    NOT lens disagreement; cross-family agreement on non-tie comparable
    pairs is 4/5 (80%).
  * Default-OFF env gate ``AGI_V8_SWARM_SELF_ARCHITECT_FROZEN`` is the
    safety lever. Operator MUST opt in via "true" or "1".
  * Rule applies ONLY to self_architect class
    (audit / blueprint / filemap / readme / bridge_patch / roi_falsifier).
  * Codegen / grid tasks (worldcup v6.2 70.3% top-tier evidence,
    malaria GIS) are UNAFFECTED — classifier returns ``None`` for them
    and the guard short-circuits to no-op (AP-11).

Anti-pattern carry (V1 / V3 bridge + META-SI rounds 1+2):
  * AP-1 — NO mode enum / hardcoded if/elif task_class. ``TaskClass`` is a
    ``Literal[...]`` pin; classification walks a frozenset registry.
  * AP-2 — typed except only (this module has no try/except — nothing to
    catch; ledger writes are delegated to ``atomic_append_jsonl``).
  * AP-3 — env helper treats empty string / unset as False (no
    empty-string fall-through).
  * AP-4 — ``RouterRule`` is frozen + ``extra="forbid"`` + Literal-pinned
    ``schema_version``.
  * AP-5 — :func:`emit_router_freeze_event` reuses
    ``agi_v8_1.state.store.atomic_append_jsonl``; we do NOT re-implement.
  * AP-6 — SINGLE rule install (self_architect class only). Other classes
    deferred to follow-up rounds.
  * AP-7 — No edits to ``orchestrator_v8.py`` / ``runtime/cli.py`` /
    ``enforcement/*`` / ``observer/*`` / ``distill/paths.py`` /
    ``causal/__init__.py`` / ``verifier/*`` / ``agents/base.py`` /
    ``state/store.py`` / ``sia/harness_safety.py`` from this module.
  * AP-8 — pathlib parents resolution (no doubled ``agi_v8_1/`` prefix).
  * AP-9 — ``__init_subclass__`` guard rejects ``schema_version``
    redeclaration on :class:`RouterRule` subclasses.
  * AP-10 — Default-OFF env gate, strict match "true" / "1" only.
  * AP-11 — Codegen / grid lanes NOT affected. Classifier returns
    ``None`` for unmatched ``task_id`` → guard no-op.

Ledger path:
  * Gate OFF (default) — ``<this package>/router_freeze_ledger.jsonl``
    (``LEDGER_PATH``, a frozen public ``__all__`` export). Unchanged.
  * ``AGI_V8_SWARM_LEDGER_IN_STATE_DIR`` = "true" / "1" —
    ``${AGI_V8_STATE_DIR or AGI_STATE_DIR or <package>/state}/swarm_v8/
    router_freeze_ledger.jsonl``, resolved by :func:`resolve_ledger_path`
    and contained by ``state.path_guard.require_under``.
    Pre-gate rows are NOT migrated (operator decision) — new appends only.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict

# AP-5: reuse the R12 canonical append helper. NO local re-implementation.
from agi_v8_1.state.store import atomic_append_jsonl

from .config import SwarmConfig
from .schemas import TaskBundle


# AP-4: Literal-pinned schema_version on the rule model.
SCHEMA_VERSION = "agi_v8_router_freeze_v1"

# AP-10: env gate — default OFF. Strict match "true" / "1" only. Anything
# else (including empty string, "yes", "True"-cased differently, etc.) is
# treated as unset and the rule no-ops.
ENV_GATE = "AGI_V8_SWARM_SELF_ARCHITECT_FROZEN"

# Ledger path next to swarm_v8 module sources. FROZEN — this is a public
# ``__all__`` export and ``task_input_loader`` imports it by name; it also
# doubles as the gate-OFF default below. Do NOT change its value; the
# relocation is layered on top via :func:`resolve_swarm_ledger_path`.
LEDGER_PATH: Path = Path(__file__).resolve().parent / "router_freeze_ledger.jsonl"


# ---------------------------------------------------------------------------
# __SLOT_SWARM_LEDGER_STATE_DIR_2026_08_08__ Ledger destination resolver.
#
# The constant above (and ``external_change_detector.DEFAULT_LEDGER_PATH``)
# points INSIDE the source package, and no module in swarm_v8 referenced
# ``AGI_V8_STATE_DIR`` at all — so a live SI cycle appended runtime state
# into the source tree (67 KB / 14 rows as of 2026-08-08) no matter how the
# operator configured the state root. This gate moves new appends under the
# state root instead; it is default-OFF and OFF returns the *same object*
# (identity, not just equality), so the pre-gate behaviour is byte-identical.
#
# AP-10 mirror: strict "true" / "1", case-SENSITIVE. AP-3: empty string is
# UNSET, never a fall-through value.
# ---------------------------------------------------------------------------
ENV_LEDGER_IN_STATE_DIR = "AGI_V8_SWARM_LEDGER_IN_STATE_DIR"

# Single path component under the state root. Validated through
# ``state.path_guard.validate_slug`` at resolve time — no hand-rolled check.
LEDGER_STATE_SUBDIR = "swarm_v8"


def _state_root(env: Mapping[str, str] | None = None) -> Path:
    """Resolve the v8.1 state root.

    Mirrors ``enforcement/executor_log._resolve_log_path`` (:166) and
    ``bus/falsifier_bus.resolve_bus_path`` (:155): ``AGI_V8_STATE_DIR`` →
    ``AGI_STATE_DIR`` → ``<package>/state``.

    AP-3: an empty string is UNSET (fall through to the next key), NOT
    "use ''". ``~`` is expanded here so containment compares the same
    shape ``require_under`` produces for the target.
    """
    e = env if env is not None else os.environ
    for key in ("AGI_V8_STATE_DIR", "AGI_STATE_DIR"):
        raw = e.get(key, "")
        if raw != "":
            return Path(raw).expanduser()
    # AP-8: agent_system/swarm_v8/router_freeze.py -> <package>/state
    return Path(__file__).resolve().parents[2] / "state"


def resolve_swarm_ledger_path(
    filename: str,
    fallback: Path,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Shared destination resolver for swarm_v8 JSONL ledgers.

    Gate OFF (default) → *fallback* is returned unchanged (identity).
    Gate ON → ``<state_root>/swarm_v8/<filename>``.

    Containment is NOT re-implemented here: ``state.path_guard`` is the
    W2A1 single source of truth for ``validate_slug`` / ``require_under``,
    and a target that escapes the state root raises ``RuntimeError``.
    """
    e = env if env is not None else os.environ
    raw = e.get(ENV_LEDGER_IN_STATE_DIR, "")  # tier: T4
    # AP-3: empty string is NOT a fall-through default.
    if raw == "":
        return fallback
    # AP-10: case-SENSITIVE strict match. No .lower() / .strip().
    if raw not in ("true", "1"):
        return fallback
    # Lazy import — keeps the gate-OFF import graph byte-identical and keeps
    # ``state.store`` the only EAGER external production dependency of the
    # swarm_v8 island (SURGERY_PLAN:667). ``state/`` is already a COPY-required
    # boundary, so the island contract still holds when the gate is ON.
    from agi_v8_1.state.path_guard import (  # noqa: PLC0415
        require_under,
        validate_slug,
    )

    root = _state_root(e)
    candidate = (
        root
        / validate_slug(LEDGER_STATE_SUBDIR, what="swarm_v8 ledger subdir")
        / validate_slug(filename, what="swarm_v8 ledger filename")
    )
    return require_under(root, candidate, what="swarm_v8 ledger")


def resolve_ledger_path(env: Mapping[str, str] | None = None) -> Path:
    """Destination for the router_freeze ledger.

    See :func:`resolve_swarm_ledger_path`. Gate OFF → :data:`LEDGER_PATH`.
    """
    return resolve_swarm_ledger_path(LEDGER_PATH.name, LEDGER_PATH, env)


# AP-1: TaskClass = Literal pin, NOT an Enum / not a hardcoded if/elif.
# Codegen and grid task classes are listed for COMPLETENESS so the
# registry can express "this bundle is known and OUT OF SCOPE" without
# a special-case path.
TaskClass = Literal[
    "self_architect_audit",
    "self_architect_blueprint",
    "self_architect_filemap",
    "self_architect_readme",
    "self_architect_bridge_patch",
    "self_architect_roi_falsifier",
    "codegen",
    "grid",
    "unknown",
]


# Registry: classify a TaskBundle by ``task_id`` substring. The match is
# substring-on-lowered-task_id so suffixes (``_v1``, ``_single_v1``,
# ``_subset4_v1``) all hit the same class. ``classify`` walks the
# registry in insertion order and returns the first match.
#
# Self-architect class (IN SCOPE for the freeze rule):
SELF_ARCHITECT_PREFIXES: "dict[str, TaskClass]" = {
    "self_architect_audit": "self_architect_audit",
    "promotion_action_filemap": "self_architect_filemap",
    "world_creation_blueprint": "self_architect_blueprint",
    "readme_augmentation": "self_architect_readme",
    "bridge_patch": "self_architect_bridge_patch",
    "roi_falsifier": "self_architect_roi_falsifier",
}

# Codegen / grid class (OUT OF SCOPE — classifier still returns a
# class label so observability is preserved, but ``is_self_architect_class``
# returns False and the guard no-ops). AP-11: these MUST NOT be affected.
NON_SELF_ARCHITECT_PREFIXES: "dict[str, TaskClass]" = {
    "worldcup": "codegen",
    "malaria_gis": "grid",
    "malaria": "grid",
}

# Frozen union for the public is_self_architect_class() set membership.
SELF_ARCHITECT_CLASSES: frozenset[TaskClass] = frozenset(
    {
        "self_architect_audit",
        "self_architect_blueprint",
        "self_architect_filemap",
        "self_architect_readme",
        "self_architect_bridge_patch",
        "self_architect_roi_falsifier",
    }
)


class RouterRule(BaseModel):
    """Frozen router-freeze rule decision record.

    AP-4: ``ConfigDict(frozen=True, extra="forbid")`` + Literal-pinned
    ``schema_version``. AP-9: subclasses rejected via
    ``__init_subclass__``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["agi_v8_router_freeze_v1"] = "agi_v8_router_freeze_v1"
    task_class: TaskClass
    action: Literal["force_single_arm", "unchanged"]
    rationale: str

    # AP-9: __init_subclass__ guard — V1 SIA lesson. Refuse any subclass
    # that tries to redeclare ``schema_version`` (would silently bypass
    # the Literal pin via field-shadowing).
    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        own_annotations = cls.__dict__.get("__annotations__", {})
        if "schema_version" in own_annotations:
            raise TypeError(
                "RouterRule subclasses must not redeclare schema_version "
                "(AP-9 / AP-4 — Literal pin must hold across the hierarchy)"
            )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _env_gate_observed(env: Mapping[str, str] | None = None) -> bool:
    """AP-10: strict-match "true" / "1" only, case-sensitive.

    Empty string / unset → False (AP-3 — no empty-string fall-through).
    "TRUE" / "True" / "yes" / "on" → False (strict-match is a safety
    lever; case variants are operator typos that must NOT silently
    enable the freeze rule).
    """
    e = env if env is not None else os.environ
    raw = e.get(ENV_GATE, "")  # tier: T4
    # AP-3: empty string is NOT a fall-through default — return False directly.
    if raw == "":
        return False
    # AP-10: case-SENSITIVE strict match. No .lower() / .strip() — operator
    # must opt in with exactly "true" or "1".
    return raw in ("true", "1")


def classify(task_bundle: TaskBundle | None) -> TaskClass | None:
    """Classify a TaskBundle by ``task_id`` substring.

    AP-1: dict-walk, not if/elif on a mode enum.

    Returns ``None`` when ``task_bundle is None`` so the CLI guard can
    short-circuit without branching (preserves W3.A8 byte-identical
    behavior when ``--task-file`` is not supplied).
    """
    if task_bundle is None:
        return None
    task_id = task_bundle.task_id.lower()
    for prefix, cls in SELF_ARCHITECT_PREFIXES.items():
        if prefix in task_id:
            return cls
    for prefix, cls in NON_SELF_ARCHITECT_PREFIXES.items():
        if prefix in task_id:
            return cls
    return "unknown"


def is_self_architect_class(task_class: TaskClass | None) -> bool:
    """True iff ``task_class`` is one of the SELF_ARCHITECT_CLASSES.

    ``None`` (no bundle) → False. ``"unknown"`` / ``"codegen"`` /
    ``"grid"`` → False (AP-11: codegen / grid path preserved).
    """
    if task_class is None:
        return False
    return task_class in SELF_ARCHITECT_CLASSES


def is_self_architect_frozen(
    task_bundle: TaskBundle | None,
    env: Mapping[str, str] | None = None,
) -> bool:
    """Combined predicate: env gate ON AND bundle is self_architect class."""
    if not _env_gate_observed(env):
        return False
    return is_self_architect_class(classify(task_bundle))


def force_single_arm(cfg: SwarmConfig) -> SwarmConfig:
    """Return a new SwarmConfig clamped to single-arm shape.

    ``SwarmConfig`` is frozen (``ConfigDict(frozen=True, extra="forbid")``
    at config.py:17), so we use ``model_copy(update=...)``.

    Single-arm shape (passes both the 192-mode validator and disables
    240-mode):

      * ``pod_lane_count = 1``      — single arm per pod (A and B)
      * ``meta_lane_count = 0``     — no meta lanes
      * ``grid_task_count = 1``     — one grid cell
      * ``scale_total = 2``         — pod_lane_count*2 + meta_lane_count
      * ``lane_scaling_240_enabled = False`` — disable 240-mode validator
      * all cross-cutting lane / task counts zeroed (router / si /
        digestive / handoff)

    The 192-mode validator at config.py:92-95 checks
    ``pod_lane_count*2 + meta_lane_count == scale_total`` → 1*2 + 0 == 2. PASS.
    """
    return cfg.model_copy(
        update={
            "pod_lane_count": 1,
            "meta_lane_count": 0,
            "grid_task_count": 1,
            "scale_total": 2,
            "lane_scaling_240_enabled": False,
            "router_lane_count": 0,
            "si_lane_count": 0,
            "digestive_lane_count": 0,
            "handoff_lane_count": 0,
            "router_task_count": 0,
            "si_task_count": 0,
            "digestive_task_count": 0,
            "handoff_task_count": 0,
        }
    )


def apply_router_freeze(
    cfg: SwarmConfig,
    task_bundle: TaskBundle | None,
    env: Mapping[str, str] | None = None,
) -> "tuple[SwarmConfig, RouterRule]":
    """Apply the freeze rule to ``cfg`` given ``task_bundle`` + env.

    Returns ``(cfg_out, rule)``. When the env gate is OFF or the bundle
    is not self_architect class, ``cfg_out is cfg`` (identity — no copy)
    and ``rule.action == "unchanged"``. AP-11: codegen / grid bundles
    short-circuit here.
    """
    task_class = classify(task_bundle)
    if not _env_gate_observed(env):
        return cfg, RouterRule(
            task_class=task_class or "unknown",
            action="unchanged",
            rationale=f"env gate {ENV_GATE} unset/non-strict — rule no-op",
        )
    if not is_self_architect_class(task_class):
        return cfg, RouterRule(
            task_class=task_class or "unknown",
            action="unchanged",
            rationale=(
                "task_class not in self_architect scope — codegen / grid "
                "path preserved (AP-11)"
            ),
        )
    return force_single_arm(cfg), RouterRule(
        task_class=task_class,  # type: ignore[arg-type]  # narrowed above
        action="force_single_arm",
        rationale=(
            "self_architect class + env gate ON — cross-family band c "
            "v3 CONFIRMED (p_one=0.0020, p_two=0.0039, n=9 non-tie). "
            "CAVEAT: spec-strict >=7/9 absolute-agreement gate MISSED "
            "(4/9 ties were rubric-floor artifacts)."
        ),
    )


def emit_router_freeze_event(
    rule: RouterRule,
    task_bundle: TaskBundle | None,
    cfg_before: SwarmConfig,
    cfg_after: SwarmConfig,
    env: Mapping[str, str] | None = None,
    ledger_path: Path | str | None = None,
) -> None:
    """Append a router_freeze decision entry to the JSONL ledger.

    AP-5: reuses :func:`agi_v8_1.state.store.atomic_append_jsonl`. No local
    file write logic.
    """
    # Explicit ``ledger_path`` still wins (that is how tests pin tmp_path);
    # only the DEFAULT is routed through the resolver.
    target = Path(ledger_path) if ledger_path is not None else resolve_ledger_path(env)
    entry: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "ts": time.time(),
        "task_id": task_bundle.task_id if task_bundle is not None else None,
        "task_class": rule.task_class,
        "env_gate_observed": _env_gate_observed(env),
        "decision": rule.action,
        "rationale": rule.rationale,
        "cfg_before": cfg_before.model_dump(),
        "cfg_after": cfg_after.model_dump(),
        "caveat_4_of_9": (
            "Spec-strict >=7/9 absolute-agreement gate MISSED (4/9). "
            "Ties = rubric-floor artifacts, not lens disagreement. "
            "Default-OFF env gate is the safety lever."
        ),
    }
    atomic_append_jsonl(target, entry)


# ---------------------------------------------------------------------------
# __SLOT_LEDGER_RANK3_2026_05_30__ External change-detect hook (Rank 3 revival)
#
# When AGI_V8_EXTERNAL_CHANGE_DETECT_ENABLED is set ("true"/"1"), this hook
# fingerprints TaskBundle JSON files + swarm_v8 internals between cycles and
# appends a ChangeReport entry to the router_freeze ledger (AP-5 reuse). The
# detector module is intentionally imported lazily so the import graph stays
# byte-identical when the gate is OFF (Rank-3 is the META-LAYER guarding
# Rank 1 / Rank 2 ledger revivals — see external_change_detector.py docstring).
# ---------------------------------------------------------------------------


def run_external_change_detect_hook(
    env: Mapping[str, str] | None = None,
    *,
    ledger_path: Path | str | None = None,
) -> "Any | None":
    """Default-OFF wrapper around ExternalChangeDetector.run_change_detect_cycle.

    When env gate AGI_V8_EXTERNAL_CHANGE_DETECT_ENABLED is unset/non-strict,
    returns None without importing the detector module (lazy import keeps
    the gate-OFF code path byte-identical).
    """
    e = env if env is not None else os.environ
    raw = e.get("AGI_V8_EXTERNAL_CHANGE_DETECT_ENABLED", "")
    if raw == "" or raw not in ("true", "1"):
        return None
    # Lazy import — only paid when gate is ON.
    from .external_change_detector import run_change_detect_cycle

    target = Path(ledger_path) if ledger_path is not None else resolve_ledger_path(e)
    return run_change_detect_cycle(env=e, ledger_path=target)


__all__ = [
    "SCHEMA_VERSION",
    "ENV_GATE",
    "ENV_LEDGER_IN_STATE_DIR",
    "LEDGER_PATH",
    "LEDGER_STATE_SUBDIR",
    "resolve_swarm_ledger_path",
    "resolve_ledger_path",
    "TaskClass",
    "SELF_ARCHITECT_CLASSES",
    "SELF_ARCHITECT_PREFIXES",
    "NON_SELF_ARCHITECT_PREFIXES",
    "RouterRule",
    "classify",
    "is_self_architect_class",
    "is_self_architect_frozen",
    "force_single_arm",
    "apply_router_freeze",
    "emit_router_freeze_event",
    "run_external_change_detect_hook",
]
