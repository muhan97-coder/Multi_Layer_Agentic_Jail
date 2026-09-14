"""# __SLOT_SI_OBJECTIVE_PROPOSER_F1_INC3_2026_07_02__ objective-driven code proposer (F1 inc3).

Increment 3 of the F1 propose-end producer. Where inc1
(:mod:`agi_v8_1.si_lanes.failure_proposer`) and inc2
(:mod:`agi_v8_1.si_lanes.llm_failure_proposer`) are FAILURE-reactive (they fire
only on a matched signature in the observation bundle) and produce ADVISORY
outputs (a guard-ledger line / a JSON proposal artifact describing a suggestion),
inc3 is OBJECTIVE-driven and produces a REAL code change: given a free-text
objective, it asks an LLM for the complete final content of the relevant file(s)
and emits an apply-ready ``create`` FileChange for each.

This is the component that turns an objective into concrete
``proposed_file_changes`` — the missing producer that made every armed cycle
apply zero changes ($0, empty payload).

Containment invariants (inc3 is a MORE dangerous surface than inc2 — the LLM's
output DOES become the applied ``content`` — so the guards are stricter):

  1. PATH is never trusted from the model. The model supplies only a *filename*;
     the proposer reduces it to ``os.path.basename`` + a strict charset filter
     and places EVERY change under one bounded shadow dir keyed by the objective
     hash: ``si_proposed/objective/<objhash8>/<safe_name>`` (relative to the
     apply ladder's ``repo_root`` == SI ``state_dir``). A model that returns
     ``{"filename": "../../etc/passwd"}`` lands at ``.../objective/<h>/passwd``.
  2. ACTION is the hardcoded constant ``"create"``. Never edit/delete/line-op,
     never model-chosen. ``create`` refuses to overwrite (safe_auto_apply), so a
     change can only ever ADD a preview file, never clobber.
  3. The shadow dir is a PREVIEW of a proposed patch, NOT the live tree. With
     ``repo_root == state_dir`` the applied file is a jailed copy you diff
     against the real source. Arming to edit live code is a separate, later step
     (point the apply ladder's ``repo_root`` at the real tree) — deliberately
     out of scope here.
  4. CONTENT (the one field the model owns) is validated: non-empty, size-bound,
     and — for a ``*.py`` target — must ``ast.parse`` clean or the change is
     dropped BEFORE it reaches the apply ladder.

Design notes:
  - Separately gated (see ``self_improvement_v8._si_objective_proposer_enabled``).
    Default OFF → byte-identical no-op.
  - DATA-DRIVEN (no mode enum / feedback_no_mode_enums_2026_05_28): the objective
    string + the target files' content ARE the domain knowledge. The system
    prompt is a single constant; there is NO per-objective branching.
  - The provider is INJECTED (``propose_fn``) so tests run fully offline & free.
    There is no default real provider, so a caller can never accidentally bill.
  - ``source_root`` (the real tree) is READ-ONLY here: target file content is
    read to give the model context; nothing is ever written to it.
  - Idempotent CHANGE dedup: a shadow file that already exists is dropped (no
    duplicate on disk). This dedups the emitted CHANGE, NOT the provider SPEND —
    the LLM is called before the dedup, so a standing objective re-bills on each
    run unless the caller gates it with ``budget_check`` (the SI call site wires
    the per-day cost cap). Persistence also only happens once the 2-env write
    gate is on; under the default dry-run posture the shadow file is never
    written, so the on-disk dedup does not fire across cycles.
  - Observability-first: every branch logs.
"""
from __future__ import annotations

import ast
import hashlib
import logging
import os
import re
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from agi_v8_1.si_lanes import breadth_select
# __SLOT_SI_PROPOSED_COMMANDS_CHANNEL_2026_08_02__ optional command channel
# (own default-OFF gate; OFF ⇒ prompt/schema/result byte-identical).
from agi_v8_1.si_lanes import command_channel

# __SLOT_FAIL_FAST_2026_07_25__ Swallowed failures route through one choke
# point: counted + named always, re-raised under AGI_V8_STRICT_FAIL_FAST.
from agi_v8_1.policy.fail_fast import (
    format_exception_for_log,
    swallowed as _swallowed,
)

logger = logging.getLogger(__name__)

# Shadow dir (RELATIVE to the apply ladder's repo_root == SI state_dir) under
# which every inc3 change is placed. Distinct subtree from inc1's ledger and
# inc2's patches so the three producers never collide.
_SHADOW_DIR_REL = "si_proposed/objective"
# Provider agent_name for the injected propose_fn / real provider closure.
_AGENT_NAME = "si_objective_proposer"

# __SLOT_HETERO_TIER_N_2026_08_15__ revival v1 seam: cheap env pre-check
# BEFORE importing hetero_lanes (mirrors hetero_lanes._GATE_ENV, duplicated
# here rather than imported so the OFF path costs zero new imports). Actual
# gate authority stays ``hetero_lanes.hetero_enabled()``.
_HETERO_GATE_ENV = "AGI_V8_SI_HETERO_LANES_ENABLED"


def _hetero_precheck() -> bool:
    return os.environ.get(_HETERO_GATE_ENV, "") in ("true", "1")


# Bounds (defense in depth — a runaway/hostile model cannot flood the jail).
_MAX_TARGETS = 12          # target files read + passed to the model as context
# __SLOT_TARGET_READ_FAILCLOSED_2026_07_24__ Two bounds, both fail-closed.
#
# PER-FILE (1 MB): opened up from 100_000, which silently truncated the flagship
# self-edit target (self_improvement_v8.py is ~125 KB) so the model reasoned
# about a partial file while believing it had the whole thing. 1 MB is ~8x the
# largest real source file in this repo, so in practice it never binds — it is a
# sanity bound against a pathological blob, not a content policy.
#
# AGGREGATE (400 KB): the bound that actually matters and did NOT exist before.
# Only the FILE COUNT was capped (_MAX_TARGETS=12), so 12 targets could sum to
# megabytes — far past any model context window (deepseek default max_tokens is
# 131_072), producing an opaque API rejection instead of a clean skip. Raising
# the per-file cap without this would just move the failure, not remove it.
# ~400 KB of code is roughly 100k tokens, leaving room for the system prompt,
# the objective and the response inside a 128k-class context.
_MAX_TARGET_READ_BYTES = 1_000_000    # per source file we read for context
_MAX_TOTAL_READ_BYTES = 400_000       # summed across all targets in one request


def _env_int(name: str, default: int) -> int:
    """Read a positive int env override; any fault falls back to *default*."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        n = int(raw)
    except (TypeError, ValueError) as _ff_exc:
        _swallowed(_ff_exc, site="si_lanes.objective_proposer._env_int:105", category="verify")
        return default
    return n if n > 0 else default


def _max_target_read_bytes() -> int:
    """Per-file read bound (env override, fail-safe default)."""
    return _env_int("AGI_V8_SI_MAX_TARGET_READ_BYTES", _MAX_TARGET_READ_BYTES)


def _max_total_read_bytes() -> int:
    """Aggregate read bound across all targets (env override, fail-safe default)."""
    return _env_int("AGI_V8_SI_MAX_TOTAL_READ_BYTES", _MAX_TOTAL_READ_BYTES)
_MAX_CHANGES = 8           # proposed files emitted per objective
_MAX_CONTENT_BYTES = 200_000       # per proposed file's content

# Generic, per-objective-INDEPENDENT system prompt. The objective + the target
# files ARE the data; this string never changes between objectives.
_SYSTEM_PROMPT = (
    "<role>You are a code-change proposer inside a self-improvement loop.</role>\n"
    "<task>Given an OBJECTIVE and the current content of the relevant files, "
    "propose the minimal set of file changes that achieve the objective. For "
    "EACH file, output its COMPLETE final content (the whole file, never a "
    "diff). Prefer modifying an existing file (echo its exact filename) over "
    "creating new ones.</task>\n"
    "<output_format>Output JSON only matching the schema — no prose, no "
    "markdown fences.</output_format>\n"
    "<containment_note>Your output is written to a sandboxed PREVIEW "
    "directory for review; it is never applied to live code, so propose "
    "freely but keep each file valid and self-contained.</containment_note>"
)

# JSON schema the injected propose_fn is asked to satisfy. Only ``content`` is
# read as an instruction (it becomes the applied file body). ``filename`` is
# read but immediately sanitised; ``rationale`` is DATA for the reviewer.
_PROPOSAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["files"],
    "properties": {
        "summary": {"type": "string"},
        "files": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["filename", "content"],
                "properties": {
                    "filename": {"type": "string"},
                    "content": {"type": "string"},
                    "rationale": {"type": "string"},
                },
            },
        },
    },
}

# Provider-shaped injection seam: (agent_name, prompt, payload, schema) -> json_str.
ProposeFn = Callable[[str, str, Mapping[str, Any], Mapping[str, Any]], str]

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]")


def _objective_hash(objective: str) -> str:
    """Deterministic 8-hex tag for an objective (keys the shadow dir)."""
    return hashlib.sha256(objective.encode("utf-8")).hexdigest()[:8]


def _safe_name(filename: Any, index: int) -> str:
    """Reduce a model-supplied filename to a bounded, escape-proof basename.

    ``os.path.basename`` strips any directory components (incl. ``..``); the
    charset filter removes anything but ``[A-Za-z0-9._-]``. Empty/degenerate
    results fall back to ``proposal_<index>.txt``. Leading dots are stripped so
    the name can never become a dotfile-escape or empty stem.
    """
    base = Path(str(filename)).name  # basename only — drops dirs and ".."
    base = _SAFE_NAME_RE.sub("_", base).lstrip(".")
    if not base or base in (".", ".."):
        base = f"proposal_{index}.txt"
    return base[:128]


def _parse_files(raw_json: Any) -> list[dict[str, str]]:
    """Parse a propose_fn result into a list of ``{filename, content, rationale}``.

    Fail-safe: malformed JSON, non-object JSON, or a missing ``files`` list
    returns ``[]`` (never raises). Items missing ``filename``/``content`` or with
    non-string values are skipped.
    """
    import json

    if not isinstance(raw_json, str):
        return []
    try:
        obj = json.loads(raw_json)
    except Exception as _ff_exc:  # noqa: BLE001 — fail-safe parser: ANY parse error degrades
        # to a no-op, never raises. json.loads can raise beyond ValueError/
        # TypeError (e.g. RecursionError on deeply-nested JSON, a RuntimeError
        # subclass), and a single malformed best-of-N candidate must not abort
        # the others — so the contract here is "never raises", full stop.
        _swallowed(_ff_exc, site="si_lanes.objective_proposer._parse_files:199", category="verify")
        return []
    if not isinstance(obj, Mapping):
        return []
    files = obj.get("files")
    if not isinstance(files, (list, tuple)):
        return []
    out: list[dict[str, str]] = []
    for item in files:
        if not isinstance(item, Mapping):
            continue
        fn = item.get("filename")
        content = item.get("content")
        if not isinstance(fn, str) or not isinstance(content, str):
            continue
        if not fn.strip() or not content.strip():
            continue
        rationale = item.get("rationale")
        out.append(
            {
                "filename": fn,
                "content": content,
                "rationale": rationale if isinstance(rationale, str) else "",
            }
        )
    return out


def _build_changes(
    files: Sequence[Mapping[str, str]],
    objective: str,
    repo_root: Path,
    *,
    id_prefix: str = "f1i3_",
) -> list[dict[str, Any]]:
    """Turn parsed model files into apply-ready ``create`` FileChange dicts.

    The path and action are computed HERE, never from the model (see the module
    containment invariants). Each file is validated (size, py-syntax) and placed
    under the bounded shadow dir. Idempotent: a shadow file that already exists
    is dropped. Deduped by safe_name; capped at ``_MAX_CHANGES``.
    """
    repo_root = Path(repo_root)
    objhash = _objective_hash(objective)
    shadow_root = f"{_SHADOW_DIR_REL}/{objhash}"
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, f in enumerate(files):
        if len(out) >= _MAX_CHANGES:
            logger.info("F1-inc3: reached _MAX_CHANGES=%d — dropping rest", _MAX_CHANGES)
            break
        content = f["content"]
        if len(content.encode("utf-8", "ignore")) > _MAX_CONTENT_BYTES:
            logger.info("F1-inc3: content over %d bytes — dropped", _MAX_CONTENT_BYTES)
            continue
        name = _safe_name(f["filename"], i)
        if name in seen:
            logger.info("F1-inc3: duplicate safe_name %r — dropped", name)
            continue
        if name.endswith(".py"):
            try:
                ast.parse(content)
            except (SyntaxError, ValueError) as exc:
                # SyntaxError = bad python; ValueError = source contains a null
                # byte (raised by ast.parse on CPython ≤3.11). Either way the
                # .py candidate is dropped BEFORE it reaches the apply ladder.
                _swallowed(exc, site="si_lanes.objective_proposer._build_changes:263", category="verify")
                logger.info(
                    "F1-inc3: %s failed py syntax check — dropped: %s",
                    name,
                    format_exception_for_log(exc),
                )
                continue
        rel = f"{shadow_root}/{name}"
        if (repo_root / rel).exists():
            logger.info("F1-inc3: shadow file already exists (idempotent drop): %s", rel)
            continue
        seen.add(name)
        out.append(
            {
                "path": rel,
                "action": "create",
                "content": content,
                # __SLOT_HETERO_ID_NS_2026_08_15__ default "f1i3_" unchanged;
                # callers pass hetero_lanes.PROPOSAL_ID_PREFIX ("f1sw_") only
                # for a candidate-set generated+selected via hetero lanes.
                "proposal_id": f"{id_prefix}{objhash}_{i}",
            }
        )
    return out


def _read_targets(
    target_files: Sequence[str] | None,
    source_root: Path | None,
) -> list[dict[str, str]]:
    """Read the current content of the target files (READ-ONLY, from source_root).

    Bounded by ``_MAX_TARGETS`` and ``_MAX_TARGET_READ_BYTES``. Paths that escape
    ``source_root`` (via ``..`` or absolute) or do not exist are skipped. Returns
    ``[{path, content}]`` used only as model context (never written anywhere).
    """
    if not target_files or source_root is None:
        return []
    root = Path(source_root).resolve()
    out: list[dict[str, str]] = []
    for rel in list(target_files)[:_MAX_TARGETS]:
        try:
            p = (root / rel).resolve()
            p.relative_to(root)  # containment: raises if the path escapes root
        except (ValueError, OSError) as _ff_exc:
            _swallowed(_ff_exc, site="si_lanes.objective_proposer._read_targets:303", category="verify")
            logger.info("F1-inc3: target %r escapes source_root — skipped", rel)
            continue
        if not p.is_file():
            logger.info("F1-inc3: target %r not a file under source_root — skipped", rel)
            continue
        try:
            data = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            _swallowed(exc, site="si_lanes.objective_proposer._read_targets:311", category="verify")
            logger.info(
                "F1-inc3: target %r unreadable — skipped: %s",
                rel,
                format_exception_for_log(exc),
            )
            continue
        # __SLOT_TARGET_READ_FAILCLOSED_2026_07_24__ FAIL-CLOSED on oversize.
        # Previously this sliced to the bound, so the model reasoned about a
        # partial file while believing it had the whole thing — and produced a
        # full-file proposal from that partial view. Skipping loudly is the
        # correct posture: no proposal beats a proposal built on a truncated
        # premise. The bound now matches _MAX_CONTENT_BYTES (what we allow the
        # model to WRITE), so the read/write asymmetry is gone.
        cap = _max_target_read_bytes()
        size = len(data.encode("utf-8", "ignore"))
        if size > cap:
            logger.warning(
                "F1-inc3: target %r is %d bytes > cap %d — SKIPPED (fail-closed; "
                "raise AGI_V8_SI_MAX_TARGET_READ_BYTES to include it)",
                rel, size, cap,
            )
            continue
        # Aggregate bound: the sum is what meets the model's context window, and
        # nothing enforced it before (only the file COUNT was capped). Stop at the
        # first target that would cross it rather than shipping a request the
        # provider will reject.
        total_cap = _max_total_read_bytes()
        so_far = sum(len(o["content"].encode("utf-8", "ignore")) for o in out)
        if so_far + size > total_cap:
            logger.warning(
                "F1-inc3: target %r (%d bytes) would push the request past the "
                "aggregate cap %d (already %d) — SKIPPED (fail-closed; raise "
                "AGI_V8_SI_MAX_TOTAL_READ_BYTES to include it)",
                rel, size, total_cap, so_far,
            )
            continue
        out.append({"path": str(rel), "content": data})
    return out


def propose_from_objective(
    objective: str,
    *,
    repo_root: Path,
    propose_fn: ProposeFn,
    source_root: Path | None = None,
    target_files: Sequence[str] | None = None,
    budget_check: "Callable[[], bool] | None" = None,
    score_fn: "Callable[[Any], float] | None" = None,
) -> dict[str, Any]:
    """Objective (+ optional target files) → inc3 ``create`` changes.

    ``propose_fn`` is REQUIRED (no default real provider) so a caller can never
    accidentally bill. Steps:
      1. validate objective / propose_fn (empty → no-op).
      2. optional ``budget_check()`` (over cap → no-op, propose_fn NOT called).
      3. read target files for context (read-only, from ``source_root``).
      4. call ``propose_fn`` (best-of-N when ``breadth_select`` is active, else
         single-shot) → parse → validate → ``_build_changes``.

    Returns a result dict:
      ``{"proposed_file_changes", "objective", "proposals", "no_op", "cost",
        "reason"}`` — plus ``"proposed_commands"`` (list[str]) ONLY when the
    command channel is gated on AND the model actually asked for a command.
    Absent otherwise, so the dict is byte-identical to the pre-channel shape.
    ``no_op`` deliberately still means "no FILE changes" (``runtime/
    first_run_drills`` reads it that way); commands are a separate channel.

    Every branch logs (observability-first). The proposer never raises on a
    provider/parse failure — it degrades to a no-op with a recorded reason.
    """
    cost: dict[str, Any] = {}

    def _no_op(reason: str) -> dict[str, Any]:
        logger.info("F1-inc3 no-op: %s", reason)
        return {
            "proposed_file_changes": [],
            "objective": objective if isinstance(objective, str) else "",
            "proposals": 0,
            "no_op": True,
            "cost": cost,
            "reason": reason,
        }

    if not isinstance(objective, str) or not objective.strip():
        return _no_op("empty_objective")
    if propose_fn is None:
        return _no_op("no_propose_fn")

    if budget_check is not None:
        try:
            allowed = bool(budget_check())
        except Exception as exc:  # noqa: BLE001 — budget probe never aborts
            _swallowed(exc, site="si_lanes.objective_proposer.propose_from_objective:396", category="verify")
            logger.warning(
                "F1-inc3 budget_check raised (treating as over-cap): %s",
                format_exception_for_log(exc),
            )
            allowed = False
        if not allowed:
            return _no_op("budget_over_cap")

    targets = _read_targets(target_files, source_root)
    user_payload: dict[str, Any] = {
        "task": "propose_minimal_change_for_objective",
        "objective": objective,
        "target_files": targets,
    }
    # __SLOT_SI_PROPOSED_COMMANDS_CHANNEL_2026_08_02__ gate OFF ⇒ ``dict(_PROPOSAL_SCHEMA)``
    # / the unmodified prompt object — the exact bytes shipped before the channel.
    schema = command_channel.with_command_channel(_PROPOSAL_SCHEMA)
    base_prompt = command_channel.prompt_with_command_channel(_SYSTEM_PROMPT)
    commands: list[str] = []

    def _call(system_prompt: str) -> tuple[list[dict[str, str]], list[str]]:
        """Return (files, commands) for ONE response — never banking commands
        itself. __SLOT_SI_CMD_VETTED_ONLY_2026_08_02__ (F3): ``_call`` used to
        ``commands.extend(...)`` directly, so under best-of-N every LOSING
        candidate's commands shipped. The caller now decides which response's
        commands survive."""
        nonlocal cost
        try:
            raw = propose_fn(_AGENT_NAME, system_prompt, user_payload, schema)
        except Exception as exc:  # noqa: BLE001 — provider failure never aborts cycle
            _swallowed(exc, site="si_lanes.objective_proposer._call:414", category="verify")
            logger.warning(
                "F1-inc3 propose_fn failed (non-fatal): %s",
                format_exception_for_log(exc),
            )
            return [], []
        usage = getattr(propose_fn, "last_usage", None)
        if isinstance(usage, Mapping) and usage:
            cost = dict(usage)
        # Commands ride a SEPARATE channel from the file changes: they are not
        # patches, so they must not pass through _build_changes / the verify
        # gate's file machinery. Gate OFF ⇒ parse_commands returns [] always.
        # Ordered AFTER _parse_files so that under AGI_V8_STRICT_FAIL_FAST a
        # malformed response still raises at the ORIGINAL parser's site.
        files = _parse_files(raw)
        return files, command_channel.parse_commands(raw)

    # __SLOT_SI_BREADTH_SELECT_2026_06_18__ best-of-N + OBJECTIVE select. Gate
    # OFF / N=1 ⇒ the single-shot ``else`` below, byte-identical.
    if breadth_select.active():
        # __SLOT_HETERO_TIER_N_2026_08_15__ hetero ON ⇒ tier-sized N off this
        # objective's own target_files. OFF (hetero_mod is None) ⇒
        # breadth_n(), byte-identical to the historical path.
        hetero_mod = None
        if _hetero_precheck():
            from agi_v8_1.capabilities import PayloadPort, resolve_payload

            _hetero_candidate = resolve_payload(PayloadPort(
                9, "agi_v8_1.si_lanes.si_autonomy_payload", "load_hetero_lanes"))()
            if _hetero_candidate.hetero_enabled():
                hetero_mod = _hetero_candidate
        id_prefix = hetero_mod.PROPOSAL_ID_PREFIX if hetero_mod is not None else "f1i3_"
        n = (
            hetero_mod.lane_count(objective, target_files, source_root)
            if hetero_mod is not None else breadth_select.breadth_n()
        )
        cand_lists: list[list[dict[str, Any]]] = []
        cand_cmds: list[list[str]] = []
        cand_usage: list[dict[str, Any] | None] = []
        prompts = [
            base_prompt + "\n\nVariation hint: " + breadth_select.hint_for(ci)
            for ci in range(n)
        ]
        if hetero_mod is not None:
            # __SLOT_HETERO_GENCAND_SWITCH_2026_08_15__ route through the
            # shared generate_candidates seam (the one inc2 already uses)
            # instead of this module's own sequential ``_call`` loop, so a
            # parallel_generate/hetero seam wired onto ``propose_fn`` by the
            # dispatch lane is actually reachable. Per-candidate downstream
            # processing (parse → build → cost snapshot) mirrors ``_call``
            # exactly, one raw/usage pair at a time in generation order, so
            # the shared ``cost`` var's last-candidate-wins semantics match
            # the historical sequential loop byte for byte.
            for raw, u in breadth_select.generate_candidates(
                propose_fn, _AGENT_NAME, prompts, user_payload, schema
            ):
                if raw is None:
                    continue
                if isinstance(u, Mapping) and u:
                    cost = dict(u)
                _files = _parse_files(raw)
                _cmds = command_channel.parse_commands(raw)
                changes = _build_changes(_files, objective, repo_root, id_prefix=id_prefix)
                if changes:
                    cand_lists.append(changes)
                    cand_cmds.append(_cmds)
                    cand_usage.append(dict(u) if isinstance(u, Mapping) and u else None)
                else:
                    # __SLOT_SI_PROPOSER_DROP_RECORD_2026_08_22__ 반증 판정이
                    # "미확정"으로 남긴 두 가지(파싱이 0건을 냈나, 파싱은
                    # 됐는데 검증에서 전부 탈락했나)를 여기서 무조건 갈라
                    # 적는다 — 판정(cand_lists 에 안 넣는다)은 그대로다.
                    logger.info(
                        "F1-inc3 candidate dropped: 0 changes (parsed_files=%d, %s)",
                        len(_files),
                        "no files in model response"
                        if not _files else "all parsed files rejected by _build_changes",
                    )
        else:
            for sp in prompts:
                _files, _cmds = _call(sp)
                changes = _build_changes(_files, objective, repo_root, id_prefix=id_prefix)
                if changes:
                    cand_lists.append(changes)
                    cand_cmds.append(_cmds)
                    cand_usage.append(None)
                else:
                    # __SLOT_SI_PROPOSER_DROP_RECORD_2026_08_22__ 위 hetero
                    # 분기와 같은 무조건 기록(판정은 안 바꾼다).
                    logger.info(
                        "F1-inc3 candidate dropped: 0 changes (parsed_files=%d, %s)",
                        len(_files),
                        "no files in model response"
                        if not _files else "all parsed files rejected by _build_changes",
                    )
        # __SLOT_SCORE_PYTEST_PHANTOM_NOTE_2026_08_22__ ⛔ PHANTOM — this call
        # does NOT pass ``objective=``, even though ``objective`` is in scope
        # (this function's first parameter). Result: ``make_objective_score_fn``
        # defaults ``objective=None`` → ``_derive_target_test(None)`` → always
        # ``None`` → the ±100 per-candidate pytest bonus (breadth_select's
        # ``AGI_V8_SI_SCORE_PYTEST_ENABLED``) is DEAD HERE regardless of that
        # gate's on/off state. NOT wired on purpose (Sol Pro codex
        # investigation, 2026-08-22): wiring it needs (1) the objective text
        # scoped to the raw goal, not the episode-log-injected prefix
        # (runtime/cli.py's ``seed_objective = f"{block}\n\n{objective}"` can
        # precede it) — an oracle-integrity risk this repo already flagged for
        # itself (runtime/goal_campaign.py's ``workspace_objective`` docstring)
        # — and (2) the R17 same-interpreter evaluator residual risk (docker/
        # sandbox/entry.py TODO: evaluator process isolation) closed first.
        # See DOCS/FEATURE_MAP.ko.md's best-of-N row for the one-line pointer.
        sfn = score_fn or breadth_select.make_objective_score_fn(repo_root)

        def _list_score(changes: list[dict[str, Any]]) -> float:
            vals = [sfn(c["content"]) for c in changes]
            return sum(vals) / len(vals) if vals else float("-inf")

        best, scored = breadth_select.select_best(cand_lists, _list_score)
        best_score = max((s for _, s in scored), default=None)
        logger.info(
            "F1-inc3 best-of-N: %d/%d valid candidate-sets, best_score=%s",
            len(cand_lists), n,
            (f"{best_score:.1f}" if best_score is not None else "n/a"),
        )
        changes = best or []
        # __SLOT_SI_CMD_VETTED_ONLY_2026_08_02__ (F3) winner's commands only.
        winner_usage: dict[str, Any] | None = None
        if best is not None:
            for _cl, _cm, _cu in zip(cand_lists, cand_cmds, cand_usage):
                if _cl is best:
                    commands.extend(_cm)
                    winner_usage = _cu
                    break
        # __SLOT_HETERO_SELECT_LOG_2026_08_15__ one row per best-of-N
        # selection this hetero lane made (no-op when exec_select_log's OWN
        # gate is off — record_event's default-OFF contract).
        if hetero_mod is not None:
            from agi_v8_1.si_lanes import exec_select_log as _esl
            winner_model = ""
            if isinstance(winner_usage, Mapping):
                winner_model = str(
                    winner_usage.get("resolved_model_id")
                    or winner_usage.get("model") or "")
            _esl.record_event(
                repo_root, event="select",
                fields={
                    "agent_name": _AGENT_NAME,
                    "worker": hetero_mod.COMPOSITE_WORKER_ID,
                    "n_candidates": len(cand_lists),
                    "best_score": best_score,
                    "winner_model": winner_model,
                    **_esl.content_fingerprint(
                        "\n".join(c.get("content", "") for c in changes)),
                },
            )
    else:
        _files, _cmds = _call(base_prompt)
        changes = _build_changes(_files, objective, repo_root)
        # Single shot: the response parsed (``parse_commands`` already returns []
        # for malformed JSON), so an empty change set is the LEGITIMATE
        # observation-only case AT THIS LANE'S LEVEL — the commands are the
        # whole work product and this lane does not drop them.
        #
        # __SLOT_SI_CMD_VERIFY_GATE_2026_08_02__ adversarial review (CC-4): say
        # what the CYCLE then does with them, because this comment used to
        # contradict the shipped ladder. ``self_improvement_v8`` runs the inc5
        # verify gate only ``if _verify_gate_on and proposer_changes``, and then
        # clears ALL commands unless the gate returned GREEN. So with the verify
        # gate ARMED, an observation-only cycle (commands, zero file changes)
        # has nothing to verify, ``_commands_verified`` stays False, and 100% of
        # these commands are dropped fail-closed. With the verify gate OFF (the
        # default) they flow to the executor seam as written here. That is
        # deliberate — commands are the higher-privilege payload of the same
        # untrusted response — but it is NOT "the commands are the work product
        # and they survive", and reading only this comment used to imply it.
        commands.extend(_cmds)

    reason = "ok" if changes else "no_changes_after_parse_validate_or_idempotent_drop"
    result = {
        "proposed_file_changes": changes,
        "objective": objective,
        "proposals": len(changes),
        "no_op": not changes,
        "cost": cost,
        "reason": reason,
    }
    # __SLOT_SI_PROPOSED_COMMANDS_CHANNEL_2026_08_02__ key present ONLY when the
    # model actually asked for a command (under best-of-N this is the union over
    # candidates, deduped + capped — each one is re-validated downstream anyway).
    command_channel.attach_commands(result, commands)
    logger.info(
        "F1-inc3 result: proposals=%d no_op=%s reason=%s objhash=%s commands=%d",
        result["proposals"], result["no_op"], reason, _objective_hash(objective),
        len(result.get("proposed_commands", ())),
    )
    return result


__all__ = [
    "ProposeFn",
    "propose_from_objective",
]
