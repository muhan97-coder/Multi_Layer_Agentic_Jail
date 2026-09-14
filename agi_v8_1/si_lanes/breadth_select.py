# __SLOT_SI_BREADTH_SELECT_2026_06_18__ Code-arena feedback → organ.
"""Best-of-N + OBJECTIVE selection for SI proposers.

The 2026-06-18 code-optimization arena measured a clear, reproducible result:
generating a DIVERSE population of candidates and selecting the best by an
OBJECTIVE score (breadth) beats single-shot LLM reasoning/refine (depth) — even
diversity-injected refine could not beat the best-of-20 empirically-measured
champion. "measurement > reasoning". (See project_code_arena_2026_06_18.)

This module encodes that lesson as a reusable mechanism the SI proposer can opt
into: instead of one proposal per diagnosis, produce N diverse candidates and
keep the one an OBJECTIVE ``score_fn`` ranks highest. The score_fn is the load-
bearing lever — the arena's win came from an objective fitness (run + measure),
so callers should pass a real objective scorer (e.g. the F2 sandbox verdict),
not a soft heuristic.

Default-OFF + N=1 ⇒ exactly the pre-existing single-shot path (byte-identical).
"""
from __future__ import annotations

# Public safety/admission remains importable without optional implementation.
from agi_v8_1.capabilities import PayloadPort, PayloadUnavailable, resolve_payload
import sys as _payload_sys

import ast
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

# __SLOT_FAIL_FAST_2026_07_25__ Swallowed failures route through one choke
# point: counted + named always, re-raised under AGI_V8_STRICT_FAIL_FAST.
from agi_v8_1.policy.fail_fast import (
    format_exception_for_log,
    safe_exception_type_name,
    swallowed as _swallowed,
)

logger = logging.getLogger(__name__)

_BON_GATE = "AGI_V8_SI_BEST_OF_N_ENABLED"
_BON_N = "AGI_V8_SI_BEST_OF_N"
_MAX_N = 32  # hard cap — breadth is cheap but not unbounded (2026-08-02: 8→32;
# the campaign wants a wide plan-candidate pool. Cost is linear in N: N LLM
# calls + N sandbox scorings per cycle, so the operative limit is the daily USD
# cap, not this number. Keep len(HINTS) ≥ the N you actually run — hints wrap
# (HINTS[i % len(HINTS)]), and duplicate hints buy duplicate candidates.

# Diverse variation hints appended per candidate (mirrors the arena's per-lane
# hints: variety IN, objective selection OUT). Kept generic / domain-agnostic.
HINTS: tuple[str, ...] = (
    "Prioritize the minimal, surgical change.",
    "Prioritize robustness and edge cases.",
    "Prioritize clarity and a single responsibility.",
    "Consider an alternative approach to the obvious one.",
    "Prioritize not regressing existing behavior.",
    "Prioritize the simplest thing that could work.",
    # 2026-08-02: 6→16 so a 16-wide pool gets 16 distinct angles (hints wrap).
    "Attack the root cause rather than the observed symptom.",
    "Prefer deleting or unifying code over adding a branch.",
    "Prioritize making the failure impossible to reintroduce.",
    "Assume the first diagnosis is wrong; verify it before changing code.",
    "Prioritize observability: make the next failure self-explaining.",
    "Prefer a data-driven table over conditional logic.",
    "Prioritize the smallest change that a reviewer could fully check.",
    "Handle the boundary cases first, then the common path.",
    "Prefer reusing an existing helper over writing a new one.",
    "Prioritize failing loudly and early over degrading silently.",
)


def best_of_n_enabled() -> bool:
    """Default-OFF gate (strict ``true``/``1``). OFF ⇒ single-shot, byte-identical."""
    return os.environ.get(_BON_GATE, "").strip().lower() in ("true", "1")  # tier: T1


def breadth_n(default: int = 1) -> int:
    """Candidate count, clamped to [1, _MAX_N]. Default 1 = single-shot."""
    raw = os.environ.get(_BON_N, "").strip()  # tier: T1
    try:
        n = int(raw) if raw else default
    except ValueError as _ff_exc:
        _swallowed(_ff_exc, site="si_lanes.breadth_select.breadth_n:50", category="verify")
        return default
    return max(1, min(_MAX_N, n))


def active() -> bool:
    """True only when breadth is gated ON AND N>1 — the ONLY case that changes
    behavior away from the single-shot path."""
    return best_of_n_enabled() and breadth_n() > 1


def hint_for(i: int) -> str:
    return HINTS[i % len(HINTS)]


# __SLOT_BREADTH_PARALLEL_2026_08_02__ Candidate GENERATION concurrency.
# ``select_best`` only ever SCORED in parallel-free code; the N generations
# themselves ran in a plain ``for`` loop, so N=16 cost 16 sequential provider
# round-trips. Measured 2026-08-02: ~3.4 min per candidate ⇒ ~54 min for one
# inc2 invocation, which exceeded the campaign's 45-min episode timeout — every
# episode was killed mid-cycle and NO episode ever reached the apply ladder.
_BON_PARALLEL = "AGI_V8_SI_BEST_OF_N_PARALLEL"


def parallel_enabled() -> bool:
    """Default-OFF gate for concurrent candidate generation."""
    return os.environ.get(_BON_PARALLEL, "") in ("true", "1")  # tier: T1


def generate_candidates(
    propose_fn: Callable[..., str],
    agent_name: str,
    prompts: Sequence[str],
    payload: Any,
    schema: Any,
) -> list[tuple[str | None, dict[str, Any] | None]]:
    """Run one generation per prompt → ``[(raw, usage), ...]``, order preserved.

    Sequential by default — byte-identical to the historical loop, including
    reading the shared ``propose_fn.last_usage`` after each call.

    Concurrent when BOTH the gate is on AND *propose_fn* carries a
    ``parallel_generate`` seam (the closure built for real providers does; the
    plain callables tests inject do not, so tests keep the sequential path).
    The seam MUST return per-call usage rather than leave it on the provider's
    shared ``last_usage``: with concurrent lanes on one provider instance that
    attribute is clobbered between dispatch and read, which silently misprices
    the run instead of failing (the audit-B3 race, fixed provider-side by
    ``async_throttled_generate_with_usage``).

    A candidate that raises is reported as ``(None, None)`` — one bad candidate
    never aborts the set, matching the sequential loop's contract.
    """
    par = getattr(propose_fn, "parallel_generate", None)
    # __SLOT_HETERO_SEAM_HONOR_2026_08_15__ (verify-lens P0 CONFIRMED) A
    # hetero-marked seam (``propose_fn.hetero``, stamped only when
    # AGI_V8_SI_HETERO_LANES_ENABLED fired) is honored WITHOUT the separate
    # AGI_V8_SI_BEST_OF_N_PARALLEL gate: otherwise "hetero ON" half-fires —
    # tier-N, f1sw_ ids and ``select`` ledger rows get stamped while
    # generation silently runs single-model sequential, mislabeling the
    # worker as hetero-mix-v1. Plain propose_fns carry no such attribute, so
    # the historical gate semantics are byte-identical.
    if ((parallel_enabled() or getattr(propose_fn, "hetero", False))
            and callable(par) and len(prompts) > 1):
        try:
            return list(par(agent_name, list(prompts), payload, schema))
        except Exception as exc:  # noqa: BLE001 — fall back, never abort a cycle
            _swallowed(exc, site="si_lanes.breadth_select.generate_candidates",
                       category="verify")
            logger.warning(
                "parallel candidate generation failed → sequential: %s",
                format_exception_for_log(exc),
            )
    out: list[tuple[str | None, dict[str, Any] | None]] = []
    for sp in prompts:
        try:
            raw = propose_fn(agent_name, sp, payload, schema)
        except Exception as exc:  # noqa: BLE001 — one bad candidate never aborts
            _swallowed(exc, site="si_lanes.breadth_select.generate_candidates:seq",
                       category="verify")
            logger.warning(
                "candidate generation failed: %s",
                format_exception_for_log(exc),
            )
            out.append((None, None))
            continue
        u = getattr(propose_fn, "last_usage", None)
        out.append((raw, dict(u) if isinstance(u, Mapping) and u else None))
    return out


# __SLOT_SCORE_PARALLEL_2026_08_15__ L2(a) — parallel candidate SCORING
# (design doc §1: "세마포어 8 — docker 512MB×8" = 4GB only when the caller
# pins each sandbox to 512 MiB, as the Track2 20260903a launcher does). Generic
# callers may configure the sandbox runner up to 4096 MiB, so this worker bound
# alone is not an aggregate-memory admission control. Mirrors the 2026-08-02
# GENERATION parallel gate above: default-OFF, and OFF is the untouched
# sequential loop, byte-identical to the pre-2026-08-15 shape.
_SCORE_PARALLEL_GATE = "AGI_V8_SI_SCORE_PARALLEL_ENABLED"
_SCORE_PARALLEL_WORKERS = 8


def score_parallel_enabled() -> bool:
    """Default-OFF gate for concurrent candidate SCORING (strict "true"/"1")."""
    return os.environ.get(_SCORE_PARALLEL_GATE, "") in ("true", "1")  # tier: T9


# __SLOT_SCORE_PYTEST_2026_08_20__ t2_scoring — per-candidate pytest bonus.
# The ±100/candidate ``pytest_passed``/``pytest_failed`` term below
# (``make_objective_score_fn``'s ``_score``) has been live since 2026-08-15
# but NEVER fires: every ``sandbox_runner.run(...)`` call in that closure
# hardcodes ``test=None``, so entry.py's ``has_test`` branch never runs and
# every candidate scores 0 passed / 0 failed regardless of actual quality.
# This gate arms deriving a target test FROM the objective text (when one is
# supplied — see ``_derive_target_test``) and threading it through as
# ``test=``. Default-OFF; OFF ⇒ ``target_test`` stays ``None`` and the
# ``sandbox_runner.run`` call is byte-identical to before this slot existed.
# ⛔ PHANTOM at two remaining callers (2026-08-22 Sol Pro investigation):
# si_lanes/objective_proposer.py and si_lanes/llm_failure_proposer.py still
# omit ``objective=``.  The objective-editor caller now supplies its objective
# (2026-09-03 candidate-selection repair), so this axis can distinguish its
# candidates when the gate is armed and the objective embeds a valid target
# test.  The two untouched callers retain their historical ``None`` behavior.
_SCORE_PYTEST_GATE = "AGI_V8_SI_SCORE_PYTEST_ENABLED"


def score_pytest_enabled() -> bool:
    """Default-OFF gate for the per-candidate pytest bonus (strict "true"/"1")."""
    return os.environ.get(_SCORE_PYTEST_GATE, "") in ("true", "1")  # tier: T9


# __SLOT_DERIVE_TEST_CANDIDATE_REF_2026_08_22__ t2_scoring follow-up (Sol Pro
# codex investigation, 2026-08-22). ``_derive_target_test``'s three checks
# below (parses / defines ``test_*`` / no ``agi_v8_1.*`` import) never look at
# whether the test actually EXAMINES the candidate: ``def test_ok():\n
# assert True`` passes all three and is returned as a "valid derived test".
# Because ``make_objective_score_fn`` derives this test ONCE per batch and
# reuses it for every candidate (see its docstring), a vacuous test like that
# gives every candidate the identical ±100 pytest_passed/pytest_failed term —
# the batch's discriminating power on that axis is mathematically zero, not
# merely weak. This gate arms a 4th static check — "the test body never
# references the candidate module" — that rejects such vacuous tests.
# Scoped to ``_derive_target_test`` itself, which is only ever called from
# ``make_objective_score_fn`` when ``score_pytest_enabled()`` is already ON
# (see below): with THIS gate OFF, ``_derive_target_test`` keeps accepting a
# vacuous test exactly as it did before this slot existed (OFF = the pre-
# existing, unexamined-test-accepting contract, byte-identical).
_DERIVE_TEST_CANDIDATE_REF_GATE = "AGI_V8_SI_DERIVE_TEST_CANDIDATE_REF_ENABLED"


def derive_test_candidate_ref_enabled() -> bool:
    """Default-OFF gate rejecting a derived test that never references the
    candidate module (strict "true"/"1"). See the slot note above."""
    return os.environ.get(_DERIVE_TEST_CANDIDATE_REF_GATE, "") in ("true", "1")  # tier: T9


def _references_candidate_symbol(tree: ast.AST) -> bool:
    """True iff *tree* imports the candidate module — mounted by the sandbox
    harness as a bare module named ``code`` (docker/sandbox/entry.py:
    ``code_dst = os.path.join(_WORK, "code.py")``; the harness contract
    verified live in tests/v8_1/test_evaluator_noninterference_2026_08_21.py,
    e.g. ``test="import code\\n\\ndef test_v(): assert code.VALUE == 42\\n"``)
    AND actually USES a name bound from that import somewhere in the body.

    A test that defines ``def test_*`` but never touches ``code`` examines
    nothing: it evaluates to the same pass/fail for every candidate in the
    batch, which is exactly the vacuous-test defect this check exists to
    reject (see ``_DERIVE_TEST_CANDIDATE_REF_GATE`` above). Import-only with
    no use (``import code`` and nothing else) is also rejected — importing
    a module without ever reading from it still asserts nothing about it.
    """
    bound_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "code" or alias.name.startswith("code."):
                    bound_names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module == "code":
                for alias in node.names:
                    if alias.name == "*":
                        # __SLOT_WILDCARD_IMPORT_REJECT_2026_08_22__ A
                        # wildcard binds an unknowable set of names, so this
                        # function cannot verify that the test body actually
                        # Load()s any of them — the exact question this
                        # check exists to answer. Treating "imports the
                        # candidate somehow" as sufficient let a vacuous
                        # test (``from code import *`` + ``assert True``,
                        # never referencing a candidate symbol) pass
                        # unexamined; reject instead of assume, matching the
                        # named-import branch below which already requires
                        # an observed ``Load``.
                        return False
                    bound_names.add(alias.asname or alias.name)
    if not bound_names:
        return False
    return any(
        isinstance(node, ast.Name) and node.id in bound_names
        and isinstance(node.ctx, ast.Load)
        for node in ast.walk(tree)
    )


# agi_v8_1.* is unresolvable inside the standalone sandbox harness (Worker A
# contract, docker/sandbox/entry.py): the candidate runs as a bare module
# named ``code`` with no repo checkout mounted (independent of
# AGI_V8_SANDBOX_WORKTREE_ENABLED, which only affects the CANDIDATE's own
# imports, not a derived test's). A derived test that imports the real
# package would ImportError on every candidate — pytest_failed for all,
# not "unmeasured" — exactly the regression this module's own scout note
# warns against.
_FORBIDDEN_TEST_IMPORT_PREFIXES = ("agi_v8_1",)

_FENCE_RE = re.compile(r"```[ \t]*[\w+-]*\r?\n(.*?)```", re.DOTALL)


def _fenced_code_blocks(text: str) -> list[str]:
    """Every ```...``` fenced block body in *text*, in order. Never raises."""
    try:
        return [m.group(1) for m in _FENCE_RE.finditer(text)]
    except Exception as _ff_exc:  # noqa: BLE001 — malformed text never aborts scoring
        _swallowed(_ff_exc, site="si_lanes.breadth_select._fenced_code_blocks",
                   category="verify")
        return []


def _imports_forbidden_module(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        for name in names:
            if any(name == p or name.startswith(p + ".")
                   for p in _FORBIDDEN_TEST_IMPORT_PREFIXES):
                return True
    return False


def _derive_target_test(objective: str | None) -> str | None:
    """Dispatch through the optional T9 implementation; safety lives here."""
    return resolve_payload(PayloadPort(9, 'agi_v8_1.si_lanes.breadth_select_payload', '_derive_target_test'))(
        _payload_sys.modules[__name__], objective
    )


def _score_candidate(score_fn: Callable[[Any], float], candidate: Any) -> float:
    """Score ONE candidate under the shared swallow contract.

    Both the sequential loop in ``select_best`` and the parallel worker below
    route a raising ``score_fn`` through this exact site string, so the
    exception→``-inf`` semantics are identical regardless of which path ran.
    Never raises outside strict fail-fast (matching the sequential contract).
    """
    try:
        return float(score_fn(candidate))
    except Exception as _ff_exc:  # noqa: BLE001 — a bad scorer never aborts selection
        _swallowed(_ff_exc, site="si_lanes.breadth_select.select_best:79", category="verify")
        return float("-inf")


def _score_parallel(
    cand_list: list[Any], score_fn: Callable[[Any], float]
) -> list[tuple[Any, float]] | None:
    """Dispatch through the optional T9 implementation; safety lives here."""
    return resolve_payload(PayloadPort(9, 'agi_v8_1.si_lanes.breadth_select_payload', '_score_parallel'))(
        _payload_sys.modules[__name__], cand_list, score_fn
    )


def select_best(
    candidates: Sequence[Any], score_fn: Callable[[Any], float]
) -> tuple[Any, list[tuple[Any, float]]]:
    """Objective selection: score every candidate, return (best, scored_pairs).

    A score_fn that raises on a candidate scores it ``-inf`` (dropped from
    contention, never crashes the loop). Empty input → (None, []). Ties resolve
    to the FIRST max (stable — preserves generation order so the result is
    deterministic for a fixed candidate list).

    Scoring runs concurrently (``AGI_V8_SI_SCORE_PARALLEL_ENABLED``, gate OFF
    by default) when there is more than one candidate; the result semantics
    (order, ties, exception→-inf) are identical to the sequential loop either
    way — see :func:`_score_candidate` / :func:`_score_parallel`. OFF is the
    original sequential loop, untouched.
    """
    scored: list[tuple[Any, float]] | None = None
    if score_parallel_enabled():
        cand_list = list(candidates)
        if len(cand_list) > 1:
            scored = _score_parallel(cand_list, score_fn)
        candidates = cand_list
    if scored is None:
        scored = []
        for c in candidates:
            try:
                s = float(score_fn(c))
            except Exception as _ff_exc:  # noqa: BLE001 — a bad scorer never aborts selection
                _swallowed(_ff_exc, site="si_lanes.breadth_select.select_best:79", category="verify")
                s = float("-inf")
            scored.append((c, s))
    if not scored:
        return None, []
    best = scored[0]
    for pair in scored[1:]:
        if pair[1] > best[1]:
            best = pair
    return best[0], scored


# ---------------------------------------------------------------------------
# OBJECTIVE score_fn — the load-bearing lever (arena: measurement > reasoning).
# ---------------------------------------------------------------------------
def _extract_code(candidate: Any) -> str:
    """Pull candidate code text (a ProposalDoc's suggested change, or a raw
    string), stripping markdown fences."""
    txt = getattr(candidate, "llm_suggested_change", None)
    if txt is None:
        txt = candidate if isinstance(candidate, str) else ""
    txt = str(txt).strip()
    if txt.startswith("```"):
        lines = txt.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        txt = "\n".join(lines)
    return txt


def _heuristic_tiebreak(candidate: Any) -> float:
    """Small heuristic (≤200) that only ORDERS objectively-tied candidates — it
    can never override the objective layers' large gaps."""
    sc = getattr(candidate, "llm_suggested_change", None)
    base = (float(len(str(sc).strip())) if sc is not None
            else float(len(candidate)) if isinstance(candidate, str) else 0.0)
    base += {"high": 30.0, "medium": 15.0, "low": 5.0}.get(
        (getattr(candidate, "llm_confidence", "") or "").strip().lower(), 0.0)
    if (getattr(candidate, "llm_rationale", "") or "").strip():
        base += 50.0
    return min(base, 200.0)


@dataclass(frozen=True)
class ObjectiveScoreResult:
    """One objective-score result plus whether its execution layer measured.

    ``score`` deliberately preserves the historical best-effort numeric value:
    callers that only need the old scorer contract still receive the same
    parse/heuristic fallback when the sandbox is unavailable.  Selection paths
    that claim execution-grounded ranking can instead consult ``sandbox_ran``
    and refuse to compare an unmeasured candidate with measured candidates.

    ``sandbox_verdict_trusted`` is deliberately always false for today's
    in-process Python grader. Candidate code shares pytest's interpreter and
    can forge its result channel; the numbers remain ranking telemetry and no
    caller may infer semantic PASS from them. Track2's goal-campaign preflight
    separately requires inc5 before apply. Status and exit code are bounded
    structural diagnostics only. Candidate/container stderr is intentionally
    absent.
    """

    score: float
    parse_ok: bool
    ndef: int
    runnable: bool
    sandbox_enabled: bool
    sandbox_ran: bool
    sandbox_verdict_trusted: bool
    stub_reason: str
    sandbox_status: str
    sandbox_exit_code: int
    sandbox_backend: str


def make_objective_score_fn(
    state_dir: Any = None, objective: str | None = None
) -> Callable[[Any], float]:
    """Dispatch through the optional T9 implementation; safety lives here."""
    return resolve_payload(PayloadPort(9, 'agi_v8_1.si_lanes.breadth_select_payload', 'make_objective_score_fn'))(
        _payload_sys.modules[__name__], state_dir, objective
    )


__all__ = [
    "HINTS",
    "ObjectiveScoreResult",
    "active",
    "best_of_n_enabled",
    "breadth_n",
    "derive_test_candidate_ref_enabled",
    "hint_for",
    "make_objective_score_fn",
    "score_parallel_enabled",
    "score_pytest_enabled",
    "select_best",
]
