"""R15 6-axis evidence extraction and per-axis voting.

Reads lane finding blocks (audit-shape or generation-shape) and produces
per-axis evidence tuples. The six axes:

  correctness:    behavioral defects, incorrect/wrong/bug findings
  safety:         red_flags, secret/token/escape/protected findings
  cost:           performance/latency/O(n^2)/hot loop findings
  maintainability: duplication/complexity/refactor/DRY findings
  novelty:        findings not present in opposite pod (diff signal)
  pareto:         intersection across correctness ∩ cost ∩ maintainability

This is the *signal-only* layer (R15 Phase 3a). The sandbox-grounded
layer (Lean 4 + Docker compiler telemetry) is R16 Phase 3b.

Gated behind ``AGI_V8_SWARM_AXIS_VOTE_ENABLED`` env knob; default false.
When disabled the surrounding controller code keeps the R14.5.h
single-boolean collapse path (byte-stable fallback).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Iterable, Mapping

# __SLOT_FAIL_FAST_2026_07_25__ Swallowed failures route through one choke
# point: counted + named always, re-raised under AGI_V8_STRICT_FAIL_FAST.
from agi_v8_1.policy.fail_fast import (
    safe_exception_type_name as _safe_exception_type_name,
    swallowed as _swallowed,
)

if TYPE_CHECKING:
    try:
        from agi_v8_1.core.sandbox_runner import SandboxTelemetry
    except ImportError:
        pass  # SandboxTelemetry not yet available; type hint only


AXIS_KEYS: tuple[str, ...] = (
    "correctness",
    "safety",
    "cost",
    "maintainability",
    "novelty",
    "pareto",
)

# __R22_SLOT__ 7th axis "causality" — gated by AGI_V8_CAUSALITY_AXIS_ENABLED.
# When the env is OFF (default) this constant exists but vote_per_axis ignores
# it, preserving the 6-axis vote_count for R17-R21 backward compat.
AXIS_CAUSALITY: "str" = "causality"


def _causality_axis_enabled() -> bool:
    """R22: 7th axis gate. Default OFF — axis_vote_count stays 6."""
    return os.getenv("AGI_V8_CAUSALITY_AXIS_ENABLED", "false").strip().lower() == "true"


# __SLOT_B1_NUMERIC_AXIS_EVIDENCE_2026_08_07__ swarm 의 **숫자** 축 신호를 증거로.
#
# ## 왜 (2026-08-07 실측)
#
# 실 deepseek swarm 을 돌려보니 레인은 진짜 발견을 냈는데 SI 투표엔 축증거가 0 이었다.
# 원인: :func:`extract_axis_evidence` 는 블록의 **텍스트를 키워드 스캔**하는데,
# ``bridge/sanitize.py`` 가 만드는 텍스트는 회계뿐이다::
#
#     findings: "swarm pod A: 240/240 lanes ok, 0 pruned, 0 aggregated weight keys"
#
# 축 키워드가 하나도 없다. 정작 쓸모 있는 레인 텍스트는 sanitize 가 버린다 —
# 그리고 **그건 고쳐선 안 되는 보안 계약이다**(raw provider 텍스트가 apply 경로에
# 닿지 않게 하는 것이 목적).
#
# ⇒ 그래서 텍스트가 아니라 **이미 화이트리스트된 수치 채널**을 읽는다. 블록은
# ``aggregated_weights`` 를 이미 싣고 있다. LLM 텍스트는 여전히 한 글자도 안 들어온다.
#
# ## 숫자를 어떻게 토큰으로 바꾸나
#
# ``AxisEvidence`` 는 토큰 튜플이고 :func:`vote_per_axis` 는 pod A/B 토큰 **집합의
# 겹침**으로 agree/disagree 를 정한다. 그래서 연속값을 **거친 밴드**로 양자화한다::
#
#     0.0 ≤ lo < 0.34 ≤ mid < 0.67 ≤ hi ≤ 1.0
#
# 이러면 미세한 수치 잡음(0.425 vs 0.444 → 둘 다 ``mid``)은 **불일치로 세지 않고**,
# 진짜 발산(0.2 vs 0.9 → ``lo`` vs ``hi``)만 disagree 가 된다. 🔑 밴드가 거칠어야
# "값이 다르다"와 "판단이 다르다"가 갈린다.
#
# ⚠️ ``novelty``/``pareto`` 는 여기서 안 만든다 — 그 둘은 pod 교차비교 산물이라
# 레인이 점수를 매길 대상이 아니다(그래서 :data:`SWARM_SCORED_AXES` 는 4개다).
_NUMERIC_AXIS_ENV = "AGI_V8_SI_SWARM_NUMERIC_AXIS_ENABLED"

#: swarm 레인이 실제로 점수 매기는 축. ``TaskBundle.variables`` 로 넘겨야 한다 —
#: 안 넘기면 pool 이 GIS ``_WEIGHT_POOL`` 로 폴백해 **가중치가 전부 버려진다**
#: (2026-08-07 프로브에서 이걸로 한 번 헛돈 씀).
SWARM_SCORED_AXES: tuple[str, ...] = (
    "correctness",
    "safety",
    "cost",
    "maintainability",
)

_BAND_LO_MAX = 0.34
_BAND_MID_MAX = 0.67

#: 경계에서 이만큼 안쪽이면 **양쪽 밴드를 다 낸다.**
#:
#: 🔴 2026-08-07 첫 라이브가 잡은 설계 결함: pod A safety=0.65 / pod B safety=0.725 는
#: 차이가 0.075 뿐인데 경계(0.67)를 사이에 둬서 ``mid`` vs ``hi`` = **disagree** 가 됐다.
#: 밴드를 거칠게 만든 목적이 정확히 그걸 막는 거였는데, 딱딱한 경계가 경계 근처에서
#: 그 목적을 뒤집었다. 겹치는 토큰을 하나 더 내면 :func:`vote_per_axis` 의 집합 교집합이
#: 이를 agree 로 본다. 진짜 발산(0.2 vs 0.9)은 여전히 겹치지 않아 disagree 다.
_BAND_EDGE_TOL = 0.06

# __SLOT_B1_BAND_GAP_GUARD_2026_08_10__ raw 격차 임계 — 밴드가 겹쳐도 격차가 크면 disagree.
#
# 🔴 2026-08-07 라이브 실측(두 번째 결함): maintainability pod A .696 vs pod B .388 —
# 격차 **0.31** — 이 agree 로 세어졌다. 위의 :data:`_BAND_EDGE_TOL` 이 비교 **양쪽에**
# 적용되니 A 밴드 {hi,mid} ∩ B 밴드 {lo,mid} = {mid}. 최악의 경우 격차
# 밴드폭+2·tol = 0.45 까지 agree 가 된다(0.28 vs 0.73). 경계 오탐을 막으려던 관용이
# agree 구역을 밴드 설계 의도보다 크게 넓혔다.
#
# ⚠️ "관용을 한쪽에만" (TODO 대안 1)은 이 사례를 **못 고친다**: B .388 의 순수 밴드가
# 이미 mid 라서, A 쪽 관용({hi,mid})만으로 교집합이 생긴다. 그래서 대안 2 를 쓴다 —
# 밴드 판정과 **별개로 raw 격차**를 본다: 두 pod 모두 수치를 냈고 |a−b| ≥ 임계면
# 밴드가 겹쳐도 disagree.
#
# 기본 임계 0.20 의 근거:
#   * 관용이 보호하도록 설계된 쌍(같은 경계 양쪽 ±tol)의 최대 격차 = 2·tol = 0.12 —
#     임계가 이보다 커야 보호가 유지된다(0.65 vs 0.725, 격차 .075 → agree 유지).
#   * 라이브 발산(.308, .3475)과 라이브 잡음(.059, .075) 사이 대략 중앙.
#   * 이 값이면 08-07 라이브 4축이 손 계산과 같은 2/4 disagree 가 된다
#     (correctness .3475, maintainability .308 → disagree / safety .059, cost .162 → agree).
#
# 게이트 default OFF — OFF 면 byte-identical. 수치 축 게이트(_NUMERIC_AXIS_ENV)가
# OFF 면 raw 값 자체가 없어 guard 는 자연히 no-op 다(키워드 토큰엔 격차 개념이 없다).
_BAND_GAP_GUARD_ENV = "AGI_V8_SI_SWARM_BAND_GAP_GUARD_ENABLED"
_BAND_GAP_THRESHOLD_ENV = "AGI_V8_SI_SWARM_BAND_GAP_THRESHOLD"
_BAND_GAP_THRESHOLD_DEFAULT = 0.20


def _numeric_axis_enabled() -> bool:
    """B1 수치 축증거 게이트. Default OFF — 꺼지면 byte-identical."""
    return os.getenv(_NUMERIC_AXIS_ENV, "false").strip().lower() in ("true", "1")  # tier: T4


def _band_gap_guard_enabled() -> bool:
    """B1 raw 격차 guard 게이트. Default OFF — 꺼지면 byte-identical."""
    return os.getenv(_BAND_GAP_GUARD_ENV, "false").strip().lower() in ("true", "1")  # tier: T4


def _band_gap_threshold() -> float:
    """raw 격차 disagree 임계 (env 조정 가능, 하드코딩 금지 — rl_config_first).

    파싱 실패/음수/비유한값은 조용히 0 으로 접지 않고 이름 붙여 세운 뒤
    기본값으로 돌아간다.
    """
    raw = os.getenv(_BAND_GAP_THRESHOLD_ENV, "")  # tier: T4
    if not raw.strip():
        return _BAND_GAP_THRESHOLD_DEFAULT
    try:
        v = float(raw.strip())
    except ValueError as _ff_exc:
        _swallowed(_ff_exc, site="core.axis_scorer._band_gap_threshold:parse",
                   category="telemetry")
        return _BAND_GAP_THRESHOLD_DEFAULT
    if not (v == v) or v in (float("inf"), float("-inf")) or v < 0.0:
        _swallowed(ValueError(f"band gap threshold out of range: {v!r}"),
                   site="core.axis_scorer._band_gap_threshold:range",
                   category="telemetry")
        return _BAND_GAP_THRESHOLD_DEFAULT
    return v


def _bands(value: float) -> tuple[str, ...]:
    """값 → 밴드 이름들. 경계 근처면 인접 밴드를 함께 낸다(위 주석 참조)."""
    if value < _BAND_LO_MAX:
        out = ["lo"]
    elif value < _BAND_MID_MAX:
        out = ["mid"]
    else:
        out = ["hi"]
    if abs(value - _BAND_LO_MAX) <= _BAND_EDGE_TOL:
        out += ["lo", "mid"]
    if abs(value - _BAND_MID_MAX) <= _BAND_EDGE_TOL:
        out += ["mid", "hi"]
    return tuple(dict.fromkeys(out))


def _numeric_axis_values(finding_block: "Mapping[str, Any]") -> dict[str, float]:
    """블록의 ``aggregated_weights`` → 축별 raw 값(0..1 클램프).

    키가 없거나 값이 수치가 아니면 **그 축은 비운다**(조용히 0.0 으로 접지 않는다 —
    "모른다"와 "0 이다"는 다른 사실이다).
    """
    out: dict[str, float] = {}
    if not isinstance(finding_block, Mapping):
        return out
    weights = finding_block.get("aggregated_weights")
    if not isinstance(weights, Mapping):
        return out
    for axis in SWARM_SCORED_AXES:
        raw = weights.get(axis)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            continue
        out[axis] = max(0.0, min(1.0, float(raw)))
    return out


def _numeric_axis_tokens(finding_block: "Mapping[str, Any]") -> dict[str, tuple[str, ...]]:
    """블록의 ``aggregated_weights`` → 축별 밴드 토큰 (raw 값 규약은 위 함수 참조)."""
    return {
        axis: tuple(f"{axis}:{b}" for b in _bands(v))
        for axis, v in _numeric_axis_values(finding_block).items()
    }


# Keyword classifiers per axis. Tuned conservatively to avoid over-classification.
# Each entry is a substring match against the lower-cased text. Substrings (not
# whole words) so we catch morphological variants ("duplicat" -> duplicate /
# duplication / duplicated). The ordering is stable so the resulting tuples are
# deterministic.
_CORRECTNESS_KEYWORDS: tuple[str, ...] = (
    "incorrect",
    "wrong",
    "bug",
    "violates",
    "contract",
    "broken",
    "fail",
    "race",
    "deadlock",
    "off-by-one",
    "off by one",
)
_SAFETY_KEYWORDS: tuple[str, ...] = (
    "secret",
    "token",
    "credential",
    "escape",
    "traversal",
    "injection",
    "auth",
    "permission",
    "protected_root",
    "leak",
)
_COST_KEYWORDS: tuple[str, ...] = (
    "performance",
    "latency",
    "slow",
    "o(n^2)",
    "o(n2)",
    "hot loop",
    "inefficient",
    "expensive",
    "timeout",
    "throttl",
)
_MAINTAINABILITY_KEYWORDS: tuple[str, ...] = (
    "duplicat",
    "complex",
    "refactor",
    "dry",
    "magic number",
    "naming",
    "dead code",
    "unused",
)


@dataclass(frozen=True)
class AxisEvidence:
    """Per-axis matched-token evidence for a single pod's finding block.

    Each field is a tuple of matched keyword tokens (lowercase, stable order).
    Empty tuple = no signal extracted on that axis. ``novelty`` is populated by
    :func:`vote_per_axis` (pod-pair diff), not by :func:`extract_axis_evidence`
    (single-pod scan). ``pareto`` is the intersection of correctness ∩ cost ∩
    maintainability and is also computed by :func:`vote_per_axis`.
    """

    correctness: tuple[str, ...] = ()
    safety: tuple[str, ...] = ()
    cost: tuple[str, ...] = ()
    maintainability: tuple[str, ...] = ()
    novelty: tuple[str, ...] = ()
    pareto: tuple[str, ...] = ()
    # __R22_SLOT__ 7th axis — sequence of causal chain node ids. Optional,
    # default empty. When the AGI_V8_CAUSALITY_AXIS_ENABLED env knob is OFF
    # (default) this field is ignored by vote_per_axis so the 6-axis vote
    # count is preserved (R17-R21 backward compat).
    causal_chain: tuple[str, ...] = ()
    # __SLOT_B1_BAND_GAP_GUARD_2026_08_10__ 축별 raw 수치 ((axis, value) 쌍, 정렬).
    # _BAND_GAP_GUARD_ENV **와** _NUMERIC_AXIS_ENV 가 둘 다 ON 일 때만 채워진다.
    # AXIS_KEYS 밖이므로 as_dict()/is_empty()/axis_evidence_total 집계에 안 잡힌다 —
    # vote_per_axis 의 raw 격차 guard 전용 채널.
    numeric_raw: tuple[tuple[str, float], ...] = ()

    def as_dict(self) -> dict[str, tuple[str, ...]]:
        base = {k: getattr(self, k) for k in AXIS_KEYS}
        if _causality_axis_enabled():
            base[AXIS_CAUSALITY] = self.causal_chain
        return base

    def is_empty(self) -> bool:
        if any(getattr(self, k) for k in AXIS_KEYS):
            return False
        if _causality_axis_enabled() and self.causal_chain:
            return False
        return True


def _scan_keywords(text: str, keywords: Iterable[str]) -> tuple[str, ...]:
    """Return matched keyword tokens (lower-cased substring scan).

    Stable order: the order of ``keywords`` is preserved; duplicates are
    de-duped while keeping the first occurrence.
    """

    if not text:
        return ()
    lower = text.lower()
    matched: list[str] = []
    seen: set[str] = set()
    for kw in keywords:
        if kw and kw in lower and kw not in seen:
            matched.append(kw)
            seen.add(kw)
    return tuple(matched)


def _coerce_text(value: Any) -> str:
    """Best-effort text coercion from heterogeneous finding shapes.

    Findings may be: plain strings, dicts with ``text``/``message``/``detail``
    keys, or nested lists. We flatten into a single space-joined string.
    """

    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        parts: list[str] = []
        for k in ("text", "message", "detail", "description", "severity", "kind", "axis"):
            v = value.get(k)
            if isinstance(v, str):
                parts.append(v)
        # Also recurse into nested values like {"finding": {...}}.
        for v in value.values():
            if isinstance(v, (Mapping, list, tuple)) and not isinstance(v, str):
                parts.append(_coerce_text(v))
        return " ".join(p for p in parts if p)
    if isinstance(value, (list, tuple)):
        return " ".join(_coerce_text(item) for item in value if item is not None)
    # Fallback: str()-ify scalars (int, float, bool).
    return str(value)


def _gather_block_text(finding_block: Mapping[str, Any]) -> str:
    """Concatenate the searchable text of an audit-shape finding block.

    Looks at the conventional R14.5.g audit fields (``findings``,
    ``policy_recommendations``, ``red_flags``, ``per_task_findings``,
    ``reviewer_acceptance``) plus a couple of generation-shape mirrors
    (``patch_notes``, ``rationale``, ``review_comments``) so the same
    extractor works for both shapes.
    """

    if not isinstance(finding_block, Mapping):
        return ""
    parts: list[str] = []
    for key in (
        "findings",
        "policy_recommendations",
        "red_flags",
        "reviewer_acceptance",
        "per_task_findings",
        "patch_notes",
        "rationale",
        "review_comments",
        "notes",
        "summary",
    ):
        if key in finding_block:
            parts.append(_coerce_text(finding_block.get(key)))
    return " ".join(p for p in parts if p)


# __R16_SLOT_C__ Worker C (sandbox-grounded axis evidence). Extend signature with
# `sandbox_telemetry: SandboxTelemetry | None = None` kwarg. When provided, merge
# sandbox signals (exit_code/type_errors → correctness, wall_clock/peak_rss → cost,
# stderr scanning → safety). Gated behind AGI_V8_SWARM_SANDBOX_ENABLED env knob.

# --- R16 Phase 3b: sandbox-telemetry helpers -----------------------------------

_SANDBOX_STDERR_SAFETY_KEYWORDS: tuple[str, ...] = (
    "secret",
    "token",
    "escape",
)

_SANDBOX_SLOW_WALL_CLOCK_SEC: float = 10.0
_SANDBOX_HEAVY_RSS_MB: float = 256.0
_SANDBOX_RUFF_WARN_THRESHOLD: int = 5


def _merge_sandbox_signals(
    base_evidence: AxisEvidence,
    telemetry: Any,  # SandboxTelemetry at runtime; Any to avoid hard import
) -> AxisEvidence:
    """Pure function: append sandbox-telemetry signal tokens to *base_evidence*.

    Does **not** mutate *base_evidence* (frozen dataclass). Returns a new
    :class:`AxisEvidence` with the sandbox tokens appended to the relevant axis
    tuples.

    If *telemetry* is None or its ``stub`` field is True, returns *base_evidence*
    unchanged (R15 keyword-only path).
    """
    if telemetry is None:
        return base_evidence
    # Guard: stub telemetry → treat as absent.
    stub = getattr(telemetry, "stub", True)
    if stub:
        return base_evidence

    # --- correctness signals ---
    new_correctness: list[str] = list(base_evidence.correctness)
    exit_code = getattr(telemetry, "exit_code", 0)
    if exit_code != 0 and "sandbox_exit_nonzero" not in new_correctness:
        new_correctness.append("sandbox_exit_nonzero")

    mypy_errors = getattr(telemetry, "mypy_errors", 0)
    if mypy_errors and mypy_errors > 0 and "sandbox_mypy_error" not in new_correctness:
        new_correctness.append("sandbox_mypy_error")

    pytest_failed = getattr(telemetry, "pytest_failed", 0)
    if pytest_failed and pytest_failed > 0 and "sandbox_pytest_fail" not in new_correctness:
        new_correctness.append("sandbox_pytest_fail")

    # --- safety signals ---
    new_safety: list[str] = list(base_evidence.safety)
    stderr_tail: str = getattr(telemetry, "stderr_tail", "") or ""
    lower_stderr = stderr_tail.lower()
    for kw in _SANDBOX_STDERR_SAFETY_KEYWORDS:
        signal = f"sandbox_stderr_{kw}"
        if kw in lower_stderr and signal not in new_safety:
            new_safety.append(signal)

    # --- cost signals ---
    new_cost: list[str] = list(base_evidence.cost)
    wall_clock_sec = getattr(telemetry, "wall_clock_sec", 0.0) or 0.0
    if wall_clock_sec > _SANDBOX_SLOW_WALL_CLOCK_SEC and "sandbox_slow" not in new_cost:
        new_cost.append("sandbox_slow")

    peak_rss_mb = getattr(telemetry, "peak_rss_mb", 0.0) or 0.0
    if peak_rss_mb > _SANDBOX_HEAVY_RSS_MB and "sandbox_memory_heavy" not in new_cost:
        new_cost.append("sandbox_memory_heavy")

    # --- maintainability signals ---
    new_maintainability: list[str] = list(base_evidence.maintainability)
    ruff_warnings = getattr(telemetry, "ruff_warnings", 0) or 0
    ruff_errors = getattr(telemetry, "ruff_errors", 0) or 0
    if ruff_errors >= 1 and "sandbox_ruff_error" not in new_maintainability:
        new_maintainability.append("sandbox_ruff_error")
    if ruff_warnings >= _SANDBOX_RUFF_WARN_THRESHOLD and "sandbox_ruff_warning" not in new_maintainability:
        new_maintainability.append("sandbox_ruff_warning")

    # Show env-knob weight (informational only; real weighting is Worker D scope)
    _sandbox_weight = os.getenv("AGI_V8_SWARM_AXIS_SANDBOX_WEIGHT", "0.5")  # noqa: F841  # tier: T4

    return AxisEvidence(
        correctness=tuple(new_correctness),
        safety=tuple(new_safety),
        cost=tuple(new_cost),
        maintainability=tuple(new_maintainability),
        # novelty / pareto remain empty — computed by vote_per_axis
        novelty=base_evidence.novelty,
        pareto=base_evidence.pareto,
        # __R22_SLOT__ 7th axis pass-through (sandbox does not derive causality)
        causal_chain=base_evidence.causal_chain,
        # __SLOT_B1_BAND_GAP_GUARD_2026_08_10__ raw 수치 pass-through — 여기서
        # 안 넘기면 telemetry 있는 경로에서 guard 채널이 조용히 증발한다.
        numeric_raw=base_evidence.numeric_raw,
    )


def extract_axis_evidence(
    finding_block: Mapping[str, Any],
    *,
    sandbox_telemetry: "SandboxTelemetry | None" = None,
) -> AxisEvidence:
    """Heuristic per-axis evidence extraction from a single pod's finding block.

    Returns :class:`AxisEvidence` with per-axis matched keyword tuples.
    ``novelty`` and ``pareto`` fields are always empty here -- both depend on
    the cross-pod comparison and are populated by :func:`vote_per_axis`.

    R16 Phase 3b: when *sandbox_telemetry* is provided and not a stub, sandbox
    execution signals are merged into the relevant axes.  When *sandbox_telemetry*
    is None or stub=True the function is byte-stable with R15.
    """

    text = _gather_block_text(finding_block)
    if not text:
        base = AxisEvidence()
    else:
        base = AxisEvidence(
            correctness=_scan_keywords(text, _CORRECTNESS_KEYWORDS),
            safety=_scan_keywords(text, _SAFETY_KEYWORDS),
            cost=_scan_keywords(text, _COST_KEYWORDS),
            maintainability=_scan_keywords(text, _MAINTAINABILITY_KEYWORDS),
        )

    # __SLOT_B1_NUMERIC_AXIS_EVIDENCE_2026_08_07__ swarm 수치 채널을 **추가**한다.
    # 키워드 토큰을 지우지 않고 합친다 — 텍스트 신호가 있으면 그것도 여전히 증거다.
    if _numeric_axis_enabled():
        numeric = _numeric_axis_tokens(finding_block)
        if numeric:
            merged = {axis: tuple(getattr(base, axis)) for axis in SWARM_SCORED_AXES}
            for axis, toks in numeric.items():
                merged[axis] = tuple(dict.fromkeys(merged[axis] + toks))
            base = replace(base, **merged)
        # __SLOT_B1_BAND_GAP_GUARD_2026_08_10__ guard ON 일 때만 raw 값을 싣는다 —
        # OFF 면 필드가 기본 () 그대로라 기존 경로와 byte-identical.
        if _band_gap_guard_enabled():
            values = _numeric_axis_values(finding_block)
            if values:
                base = replace(base, numeric_raw=tuple(sorted(values.items())))

    return _merge_sandbox_signals(base, sandbox_telemetry)


def _axis_aligned(a_tokens: tuple[str, ...], b_tokens: tuple[str, ...]) -> tuple[bool, str]:
    """Pair-wise alignment rule for a single axis.

    Returns ``(aligned, direction)`` where direction is one of
    ``"neutral"`` (both empty), ``"agree"`` (overlap), ``"disagree"``
    (disjoint or one-sided).
    """

    a_set = set(a_tokens)
    b_set = set(b_tokens)
    if not a_set and not b_set:
        return True, "neutral"
    if not a_set or not b_set:
        return False, "disagree"
    if a_set & b_set:
        return True, "agree"
    return False, "disagree"


def vote_per_axis(
    a_evidence: AxisEvidence,
    b_evidence: AxisEvidence,
) -> tuple[dict[str, bool], dict[str, dict[str, Any]]]:
    """Six-axis vote: alignment booleans + per-axis detail.

    Alignment rules:
      * both empty            -> ``True`` (no signal, no conflict)
      * overlapping tokens    -> ``True`` (agree finding)
      * one empty, one full   -> ``False`` (asymmetric signal)
      * disjoint non-empty    -> ``False`` (disagreement)

    ``novelty`` is True iff at least one axis (correctness / safety / cost /
    maintainability) carries a finding that exists in exactly one pod -- i.e.,
    the diff captures *something new*. ``pareto`` is True iff the four base
    axes all aligned (so neither pod dominates on any of them).
    """

    alignment: dict[str, bool] = {}
    detail: dict[str, dict[str, Any]] = {}

    base_axes = ("correctness", "safety", "cost", "maintainability")
    for axis in base_axes:
        a_tokens = getattr(a_evidence, axis)
        b_tokens = getattr(b_evidence, axis)
        aligned, direction = _axis_aligned(a_tokens, b_tokens)
        alignment[axis] = aligned
        detail[axis] = {
            "a_tokens": tuple(a_tokens),
            "b_tokens": tuple(b_tokens),
            "shared": tuple(sorted(set(a_tokens) & set(b_tokens))),
            "direction": direction,
        }

    # __SLOT_B1_BAND_GAP_GUARD_2026_08_10__ raw 격차 guard. 밴드 관용(_BAND_EDGE_TOL)
    # 이 양쪽에 적용돼 격차 0.31(라이브: maintainability .696 vs .388)이 agree 로
    # 세어지던 결함의 수리. 두 pod 모두 raw 수치를 냈고 |a−b| ≥ 임계면 밴드가
    # 겹쳐도 disagree. 반드시 novelty/pareto 계산 **앞**에서 뒤집는다 — pareto 는
    # 아래에서 alignment 를 읽는다. OFF(기본)면 numeric_raw 가 항상 빈 튜플이라
    # 이 블록은 detail 에 키 하나 안 남기고 통과한다(byte-identical).
    if _band_gap_guard_enabled():
        threshold = _band_gap_threshold()
        a_raw = dict(a_evidence.numeric_raw)
        b_raw = dict(b_evidence.numeric_raw)
        for axis in base_axes:
            if axis not in a_raw or axis not in b_raw:
                continue
            gap = abs(a_raw[axis] - b_raw[axis])
            fired = gap > 0.0 and gap >= threshold
            detail[axis]["raw_gap"] = round(gap, 6)
            detail[axis]["raw_gap_threshold"] = threshold
            detail[axis]["gap_guard_fired"] = fired
            if fired and alignment[axis]:
                alignment[axis] = False
                detail[axis]["direction"] = "disagree"

    # Novelty: True iff some base axis has *asymmetric* tokens (something one
    # pod found that the other didn't). The diff signal is what matters here:
    # if both pods produced identical (or both-empty) findings on every base
    # axis, there's no novelty.
    novel_tokens: list[str] = []
    for axis in base_axes:
        a_only = set(getattr(a_evidence, axis)) - set(getattr(b_evidence, axis))
        b_only = set(getattr(b_evidence, axis)) - set(getattr(a_evidence, axis))
        for tok in sorted(a_only):
            novel_tokens.append(f"A:{axis}:{tok}")
        for tok in sorted(b_only):
            novel_tokens.append(f"B:{axis}:{tok}")
    # Alignment on novelty axis: True iff there is *no* novelty (pods agree
    # completely on the four base axes). The detail tuple records the diff.
    alignment["novelty"] = not novel_tokens
    detail["novelty"] = {
        "a_tokens": tuple(t for t in novel_tokens if t.startswith("A:")),
        "b_tokens": tuple(t for t in novel_tokens if t.startswith("B:")),
        "shared": (),
        "direction": "agree" if not novel_tokens else "disagree",
    }

    # Pareto: True iff all four base axes are aligned (neither pod dominates).
    pareto_aligned = all(alignment[axis] for axis in base_axes)
    pareto_tokens: tuple[str, ...] = ()
    if pareto_aligned:
        # When aligned across base axes, surface the intersection of shared
        # tokens on correctness ∩ cost ∩ maintainability as the Pareto seed.
        c_shared = set(detail["correctness"]["shared"])
        co_shared = set(detail["cost"]["shared"])
        m_shared = set(detail["maintainability"]["shared"])
        pareto_tokens = tuple(sorted(c_shared & co_shared & m_shared))
    alignment["pareto"] = pareto_aligned
    detail["pareto"] = {
        "a_tokens": (),
        "b_tokens": (),
        "shared": pareto_tokens,
        "direction": "agree" if pareto_aligned else "disagree",
    }

    # __R22_SLOT__ 7th axis: 'causality' — score = normalized length of
    # causal_chain (cap 1.0). Only contributes when the gate is ON; otherwise
    # vote_count remains 6 (R17-R21 invariant).
    if _causality_axis_enabled():
        a_chain = a_evidence.causal_chain
        b_chain = b_evidence.causal_chain
        # Alignment rule: same as base axes (overlap or both-empty → True).
        aligned_c, direction_c = _axis_aligned(a_chain, b_chain)
        alignment[AXIS_CAUSALITY] = aligned_c
        # Score = max(len(a_chain), len(b_chain)) capped to 1.0 via /10.
        a_score = min(1.0, len(a_chain) / 10.0)
        b_score = min(1.0, len(b_chain) / 10.0)
        detail[AXIS_CAUSALITY] = {
            "a_tokens": tuple(a_chain),
            "b_tokens": tuple(b_chain),
            "shared": tuple(sorted(set(a_chain) & set(b_chain))),
            "direction": direction_c,
            "a_score": round(a_score, 6),
            "b_score": round(b_score, 6),
            "combined_score": round(max(a_score, b_score), 6),
        }

    return alignment, detail


def vote_axes_with_evidence(
    pod_a_block: Mapping[str, Any],
    pod_b_block: Mapping[str, Any],
) -> tuple[dict[str, bool], int, dict[str, dict[str, Any]]]:
    """Top-level entry. Combine extraction + vote in one call.

    Returns ``(alignment_map, axes_aligned_count, per_axis_detail)`` where
    ``axes_aligned_count = sum(alignment_map.values())`` -- analogous to the
    legacy single-bool collapse but now differentiated per-axis.
    """

    a_ev = extract_axis_evidence(pod_a_block)
    b_ev = extract_axis_evidence(pod_b_block)
    alignment, detail = vote_per_axis(a_ev, b_ev)
    axes_aligned_count = sum(1 for v in alignment.values() if v)
    return alignment, axes_aligned_count, detail


def is_axis_vote_enabled() -> bool:
    """Read the ``AGI_V8_SWARM_AXIS_VOTE_ENABLED`` env knob.

    Default ``false`` -- when disabled the controller keeps the R14.5.h
    byte-stable single-boolean collapse path.
    """

    raw = os.getenv("AGI_V8_SWARM_AXIS_VOTE_ENABLED", "false")  # tier: T4
    return (raw or "false").strip().lower() == "true"


# __SLOT_P0_2_EVIDENCE_FREE_CONSENSUS_2026_08_17__ Round 6 AX — audit P0 #2.
#
# ## What (2026-08-17 audit + Round 6 verifier, both CONFIRMED)
#
# ``_axis_aligned()`` legitimately returns ``(True, "neutral")`` when both
# pods' token sets are empty (core/axis_scorer.py:542-545 — "no signal, no
# conflict" is a defensible per-axis rule). But that leaf judgment climbs the
# chain unchanged: 4 base axes aligned -> novelty aligned (no diff) -> pareto
# aligned (all base aligned) -> 6/6 -> ``self_improvement_v8.py`` sets
# ``status="consensus"`` -> feeds ``do_real_write = consensus and verify_pass
# and write_on``. The more evidence is missing, the more "aligned" the vote
# looks. This is the same "UNKNOWN folded into 0/full-marks" failure mode the
# prior audit found elsewhere, reappearing at the evaluation layer.
#
# ## Fix shape (additive only — vote_per_axis / _axis_aligned / AxisEvidence
# are byte-identical; nothing above this slot changed)
#
# A parallel 5-state diagnosis entry point that a caller can use INSTEAD of
# reading axes_aligned_count directly for consensus/write decisions:
#
#   PASS / FAIL           -- real, eligible measurement, in either direction.
#   UNMEASURED            -- both pods produced zero axis evidence. Nothing
#                            was measured, so nothing was "passed".
#   INELIGIBLE            -- exactly one pod produced zero axis evidence
#                            (asymmetric — the audit's "empty vs empty-string"
#                            distinction, generalized). Distinct from
#                            UNMEASURED: "no evidence at all" and "evidence
#                            imbalance" are different facts and must not
#                            collapse to the same status.
#   EVALUATOR_ERROR       -- the pod block itself is malformed (not a Mapping,
#                            not None) or extraction/vote raised. fail-closed:
#                            never silently treated as empty-and-passing.
#
# ``ConsensusDiagnosis`` is deliberately narrow — status/counts/reason only.
# It carries NO write/patch/apply-authority field. DIAGNOSIS_CONSENSUS (axis
# vote agrees on the problem definition) is not PATCH_AUTHORIZED (permission
# to mutate). The write-authority decision (``do_real_write`` in
# self_improvement_v8.py, a different track's file) must combine this
# diagnosis with verify_pass/write_on itself — see the integration notes in
# report_AX.md for the exact call site and kwarg signature.
#
# ## Gate — AGI_V8_AXIS_ELIGIBILITY_ENABLED, default ON
#
# This is an eval-integrity repair, not a new feature, so it defaults ON per
# repo convention for this track. OFF reproduces the pre-repair legacy
# semantics exactly (both-empty -> PASS, evidence counts reported as -1 =
# "not measured under this gate") rather than silently changing behavior for
# anyone relying on the old collapse. Nothing in vote_per_axis / _axis_aligned
# / AxisEvidence / extract_axis_evidence is touched by this slot, so existing
# callers (self_improvement_v8.py's axes_aligned>=_CONSENSUS_THRESHOLD path)
# stay byte-identical regardless of this gate's value until a future patch
# wires them to diagnose_consensus_from_blocks explicitly.

DIAG_PASS = "PASS"
DIAG_FAIL = "FAIL"
DIAG_UNMEASURED = "UNMEASURED"
DIAG_EVALUATOR_ERROR = "EVALUATOR_ERROR"
DIAG_INELIGIBLE = "INELIGIBLE"

_AXIS_ELIGIBILITY_ENV = "AGI_V8_AXIS_ELIGIBILITY_ENABLED"


def _axis_eligibility_enabled() -> bool:
    """P0 #2 5-state diagnosis gate. Default ON (eval-integrity repair).

    OFF preserves the pre-repair boolean-only collapse (both-empty counts as
    PASS) for any caller that still needs the old semantics verbatim.
    """
    raw = os.getenv(_AXIS_ELIGIBILITY_ENV, "true")  # tier: T4
    return (raw or "true").strip().lower() in ("true", "1")


@dataclass(frozen=True)
class ConsensusDiagnosis:
    """Result of a 5-state diagnosis vote. Diagnosis-only — see slot docstring
    above for why this type deliberately has no write/patch-authority field.
    """

    status: str
    axes_aligned_count: int
    evidence_count_a: int
    evidence_count_b: int
    reason: str = ""


def axis_evidence_count(evidence: AxisEvidence) -> int:
    """Total matched-token count across the base+derived axes of one pod's
    :class:`AxisEvidence`. Mirrors the ``axis_evidence_total`` stamp
    self_improvement_v8.py already emits (audit A2 observability), exposed
    here as a reusable primitive so eligibility can be computed per-pod
    instead of only as a combined total.
    """

    total = 0
    for axis in AXIS_KEYS:
        total += len(getattr(evidence, axis, ()) or ())
    if _causality_axis_enabled():
        total += len(evidence.causal_chain or ())
    return total


def diagnose_consensus(
    a_evidence: AxisEvidence,
    b_evidence: AxisEvidence,
    alignment: "Mapping[str, bool]",
    *,
    threshold: int = 5,
) -> ConsensusDiagnosis:
    """Combine per-axis alignment with per-pod evidence counts into a 5-state
    diagnosis. Pure function — no I/O, no mutation.

    Eligibility is checked BEFORE the pass/fail split: an evidence-free (or
    evidence-asymmetric) vote never reaches PASS, no matter how many axes
    happened to align on empty sets.
    """

    axes_aligned_count = sum(1 for v in alignment.values() if v)

    if not _axis_eligibility_enabled():
        # Legacy path: old boolean-only collapse, unchanged decision. Evidence
        # counts are reported as -1 ("not measured under this gate") so a
        # caller can tell this diagnosis did not apply eligibility rules,
        # rather than silently claiming 0 evidence was seen.
        status = DIAG_PASS if axes_aligned_count >= threshold else DIAG_FAIL
        return ConsensusDiagnosis(
            status=status,
            axes_aligned_count=axes_aligned_count,
            evidence_count_a=-1,
            evidence_count_b=-1,
            reason="legacy_gate_off",
        )

    count_a = axis_evidence_count(a_evidence)
    count_b = axis_evidence_count(b_evidence)

    if count_a == 0 and count_b == 0:
        return ConsensusDiagnosis(
            status=DIAG_UNMEASURED,
            axes_aligned_count=axes_aligned_count,
            evidence_count_a=count_a,
            evidence_count_b=count_b,
            reason="both_pods_zero_evidence",
        )
    if count_a == 0 or count_b == 0:
        return ConsensusDiagnosis(
            status=DIAG_INELIGIBLE,
            axes_aligned_count=axes_aligned_count,
            evidence_count_a=count_a,
            evidence_count_b=count_b,
            reason="asymmetric_zero_evidence",
        )

    status = DIAG_PASS if axes_aligned_count >= threshold else DIAG_FAIL
    return ConsensusDiagnosis(
        status=status,
        axes_aligned_count=axes_aligned_count,
        evidence_count_a=count_a,
        evidence_count_b=count_b,
        reason="",
    )


def diagnose_consensus_from_blocks(
    pod_a_block: Any,
    pod_b_block: Any,
    *,
    threshold: int = 5,
) -> ConsensusDiagnosis:
    """Top-level entry point: extract + vote + diagnose from raw pod blocks.

    fail-closed: a malformed block (not a ``Mapping``, not the extraction/vote
    pipeline's expected shape) or any fault raised while extracting/voting
    produces ``EVALUATOR_ERROR`` — never a silent PASS. This is the
    "evaluator absent/broken = REJECT, not approval" contract from the repo
    discipline, applied to the axis-vote evaluator specifically.
    """

    if not isinstance(pod_a_block, Mapping) or not isinstance(pod_b_block, Mapping):
        return ConsensusDiagnosis(
            status=DIAG_EVALUATOR_ERROR,
            axes_aligned_count=0,
            evidence_count_a=-1,
            evidence_count_b=-1,
            reason="pod_block_not_mapping",
        )

    try:
        a_ev = extract_axis_evidence(pod_a_block)
        b_ev = extract_axis_evidence(pod_b_block)
        alignment, _detail = vote_per_axis(a_ev, b_ev)
    except Exception as exc:  # fail-closed: evaluator fault -> REJECT, not PASS.
        _swallowed(
            exc,
            site="core.axis_scorer.diagnose_consensus_from_blocks",
            category="verify",
        )
        # __SLOT_NONLOGGER_R_2026_08_18__ swapped bare ``type(exc).__name__``
        # for the hardened helper (byte-identical output for ordinary
        # exceptions; a hostile ``__str__``/metaclass on ``exc`` no longer
        # reaches this reason string).
        return ConsensusDiagnosis(
            status=DIAG_EVALUATOR_ERROR,
            axes_aligned_count=0,
            evidence_count_a=-1,
            evidence_count_b=-1,
            reason=f"extraction_error:{_safe_exception_type_name(exc)}",
        )

    return diagnose_consensus(a_ev, b_ev, alignment, threshold=threshold)


def axis_auto_adopt_threshold() -> int:
    """Read the partial-bucket auto-adopt threshold (default 5/6).

    Controlled by ``AGI_V8_SWARM_AXIS_AUTO_ADOPT_THRESHOLD``. When
    ``axes_aligned >= threshold`` the partial bucket auto-adopts Pod A.
    """

    raw = os.getenv("AGI_V8_SWARM_AXIS_AUTO_ADOPT_THRESHOLD", "5")  # tier: T4
    try:
        v = int((raw or "5").strip())
    except ValueError as _ff_exc:
        _swallowed(_ff_exc, site="core.axis_scorer.axis_auto_adopt_threshold:505", category="telemetry")
        return 5
    if v < 0:
        return 0
    if v > len(AXIS_KEYS):
        return len(AXIS_KEYS)
    return v
