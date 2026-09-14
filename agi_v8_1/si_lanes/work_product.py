# __SLOT_SI_WORK_PRODUCT_LANE_2026_08_02__ 목표-구동 산출물 레인 (F3).
"""Work-product lane — the loop's HANDS for goal-card episodes.

Why this exists
---------------
Every other propose lane in ``si_lanes/`` is *failure-driven*: it reads a
failure signature out of an observation bundle and suggests a repair. A goal
card is not a failure — it is a **task**, and its verdict is a file the grader
reads out of the card workspace. On 2026-08-02 a 12-lane campaign closed 7
cards and every single one came back ``EXTRACT_FAIL``: the loop had eyes (the
command channel emitted 8/8 after the contract rewrite) but no hands. It could
observe, diagnose, and write proposal artifacts under ``si_proposed/`` — and
nothing it wrote could ever be the file the grader opens.

This lane closes that gap WITHOUT weakening the two invariants that make the
loop safe to run unattended:

  1. **The model never picks a path or an action.** It returns Python source as
     DATA. The artifact path (``workspace/.agent/step_<cycle>_<n>.py``) and the
     action (``create``) are computed here, locally, exactly like inc2's
     proposal artifacts. A ``path`` key in the model's JSON is ignored.
  2. **Every write stays inside the jail.** The artifact lands under the SI
     ``state_dir`` through the ordinary ``SafeAutoApply`` session, and it is
     executed by Stage 4 (``command_executor.execute_artifact_run``) with
     ``cwd`` pinned to the card workspace — itself a subdirectory of the jail.

The script the model writes is what actually produces the work product, so the
loop's reach is no longer limited to text a proposer can author directly: it can
compute, transform, and iterate. That capability is why the runner should be
sandboxed (``AGI_V8_SI_ARTIFACT_SANDBOX=bwrap``) — see the containment note in
``enforcement/command_executor.py``.

Default-OFF (``AGI_V8_SI_WORK_PRODUCT_ENABLED``): gate off ⇒ this module is
never imported by the cycle and the payload is byte-identical to before.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Callable, Mapping, Sequence

from agi_v8_1.policy.fail_fast import (
    format_exception_for_log,
    safe_exception_type_name,
    swallowed as _swallowed,
)

logger = logging.getLogger(__name__)

_ENABLE_ENV = "AGI_V8_SI_WORK_PRODUCT_ENABLED"
# Artifact dir RELATIVE to the apply ladder's repo_root (== SI state_dir). Kept
# under the workspace (not si_proposed/) so the script sits beside the files it
# produces and the grader's cwd is its cwd. The dot-prefix keeps agent scratch
# visually separate from the graded product.
_WORKSPACE_REL = "workspace"
_ARTIFACT_DIR_REL = f"{_WORKSPACE_REL}/.agent"
_AGENT_NAME = "si_work_product"
_SCHEMA_VERSION = "agi_v8_1_si_work_product_v1"
# Bound on how much model-authored source we accept in one step. A runaway
# response should be dropped loudly here, not written to disk and executed.
_MAX_SCRIPT_CHARS = 200_000
# script 외 필드도 무한이면 같은 구멍이다 — summary/expected_outputs 는 사람이
# 읽는 메타데이터지 산출물이 아니므로 좁게 묶는다.
_MAX_SUMMARY_CHARS = 2_000
_MAX_EXPECTED_OUTPUT_CHARS = 512
_MAX_EXPECTED_OUTPUTS = 64

_SYSTEM_PROMPT = (
    "You are the acting half of a self-improving agent loop working on one "
    "task. You are given the task objective and, on retries, what your previous "
    "attempt produced. Reply with JSON matching the schema: a single "
    "self-contained Python 3 script that, when run with its working directory "
    "set to the task workspace, creates the work products the task asks for. "
    "The script may read the staged task assets, compute, and write files under "
    "the workspace; it has no network credentials and nothing outside the "
    "workspace persists. Prefer the standard library. Print a short JSON line "
    "summarising what you wrote so the next attempt can see it. Make the script "
    "runnable as-is: no placeholders, no TODOs, no prompts for input. Output "
    "JSON only — no prose, no markdown fences. "
    # 🔴 2026-08-29: 이 한 문장이 없어서 20260829b 가 죽었다. 파서는 200,000자
    #    초과를 드롭하는데 모델은 그 사실을 몰랐고, max_tokens 131,072 은
    #    파서가 절대 받지 않는 크기(≈400KB)까지 허용했다.  3콜 중 2콜이
    #    상한에서 잘려(finish_reason='length') 39분·$0.033 씩 통째로 버려졌다.
    #    한도는 **모델이 볼 수 있는 자리**에 있어야 한다.
    f"The script field must not exceed {_MAX_SCRIPT_CHARS:,} characters; a "
    "longer script is discarded in full and the attempt produces nothing, so "
    "keep it focused on what the objective actually asks for."
)

_SCRIPT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["script"],
    "properties": {
        # maxLength 는 _MAX_SCRIPT_CHARS 에서 **파생**한다 — 리터럴을 복사하면
        # 검사 안 받는 쪽이 썩는다(파서만 조이고 스키마는 옛 값을 광고하는 상태).
        "script": {"type": "string", "maxLength": _MAX_SCRIPT_CHARS},
        "summary": {"type": "string", "maxLength": _MAX_SUMMARY_CHARS},
        "expected_outputs": {
            "type": "array",
            "items": {"type": "string", "maxLength": _MAX_EXPECTED_OUTPUT_CHARS},
            "maxItems": _MAX_EXPECTED_OUTPUTS,
        },
    },
}

# (agent_name, prompt, payload, schema) -> json_str — same seam shape as inc2.
ProposeFn = Callable[[str, str, Mapping[str, Any], Mapping[str, Any]], str]

__all__ = [
    "enabled",
    "artifact_rel_path",
    "build_payload",
    "propose_work_product",
]


def enabled() -> bool:
    """Default-OFF gate (strict ``"true"``/``"1"``)."""
    return os.environ.get(_ENABLE_ENV, "") in ("true", "1")  # tier: T1


def artifact_rel_path(cycle_id: str, attempt: int) -> str:
    """Jail-relative artifact path — computed LOCALLY, never model-supplied.

    ``cycle_id`` is sanitised to a conservative charset and length-bounded, so a
    hostile or malformed cycle id cannot steer the write out of the artifact
    directory (``plan_artifact_run`` re-validates containment regardless).
    """
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(cycle_id))[:48].strip("._-") or "cycle"
    return f"{_ARTIFACT_DIR_REL}/step_{safe}_{int(attempt):02d}.py"


def build_payload(
    objective: str, feedback: Mapping[str, Any] | None = None,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """``(system_prompt, user_payload, schema)`` for the propose_fn.

    The system prompt is a CONSTANT — the task text is data, so there is no
    per-card branching here or anywhere downstream (schema-driven, no mode
    enums). ``feedback`` is the previous attempt's observable result (exit code,
    output tails, grader verdict); it is what makes attempt N+1 a revision
    rather than a fresh guess.
    """
    payload: dict[str, Any] = {
        "task": "produce_work_product",
        "objective": str(objective),
    }
    if feedback:
        payload["previous_attempt"] = dict(feedback)
    return _SYSTEM_PROMPT, payload, dict(_SCRIPT_SCHEMA)


def _parse_script(raw_json: Any) -> tuple[str, str, list[str]] | None:
    """Parse a propose_fn result into ``(script, summary, expected_outputs)``.

    Fail-safe → None (never raises): malformed JSON, non-object JSON, a missing
    or empty ``script``, or an over-long script are all a no-op, because the
    alternative — writing an unparsed blob to disk and executing it — is the
    one outcome this lane must never produce. Any ``path``/``action`` key the
    model emits is IGNORED by construction: they are not read here at all.
    """
    if isinstance(raw_json, (str, bytes)):
        try:
            raw_json = json.loads(raw_json)
        except (ValueError, TypeError) as exc:
            _swallowed(exc, site="si_lanes.work_product._parse_script",
                       category="verify")
            logger.warning(
                "work_product: unparseable JSON (%s)",
                safe_exception_type_name(exc),
            )
            return None
    if not isinstance(raw_json, Mapping):
        logger.warning("work_product: non-object JSON response")
        return None
    script = raw_json.get("script")
    if not isinstance(script, str) or not script.strip():
        logger.warning("work_product: missing/empty script field")
        return None
    if len(script) > _MAX_SCRIPT_CHARS:
        logger.warning(
            "work_product: script too long (%d > %d chars) — dropped",
            len(script), _MAX_SCRIPT_CHARS,
        )
        return None
    # 스키마의 maxLength 는 모델에게 하는 *광고*다 — 강제는 여기서 한다.
    # ⚠️ 다만 script 와 달리 summary/expected_outputs 는 산출물이 아니라
    #    메타데이터다. 길다고 멀쩡한 script 를 통째로 버리면 그게 회귀이므로
    #    드롭이 아니라 **잘라낸다**.
    summary = raw_json.get("summary")
    summary = summary[:_MAX_SUMMARY_CHARS] if isinstance(summary, str) else ""
    outs = raw_json.get("expected_outputs")
    if isinstance(outs, Sequence) and not isinstance(outs, (str, bytes)):
        bounded = [str(o)[:_MAX_EXPECTED_OUTPUT_CHARS]
                   for o in outs[:_MAX_EXPECTED_OUTPUTS]]
    else:
        bounded = []
    return (script, summary, bounded)


def propose_work_product(
    *,
    objective: str,
    cycle_id: str,
    attempt: int,
    propose_fn: ProposeFn,
    feedback: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Ask for one work-product script; return apply-ladder-shaped output.

    Returns ``{"proposed_file_changes": [...], "proposed_artifact_runs": [...],
    "no_op": bool, "reason": str, "summary": str, "cost": Any}``. A no-op
    (provider error, unusable response) returns EMPTY lists and a reason — it
    never returns a partially-formed change.

    ``proposed_artifact_runs`` entries are ``{"path", "cwd"}``, both computed
    locally; the ladder re-validates each against ``plan_artifact_run`` before
    anything executes, because proposing is not permission.
    """
    def _no_op(reason: str) -> dict[str, Any]:
        return {"proposed_file_changes": [], "proposed_artifact_runs": [],
                "no_op": True, "reason": reason, "summary": "", "cost": None}

    if not str(objective or "").strip():
        return _no_op("empty objective")
    prompt, payload, schema = build_payload(objective, feedback)
    try:
        raw = propose_fn(_AGENT_NAME, prompt, payload, schema)
    except Exception as exc:  # noqa: BLE001 — a provider failure is a no-op cycle
        _swallowed(exc, site="si_lanes.work_product.propose_work_product",
                   category="verify")
        logger.warning(
            "work_product: propose_fn failed: %s",
            format_exception_for_log(exc),
        )
        return _no_op(f"propose_fn_error:{safe_exception_type_name(exc)}")
    parsed = _parse_script(raw)
    if parsed is None:
        return _no_op("unusable_response")
    script, summary, expected = parsed
    rel = artifact_rel_path(cycle_id, attempt)
    return {
        "proposed_file_changes": [{
            "path": rel,           # locally computed
            "action": "create",    # hardcoded
            "content": script,     # the ONLY model-supplied value
            "source": _AGENT_NAME,
            "schema_version": _SCHEMA_VERSION,
        }],
        "proposed_artifact_runs": [{"path": rel, "cwd": _WORKSPACE_REL}],
        "no_op": False,
        "reason": "ok",
        "summary": summary,
        "expected_outputs": expected,
        "cost": getattr(propose_fn, "last_usage", None),
    }
