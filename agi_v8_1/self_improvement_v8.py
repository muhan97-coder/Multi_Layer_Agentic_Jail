"""agi_v8_1 self-improvement cycle (R18 seed).

v5/v7.1 의 monolithic ``self_improvement_agent`` (1617 LoC) 대신, Pod A/B + Meta
분산 합의 위에 얇은 SI cycle 시드. R18 은 axis_scorer + apply_chain wire.
R19~R20 에서 real agents (executor/planner/critic) + provider lane 통합.

Cycle skeleton:
  1. Receive Pod A / Pod B finding blocks (R18 fixtures; R19 real agents).
  2. Extract 6-axis evidence per pod via :mod:`agi_v8_1.core.axis_scorer`.
  3. Vote per axis -> count axes_aligned.
  4. Derive status: consensus / split / blocked / advisory_stub.
  5. Append decision to R12 apply chain (prev_hash chained).
  6. Append cycle event to cycle_log JSONL.

State files (under ``state_dir``):
  - apply_chain.jsonl   — R12 Merkle audit (verifiable intent).
  - cycle_log.jsonl     — observability stream (not Merkle-chained).
"""

from __future__ import annotations
import json
import logging
import os
import re
import stat
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping, Sequence, TYPE_CHECKING

from agi_v8_1.runtime import objective_patch_mode as _objective_patch_mode
from agi_v8_1.state.store import atomic_append_jsonl, read_jsonl

# __SLOT_FAIL_FAST_2026_07_25__ Swallow choke point. Handlers that must not
# abort a cycle route through this instead of `pass`, so every absorbed failure
# is counted + named (census) and re-raises under AGI_V8_STRICT_FAIL_FAST=true.
from agi_v8_1.policy.fail_fast import (
    CATEGORY_PERSIST as _FF_PERSIST,
    CATEGORY_TELEMETRY as _FF_TELEMETRY,
    CATEGORY_VERIFY as _FF_VERIFY,
    format_exception_for_critical_record as _format_exception_for_critical_record,
    format_exception_for_log as _format_exception_for_log,
    format_exception_for_sink as _format_exception_for_sink,
    safe_exception_type_name as _safe_exception_type_name,
    record_critical_failure as _record_critical_failure,
    swallowed as _swallowed,
)
# META-SI round 2: data-driven 5-stage coverage wire (MUT-5 follow-up).
# All five SI stages register at module import time (idempotent — re-import
# does not double-register thanks to the registry's last-writer-wins on
# payload + count preservation). Stage IDs live in SI_STAGE_IDS so the
# tests + later mutations can iterate without re-declaring strings.
from agi_v8_1.observability.coverage_meter import (
    LOGGER_DROP_TOTAL_STAGE,
    increment_logger_drop_total,
    increment_stage as _coverage_increment_stage,
    register_stage as _coverage_register_stage,
)

# Ordered tuple of the five SI stage_ids wired in round 2. Stable order
# matches the cycle's actual execution sequence so audits / tests can
# assert against it. NO enum: pure data tuple per
# ``feedback_no_mode_enums_2026_05_28``.
SI_STAGE_IDS: tuple[str, ...] = (
    "si.cycle.cycle_start",
    "si.cycle.pod_block_fill",
    "si.cycle.axis_scorer",
    "si.cycle.apply_chain",
    "si.cycle.cycle_end",
)

# Module-import-time registration lock — guards the SI_STAGE_IDS loop
# below from concurrent re-import races (rare under pytest collection,
# but cheap to defend). Also reused by ``_ensure_registered()`` so the
# in-cycle helper and module-init bootstrap share one critical section.
_REGISTRATION_LOCK = threading.Lock()


def _ensure_registered() -> None:
    """Idempotent, thread-safe re-registration of all 5 SI stage_ids.

    META-SI round 3 MUT-2: defends against the
    ``module_import_register_loop_vs_reset_registry`` race called out in
    round-2 RISKY notes. Test fixtures regularly call
    ``coverage_meter.reset_registry()`` between cases; the bootstrap loop
    below only runs at module import time, so a reset performed AFTER
    import-time bootstrap (but BEFORE the cycle's stage-0 increment)
    would leave the meter empty and corrupt the stage-coverage snapshot.

    Calling ``_ensure_registered()`` at cycle entry repaints the
    registry. The underlying ``_CoverageRegistry.register`` preserves
    existing ``count`` on re-registration (last-writer-wins on payload
    only), so test-primed counts are NOT clobbered — confirmed by the
    round-3 idempotency test.
    """
    with _REGISTRATION_LOCK:
        for _sid in SI_STAGE_IDS:
            _coverage_register_stage(
                _sid, payload={"layer": "self_improvement_v8"}
            )


# Module-import bootstrap: registers all 5 stage_ids once. Re-invoked
# defensively at cycle entry via _ensure_registered() to survive
# test-driven reset_registry() calls between the bootstrap and the first
# stage increment.
_ensure_registered()


# --- SIA gate (default OFF) --------------------------------------------
# Lane B (SIA) integration. Strict 'true' check to match the rest of V8's
# env-gate convention (no truthy-string drift). Lane B's controller +
# ledger share the same gate, so a default-OFF cycle never persists a
# SIA chain entry. See agi_v8_1/sia/self_rewrite_controller.py.
_SIA_ENV_ENABLED = "AGI_V8_SIA_ENABLED"


def _sia_enabled() -> bool:
    """Default-OFF SIA gate. True iff AGI_V8_SIA_ENABLED == 'true' exactly."""
    return os.environ.get(_SIA_ENV_ENABLED, "") == "true"  # tier: T1


# --- R1 observation gate (default OFF) ---------------------------------
# C6 fix: cycle_logger.read_since is write-only until this round wires it
# as the SI loop's FIRST production consumer (PART1 S1 "관측 단계"). When
# the gate is ON *and* a cycle_logger is supplied, the loop reads the
# trailing 3-cycle context (via read_since/read_recent) into an
# ObservationBundle and injects it into the cycle's evaluation context.
#
# Strict env match (exact "true"/"1") mirrors the continuation_ring /
# previous_run_context gate convention. Default OFF == byte-equivalent
# no-op: when the gate is unset the observation block never runs, so a
# cycle's apply_chain + cycle_log output is identical to the pre-R1 path.
_CYCLE_LOGGER_ENV_ENABLED = "AGI_V8_CYCLE_LOGGER_ENABLED"

# Trailing window the observation stage rehydrates (v7.1 3-cycle learning).
_OBSERVATION_CYCLE_WINDOW = 3


def _cycle_logger_observation_enabled() -> bool:
    """Default-OFF R1 observation gate.

    True iff ``AGI_V8_CYCLE_LOGGER_ENABLED`` is exactly ``"true"`` or
    ``"1"``. Any other value (including unset) leaves the observation
    stage dormant → byte-equivalent no-op cycle.
    """
    return os.environ.get(_CYCLE_LOGGER_ENV_ENABLED, "") in ("true", "1")  # tier: T2


# __SLOT_SI_CODE_PROPOSER_F1_2026_06_13__ Default-OFF gate for the F1
# failure-signature proposer (the missing producer of proposed_file_changes).
_SI_CODE_PROPOSER_ENV = "AGI_V8_SI_CODE_PROPOSER_ENABLED"


def _si_code_proposer_enabled() -> bool:
    """Default-OFF F1 proposer gate (strict ``"true"``/``"1"``).

    OFF (default) → no proposer run → the apply ladder receives no extra
    changes → byte-equivalent to the pre-F1 empty-payload path.
    """
    return os.environ.get(_SI_CODE_PROPOSER_ENV, "") in ("true", "1")  # tier: T1


# __SLOT_F2_SANDBOX_VERIFY_2026_06_14__ Default-OFF gate for the F2
# sandbox-runner producer in the SI verify path (the missing consumer of
# core.sandbox_runner telemetry inside the closed loop).
_SI_SANDBOX_VERIFY_ENV = "AGI_V8_SI_SANDBOX_VERIFY_ENABLED"


def _si_sandbox_verify_enabled() -> bool:
    """Default-OFF F2 SI-call-site sandbox-verify gate (strict ``"true"``/``"1"``).

    DOUBLE-GATE NOTE: this is only the SI *call-site* gate. The sandbox
    executor ``core.sandbox_runner.run()`` carries its OWN independent internal
    gate ``is_sandbox_enabled()`` (env ``AGI_V8_SWARM_SANDBOX_ENABLED``). When
    that swarm gate is OFF, ``run()`` returns a ``disabled`` *stub* telemetry —
    that is correct graceful degradation (no docker call, advisory no-op), NOT
    an error. So with THIS gate ON but the swarm gate OFF, the verify path runs
    the producer, observes a stub, and folds nothing into the verdict — exactly
    the dormant-but-wired posture.

    OFF (default) → ``_maybe_run_sandbox_verify`` returns None before any import
    or call → the verify stage is byte-identical to the pre-F2 path.
    """
    return os.environ.get(_SI_SANDBOX_VERIFY_ENV, "") in ("true", "1")  # tier: T5


# __SLOT_SANDBOX_FAIL_CLOSED_2026_08_15__ Default-OFF gate for the L3 promotion
# of F2's advisory sandbox signal into an actual apply-blocking verdict
# (revival v1 §1 L3 — the "SEPARATE future gate" F2 explicitly pre-announced
# at :1276-1281). Independent of ``AGI_V8_SI_SANDBOX_VERIFY_ENABLED`` above:
# THIS gate only has any effect when that one is already ON and produced a
# real (non-stub) telemetry. ⚠️ Honest vocabulary note (verify-lens P1,
# 2026-08-15): ``stub=True`` is NOT purely "infrastructure absence" —
# sandbox_runner's STUB_REASONS mixes absence (disabled/docker_missing/
# docker_unavailable/image_missing) with execution-shaped outcomes
# (container_failed/timeout/exception). v1 deliberately treats ALL stub as
# no-fire (an ambiguous run must not block an apply the advisory posture
# would have allowed), and ``verdict["sandbox_gate"].stub_reason`` makes
# those escapes countable. Whether ``timeout`` should fire this gate is an
# explicitly OPEN future decision — do not silently widen the fire set.
_SI_SANDBOX_FAIL_CLOSED_ENV = "AGI_V8_SI_SANDBOX_FAIL_CLOSED"


def _si_sandbox_fail_closed_enabled() -> bool:
    """Default-OFF L3 sandbox fail-closed gate (strict ``"true"``/``"1"``)."""
    return os.environ.get(_SI_SANDBOX_FAIL_CLOSED_ENV, "") in ("true", "1")  # tier: T9


# __SLOT_SI_LLM_PROPOSER_F1_INC2_2026_06_14__ Default-OFF gate for the F1
# increment-2 LLM proposer (the LLM-backed producer of proposal ARTIFACTS).
# Independent of inc1's (AGI_V8_SI_CODE_PROPOSER_ENABLED) and F2's gates: a
# cycle can run any subset of {inc1, inc2, F2} on/off without coupling. Real
# provider spend additionally requires AGI_V8_PROVIDERS_ENABLED (defense in
# depth — see _build_si_llm_propose_fn).
_SI_LLM_PROPOSER_ENV = "AGI_V8_SI_LLM_PROPOSER_ENABLED"


def _si_llm_proposer_enabled() -> bool:
    """Default-OFF F1 inc2 LLM-proposer gate (strict ``"true"``/``"1"``).

    OFF (default) → the inc2 block never runs → ``proposer_changes`` is not
    appended to → byte-equivalent to the inc1-and-inc2-off payload.
    """
    return os.environ.get(_SI_LLM_PROPOSER_ENV, "") in ("true", "1")  # tier: T1


# __SLOT_SI_OUTPUT_SPILL_2026_08_23__ 벤치 백로그 #6-1 (durable output spill).
# ``_run_apply_ladder``'s artifact-run loop keeps only a 2000-char
# ``stdout_tail``/``stderr_tail`` — anything beyond that is silently gone
# from BOTH ledgers the ``a_rec`` dict feeds (workspace/.agent/runs.jsonl
# AND the apply_chain Merkle entry). OFF (default) → the block that reads
# this gate never runs → zero new keys, zero new files → byte-identical to
# before this slot existed.
_SI_OUTPUT_SPILL_ENV = "AGI_V8_SI_OUTPUT_SPILL_ENABLED"

# workspace/.agent/spill/ — same parent (``workspace/.agent``) as the F3
# work-product runs log, so a jail teardown/inspection has one place to look.
_SPILL_DIR_REL = ("workspace", ".agent", "spill")


def _si_output_spill_enabled() -> bool:
    """Default-OFF output-spill gate (strict ``"true"``/``"1"``)."""
    return os.environ.get(_SI_OUTPUT_SPILL_ENV, "") in ("true", "1")  # tier: T9


def _spill_basename(req_rel: str) -> str:
    """Local-only basename sanitize for a spill filename fragment.

    Mirrors the existing ``si_lanes.answer_product.answer_rel_path``
    convention (conservative charset + length cap) rather than inventing a
    new one. ``Path(...).name`` already drops any directory component —
    including ``..`` segments — before the regex ever runs, so a hostile
    ``req_rel`` like ``"../../etc/passwd"`` degrades to just ``"passwd"``;
    nothing traversal-shaped can survive into the filename.
    """
    name = Path(str(req_rel)).name
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name)[:64].strip("._-")
    return safe or "artifact"


def _spill_file_path(state_dir: "Path", req_rel: str, stream: str) -> Path:
    """Compute (never create) a jail-local spill path for one output stream.

    Pure + local: the path is built entirely from ``state_dir`` (operator
    config) and a locally-sanitized fragment of ``req_rel`` (itself already
    the artifact's own **planned** path, not raw model text) plus a wall
    clock stamp and a random suffix for collision-freedom across runs of the
    same artifact in one cycle. The LLM never supplies or influences the
    resulting path beyond contributing characters to a basename that gets
    charset-filtered first.
    """
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    uid = uuid.uuid4().hex[:8]
    fname = f"{ts}_{_spill_basename(req_rel)}_{stream}_{uid}.txt"
    return Path(state_dir).joinpath(*_SPILL_DIR_REL, fname)


def _apply_output_spill(
    a_rec: dict[str, Any], *, stream: str, full_text: str,
    state_dir: "Path", req_rel: str,
) -> None:
    """Gate-guarded pure addition to ``a_rec`` — OFF leaves it untouched.

    ON: always records the true length + whether the existing tail actually
    dropped anything (``*_full_len``/``*_truncated`` — "it was not
    truncated" is itself a fact worth keeping, not a silent default). Only
    when truncation actually happened does it spill the full body to a
    jail-local file (small outputs are not spilled — no wasted I/O for
    something the tail already captured whole).

    Never raises: a spill-write failure (e.g. ``workspace/.agent/spill``
    blocked by a same-named file) degrades to ``*_spill_path=None`` and is
    routed through the fail-fast swallow choke point — the tail this
    function's caller already computed is untouched, so nothing is lost
    beyond the extra channel this gate adds.
    """
    if not _si_output_spill_enabled():
        return
    full_len = len(full_text)
    truncated = full_len > 2000
    a_rec[f"{stream}_full_len"] = full_len
    a_rec[f"{stream}_truncated"] = truncated
    spill_rel: str | None = None
    if truncated:
        try:
            abs_path = _spill_file_path(state_dir, req_rel, stream)
            # 적대검증(HIGH) 회수 — 젤 탈출 봉인: 에이전트는 워크스페이스에
            # 쓸 수 있으므로 ``workspace/.agent/spill`` 자리에 **심링크**를
            # 심어 두면 여기 write 가 그 링크를 따라가 젤 밖(소스트리·홈)에
            # 임의 파일을 만든다. ① 부모 사슬을 실경로로 풀어 젤 안인지
            # 확인하고 ② O_EXCL|O_NOFOLLOW 로 새 일반 파일만 만든다.
            jail_root = Path(state_dir).resolve(strict=False)
            parent = abs_path.parent
            parent.mkdir(parents=True, exist_ok=True)
            real_parent = parent.resolve(strict=False)
            if not (real_parent == jail_root
                    or jail_root in real_parent.parents):
                raise ValueError(
                    "spill 경로가 젤 밖을 가리킨다(심링크 의심) — 거부")
            fd = os.open(
                str(abs_path),
                os.O_WRONLY | os.O_CREAT | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            with os.fdopen(fd, "w", encoding="utf-8", errors="replace") as fh:
                fh.write(full_text)
            spill_rel = abs_path.relative_to(Path(state_dir)).as_posix()
        except Exception as exc:  # noqa: BLE001 — spill must not sink a cycle
            _swallowed(
                exc, site="self_improvement_v8._apply_output_spill",
                category=_FF_PERSIST,
            )
    a_rec[f"{stream}_spill_path"] = spill_rel


def _build_si_llm_propose_fn(state_dir: Path) -> "Any | None":
    """Build the inc2 ``propose_fn`` closure (or None when providers are OFF).

    Defense in depth: real LLM spend requires BOTH the inc2 gate AND
    ``providers_enabled()``. When providers are OFF this returns None, so even
    with the inc2 gate ON the cycle stays free + offline. When ON, it lazily
    imports the canonical real provider (DeepSeek) and returns a closure shaped
    like ``propose_fn`` (the seam tests replace with a stub). The closure also
    mirrors the provider's ``last_usage`` so the proposer can record cost.
    """
    # __SLOT_PARALLEL_SPEND_ATTRIB_2026_08_02__ ``partial_usage_from_exc`` lets
    # the concurrent failure path recover tokens the API already billed before
    # the call died in post-processing (see providers/base.py).
    from agi_v8_1.providers.base import (
        partial_usage_from_exc as _partial_usage_from_exc,
        providers_enabled,
    )

    if not providers_enabled():
        logger.info(
            "F1-inc2: providers disabled → no real propose_fn (gate ON is a no-op)"
        )
        return None

    from agi_v8_1.providers.plugins import load_provider_class

    # __SLOT_SI_WORKER_PROVIDER_2026_08_01__ worker-provider seam. Unset or
    # "deepseek" → the historical DeepSeek path, byte-identical. "openai" →
    # OpenAIModelProvider with an EXPLICIT model (OPENAI_WORKER_MODEL — no
    # default, mirroring the deepseek explicit-model policy) and EXPLICIT
    # per-1k prices (OPENAI_PRICE_PER_1K_INPUT/OUTPUT) so the P2 spend ledger
    # never writes silent-$0 rows (the "numerator is always 0" trap). Any
    # missing piece fail-closes to None — a free offline cycle, never a
    # mispriced billable one.
    _worker = os.environ.get("AGI_V8_SI_WORKER_PROVIDER", "").strip().lower() or "deepseek"
    if _worker == "openai":
        _model = os.environ.get("OPENAI_WORKER_MODEL", "").strip()
        _p_in = os.environ.get("OPENAI_PRICE_PER_1K_INPUT", "").strip()
        _p_out = os.environ.get("OPENAI_PRICE_PER_1K_OUTPUT", "").strip()
        if not (_model and _p_in and _p_out):
            logger.warning(
                "F1-inc2: worker=openai requires OPENAI_WORKER_MODEL + "
                "OPENAI_PRICE_PER_1K_INPUT/OUTPUT (explicit model + explicit "
                "prices) — refusing to build propose_fn (fail-closed)"
            )
            return None
        try:
            OpenAIModelProvider = load_provider_class("openai")

            provider = OpenAIModelProvider(model=_model)
            provider.price_per_1k_input = float(_p_in)
            provider.price_per_1k_output = float(_p_out)
        except Exception as exc:  # noqa: BLE001 — never abort a cycle on import failure
            logger.warning(
                "F1-inc2: openai provider build failed: %s",
                _format_exception_for_log(exc),
            )
            return None
    elif _worker == "openrouter":
        _model = os.environ.get("OPENROUTER_WORKER_MODEL", "").strip()
        if not _model:
            logger.warning(
                "F1-inc2: worker=openrouter requires OPENROUTER_WORKER_MODEL — "
                "refusing to build propose_fn (fail-closed)"
            )
            return None
        try:
            OpenRouterProvider = load_provider_class("openrouter")
            from agi_v8_1.providers.pricing import (
                OPENROUTER_KNOWN_MODELS,
                get_openrouter_pricing,
            )

            _or_key = _model.strip().lower()
            if _or_key not in OPENROUTER_KNOWN_MODELS:
                # 미등재=단가 미상=상한 검사 불가 — inf 산술(NaN 함정)에 기대지
                # 않고 명시 거부(hetero_pod·progress_oracle 과 같은 방향).
                logger.warning(
                    "F1-inc2: worker=openrouter model %r not in "
                    "OPENROUTER_PRICING_CATALOG — refusing (fail-closed)", _model,
                )
                return None
            # __SLOT_REASONING_EFFORT_SI_LANE_2026_08_29__ 정책은 여기서 정한다.
            # provider 는 env 를 안 읽는다(누가 부르는지 모르므로). 이 레인만
            # 읽어 넘기므로 워커 심(bridge/swarm_worker_seam)은 구조적으로 면역이고,
            # 벤치가 재는 대상의 추론 강도는 안 흔들린다.
            # 🔴 왜: 20260829b 에서 si_work_product 3콜이 max_tokens 131,072 를
            # 먹고 2콜이 finish_reason='length' 로 잘렸다(arm 당 39분, 카드 0장).
            # 라이브 실측상 전부 추론 폭주였고 effort=low 면 콜당 ~6,500 토큰으로
            # **완주**한다. 미설정이면 필드 자체가 안 나가 요청은 바이트 동일하다.
            from agi_v8_1.providers.openrouter_provider import (
                si_reasoning_effort,
                si_reasoning_max_tokens,
            )

            provider = OpenRouterProvider(
                model=_or_key, reasoning_effort=si_reasoning_effort(),
                reasoning_max_tokens=si_reasoning_max_tokens(),
            )
            _entry = get_openrouter_pricing(_or_key)
            provider.price_per_1k_input = float(_entry["input_per_1k_tokens_usd"])
            provider.price_per_1k_output = float(_entry["output_per_1k_tokens_usd"])
        except Exception as exc:  # noqa: BLE001 — never abort a cycle on import failure
            logger.warning(
                "F1-inc2: openrouter provider build failed: %s",
                _format_exception_for_log(exc),
            )
            return None
    elif _worker == "deepseek":
        try:
            DeepSeekProvider = load_provider_class("deepseek")
        except Exception as exc:  # noqa: BLE001 — never abort a cycle on import failure
            logger.warning(
                "F1-inc2: deepseek provider import failed: %s",
                _format_exception_for_log(exc),
            )
            return None

        provider = DeepSeekProvider()
    else:
        logger.warning(
            "F1-inc2: unknown AGI_V8_SI_WORKER_PROVIDER=%r — refusing "
            "(fail-closed; valid: deepseek, openai, openrouter)", _worker,
        )
        return None

    # __SLOT_SI_SPEND_LEDGER_P2_2026_07_31__ default-OFF spend rows: without
    # these, the daily cap's ``spent_today`` never sees this lane's (billable)
    # direct provider calls and can never fire. Gate OFF → byte-identical.
    from agi_v8_1.enforcement import si_spend_ledger as _spend_ledger
    from agi_v8_1.runtime.llm_call_observation import capture_request

    def _record_spend(
        agent: str, t0: float, error: str | None,
        pre_usage: "Mapping[str, Any] | None" = None,
        usage_override: "Mapping[str, Any] | None" = None,
        usage_unknown: bool = False,
        *,
        provider_override: "Any | None" = None,
        call_observation: "Any | None" = None,
        provider_error_type: str | None = None,
    ) -> None:
        # __SLOT_HETERO_LANES_ATTACH_2026_08_15__ ``provider_override`` lets a
        # hetero-lane call bill against ITS OWN provider instance (own model,
        # own price) instead of the closure's single ``provider``. ``None``
        # (every existing call site) reads the closure's ``provider`` exactly
        # as before — byte-identical when the hetero gate is OFF.
        _p = provider if provider_override is None else provider_override
        # __SLOT_BREADTH_PARALLEL_2026_08_02__ ``usage_override`` carries the
        # per-call snapshot returned by ``async_throttled_generate_with_usage``.
        # The shared ``provider.last_usage`` read below is correct ONLY while
        # calls are serialised; with N concurrent candidates on one provider it
        # is clobbered between dispatch and read and this row would be priced
        # against another candidate's tokens. None (the sequential path) keeps
        # the historical read byte-identical.
        #
        # __SLOT_PARALLEL_SPEND_ATTRIB_2026_08_02__ ``usage_unknown=True`` is the
        # EXPLICIT "this call's usage is not knowable" signal.
        #
        # Honest accounting of what it buys, because the obvious overclaim is
        # wrong: today it does NOT change the row a caller could get with
        # ``usage_override={}``. An empty usage carries no token evidence, so the
        # ledger's own fail-closed check downgrades ``usage_measured`` to False
        # either way, and the priced cost is $0 either way. What the keyword does
        # buy is the branch below: it bars the fall-through to the SHARED
        # ``provider.last_usage`` unconditionally, so a caller that states its
        # ignorance can never be silently priced against a sibling's tokens even
        # if it forgets ``usage_override``. That guarantee is pinned
        # behaviourally (not just by the AST test) by
        # ``test_the_real_sequential_path_poisons_the_shared_attribute``.
        if usage_unknown:
            usage: dict[str, Any] = {}
        elif usage_override is not None:
            usage = dict(usage_override)
        else:
            usage = dict(getattr(_p, "last_usage", {}) or {})
        # A call that failed BEFORE the provider recorded its usage leaves the
        # PREVIOUS call's tokens in last_usage — billing those again would
        # double-count. Unchanged usage on the error path ⇒ "we don't know"
        # ⇒ an honest $0 error row, not a re-bill. Same "we don't know", so the
        # same marker as the parallel path: consistent across both.
        if error is not None and pre_usage is not None and usage == dict(pre_usage):
            usage = {}
            usage_unknown = True
        _spend_ledger.record_llm_call(
            executor="si_llm_propose",
            agent_name=agent,
            usage=usage,
            price_per_1k_input=float(getattr(_p, "price_per_1k_input", 0.0) or 0.0),
            price_per_1k_output=float(getattr(_p, "price_per_1k_output", 0.0) or 0.0),
            wall_ms=int((time.perf_counter() - t0) * 1000),
            error=error,
            # P2 honesty: distinguishes "$0 because it was free" from "$0 because
            # we never learned the cost". A True claim with no token evidence is
            # downgraded by the ledger, so this can never overstate certainty.
            usage_measured=not usage_unknown,
            **({"call_id": call_observation.call_id,
                "request_observation": call_observation.summary(provider_error_type)}
               if call_observation is not None else {}),
        )

    def _propose_fn(
        agent: str,
        prompt: str,
        payload: Mapping[str, Any],
        schema: Mapping[str, Any],
    ) -> str:
        _t0 = time.perf_counter()
        _pre = dict(getattr(provider, "last_usage", {}) or {})
        _call = (capture_request(_worker, getattr(provider, "model", None))
                 if _spend_ledger.enabled() else None)
        try:
            out = provider.generate(agent, prompt, payload, schema)
        except Exception as exc:
            # A provider exception may have a hostile ``__str__``.  The spend
            # row is part of the cost-control boundary, so format through the
            # total, fail-closed adapter before recording and then re-raise the
            # original object with a bare ``raise``.
            _record_spend(
                agent,
                _t0,
                _format_exception_for_critical_record(exc),
                pre_usage=_pre,
                call_observation=_call,
                provider_error_type=_safe_exception_type_name(exc),
            )
            raise
        # Mirror provider usage onto the closure so propose_from_observation can
        # snapshot it into the result's ``cost`` (it reads propose_fn.last_usage).
        try:
            _propose_fn.last_usage = dict(getattr(provider, "last_usage", {}) or {})
        except Exception as _ff_exc:  # noqa: BLE001
            _swallowed(_ff_exc, site="self_improvement_v8._propose_fn:236", category="verify")
            _propose_fn.last_usage = {}
        _record_spend(agent, _t0, None, call_observation=_call)
        return out

    _propose_fn.last_usage = {}  # type: ignore[attr-defined]

    # __SLOT_BREADTH_PARALLEL_2026_08_02__ Concurrent best-of-N generation seam.
    # Only ``breadth_select.generate_candidates`` looks for this attribute, and
    # only when its own default-OFF gate is on — so an unset gate, or the plain
    # callables tests inject, keep the sequential loop unchanged.
    #
    # Usage is taken from the RETURN VALUE of
    # ``async_throttled_generate_with_usage`` (per-thread sink) instead of the
    # provider's shared ``last_usage``: N concurrent calls on ONE provider
    # instance clobber that attribute between dispatch and read, which
    # MISPRICES the run silently rather than failing (audit-B3, fixed
    # provider-side 2026-07-25). Spend is recorded per call, as sequentially.
    def _parallel_generate(
        agent: str,
        prompts: Sequence[str],
        payload: Mapping[str, Any],
        schema: Mapping[str, Any],
    ) -> list[tuple[str | None, dict[str, Any] | None]]:
        import asyncio

        async def _one(sp: str) -> tuple[str | None, dict[str, Any] | None]:
            _t0 = time.perf_counter()
            _call = (capture_request(_worker, getattr(provider, "model", None))
                     if _spend_ledger.enabled() else None)
            # __SLOT_PRIVATE_USAGE_BOX_2026_08_02__ A dict owned by THIS
            # candidate and nothing else. The provider fills it with this call's
            # sealed usage whether the call succeeds or dies, so the failure
            # branch never has to interrogate a shared object (neither the
            # provider's ``last_usage`` nor an exception instance that a sibling
            # candidate might be raising at the same moment).
            _box: dict[str, Any] = {}
            try:
                out, usage = await provider.async_throttled_generate_with_usage(
                    agent, sp, payload, schema, usage_sink=_box,
                )
            except Exception as exc:  # noqa: BLE001 — one candidate never aborts
                # __SLOT_PARALLEL_SPEND_ATTRIB_2026_08_02__ This branch must
                # NEVER fall through to the shared ``provider.last_usage``.
                # N candidates share ONE provider instance, so that attribute
                # belongs to whichever call wrote it last, and reading it here is
                # wrong in BOTH directions: it either re-bills a sibling
                # candidate's tokens (already charged once via that candidate's
                # own ``usage_override`` — a straight double-count) or, when it
                # is empty/stale, it EVAPORATES tokens that really were charged.
                #
                # Both directions were measured 2026-08-02, on production code
                # paths only (no test-only pokes at the shared dict):
                #   evaporation — a real DeepSeekProvider whose response has
                #     empty ``choices``, which providers/retry.py classifies as
                #     transient, so tenacity pays for it again: 4 paid POSTs,
                #     $16.610 really spent, HEAD ledger $0.110;
                #   double count — the SAME closure exposes a sequential
                #     ``fn(...)`` over the SAME provider, and that path has no
                #     sink, so a finished sequential call leaves its tokens in
                #     the shared attribute through the ordinary setter. A later
                #     concurrent failure reading it books that completed call's
                #     $0.660 a second time.
                # Either way it is the daily cost cap's NUMERATOR going wrong —
                # and in the evaporation direction the cap quietly loosens
                # precisely when calls are failing. Only THIS call's own
                # snapshot may be billed; absent one, say "unknown" out loud
                # rather than "$0".
                #
                # __SLOT_PRIVATE_USAGE_BOX_2026_08_02__ The box is authoritative
                # — it can only have been written by this very invocation. The
                # exception stamp is the fallback for the (currently
                # unreachable) case of a provider that fails before the box is
                # written; it is one-shot, so it cannot be billed twice either.
                _recovered = dict(_box) or _partial_usage_from_exc(exc)
                _record_spend(
                    agent,
                    _t0,
                    _format_exception_for_critical_record(exc),
                    usage_override=_recovered,
                    usage_unknown=not _recovered,
                    call_observation=_call,
                    provider_error_type=_safe_exception_type_name(exc),
                )
                _swallowed(exc, site="self_improvement_v8._parallel_generate",
                           category="verify")
                return (None, None)
            _record_spend(agent, _t0, None, usage_override=dict(usage or {}),
                          call_observation=_call)
            return (out, dict(usage or {}))

        async def _all() -> list[tuple[str | None, dict[str, Any] | None]]:
            return list(await asyncio.gather(*(_one(sp) for sp in prompts)))

        return asyncio.run(_all())

    # __SLOT_HETERO_LANES_ATTACH_2026_08_15__ Revival v1 L1: heterogeneous
    # best-of-N lanes across THREE distinct provider INSTANCES (own model, own
    # price) instead of N samples on the ONE ``provider`` above. Built in the
    # SAME closure as ``_parallel_generate``/``_record_spend`` so a lane call
    # reuses the exact budget-accounting + spend-ledger seam the single-worker
    # path already has — the point of "reuse propose_fn/provider, never
    # swarm_v8" (design doc SWARM_EXECUTOR_REVIVAL_2026_08_15 §1). Defining
    # this factory costs nothing when the gate below is OFF: it does no
    # import/I-O at definition time (only inside, at CALL time) and is never
    # attached to ``_propose_fn.parallel_generate`` unless the gate fires.
    def _make_hetero_parallel_generate(
        hetero_mod: "Any", esl_mod: "Any", hetero_state_dir: Path,
    ):
        from agi_v8_1.capabilities import PayloadPort, resolve_payload

        executor_payload = resolve_payload(PayloadPort(
            9, "agi_v8_1.si_lanes.hetero_executor_payload", "HeteroExecutor",
        ))
        def _hetero_parallel_generate(
            agent: str,
            prompts: Sequence[str],
            payload: Mapping[str, Any],
            schema: Mapping[str, Any],
        ) -> list[tuple[str | None, dict[str, Any] | None]]:
            import asyncio

            n = len(prompts)
            lanes, dropped = hetero_mod.build_lanes(n, agent_name=agent)
            for d in dropped:
                esl_mod.record_event(hetero_state_dir, event="trim", fields=d.as_fields())

            # __SLOT_EXECUTOR_TICKET_CONSUME_2026_08_16__ track 4 — read the
            # executor's planned DispatchTicket order (agents/executor.py
            # ExecutorAgent.plan_dispatch) as a plan INPUT to this cycle's
            # lane assignment. Independent default-OFF gate
            # (AGI_V8_EXECUTOR_TICKET_CONSUME_ENABLED): OFF, or no active
            # plan (si_lanes.ticket_plan.active_plan_scope never entered this
            # call), or a plan whose length does not match this cycle's lane
            # count — ``lanes`` is left exactly as build_lanes produced it,
            # byte-identical to pre-consume behavior. Lazy import mirrors the
            # hetero attach above: zero cost when the gate is off.
            try:
                from agi_v8_1.si_lanes import ticket_plan as _ticket_plan_mod

                if _ticket_plan_mod.tickets_consume_enabled():
                    _plan = _ticket_plan_mod.get_active_plan()
                    if _plan and len(_plan) == len(lanes):
                        lanes = list(
                            _ticket_plan_mod.reorder_lanes_by_plan(_plan, lanes)
                        )
                        esl_mod.record_event(
                            hetero_state_dir,
                            event="ticket_plan",
                            fields={
                                "plan_len": len(_plan),
                                "order": [e.order_index for e in _plan],
                                "original_slots": [
                                    e.original_slot for e in _plan
                                ],
                                "allowed_paths_count": sum(
                                    len(e.allowed_paths) for e in _plan
                                ),
                            },
                        )
            except Exception as exc:  # noqa: BLE001 — ticket consume never blocks a cycle
                _swallowed(
                    exc,
                    site="self_improvement_v8._hetero_parallel_generate:ticket_plan",
                    category="verify",
                )

            # Effective per-cycle cap = min(static, caller's remaining daily
            # budget). An UNKNOWN remaining (cap read failed, or today's spend
            # is only partially measured) is not "unlimited" — it reads as
            # zero, so ``plan_calls`` drops every lane (fail-closed; design
            # doc §1 "정적 상수 단독 금지" — the static constant alone must
            # never authorize spend).
            remaining: float | None
            try:
                from agi_v8_1.runtime import daily_cost_cap as _dcc

                _cap_check = _dcc.check(hetero_state_dir)
                remaining = (
                    float(_cap_check.get("remaining_usd"))
                    if _cap_check.get("spend_is_complete")
                    else None
                )
            except Exception as exc:  # noqa: BLE001 — budget preflight never aborts a cycle
                _swallowed(
                    exc,
                    site="self_improvement_v8._hetero_parallel_generate:budget",
                    category="verify",
                )
                remaining = None

            kept, plan_dropped = hetero_mod.plan_calls(lanes, remaining_usd=remaining)
            for d in plan_dropped:
                esl_mod.record_event(hetero_state_dir, event="trim", fields=d.as_fields())

            if not kept:
                # Hetero dispatch is unavailable this cycle (no lanes built,
                # or the budget preflight trimmed every one) — the historical
                # single-worker path keeps producing candidates rather than
                # the cycle going empty-handed.
                esl_mod.record_event(
                    hetero_state_dir,
                    event="trim",
                    fields={
                        "lane_id": "", "model": "",
                        "reason": "hetero_fallback_to_single", "detail": f"n={n}",
                    },
                )
                return _parallel_generate(agent, prompts, payload, schema)

            # __SLOT_HETERO_PROMPT_ALIGN_2026_08_15__ (verify-lens P1 CONFIRMED)
            # A MID-list drop (an expensive lane trimmed while a cheaper later
            # one survives) must not shift every following prompt onto the
            # wrong lane: each surviving lane keeps its ORIGINAL interleave
            # index (``lane.index``, stamped by ``build_lanes``), its prompt is
            # ``prompts[lane.index]``, and its result lands at that same slot —
            # dropped indices stay ``(None, None)``. Fallback assignment (index
            # missing/duplicated) fills the lowest unused slot.
            assigned = executor_payload.assign(kept, n)

            async def _lane_call(
                idx: int, lane: "Any"
            ) -> tuple[str | None, dict[str, Any] | None]:
                _t0 = time.perf_counter()
                _call = (capture_request(lane.family, lane.model)
                         if _spend_ledger.enabled() else None)
                _box: dict[str, Any] = {}
                try:
                    out, usage = await lane.provider.async_throttled_generate_with_usage(
                        agent, prompts[idx], payload, schema, usage_sink=_box,
                    )
                except Exception as exc:  # noqa: BLE001 — one lane never aborts the set
                    # Same recovery contract as ``_parallel_generate``'s inner
                    # candidate helper: only THIS call's own snapshot (box,
                    # then the exception's partial-usage stamp) may be billed.
                    _recovered = dict(_box) or _partial_usage_from_exc(exc)
                    _err = _format_exception_for_critical_record(exc)
                    _record_spend(
                        agent, _t0, _err,
                        usage_override=_recovered,
                        usage_unknown=not _recovered,
                        provider_override=lane.provider,
                        call_observation=_call,
                        provider_error_type=_safe_exception_type_name(exc),
                    )
                    # Both durable failure rows must land before the ordinary
                    # fail-fast choke point.  In strict mode ``_swallowed``
                    # re-raises the *original* provider exception; keeping it
                    # above ``record_event`` silently erased the generate row
                    # even though the spend row had already survived.  The
                    # exception text is already critical-record formatted and
                    # each downstream writer masks it again at its own sink.
                    esl_mod.record_event(
                        hetero_state_dir,
                        event="generate",
                        fields={
                            "lane_id": lane.lane_id, "model": lane.model,
                            "prompt_idx": idx, "ok": False,
                            "prompt_tokens": (_recovered or {}).get("input_tokens"),
                            "completion_tokens": (_recovered or {}).get("output_tokens"),
                            "wall_ms": int((time.perf_counter() - _t0) * 1000),
                            "error": _err,  # record_event 가 300자 캡+재마스킹(이중 방어)
                        },
                    )
                    _swallowed(
                        exc, site="self_improvement_v8._hetero_parallel_generate",
                        category="verify",
                    )
                    return (None, None)
                _record_spend(
                    agent, _t0, None,
                    usage_override=dict(usage or {}),
                    provider_override=lane.provider,
                    call_observation=_call,
                )
                esl_mod.record_event(
                    hetero_state_dir,
                    event="generate",
                    fields={
                        "lane_id": lane.lane_id, "model": lane.model,
                        "prompt_idx": idx, "ok": True,
                        "prompt_tokens": (usage or {}).get("input_tokens"),
                        "completion_tokens": (usage or {}).get("output_tokens"),
                        "wall_ms": int((time.perf_counter() - _t0) * 1000),
                        "error": "",
                    },
                )
                return (out, dict(usage or {}))

            return asyncio.run(executor_payload.collect(assigned, n, _lane_call))

        return _hetero_parallel_generate

    _propose_fn.parallel_generate = _parallel_generate  # type: ignore[attr-defined]

    # __SLOT_HETERO_LANES_ATTACH_2026_08_15__ default-OFF gate: swap the seam
    # above for heterogeneous-lane dispatch. OFF (default) → the strict check
    # below short-circuits before any import — byte-identical to pre-attach.
    if os.environ.get("AGI_V8_SI_HETERO_LANES_ENABLED", "") in ("true", "1"):
        _hetero_state_dir = Path(state_dir)
        from agi_v8_1.capabilities import PayloadUnavailable

        try:
            from agi_v8_1.capabilities import PayloadPort, resolve_payload
            from agi_v8_1.si_lanes import exec_select_log as _esl

            _hetero = resolve_payload(PayloadPort(
                9, "agi_v8_1.si_lanes.si_autonomy_payload", "load_hetero_lanes"))()
            _propose_fn.parallel_generate = _make_hetero_parallel_generate(  # type: ignore[attr-defined]
                _hetero, _esl, _hetero_state_dir,
            )
            # __SLOT_HETERO_SEAM_MARK_2026_08_15__ (verify-lens P0 CONFIRMED)
            # ``generate_candidates`` routes to ``parallel_generate`` only when
            # ITS OWN gate (AGI_V8_SI_BEST_OF_N_PARALLEL) is on — an
            # undocumented second dependency. This marker lets breadth_select
            # honor the hetero seam on the HETERO gate alone, so "hetero ON"
            # can never half-fire (tier-N + f1sw_ ids + select rows stamped
            # while generation silently ran single-model sequential). The
            # attribute exists ONLY when the hetero gate fired — OFF stays
            # byte-identical.
            _propose_fn.hetero = True  # type: ignore[attr-defined]
            if not _esl.enabled():
                logger.warning(
                    "F1-hetero: AGI_V8_EXEC_SELECT_LOG_ENABLED is OFF — lane "
                    "drops/trims this cycle are NOT countable (the ledger "
                    "no-ops). Arm both gates together for honest accounting."
                )
        except PayloadUnavailable:
            logger.warning("F1-hetero: T9 payload unavailable; read Plz_ReadMe.md §T9")
            raise
        except Exception as exc:  # noqa: BLE001 — installed hetero attach compatibility
            _swallowed(
                exc,
                site="self_improvement_v8._build_si_llm_propose_fn:hetero_attach",
                category="verify",
            )
            logger.warning(
                "F1-hetero: lane attach failed (%s) — keeping single-worker seam",
                _safe_exception_type_name(exc),
            )

    del state_dir  # repo_root is passed separately by the caller (== state_dir)
    return _propose_fn


# __SLOT_CROSS_MODEL_ADVERSARY_2026_07_25__ Optional model override so the S3
# second opinion is genuinely CROSS-model rather than a second sample of the same
# one. Unset → the provider's own resolution applies (same model as the worker);
# that is still an independent call that sees the diff, but it is NOT a
# cross-model check, so we log the distinction instead of implying one.
_CROSS_MODEL_ADVERSARY_MODEL_ENV = "AGI_V81_CROSS_MODEL_VERIFY_MODEL"
# Default reviewer: Claude Sonnet 5. The proposer lanes run on DeepSeek, so a
# Claude reviewer makes S3 a genuine CROSS-model check (different vendor, weights
# and failure modes) rather than a second sample of the same model.
_CROSS_MODEL_ADVERSARY_DEFAULT_MODEL = "claude-sonnet-5"


def _build_cross_model_adversary(changes: "Sequence[Any]") -> "tuple[Any | None, str]":
    """Build the S3 cross-model ``adversary`` closure + a status string.

    Returns ``(adversary, status)``. ``adversary is None`` is SAFE, not a bypass:
    the caller hands None to ``run_cross_model_verify``, which fail-closes to a
    block. So every "cannot review" path degrades to "do not apply", never to
    "skip the check and apply anyway". ``status`` names WHY, so the verdict can
    distinguish providers-off from budget-exhausted from a live reviewer.

    Defense in depth, mirroring ``_build_si_llm_propose_fn``: real reviewer spend
    requires BOTH ``AGI_V81_CROSS_MODEL_VERIFY_ENABLED`` (checked by the caller,
    which only builds this when the verify gate is on) AND ``providers_enabled()``
    — plus, uniquely here, remaining budget on the durable USD ledger.
    """
    from agi_v8_1.providers.base import providers_enabled
    from agi_v8_1.capabilities import PayloadUnavailable

    if not providers_enabled():
        logger.info(
            "cross_model: providers disabled → no real adversary "
            "(verify gate ON stays fail-closed)"
        )
        return None, "none:providers_off"

    model = (
        os.environ.get(_CROSS_MODEL_ADVERSARY_MODEL_ENV, "").strip()  # tier: T6
        or _CROSS_MODEL_ADVERSARY_DEFAULT_MODEL
    )
    try:
        from agi_v8_1.verifier import cross_model_budget as _budget
        from agi_v8_1.capabilities import PayloadPort, resolve_payload

        build_adversary = resolve_payload(PayloadPort(
            6, "agi_v8_1.verifier.cross_model_adversary", "build_adversary",
        ))
        _CM_AGENT = resolve_payload(PayloadPort(
            6, "agi_v8_1.verifier.cross_model_adversary", "AGENT_NAME", False,
        ))
    except PayloadUnavailable as exc:
        _record_critical_failure(exc, site="self_improvement_v8.cross_model_payload", category="verify")
        logger.warning("cross_model: T6 payload unavailable; read Plz_ReadMe.md §T6")
        return None, "none:T6_payload_unavailable"
    except Exception as exc:  # noqa: BLE001 — import failure must not abort a cycle
        logger.warning(
            "cross_model: adversary import failed: %s",
            _format_exception_for_log(exc),
        )
        return None, "none:import_failed"

    # Durable cap re-read from disk every cycle — the armed tick model runs one
    # process per cycle, so an in-memory counter would never accumulate.
    if _budget.exhausted():
        snap = _budget.status()
        logger.error(
            "cross_model: USD budget exhausted (spent $%.4f of $%.2f cap) → "
            "no reviewer; verify stays fail-closed. Raise %s or clear %s to resume.",
            snap["spent_usd"], snap["cap_usd"], _budget.CAP_ENV, snap["ledger"],
        )
        return None, "none:budget_exhausted"

    is_claude = model.startswith("claude-")
    is_openrouter = "/" in model  # OpenRouter 의 provider/model 관례 (예: stealth/ox-alpha)
    cost_envelope: dict[str, Any]
    try:
        _provider_module = "agi_v8_1.verifier.reviewer_provider_payload"
        _output_ceiling = resolve_payload(PayloadPort(6, _provider_module, "output_ceiling"))
        _build_reviewer = resolve_payload(PayloadPort(6, _provider_module, "build_provider"))
        cost_envelope = _budget.call_cost_envelope(
            model=model, max_output_tokens=_output_ceiling(model), paid_attempts=1,
        )
        provider: Any = _build_reviewer(model, _CM_AGENT, cost_envelope)
    except PayloadUnavailable as exc:
        _record_critical_failure(exc, site="self_improvement_v8.reviewer_payload", category="verify")
        logger.warning("cross_model: T6 payload unavailable; read Plz_ReadMe.md §T6")
        return None, "none:T6_payload_unavailable"
    except Exception as exc:  # noqa: BLE001 — provider ctor enforces its own gates
        _swallowed(exc, site="self_improvement_v8._build_cross_model_adversary:315", category="verify")
        logger.warning(
            "cross_model: provider construction failed for model=%s: %s",
            model,
            _format_exception_for_log(exc),
        )
        return None, "none:provider_unavailable"

    worker_model = os.environ.get("DEEPSEEK_WORKER_MODEL", "").strip() or "deepseek default"
    if is_claude:
        logger.info(
            "cross_model: reviewer=%s (Anthropic) vs proposer=%s — genuine cross-model; "
            "budget remaining $%.4f",
            model, worker_model, _budget.remaining_usd(),
        )
    else:
        logger.warning(
            "cross_model: reviewer=%s shares the proposer's provider family (%s) — "
            "this is a SECOND OPINION, not a true cross-model check",
            model, worker_model,
        )

    def _review_fn(
        agent: str,
        prompt: str,
        payload: Mapping[str, Any],
        schema: Mapping[str, Any],
    ) -> str:
        # Reserve-then-reconcile: charge a conservative estimate BEFORE the call
        # so a crash mid-dispatch leaves the ledger over-charged (safe) rather
        # than un-charged (which would silently extend the budget).
        #
        # __SLOT_P0B_USAGE_UNMEASURED_2026_08_17__ V2 makes this defect fix
        # structural: malformed/missing usage can never refund a reservation.
        # The old knob remains observable, but its unsafe rollback direction
        # no longer changes accounting bytes.
        strict = _budget.usage_measured_strict_enabled()
        if not strict:
            # V2 makes the safe measurement contract structural.  The legacy
            # OFF value is retained as an observable compatibility knob, but
            # it may no longer turn malformed usage into a measured refund.
            logger.warning(
                "cross_model: legacy usage-measurement OFF requested; "
                "v2 fail-closed accounting remains active"
            )
        reserve = 0.0
        transaction_id = ""
        try:
            # The reservation is the immutable one-attempt wire envelope,
            # never a prompt-average estimate.  It includes the entire priced
            # context window plus the exact provider max_tokens request, so
            # system/schema framing and tokenizer uncertainty cannot make the
            # pre-dispatch principal smaller than an allowed response.
            if getattr(provider, "max_tokens", None) != cost_envelope["max_output_tokens"]:
                raise RuntimeError("cross_model_envelope_output_drift")
            if getattr(provider, "max_attempts", None) != 1:
                raise RuntimeError("cross_model_envelope_attempt_drift")
            if is_claude and getattr(provider, "prompt_cache_enabled", None) is not False:
                raise RuntimeError("cross_model_envelope_cache_drift")
            if is_claude and getattr(provider, "sdk_max_retries", None) != 0:
                raise RuntimeError("cross_model_envelope_sdk_retry_drift")
            if is_openrouter and getattr(provider, "allow_fallbacks", None) is not False:
                raise RuntimeError("cross_model_envelope_fallback_drift")
            reserve = float(cost_envelope["reserve_usd"])
            # __SLOT_CROSS_MODEL_ATOMIC_RESERVE_2026_08_25__ R21 P1: the old
            # ``exhausted()`` check (in ``_build_cross_model_adversary``, once
            # per process) plus a bare ``record_spend(kind="reserve")`` here
            # were two separate disk round-trips with no lock spanning them —
            # two concurrent processes could both pass the earlier check and
            # both append their reserve, jointly overspending the cap even
            # though each individual append durably succeeded (reproduced:
            # cap=$1.0, two $0.6 reserves, both ack=True, final spent=$1.2).
            # ``reserve_if_capacity`` holds one lock across resum + capacity
            # check + append, so only one racer can ever claim the last slice
            # of headroom.
            #
            # A reservation is always an ESTIMATE, never a measured cost —
            # tag it so a reader can tell a priced reservation apart from a
            # genuinely-measured $0 charge.
            receipt = _budget.reserve_if_capacity(
                reserve, model=model, kind="reserve",
                usage_measured=False,
                detail={"call_cost_envelope": dict(cost_envelope)},
            )
            # A priced estimate is not a reservation until the receipt says
            # so. Refuse before provider dispatch on ANY non-acknowledged
            # receipt (capacity exhausted, non-finite ledger state, or a
            # durable-append failure) — the outer adversary converts this into
            # typed unverifiable evidence and the verify stage stays
            # fail-closed.
            if receipt.get("acknowledged") is not True:
                logger.error(
                    "cross_model: atomic reserve refused for model=%s (%s) — "
                    "refusing reviewer dispatch",
                    model, receipt.get("reason"),
                )
                raise RuntimeError(
                    f"cross_model_reservation_refused:{receipt.get('reason')}"
                )
            transaction_id = str(receipt.get("transaction_id") or "")
            if not _budget._valid_transaction_id(transaction_id):
                logger.error(
                    "cross_model: acknowledged reserve has no valid transaction identity"
                )
                raise RuntimeError("cross_model_reservation_refused:invalid_transaction_id")
            # Reconciliation is defined against the exact 8-decimal amount
            # that was durably written, not the pre-quantized estimate.
            reserve = float(receipt["reserved_usd"])
        except Exception as exc:  # noqa: BLE001 — unpriced model → no dispatch
            logger.error(
                "cross_model: cannot reserve priced call for model=%s (%s) — refusing",
                model,
                _format_exception_for_log(exc),
            )
            raise
        try:
            out = provider.generate(agent, prompt, payload, schema)
        except Exception as provider_exc:  # noqa: BLE001 — paid outcome is unknown
            # The reserve is already durable.  Close the transaction with a
            # zero-delta, explicitly unmeasured row and poison future
            # admission: a provider exception can happen after bytes crossed
            # the wire, so neither billing outcome nor served-model identity
            # may be inferred from the exception alone.
            recorded = _budget.record_spend(
                0.0,
                model=model,
                kind="reconcile",
                usage_measured=False,
                detail={
                    "reserved_usd": reserve,
                    "usage_unmeasured": True,
                    "model_identity_unverified": True,
                    "provider_outcome_unverified": True,
                },
                transaction_id=transaction_id,
            )
            if recorded is not True:
                logger.error(
                    "cross_model: provider failed and uncertainty marker was "
                    "not durable; open reserve remains charged"
                )
            raise RuntimeError("cross_model_provider_outcome_unverifiable") from provider_exc
        reconciled = False
        try:
            raw_last_usage = getattr(provider, "last_usage", None)
            # Provider implementations publish a plain dict.  Snapshot it
            # exactly once so a mutable/shared provider object cannot change
            # identity or token fields between certification and pricing; a
            # hostile Mapping subclass is unmeasured rather than re-read.
            last_usage = (
                dict(raw_last_usage) if type(raw_last_usage) is dict else None
            )
            identity_measured = (
                last_usage is not None
                and type(last_usage.get("requested_model_id")) is str
                and type(last_usage.get("resolved_model_id")) is str
                and last_usage.get("requested_model_id") == model
                and last_usage.get("resolved_model_id") == model
            )
            measured = identity_measured and _budget.usage_is_measured(last_usage)
            if measured and (
                last_usage["input_tokens"] > cost_envelope["max_input_tokens"]
                or last_usage["output_tokens"] > cost_envelope["max_output_tokens"]
            ):
                raise RuntimeError("cross_model_usage_exceeds_cost_envelope")
            billed_attempts = (
                last_usage.get("billed_attempts")
                if isinstance(last_usage, Mapping)
                else None
            )
            if billed_attempts is not None and (
                type(billed_attempts) is not int or billed_attempts != 1
            ):
                raise RuntimeError("cross_model_billed_attempts_exceed_envelope")
            actual = (
                _budget.usage_to_usd(last_usage, model=model) if measured else 0.0
            )
            if not measured:
                # No token evidence at all: do NOT refund the reservation —
                # that would be exactly the "unknown collapsed into $0" defect
                # this gate closes. Keep the conservative reserved amount as
                # the final charge and record the fact explicitly (delta=0,
                # usage_measured=False) rather than staying silent about it.
                recorded = _budget.record_spend(
                    0.0, model=model, kind="reconcile", usage_measured=False,
                    detail={
                        "reserved_usd": reserve,
                        "usage_unmeasured": True,
                        "model_identity_unverified": not identity_measured,
                    },
                    transaction_id=transaction_id,
                )
                if recorded is not True:
                    raise RuntimeError("cross_model_reconcile_not_durable")
                else:
                    reconciled = True
                    logger.warning(
                        "cross_model: review usage UNMEASURED (model=%s) — keeping "
                        "the reserved $%.5f charged (no refund), $%.4f left of $%.2f",
                        model, reserve, _budget.remaining_usd(), _budget.cap_usd(),
                    )
                if not identity_measured:
                    raise RuntimeError("cross_model_model_identity_unverified")
                # Exact served-model identity is necessary but not sufficient
                # to consume the reviewer result.  Missing/malformed token
                # evidence keeps the full envelope charged, and the current
                # apply must fail closed instead of treating an unmeasured paid
                # outcome as an admissible second opinion.
                raise RuntimeError("cross_model_usage_unmeasured")
            else:
                # Net the reserve out and book the measured cost in its place.
                actual_q = _budget.quantize_usd_ceiling(actual)
                delta_q = round(actual_q - reserve, 8)
                recorded = _budget.record_spend(
                    delta_q, model=model, kind="reconcile",
                    usage_measured=True,
                    detail={"actual_usd": actual_q, "reserved_usd": reserve},
                    transaction_id=transaction_id,
                )
                if recorded is not True:
                    raise RuntimeError("cross_model_reconcile_not_durable")
                else:
                    reconciled = True
                    logger.info(
                        "cross_model: review cost $%.5f (model=%s), $%.4f left of $%.2f",
                        actual_q, model, _budget.remaining_usd(), _budget.cap_usd(),
                    )
                if actual_q > reserve + 5e-9:
                    raise RuntimeError("cross_model_cost_envelope_breached")
                if _budget.spent_usd() > _budget.cap_usd() + 5e-9:
                    raise RuntimeError("cross_model_cap_breached_after_reconcile")
        except Exception as exc:  # noqa: BLE001 — apply must stop on accounting uncertainty
            if not reconciled:
                # A successful provider return followed by malformed,
                # overflowing, or envelope-breaking usage is still a paid
                # outcome with unknown trustworthy cost.  Close the open
                # reserve conservatively and poison future admission before
                # rejecting the review result.  Never fold this path to $0.
                uncertainty_recorded = _budget.record_spend(
                    0.0,
                    model=model,
                    kind="reconcile",
                    usage_measured=False,
                    detail={
                        "reserved_usd": reserve,
                        "usage_unmeasured": True,
                        "provider_outcome_unverified": True,
                        "accounting_failure": "unverifiable",
                    },
                    transaction_id=transaction_id,
                )
                if uncertainty_recorded is not True:
                    logger.error(
                        "cross_model: accounting failed and uncertainty marker "
                        "was not durable; open reserve remains charged"
                    )
            logger.error(
                "cross_model: spend reconcile failed; refusing review result: %s",
                _format_exception_for_log(exc),
            )
            raise RuntimeError("cross_model_accounting_unverifiable") from exc
        return out

    return build_adversary(changes, review_fn=_review_fn), model


# __SLOT_PROGRESS_ORACLE_2026_08_22__ bench backlog #2 — builds the call_fn
# the S-end progress ledger oracle invokes (runtime/progress_oracle.py owns
# the prompt/schema/parse; this function owns the provider + cost wiring,
# mirroring ``_build_cross_model_adversary`` immediately above one file up).
def _progress_oracle_usd_from_usage(provider: "Any", usage: Mapping[str, Any]) -> float:
    """Generic (provider-family-agnostic) token→USD estimate.

    Reads ``price_per_1k_input``/``price_per_1k_output`` off the constructed
    provider INSTANCE via getattr — the same duck-typed read
    ``_record_spend`` (si_llm_propose, far above) uses — rather than a
    provider-specific pricing-catalog lookup, so this works unchanged across
    Anthropic/DeepSeek/OpenRouter without a 3-way price branch to keep in
    sync with the 3-way provider-construction branch below.
    """
    price_in = float(getattr(provider, "price_per_1k_input", 0.0) or 0.0)
    price_out = float(getattr(provider, "price_per_1k_output", 0.0) or 0.0)

    def _tok(key: str) -> int:
        v = usage.get(key)
        return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0

    return round(
        (_tok("input_tokens") / 1000.0) * price_in
        + (_tok("output_tokens") / 1000.0) * price_out,
        8,
    )


def _build_progress_oracle_call_fn(
    *, model: str, state_dir: "Path",
) -> "tuple[Any | None, str | None]":
    """Build the progress-oracle ``call_fn`` closure + an unavailable reason.

    Returns ``(call_fn, unavailable_reason)`` — ``call_fn is None`` means the
    caller must not attempt a call this cycle; ``unavailable_reason`` names
    why (status vocabulary owned by ``runtime.progress_oracle``). Every
    "cannot call" path here still lets the caller log a NAMED row — this
    function never silently drops the cycle's oracle observation, it only
    ever refuses to spend on it.

    Provider resolution (3-way, by model-string shape — no per-provider
    special case beyond this): ``claude-*`` → Anthropic; a model id
    containing ``"/"`` (OpenRouter's ``provider/model`` convention, e.g. the
    ox-alpha free-window id ``"stealth/ox-alpha"``) → OpenRouter; anything
    else → DeepSeek (mirrors ``_build_cross_model_adversary``'s original
    2-way fallback, widened by exactly the one case cross-model didn't need).
    """
    from agi_v8_1.providers.base import providers_enabled

    if not providers_enabled():
        return None, "oracle_unavailable:providers_off"

    from agi_v8_1.runtime import progress_oracle as _po

    cap_status = _po.daily_cap_status(state_dir)
    if cap_status["exceeded"]:
        logger.info(
            "progress_oracle: daily cap reached (spent=%s of $%.2f) — skipping "
            "this cycle's call",
            cap_status["spent_usd"], cap_status["cap_usd"],
        )
        return None, _po.STATUS_SKIP_DAILY_CAP

    is_claude = model.startswith("claude-")
    is_openrouter = "/" in model
    try:
        if is_claude:
            from agi_v8_1.providers.anthropic_provider import AnthropicModelProvider

            provider: Any = AnthropicModelProvider(model=model, agent_name=_po.AGENT_NAME)
        elif is_openrouter:
            from agi_v8_1.providers.openrouter_provider import OpenRouterProvider

            from agi_v8_1.providers.openrouter_provider import (
                si_reasoning_effort,
                si_reasoning_max_tokens,
            )

            provider = OpenRouterProvider(
                model=model, agent_name=_po.AGENT_NAME,
                reasoning_effort=si_reasoning_effort(),
                reasoning_max_tokens=si_reasoning_max_tokens(),
            )
        else:
            from agi_v8_1.providers.deepseek_provider import DeepSeekProvider

            provider = DeepSeekProvider(model=model, agent_name=_po.AGENT_NAME)
    except Exception as exc:  # noqa: BLE001 — provider ctor enforces its own gates
        _swallowed(
            exc, site="self_improvement_v8._build_progress_oracle_call_fn:ctor",
            category=_FF_TELEMETRY,
        )
        return None, f"oracle_unavailable:{_safe_exception_type_name(exc)}"

    # __SLOT_PROGRESS_ORACLE_PRICE_PIN_2026_08_22__ 적대검증(08-22) P0:
    # Anthropic/OpenRouter 인스턴스는 price_per_1k_* attr 를 스스로 안 채워서
    # 모든 실콜이 $0.00 으로 기록됐다 — $2 일일 캡이 기본 모델(claude-sonnet-5)
    # 에서 영원히 침묵(미측정→0 접기). hetero_lanes 의 카탈로그 핀 관례를 그대로
    # 따르고, 단가를 못 얻으면(미등재/∞) 콜 자체를 거부한다(fail-closed —
    # si_llm_propose 의 unpriced-openai 거부 선례).
    try:
        if is_claude:
            from agi_v8_1.providers.pricing import get_anthropic_pricing

            _entry: Mapping[str, Any] | None = get_anthropic_pricing(model)
        elif is_openrouter:
            from agi_v8_1.providers.pricing import get_openrouter_pricing

            _entry = get_openrouter_pricing(model)
        else:
            _entry = None  # DeepSeekProvider 는 __init__ 이 단가를 스스로 핀한다
    except Exception as exc:  # noqa: BLE001 — 카탈로그 미등재(KeyError) 포함
        _swallowed(
            exc, site="self_improvement_v8._build_progress_oracle_call_fn:price",
            category=_FF_TELEMETRY,
        )
        return None, "oracle_unavailable:unpriced_model"
    if _entry is not None:
        _pin_in = float(_entry["input_per_1k_tokens_usd"])
        _pin_out = float(_entry["output_per_1k_tokens_usd"])
        if _pin_in == float("inf") or _pin_out == float("inf"):
            return None, "oracle_unavailable:unpriced_model"
        provider.price_per_1k_input = _pin_in
        provider.price_per_1k_output = _pin_out

    def _call_fn(prompt: str, payload: Mapping[str, Any], schema: Mapping[str, Any]) -> str:
        try:
            out = provider.generate(_po.AGENT_NAME, prompt, payload, schema)
        except Exception:
            # Simpler than cross_model's reserve/reconcile on purpose: this
            # call gates NOTHING (record-only, see module docstring), so the
            # extra accounting precision that protects an apply-blocking
            # budget is not proportionate here. Matches si_llm_propose's own
            # plain post-call ``_record_spend`` convention (far above).
            _po.record_spend(state_dir, 0.0, model=model, status="error")
            raise
        usage = dict(getattr(provider, "last_usage", {}) or {})
        _po.record_spend(
            state_dir,
            _progress_oracle_usd_from_usage(provider, usage),
            model=model,
            status="ok",
        )
        return out

    return _call_fn, None


# __SLOT_SI_OBJECTIVE_PROPOSER_F1_INC3_2026_07_02__ Default-OFF gate for the F1
# increment-3 objective-driven code proposer (the producer that turns an
# OBJECTIVE into REAL ``create`` changes — where inc1/inc2 are failure-reactive
# + advisory-only). Independent of inc1/inc2/F2 gates, but mutually exclusive
# with inc4: both produce alternative full postimages for the same canonical
# target. Real provider spend additionally requires AGI_V8_PROVIDERS_ENABLED
# (reuses _build_si_llm_propose_fn — defense in depth).
_SI_OBJECTIVE_PROPOSER_ENV = _objective_patch_mode.PROPOSER_ENV
# Comma-separated relpaths (under the source tree) the objective may read as
# CONTEXT. Operator-bounded allowlist; empty → the proposer runs with no target
# context (objective-only → new-file proposals). Read-only: never written.
_SI_OBJECTIVE_TARGETS_ENV = "AGI_V8_SI_OBJECTIVE_TARGETS"
# Real source-tree root (READ-ONLY for inc3 target context only — the apply
# ladder still writes to the jailed state_dir, not here). self_improvement_v8
# lives AT the package root, so its own parent dir IS the agi_v8_1 package dir.
_SI_SOURCE_ROOT = Path(__file__).resolve().parent


def _si_objective_proposer_enabled() -> bool:
    """Default-OFF F1 inc3 objective-proposer gate (strict ``"true"``/``"1"``).

    OFF (default) → the inc3 block never runs → ``proposer_changes`` is not
    appended to → byte-equivalent to the inc1/inc2-off payload.
    """
    return _objective_patch_mode.proposer_enabled()  # tier: T1


def _si_objective_targets(targets: "Sequence[str] | str | None" = None) -> list[str]:
    """Inc3/4/5 target-file allowlist (comma-sep relpaths), normalized.

    Bounded to 12 entries. Empty/unset → no target context (the proposer then
    produces objective-only new-file proposals).

    __SLOT_CARD_SCOPED_TARGETS_2026_08_22__ *targets* opens the seam for a
    caller that already KNOWS the scope (a goal card declaring
    ``workspace.target_files``) instead of forcing every scope through one
    process-global env var. ``None`` (every existing call site) keeps the
    former env-only behaviour byte-identical — the env stays the live channel
    for the campaign path, where the scope crosses a subprocess boundary and
    ``run_one_si_cycle`` never sees the card at all.

    ⛔ An explicitly passed empty sequence is NOT the same as ``None``: it means
    "the caller measured the scope and it is empty", so we do not silently fall
    back to the ambient env (that would let a stale global override a caller
    who knows better).
    """
    if targets is None:
        raw = os.environ.get(_SI_OBJECTIVE_TARGETS_ENV, "")
        parts: list[str] = raw.split(",")
    elif isinstance(targets, str):
        parts = targets.split(",")
    else:
        parts = [str(t) for t in targets]
    return [t.strip() for t in parts if t.strip()][:12]


def _si_cycle_source_root(
    state_dir: Path,
    target_files: Sequence[str],
) -> Path:
    """Resolve the parent-declared staged repo used by context, verify, apply.

    Ordinary non-campaign SI keeps the historical live read-only source root.
    When goal_campaign transports a single staged repo name, the value is a
    protected parent-derived env fact: resolve only ``state/workspace/<name>``,
    reject symlink components, require a fresh repository directory, and prove
    every declared target is one regular file below that exact root.
    """

    from agi_v8_1.runtime import workspace_snapshot as _wsnap

    repo_name = _wsnap.episode_repo_name_from_env()
    if not repo_name:
        return _SI_SOURCE_ROOT
    if (
        re.fullmatch(r"[A-Za-z0-9_.-]+", repo_name) is None
        or repo_name in {".", ".."}
    ):
        raise ValueError("episode workspace repo name is invalid")
    state = Path(state_dir).resolve(strict=True)
    workspace_spelling = Path(state_dir) / "workspace"
    repo_spelling = workspace_spelling / repo_name
    if workspace_spelling.is_symlink() or repo_spelling.is_symlink():
        raise ValueError("episode workspace repo path contains a symlink")
    workspace = workspace_spelling.resolve(strict=True)
    source = repo_spelling.resolve(strict=True)
    if workspace.parent != state or source.parent != workspace:
        raise ValueError("episode workspace repo escaped the state directory")
    metadata = source.lstat()
    git_metadata = (source / ".git").lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or not stat.S_ISDIR(git_metadata.st_mode)
    ):
        raise ValueError("episode workspace repo is not a fresh git directory")
    for raw_target in target_files:
        target_rel = _wsnap.normalize_protected_rel(raw_target)
        if target_rel is None or target_rel != raw_target:
            raise ValueError("episode target path is not canonical")
        spelling = source.joinpath(*target_rel.split("/"))
        cursor = source
        for part in target_rel.split("/"):
            cursor = cursor / part
            if cursor.is_symlink():
                raise ValueError("episode target path contains a symlink")
        target = spelling.resolve(strict=True)
        target_metadata = target.lstat()
        if (
            source not in target.parents
            or not stat.S_ISREG(target_metadata.st_mode)
        ):
            raise ValueError("episode target is not a regular file in the repo")
    return source


# __SLOT_SI_OBJECTIVE_EDITOR_F1_INC4_2026_07_02__ Default-OFF gate for the F1
# increment-4 anchored-diff editor (inc3's variant that emits find/replace edits
# instead of whole files — cheaper + no drift on untouched lines). Reuses inc3's
# target allowlist + source root.  Its shadow subdir differs from inc3's, but
# verify maps by canonical target basename; enabling both is therefore refused
# before any cycle/provider work rather than silently selecting a winner.
_SI_OBJECTIVE_EDIT_ENV = _objective_patch_mode.EDITOR_ENV


def _si_objective_edit_enabled() -> bool:
    """Default-OFF F1 inc4 anchored-editor gate (strict ``"true"``/``"1"``)."""
    return _objective_patch_mode.editor_enabled()


# __SLOT_SI_VERIFY_GATE_F1_INC5_2026_07_02__ Default-OFF gate for the F1
# increment-5 worktree test-verification gate. When ON, proposed code patches
# (inc3/inc4) are run against the real test suite in an isolated git worktree
# BEFORE the apply ladder sees them; a patch that breaks the suite is dropped
# (fail-closed). OFF (default) → the module is never imported → byte-identical.
_SI_VERIFY_GATE_ENV = "AGI_V8_SI_VERIFY_GATE_ENABLED"

# __SLOT_VERIFY_RESULT_EVENT_2026_08_22__ 관문 판정을 이벤트 스트림에 싣는 게이트.
#
# 🔴 왜 필요했나(2026-08-22 실측): 캠페인 한 에피소드의 세 사이클이
#   passed/failed = 30/1 → 30/1 → 24/7 로 **나빠지는 동안**, 그 사이클들의
#   자기 기억(cycle_log)에는 `consensus_diagnosis: PASS` 가 세 번 찍혀 있었다.
#   관문은 RED 를 내고 있었는데도. 원인은 원장이 둘이라는 것이다 —
#   `cycle_log.jsonl` 은 사람이 읽는 결과 기록이고, 되먹임(관측 번들)은
#   `cycle_logger.read_since` 로 **이벤트 스트림만** 읽는다. 관문 판정은 그
#   스트림에 한 번도 실린 적이 없어서, 루프는 자기가 지난 사이클에 초록 6개를
#   더 깨뜨렸다는 사실을 볼 **수단 자체가 없었다**.
#
# ⛔ 미측정을 0 으로 접지 않는다: 관문이 에러로 끝났으면 passed/failed 는
#   `None` 이고 `measured=False` 다. 0/0 으로 적으면 "깨끗한 사이클"과
#   "재보지 못한 사이클"이 원장에서 구별되지 않는다.
_VERIFY_STAMP_ENV = "AGI_V8_CYCLE_VERIFY_STAMP_ENABLED"
#: 이벤트 한 줄이 원장을 삼키지 않게 실패 시험 이름 개수를 묶는다.
_VERIFY_FAILED_TESTS_CAP = 20


def _verify_stamp_enabled() -> bool:
    """default-OFF. OFF 면 `verify_result` 이벤트가 아예 안 생겨 스트림이 종전과 같다."""
    return os.environ.get(_VERIFY_STAMP_ENV, "") in ("true", "1")  # tier: T9


# __SLOT_CROSS_MODEL_CONSENSUS_ONLY_2026_09_11__ 비합의 사이클의 교차검증 생략.
#
# 실측(2026-09-10, gc-mut-sia-harness_safety-4f60ac84 flash 판): 8사이클 전부
# ``status ∈ {split, advisory_stub}`` 라 ``do_real_write = consensus and verify_pass
# and write_on`` 이 구조적으로 False(apply 는 dry-run)였는데, S3 는 사이클마다
# sonnet 교차검증 2콜을 지불했다 — 16콜 $0.958 = 판 비용 $1.45 의 66%. 그 판정은
# 어디에도 소비되지 않았다(비합의면 쓰기 자체가 없다). ON 이면 사다리가 consensus
# 를 판정한 **뒤** 비합의 사이클에만 S3 에 생략 이유를 넘긴다; consensus 사이클은
# 종전 그대로(교차검증이 쓰기를 막는 유일한 자리). default-OFF, strict, OFF =
# byte-identical(S3 호출 인자도 verdict 모양도 종전과 같다).
_CROSS_MODEL_CONSENSUS_ONLY_ENV = "AGI_V8_SI_CROSS_MODEL_CONSENSUS_ONLY_ENABLED"


def _cross_model_consensus_only_enabled() -> bool:
    return os.environ.get(_CROSS_MODEL_CONSENSUS_ONLY_ENV, "") in ("true", "1")  # tier: T6


# __SLOT_VERIFY_STAMP_GOAL_2026_09_11__ "검증 초록 ≠ 착지" 를 다음 사이클에 싣는다.
#
# 실측(2026-09-10, gc-mut-sia-harness_safety-4f60ac84 flash 판): 후보는 1사이클에 이미
# public 목표 검사를 통과하는 패치를 냈지만(verify 52/0 + public passed) apply 는
# ``apply_blocked=no_consensus`` 8/8 — 다음 사이클 입력에는 ``verify_result=
# {ok:true, passed:52, failed:0}`` 만 갔다. 목표 검사 결과도 "적용 안 됐다"도 0글자.
# ON 이면 ``verify_result`` payload 에 ``public`` 서브딕트(고정 어휘 4키)를 싣는다.
# 별도 게이트인 이유: ``AGI_V8_CYCLE_VERIFY_STAMP_ENABLED`` 는 라이브에서 이미 armed 라
# 거기 얹으면 원장 행 모양이 즉시 바뀐다. default-OFF, strict, OFF = byte-identical.
_VERIFY_STAMP_GOAL_ENV = "AGI_V8_CYCLE_VERIFY_STAMP_GOAL_ENABLED"
_PUBLIC_STAMP_UNKNOWN: "Mapping[str, Any]" = {
    "status": "unknown", "reason": None, "passed": None, "failed": None,
}


def _verify_stamp_goal_enabled() -> bool:
    return os.environ.get(_VERIFY_STAMP_GOAL_ENV, "") in ("true", "1")  # tier: T9


# __SLOT_CROSS_MODEL_REVIEWER_DETAIL_2026_09_13__ 교차검증 판정의 **사유**를 원장에.
# 실측(2026-09-12, B 카드 openai_provider-9956006d·4e561155·26d23934, pro 워커): consensus
# 사이클의 올바른 패치(verify 369/0·public 통과)를 sonnet 2차 의견이 refute 해 dry-run 으로
# 떨어졌는데, 남은 건 apply_chain 의 `apply_verify_blocked_reason="cross_model_refuted"`
# 여섯 글자뿐 — 리뷰어가 돌려준 per-change reason 은 adversary→verifier 경계에서 버려져
# 저널·events·chain·episode 어디에도 없었다. "sonnet 이 옳았나" 를 판정할 수 없다.
# ON 이면 (a) `_run_verify_stage` verdict 에 `cross_model_reviewer`(adversary 의
# `last_verdicts`, path/endorse/reason≤600자, 없으면 None) (b) apply_chain 행에
# `apply_cross_model`(agree/score/reason/evidence≤8/reviewer) 이 실린다.
# default-OFF, strict, OFF = byte-identical(키 자체가 없다). 판정 로직 무접촉 — 관측만.
_CROSS_MODEL_REVIEWER_DETAIL_ENV = "AGI_V8_SI_CROSS_MODEL_REVIEWER_DETAIL_ENABLED"
_CROSS_MODEL_EVIDENCE_MAX_ITEMS = 8


def _cross_model_reviewer_detail_enabled() -> bool:
    return os.environ.get(_CROSS_MODEL_REVIEWER_DETAIL_ENV, "") in ("true", "1")  # tier: T9


def _reviewer_verdicts(adversary: Any) -> "list[dict[str, Any]] | None":
    """adversary 가 마지막 호출에서 남긴 per-change 판정 사본. 없으면(adversary None,
    호출 전, 리뷰 응답 말포드로 raise) None — 모름을 빈 목록으로 접지 않는다."""
    raw = getattr(adversary, "last_verdicts", None) if adversary is not None else None
    if not isinstance(raw, list):
        return None
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        out.append({
            "path": str(item.get("path", "")),
            "endorse": bool(item.get("endorse", False)),
            "reason": str(item.get("reason", "")),
        })
    return out


def _cross_model_detail(verdict: Any) -> "dict[str, Any] | None":
    """verify verdict → apply_chain 행에 실을 교차검증 요약. verdict 없음 → None."""
    if not isinstance(verdict, Mapping):
        return None
    cm = verdict.get("cross_model")
    ran = isinstance(cm, Mapping)
    evidence = list(cm.get("evidence") or ()) if ran else []
    return {
        "adversary": str(verdict.get("cross_model_adversary") or "none"),
        "ran": ran,
        "agree": (bool(cm.get("agree")) if ran else None),
        "agreement_score": (float(cm.get("agreement_score") or 0.0) if ran else None),
        "reason": (str(cm.get("reason") or "") if ran else None),
        "evidence": [str(e) for e in evidence[:_CROSS_MODEL_EVIDENCE_MAX_ITEMS]],
        "reviewer": verdict.get("cross_model_reviewer"),
    }


def _public_stamp(observation: Any) -> dict[str, Any]:
    """public goal check 관측(``agi_public_goal_check_observation_v1``) → 4키 스탬프.

    어휘는 ``runtime.public_goal_checks.validate_observation`` 의 화이트리스트를
    **재사용**한다(사본 금지) — 계약 밖 값은 전부 ``unknown``/None. counts 가 없으면
    passed/failed 는 None 이다(미측정을 0 으로 접지 않는다). ``failed`` 는 pytest 의
    failed+error 합. failed_tests·노드 id·후보 출력은 여기 절대 실리지 않는다.
    """
    from agi_v8_1.runtime.public_goal_checks import validate_observation

    valid = validate_observation(observation)
    if valid is None:
        return dict(_PUBLIC_STAMP_UNKNOWN)
    counts = valid.get("counts")
    passed: int | None = None
    failed: int | None = None
    if isinstance(counts, Mapping):
        passed = int(counts["passed"])
        failed = int(counts["failed"]) + int(counts["error"])
    return {
        "status": str(valid["status"]),
        "reason": str(valid["reason"]),
        "passed": passed,
        "failed": failed,
    }


# --- bench backlog #2: progress ledger oracle gate (default OFF) --------
# __SLOT_PROGRESS_ORACLE_2026_08_22__ Cheap literal-string gate check (no
# import of runtime.progress_oracle) so the OFF path never touches that
# module — mirrors the ``_verify_stamp_enabled`` / ``_CYCLE_LOGGER_ENV_
# ENABLED`` convention one function up. The env NAME is still owned by
# runtime.progress_oracle.ENV_ENABLED (this constant equals it; a test pins
# that equality so the two cannot drift).
_PROGRESS_ORACLE_ENV_ENABLED = "AGI_V8_PROGRESS_ORACLE_ENABLED"


def _progress_oracle_enabled() -> bool:
    """default-OFF. OFF 면 `progress_verdict` 이벤트가 아예 안 생기고 오라클
    관련 모듈이 import 조차 안 된다(아래 call site 참조)."""
    return os.environ.get(_PROGRESS_ORACLE_ENV_ENABLED, "") in ("true", "1")  # tier: T9


# --- bench backlog #3: durable objective reinjection gate (default OFF) --
# __SLOT_DURABLE_GOAL_2026_08_22__ Same cheap literal-string-gate shape as
# ``_progress_oracle_enabled`` one function up (no import of
# runtime.durable_goal on the OFF path). The env NAME is still owned by
# runtime.durable_goal.ENV_ENABLED (this constant equals it; a test pins
# that equality so the two cannot drift).
_GOAL_REINJECT_ENV_ENABLED = "AGI_V8_GOAL_REINJECT_ENABLED"


def _goal_reinject_enabled() -> bool:
    """default-OFF. OFF 면 `goal_reinject` 이벤트가 아예 안 생기고
    durable_goal 관련 모듈이 import 조차 안 된다(아래 call site 참조)."""
    return os.environ.get(_GOAL_REINJECT_ENV_ENABLED, "") in ("true", "1")  # tier: T9


def _si_verify_gate_enabled() -> bool:
    """Default-OFF F1 inc5 verify-gate gate (strict ``"true"``/``"1"``)."""
    return os.environ.get(_SI_VERIFY_GATE_ENV, "") in ("true", "1")  # tier: T5


# __SLOT_C1_RING_REHYDRATE_2026_07_25__ Default-OFF gate for continuation-ring
# rehydration across processes (audit C finding).
#
# ``trailing_escalated_count()`` reads ONLY the ring's in-memory ``_entries``,
# and the one rehydration path (``StatusHistoryRing.load_from_sidecar``) had ZERO
# production callers. In the ARMED tick deployment that is fatal: scripts/tick.sh
# runs ONE process per cron tick, ``_real_dispatch`` → ``run_orchestrator_cycle``
# runs exactly ONE cycle, and the SI loop appends exactly ONE ring entry per
# cycle — so the ring held at most 1 entry per process, the escalate_streak the
# policy saw was capped at 1, and ``force_replan = streak >= 3`` could NEVER fire
# however many consecutive cycles escalated. The JSONL sidecar had the true
# history on disk the entire time; only the read-back was missing.
#
# Arming this makes the SI loop build its default ring via ``load_from_sidecar``
# so the streak survives process boundaries and force_replan becomes reachable.
# OFF (default) keeps the pre-fix empty-ring construction → byte-identical.
_CONTINUATION_RING_REHYDRATE_ENV = "AGI_V8_CONTINUATION_RING_REHYDRATE_ENABLED"


def _continuation_ring_rehydrate_enabled() -> bool:
    """Default-OFF cross-process ring rehydration gate (strict ``"true"``/``"1"``)."""
    return os.environ.get(_CONTINUATION_RING_REHYDRATE_ENV, "") in ("true", "1")


def _previous_run_id(state_dir: "str | Path", cycle_id: Any) -> "str | None":
    """__SLOT_PREV_RUN_UNKNOWN_2026_08_04__ 이전 런의 id — **없으면 None**.

    ⚠️ 2026-08-04 현재 이 함수는 사실상 항상 None 을 돌려준다. 그게 정직한 상태다:
    `core/previous_run_context` 의 계약은 `runs_root/<run_id>/artifacts/` 인데
    **그 경로에 쓰는 코드가 트리 전체에 없다**(젤 구조 = campaign_ledger.jsonl +
    episodes/). 즉 소비자만 있고 생산자가 없다.

    그렇다면 왜 지우지 않고 이 seam 을 두나: 생산자가 생기는 순간 여기 한 곳만
    맞으면 이어지기 때문이다. 그리고 **없다는 사실이 원장에 이름으로 남는다**
    (`prev_run_context_status="no_previous_run"`) — 예전처럼 0.0 으로 위장되지 않는다.

    현재 사이클은 자기 자신의 이전 런이 될 수 없으므로 제외한다. 이건 예전 호출이
    저지른 바로 그 실수다(`previous_run_id=str(cycle_id)`).
    """
    try:
        root = Path(state_dir)
        if not root.is_dir():
            return None
        me = str(cycle_id)
        cands = [
            d for d in root.iterdir()
            if d.is_dir() and d.name != me and (d / "artifacts").is_dir()
        ]
        if not cands:
            return None
        return max(cands, key=lambda d: d.stat().st_mtime).name
    except OSError as _ff_exc:
        _swallowed(_ff_exc, site="self_improvement_v8._previous_run_id", category="verify")
        return None


def _verify_history_rows(
    events: "Sequence[Any]", *, window: int = _OBSERVATION_CYCLE_WINDOW
) -> list[dict[str, Any]]:
    """Fold a raw event stream into trailing per-cycle rows with verify folded in.

    __SLOT_VERIFY_FEEDBACK_ARM_2026_08_23__ Pure extraction of the S1a fold
    (was inline in :func:`_build_observation_bundle`) so a caller that runs
    BEFORE this cycle's own observation_bundle exists — the swarm evidence
    dispatcher, which fires strictly earlier in the same tick
    (orchestrator_v8.py: ``swarm_evidence_blocks()`` at :523 precedes
    ``run_one_si_cycle()`` at :560) — can fold the SAME durable stream the
    same way instead of reinventing it. Mechanical extraction only: output
    is byte-identical to the code this replaces, proved by re-running
    ``tests/v8_1/test_verify_result_feedback_2026_08_22.py`` unchanged.
    """
    per_cycle: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    # __SLOT_VERIFY_RESULT_EVENT_2026_08_22__ 관문 판정은 **따로** 모은다.
    # 위 축약은 사이클당 마지막 이벤트만 남기므로, `verify_result` 뒤에
    # `cycle_end` 가 오면 판정이 덮여 사라진다. 순서에 의존하지 않게 별도 맵에
    # 담고 아래에서 합친다. 게이트 OFF 면 이 맵이 비어 행 모양이 종전과 같다.
    verify_by_cycle: dict[str, dict[str, Any]] = {}
    for ev in events:
        if not isinstance(ev, Mapping):
            continue
        cid = str(ev.get("cycle_id", ""))
        if not cid:
            continue
        payload = ev.get("payload")
        etype = str(ev.get("event_type", ""))
        if etype == "verify_result" and isinstance(payload, Mapping):
            verify_by_cycle[cid] = dict(payload)
        if etype in ("progress_verdict", "goal_reinject"):
            # 적대검증(08-22): 이 축약은 최후 이벤트 승자다 — cycle_end 뒤에
            # 찍히는 progress_verdict 가 사이클 대표행(status)을 덮어 모든
            # 번들 소비자를 오염시켰다. verify_result 전용 맵과 같은 이유로
            # 텔레메트리 2종은 대표행 경합에서 제외한다(창 오염도 방지).
            continue
        if cid not in per_cycle:
            order.append(cid)
        per_cycle[cid] = {
            "cycle_id": cid,
            "event_type": etype,
            "status": (
                payload.get("status")
                if isinstance(payload, Mapping)
                else None
            ),
        }
    trailing_ids = order[-window:] if window > 0 else list(order)
    return [
        ({**per_cycle[c], "verify": verify_by_cycle[c]}
         if c in verify_by_cycle else per_cycle[c])
        for c in trailing_ids
    ]


def recent_verify_history(
    cycle_logger: "Any", *, window: int = _OBSERVATION_CYCLE_WINDOW
) -> list[dict[str, Any]]:
    """Re-read the durable event stream and fold it the same way S1a does.

    __SLOT_VERIFY_FEEDBACK_ARM_2026_08_23__ Exported for a caller that must
    run BEFORE this cycle's own :func:`_build_observation_bundle` exists —
    concretely, ``bridge.si_evidence.swarm_evidence_blocks`` dispatches the
    pods whose text the consensus vote actually reads, and that dispatch
    happens strictly earlier in the tick than this cycle's observation
    stage (see :func:`_verify_history_rows` docstring for the anchor).
    Failures are swallowed the same way S1a swallows them — an unreadable
    stream folds to an empty history, never a crash.
    """
    try:
        # read_since(0.0) == strictly-after-epoch == full ordered stream.
        events = cycle_logger.read_since(0.0)
    except (AttributeError, OSError, ValueError, TypeError) as _ff_exc:
        _swallowed(
            _ff_exc, site="self_improvement_v8.recent_verify_history",
            category="verify",
        )
        events = []
    return _verify_history_rows(events, window=window)


def _build_observation_bundle(
    cycle_logger: "Any",
    *,
    cycle_id: str,
    state_dir: Path,
) -> dict[str, Any]:
    """R1 S1 observation stage — first production consumer of read_since.

    Reads the trailing ``_OBSERVATION_CYCLE_WINDOW`` cycles' events off the
    supplied ``cycle_logger`` (via :meth:`CycleLogger.read_since` — the C6
    fix's load-bearing call) and folds in the previous-run context +
    cycle-policy drift keys (both already-shipped modules, reused per the
    PART1 S1 table). Returns a lightweight, JSON-serialisable dict bundle.

    The bundle is advisory observation only: it carries no raw provider
    output and never mutates state. ``read_since(0.0)`` returns the full
    ordered event stream (strictly-after-epoch == every real event); the
    trailing N distinct cycle_ids form the rehydrated context.
    """
    bundle: dict[str, Any] = {
        "recent_cycles": [],
        "recent_cycle_ids": [],
        # __SLOT_PREV_RUN_UNKNOWN_2026_08_04__ 🔴 여기 기본값이 0.0 이었다.
        # 이전 런이 **없다/못 읽었다** 를 "0점" 으로 적으면 원장에서 둘이 안 갈린다.
        # 이 파일의 자매 지표들이 지키는 원칙과도 어긋난다 — 모름 ≠ 평온.
        "prev_run_best_score": None,
        "prev_run_context_status": "not_attempted",
        "policy_drift_keys": [],
        "observed_count": 0,
    }

    # --- S1a: read_since — trailing 3-cycle rehydration (C6 first use) ---
    try:
        # read_since(0.0) == strictly-after-epoch == full ordered stream.
        events = cycle_logger.read_since(0.0)
    except (AttributeError, OSError, ValueError, TypeError) as _ff_exc:
        _swallowed(_ff_exc, site="self_improvement_v8._build_observation_bundle:493", category="verify")
        events = []

    # Reduce events to the trailing N distinct cycles (newest last), verify
    # folded in — factored into _verify_history_rows (mechanical extraction,
    # byte-identical output) so recent_verify_history() can reuse the exact
    # same fold from a caller that runs before this bundle exists.
    recent_rows = _verify_history_rows(events, window=_OBSERVATION_CYCLE_WINDOW)
    bundle["recent_cycles"] = recent_rows
    bundle["recent_cycle_ids"] = [str(r["cycle_id"]) for r in recent_rows]
    bundle["observed_count"] = len(recent_rows)

    # --- S1b: previous_run_context (reused module; gate-honoring) -------
    # load_previous_run_context is itself env-gated (AGI_V8_PREV_RUN_CONTEXT_
    # ENABLED) and returns an empty PrevRunContext when its own gate is OFF,
    # so this is a safe best-effort read that adds the prior rubric baseline
    # only when that gate is also enabled.
    # __SLOT_PREV_RUN_UNKNOWN_2026_08_04__
    # 🔴 여기 호출이 **현재 사이클 id 를 "이전 런 id" 자리에** 넘기고 있었다:
    #       load_previous_run_context(previous_run_id=str(cycle_id), runs_root=state_dir)
    # 모듈 계약은 `runs_root/<previous_run_id>/artifacts/` 인데
    #   ① cycle_id 는 이전 런이 아니라 지금 돌고 있는 사이클이고
    #   ② 그 경로에 **쓰는 코드가 트리 전체에 하나도 없다**(젤 구조는
    #      campaign_ledger.jsonl + episodes/ 다)
    # 그래서 항상 빈 컨텍스트가 돌아왔고, 원장에는 `prev_run_best_score: 0.0` 이
    # 매 사이클 **정상 baseline 처럼** 찍혔다. 꺼진 것보다 나쁘다 — 꺼졌으면
    # "안 이어진다"를 알기라도 한다.
    #
    # 지금 고칠 수 있는 것과 없는 것을 가른다:
    #   고침   거짓 0.0 을 지운다. 이전 런 id 가 없으면 **호출하지 않고** None 으로 둔다
    #   못 고침 진짜 이어붙이려면 **생산자**가 필요하다(그 경로에 쓰는 쪽).
    #          그건 사이클 간 기억 채널 설계 항목이고, 여기서 조용히 지어내지 않는다.
    prev_status_tail: tuple[str, ...] = ()
    prev_run_id = _previous_run_id(state_dir, cycle_id)
    if prev_run_id is None:
        bundle["prev_run_context_status"] = "no_previous_run"
    else:
        try:
            from agi_v8_1.core.previous_run_context import (
                load_previous_run_context,
            )

            prev_ctx = load_previous_run_context(
                previous_run_id=prev_run_id,
                runs_root=Path(state_dir),
            )
            if prev_ctx.is_empty():
                bundle["prev_run_context_status"] = "found_but_empty"
            else:
                bundle["prev_run_best_score"] = float(prev_ctx.best_score)
                bundle["prev_run_context_status"] = "loaded"
                prev_status_tail = tuple(prev_ctx.status_history_tail)
        except (ImportError, OSError, ValueError, TypeError) as _ff_exc:
            bundle["prev_run_context_status"] = (
                f"error:{_safe_exception_type_name(_ff_exc)}"
            )
            _swallowed(_ff_exc, site="self_improvement_v8._build_observation_bundle:540", category="verify")

    # --- S1c: cycle_policy_drift.drift_keys (reused module) -------------
    # Compare the trailing two observed cycle snapshots to surface which
    # observable fields drifted between them — the same drift signal v7.1
    # fed into build_cycle_policy. Pure function, no state.
    try:
        from agi_v8_1.core.cycle_policy_drift import drift_keys

        recent = bundle["recent_cycles"]
        if len(recent) >= 2:
            bundle["policy_drift_keys"] = drift_keys(recent[-1], recent[-2])
    except (ImportError, ValueError, TypeError) as _ff_exc:
        _swallowed(_ff_exc, site="self_improvement_v8._build_observation_bundle:553", category="verify")
        pass

    if prev_status_tail:
        bundle["prev_status_history_tail"] = list(prev_status_tail)

    return bundle


# --- R2 SI dispatcher gate (default OFF) --------------------------------
# C13 fix: ``sia/self_improvement_dispatcher_v8.py SIDispatcherV8.dispatch_cycle``
# (the pod-aware 7-stage advisory dispatcher) shipped with a unit-tested body
# but ZERO production caller — the ``phase_executors/`` glue that was supposed
# to invoke it does not exist in this tree (C8/C13). This round wires
# ``dispatch_cycle`` directly into ``run_one_si_cycle`` (no glue), gated behind
# the dispatcher's own ``AGI_V8_SI_DISPATCHER_V8_ENABLED`` env. Gate-ON →
# advisory dispatch runs (its own internal default-OFF short-circuit is
# bypassed because the same env flips both); gate-OFF (default) → no-op, no
# disk write, byte-equivalent to the pre-R2 cycle.
#
# The dispatcher is ADVISORY: it persists its own 7-stage chain JSONL under
# state_dir, never touches the SI apply chain, and its DispatchOutcome is
# surfaced only as a small summary on SICycleOutcome (no apply, no shell).
_SI_DISPATCHER_ENV_ENABLED = "AGI_V8_SI_DISPATCHER_V8_ENABLED"


def _si_dispatcher_v8_enabled() -> bool:
    """Default-OFF R2 dispatcher gate.

    True iff ``AGI_V8_SI_DISPATCHER_V8_ENABLED`` is exactly ``"true"``. This is
    the SAME env the dispatcher's own ``SIDispatcherV8.is_enabled()`` keys off,
    so a single flag both authorises the call SITE here and un-short-circuits
    the dispatcher body — no second gate to drift out of sync.
    """
    return os.environ.get(_SI_DISPATCHER_ENV_ENABLED, "") == "true"


def _run_si_dispatcher_advisory(
    *,
    cycle_id: str,
    state_dir: Path,
    status: str,
    input_mode: str,
    pod_ids: Sequence[str] = (),
) -> Mapping[str, Any] | None:
    """R2: invoke ``SIDispatcherV8.dispatch_cycle`` directly (no glue).

    Returns a lightweight, JSON-serialisable summary of the dispatcher's
    :class:`DispatchOutcome` (cycle_id / enabled / stages / chain entries /
    failures count), or ``None`` when the gate is OFF or the dispatch raises.
    Advisory only — the dispatcher writes its OWN chain file under ``state_dir``
    and never mutates the SI apply chain.
    """
    if not _si_dispatcher_v8_enabled():
        return None
    try:
        from agi_v8_1.capabilities import PayloadPort, resolve_payload

        SIDispatcherV8 = resolve_payload(PayloadPort(
            9, "agi_v8_1.si_lanes.si_autonomy_payload", "dispatcher_class"))()

        dispatcher = SIDispatcherV8(state_dir=state_dir)
        outcome = dispatcher.dispatch_cycle(
            cycle_id,
            role_id="si_loop_v81",
            pod_ids=tuple(pod_ids),
            inputs={"si_status": status, "input_mode": input_mode},
        )
        return {
            "cycle_id": outcome.cycle_id,
            "enabled": outcome.enabled,
            "stage_count": len(outcome.stage_records),
            "chain_entries_written": outcome.chain_entries_written,
            "failures": len(outcome.failures),
            "last_entry_hash": outcome.last_entry_hash,
            "input_mode": outcome.input_mode,
        }
    except (ImportError, OSError, ValueError, TypeError, RuntimeError, KeyError) as _ff_exc:
        # Advisory dispatch must never abort the SI cycle. Swallow the
        # realistic dispatch fault surface and report None (= not dispatched).
        _swallowed(_ff_exc, site="self_improvement_v8._run_si_dispatcher_advisory:627", category="verify")
        return None


# --- R3 apply ladder (C7 반증) -----------------------------------------------
# PART1 §2.1/§2.3/§2.4 + S4/S5. The pre-R3 cycle wrote a Merkle apply_chain
# entry with literal placeholder snapshot-hash strings (``pre_apply_hash`` /
# ``post_apply_hash`` set to fixed ``stub_*`` sentinels) — an apply *theatre*:
# the audit chain recorded an apply that never happened (audit claim C7/C16).
# R3 replaces the theatre with a real ladder:
#
#   S3 verify : rubric_emergency (opt-out gate, default ON) → BLOCK on
#               emergency regression; pre_commit_gate_v81 g1-g8 (fail-closed)
#               run against the changed-file set just before apply.
#   S4 apply  : when consensus AND verify-pass AND write_enabled() → run a real
#               SafeAutoApply.apply_session; when the 2-env write gate is OFF
#               (default) → dry_run=True ApplyResult, still computing the REAL
#               (dry-run) aggregate hash of the on-disk snapshot. Either way the
#               closed loop completes and S5 records a measured hash.
#   S5 record : apply_chain_full.append with result.pre/post_apply_hash (real),
#               + a separate decision_source="rollback" entry when a session
#               revert fired (PART1 §2.4 audit parity).
#
# The verify stage is run unconditionally (it is local + cheap); the WRITE is
# what the 2-env operator gate protects. So the ladder always produces a real
# ApplyResult — gate-OFF just means dry_run. NO mode enum: the ladder branches
# on data (consensus status + verdict + write_enabled), never on a hardcoded
# mode string (feedback_no_mode_enums_2026_05_28).


def _extract_file_changes(*pod_blocks: Mapping[str, Any] | None) -> list[Any]:
    """Schema-driven extraction of apply-ready FileChange[] from pod blocks.

    A pod block MAY carry a ``proposed_file_changes`` list (data-driven, no mode
    enum): each entry is a mapping with at least ``path`` + ``action`` (matching
    :class:`SafeAutoApply.FileChange`). When absent (the common case — stub
    fixtures, advisory-only cycles) this returns ``[]``: the ladder still runs,
    producing a real empty-set aggregate hash so the closed loop completes.
    """
    from agi_v8_1.enforcement.safe_auto_apply import FileChange

    changes: list[Any] = []
    for block in pod_blocks:
        if not isinstance(block, Mapping):
            continue
        raw = block.get("proposed_file_changes")
        if not isinstance(raw, (list, tuple)):
            continue
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            path = str(item.get("path", "")).strip()
            action = str(item.get("action", "")).strip()
            if not path or not action:
                continue
            changes.append(
                FileChange(
                    path=path,
                    action=action,
                    content=str(item.get("content", "")),
                    old_content=str(item.get("old_content", "")),
                    target_line=str(item.get("target_line", "")),
                    replacement=str(item.get("replacement", "")),
                    proposal_id=str(item.get("proposal_id", "")),
                    expected_preimage_sha256=str(
                        item.get("expected_preimage_sha256", "")
                    ),
                )
            )
    return changes


# __SLOT_W_STAGE3_COMMAND_EXEC_2026_06_18__ Schema-driven extraction of proposed
# SHELL COMMANDS from pod blocks (a separate channel from proposed_file_changes:
# a command has no file to back up, so it never enters the SafeAutoApply file
# session). A pod block MAY carry ``proposed_commands`` — a list of command
# strings. Absent → []: the command seam stays a no-op and the apply ladder is
# byte-identical.
#
# __SLOT_SI_PROPOSED_COMMANDS_CHANNEL_2026_08_02__ PRODUCER NOTE: from Stage 3
# (2026-06-18) until 2026-08-02 this reader had NO writer anywhere in the tree
# (grep: 2 readers, 0 producers), so the armed executor was unreachable — the
# campaign's executor_log measured command=0. The producers are now the F1 LLM
# lanes (inc2/inc3/inc4) via ``si_lanes/command_channel.py``, riding in on the
# synthetic proposer block built by ``_run_apply_ladder``. A non-list value is
# still [] (a bare string is NOT split into a command here), so no pod-block
# contract shifted.
#
# __SLOT_SI_CMD_GLOBAL_CAP_2026_08_02__ adversarial-review-3 fix (F6). This
# extractor USED to be strictly weaker than the lane-side normaliser it claimed
# to defer to: ``str(item).strip()`` STRINGIFIES a non-string item, so ``None``
# became the command ``"None"`` and ``{"a": 1}`` became ``"{'a': 1}"`` — a
# command fabricated out of a malformed field — and there was no newline/NUL
# check, no length cap and no dedup. Harmless on the proposer path (the lane
# normalises first) but the pod-block path has no such upstream, and pod blocks
# are caller-supplied. It now delegates to the ONE normaliser (which also caps
# the total ACROSS blocks), so there is a single definition of "a command
# shape", not two that drift.
def _extract_commands(*pod_blocks: Mapping[str, Any] | None) -> list[str]:
    flat: list[Any] = []
    for block in pod_blocks:
        if not isinstance(block, Mapping):
            continue
        raw = block.get("proposed_commands")
        if not isinstance(raw, (list, tuple)):
            continue
        flat.extend(raw)
    if not flat:
        return []
    # Lazy import: never taken on the production pod path (pods emit no
    # commands), so the no-commands cycle stays byte-identical AND import-free.
    from agi_v8_1.si_lanes.command_channel import normalize_commands

    return normalize_commands(flat)


# __SLOT_F2_SANDBOX_VERIFY_2026_06_14__ pure predicate: from the apply-ready
# FileChange[] set, select the single whole-file .py unit (+ optional test) the
# sandbox should execute. Data-driven only — NO mode enum / NO per-domain
# branching (feedback_no_mode_enums_2026_05_28).
def _select_sandbox_unit(
    changes: Sequence[Any],
) -> tuple[str, str | None, str] | None:
    """Pick the first runnable whole-file ``.py`` change + an optional test.

    A "runnable unit" is a FileChange-like object whose ``action`` is
    ``create``/``replace`` (whole-file actions — NOT line fragments like
    ``insert_line``/``delete_line``/``replace_line``), whose ``path`` ends in
    ``.py``, that is NOT a test path (basename ``test_*.py`` / ``*_test.py``),
    and whose ``content`` is non-empty.

    Separately scans for the first *test* FileChange (basename ``test_*.py`` /
    ``*_test.py``, create/replace, non-empty content) to hand the sandbox a test
    to run against the code.

    Returns ``(code, test_or_None, lane_id)`` where ``lane_id`` is the basename
    of the code path, or ``None`` when no whole-file ``.py`` code unit is
    present. Performs NO I/O.
    """
    _whole_file_actions = {"create", "replace"}

    def _is_test_basename(name: str) -> bool:
        return name.startswith("test_") and name.endswith(".py") or name.endswith(
            "_test.py"
        )

    code: str | None = None
    code_lane: str = ""
    test: str | None = None

    for ch in changes:
        action = str(getattr(ch, "action", "")).strip()
        path = str(getattr(ch, "path", "")).strip()
        content = str(getattr(ch, "content", "") or "")
        if action not in _whole_file_actions:
            continue
        if not path.endswith(".py"):
            continue
        if not content:
            continue
        basename = path.rsplit("/", 1)[-1]
        if _is_test_basename(basename):
            if test is None:
                test = content
            continue
        # First non-test whole-file .py change → that's the code unit.
        if code is None:
            code = content
            code_lane = basename

    if code is None:
        return None
    return (code, test, code_lane)


# __SLOT_F2_SANDBOX_VERIFY_2026_06_14__ producer wire: run the sandbox executor
# over the selected unit and return its telemetry (advisory). Default-OFF.
def _maybe_run_sandbox_verify(changes: Sequence[Any]) -> "SandboxTelemetry | None":
    """Run ``core.sandbox_runner.run`` over the selected unit; advisory only.

    Contract (observability_first — NO silent branch):
      - SI gate OFF → return None *before* any import or selection (byte-identical
        guarantee for the default path).
      - No runnable unit in the change set → info log + return None.
      - Missing T5 operation code raises a typed refusal, converted here to
        named unavailable telemetry. Installed execution failures keep their
        existing stubs; other faults retain the fail-fast diagnostic path.
      - A stub telemetry (e.g. swarm gate ``AGI_V8_SWARM_SANDBOX_ENABLED`` OFF,
        docker absent) → info log carrying ``stub_reason`` (graceful degradation,
        not an error) and is returned as-is.
      - A real telemetry → info log with the exec signals.
    """
    if not _si_sandbox_verify_enabled():
        return None  # byte-identical default path

    unit = _select_sandbox_unit(changes)
    if unit is None:
        logger.info(
            "F2 sandbox: no runnable unit in change set; sandbox no-op "
            "(change_count=%d)",
            len(changes),
        )
        return None
    code, test, lane = unit

    from agi_v8_1.core import sandbox_runner
    from agi_v8_1.capabilities import PayloadUnavailable

    try:
        telemetry = sandbox_runner.run(code=code, test=test, lane_id=lane)
    except PayloadUnavailable as exc:
        _record_critical_failure(exc, site="self_improvement_v8.sandbox_payload", category="verify")
        logger.warning("F2 sandbox: T5 payload unavailable; read Plz_ReadMe.md §T5")
        return sandbox_runner._stub(
            reason="payload_unavailable", lane_id=lane, image="",
            stderr_tail="T5_payload_unavailable; Plz_ReadMe.md §T5",
        )
    except Exception as exc:  # noqa: BLE001 — sandbox fault never aborts a cycle
        _swallowed(exc, site="self_improvement_v8._maybe_run_sandbox_verify:812", category="verify")
        logger.warning(
            "F2 sandbox run failed (non-fatal): %s",
            _format_exception_for_log(exc),
        )
        return None

    if getattr(telemetry, "stub", False):
        logger.info(
            "F2 sandbox stub (graceful no-op): reason=%s lane=%s",
            getattr(telemetry, "stub_reason", ""),
            lane,
        )
    else:
        logger.info(
            "F2 sandbox ran lane=%s exit=%s wall=%.3fs rss=%.1fMB "
            "pytest=%d/%d ruff_err=%d mypy_err=%d",
            lane,
            getattr(telemetry, "exit_code", -1),
            float(getattr(telemetry, "wall_clock_sec", 0.0) or 0.0),
            float(getattr(telemetry, "peak_rss_mb", 0.0) or 0.0),
            int(getattr(telemetry, "pytest_passed", 0) or 0),
            int(getattr(telemetry, "pytest_failed", 0) or 0),
            int(getattr(telemetry, "ruff_errors", 0) or 0),
            int(getattr(telemetry, "mypy_errors", 0) or 0),
        )
    return telemetry


def _run_verify_stage(
    changes: Sequence[Any],
    *,
    rubric_evaluation: Mapping[str, Any] | None,
    verified_apply_confirmation_id: str | None = None,
    # __SLOT_CROSS_MODEL_CONSENSUS_ONLY_2026_09_11__ non-None ⇒ the caller (apply
    # ladder) already knows this cycle cannot write (status != consensus): the
    # paid cross-model 2nd opinion is neither built nor called and the verdict
    # names why (``cross_model_adversary="skipped:<reason>"``). None (default)
    # keeps the historical path byte-identical.
    cross_model_skip_reason: str | None = None,
) -> dict[str, Any]:
    """S3 verify: rubric_emergency + pre_commit_gate_v81 g1-g8 → verdict.

    Returns a JSON-serialisable verdict dict::

        {"ok": bool, "blocked_reason": str, "emergency": bool,
         "gate_ok": bool, "gate_reason": str, "gate_tests_run": int,
         "cross_model": dict | None}

    - rubric_emergency (``core/rubric_emergency.py``, opt-out gate default ON):
      ``should_force_critic`` True == an emergency regression fired → the apply
      is BLOCKED (verdict ok=False, emergency=True). When no rubric evaluation
      is supplied there is no emergency signal (ok stays gated on the g1-g8 run).
    - pre_commit_gate_v81 g1-g8 (fail-closed default, R2 module) runs the
      sentinel pytest subset against the changed-file set. ``gate.ok`` False
      (failed/timeout/no_sentinels/error) → BLOCK.
    - cross_model_verifier (``verifier/wire_cross_model_v81``, C14 wire,
      ``AGI_V81_CROSS_MODEL_VERIFY_ENABLED`` default-OFF / operator-approve /
      cost): an OPTIONAL second opinion. Gate-OFF → ``run_cross_model_verify``
      returns None and the verdict is byte-identical to the no-cross-model
      path (no provider, no network). Gate-ON → a *refuted* 2nd opinion BLOCKs.
    """
    from agi_v8_1.core.rubric_emergency import should_force_critic
    from agi_v8_1.enforcement.pre_commit_gate_v81 import run_pre_commit_gate
    from agi_v8_1.policy.execution_authority import (
        execution_authority_enabled as _execution_authority_enabled,
    )
    from agi_v8_1.verifier.wire_cross_model_v81 import (
        cross_model_verify_enabled,
        run_cross_model_verify,
    )

    verdict: dict[str, Any] = {
        "ok": True,
        "blocked_reason": "",
        "emergency": False,
        "gate_ok": True,
        "gate_reason": "skipped",
        "gate_tests_run": 0,
        "cross_model": None,
        # __SLOT_CROSS_MODEL_ADVERSARY_2026_07_25__ which independent reviewer
        # judged this apply: "none" (no real adversary — the verdict is a
        # fail-closed block, not an approval), "provider_default", or the model id.
        "cross_model_adversary": "none",
        # __SLOT_F2_SANDBOX_VERIFY_2026_06_14__ stable shape: advisory sandbox
        # telemetry (serialized dict) + merged axis tokens. Both None unless the
        # F2 gate is ON AND a real (non-stub) sandbox run produced signals.
        "sandbox": None,
        "sandbox_axis_tokens": None,
    }

    # __SLOT_SI_VERIFY_APPLY_DIGEST_BIND_2026_08_17__ Round 5 §2 partial: bind
    # the verdict to the ACTUAL BYTES it was computed over, present on EVERY
    # return path (early-return blocks included) so ``_run_apply_ladder`` can
    # always compare it against what it is about to apply. Gate-guarded so
    # OFF keeps this verdict shape byte-identical to the pre-fix one.
    if _execution_authority_enabled():
        from agi_v8_1.policy.execution_authority import changes_digest as _cd

        verdict["verified_content_digest"] = _cd(changes)

    # --- rubric emergency regression guard (default ON, env opt-out) ---
    if rubric_evaluation is not None and should_force_critic(rubric_evaluation):
        verdict["ok"] = False
        verdict["emergency"] = True
        verdict["blocked_reason"] = "rubric_emergency_regression"
        return verdict

    # --- g1-g8 pre-commit gate (fail-closed) just before apply ---
    # A completion-authority batch has already run the card's exact pytest
    # manifest against the candidate snapshot.  The legacy stem mapper cannot
    # express every such manifest (openai_provider.py maps to zero tests), so
    # reuse that result only when the private in-process confirmation matches
    # every path/action/content/CAS byte in this complete transaction. Plain
    # changes and forged IDs still take (or fail) the historical gate.
    if verified_apply_confirmation_id is not None:
        from agi_v8_1.capabilities import PayloadPort, resolve_payload

        review_verified_apply_for_ladder = resolve_payload(PayloadPort(
            5, "agi_v8_1.si_lanes.verify_access_payload", "review_verified_apply_for_ladder"))

        exact_targets = review_verified_apply_for_ladder(
            verified_apply_confirmation_id, changes,
        )
        if exact_targets is None:
            verdict["gate_ok"] = False
            verdict["gate_reason"] = "verified_apply_authority_mismatch"
            verdict["gate_tests_run"] = 0
            verdict["ok"] = False
            verdict["blocked_reason"] = (
                "pre_commit_gate:verified_apply_authority_mismatch"
            )
        else:
            verdict["gate_ok"] = True
            verdict["gate_reason"] = (
                "completion_authority_exact_pytest_reused"
            )
            verdict["gate_tests_run"] = len(exact_targets)
    else:
        changed_files = [str(getattr(c, "path", "")) for c in changes]
        changed_files = [p for p in changed_files if p]
        gate = run_pre_commit_gate(changed_files)
        verdict["gate_ok"] = bool(gate.ok)
        verdict["gate_reason"] = gate.reason
        verdict["gate_tests_run"] = len(gate.tests_run)
        if not gate.ok:
            verdict["ok"] = False
            verdict["blocked_reason"] = f"pre_commit_gate:{gate.reason}"

    # --- cross_model 2nd opinion (C14 wire; default-OFF no-op) ---------------
    # The spec is a data-driven view of the proposed changes (path→action), so
    # the second opinion has positive evidence to score (an empty spec would
    # fail-closed by design). Gate-OFF → run_cross_model_verify returns None
    # (no verifier built, no adversary called) and the verdict is unchanged.
    cm_spec = {
        str(getattr(c, "path", "")): str(getattr(c, "action", ""))
        for c in changes
        if str(getattr(c, "path", ""))
    }
    # __SLOT_CROSS_MODEL_ADVERSARY_2026_07_25__ Build the REAL independent
    # reviewer. Only attempted when the verify gate is on (otherwise
    # run_cross_model_verify is a no-op anyway and we must not spend a call);
    # None (providers OFF / import or ctor failure) keeps the fail-closed block.
    _adversary = None
    _adversary_status = "none"
    # __SLOT_CROSS_MODEL_CONSENSUS_ONLY_2026_09_11__ The caller proved this cycle
    # cannot write (status != consensus) — a second opinion here would cost a
    # provider call and be consumed by nothing. Skip it LOUDLY (named status,
    # ``cross_model=None``) and only when the cross-model gate is ON: with the
    # gate OFF the historical path already did nothing, and leaving a
    # "skipped" mark there would itself be a change.
    if cross_model_skip_reason and cross_model_verify_enabled():
        cross_model = None
        _adversary_status = "skipped:" + str(cross_model_skip_reason)
    else:
        if cross_model_verify_enabled():
            _adversary, _adversary_status = _build_cross_model_adversary(changes)
        # agreement_threshold=1.0 — UNANIMOUS endorsement required. The apply
        # ladder is all-or-nothing (one session applies every change), so the
        # default 0.5 would let a set through while a minority of its changes
        # were explicitly NOT endorsed — and those would be applied too.
        # Requiring every change to be endorsed keeps "verified" and "applied"
        # describing the same set.
        cross_model = run_cross_model_verify(
            cm_spec, adversary=_adversary, agreement_threshold=1.0
        )
    verdict["cross_model"] = cross_model
    verdict["cross_model_adversary"] = _adversary_status
    # __SLOT_CROSS_MODEL_REVIEWER_DETAIL_2026_09_13__ 사유는 게이트 ON 일 때만 verdict 에.
    if _cross_model_reviewer_detail_enabled():
        verdict["cross_model_reviewer"] = _reviewer_verdicts(_adversary)
    if cross_model is not None and cross_model.get("refuted"):
        verdict["ok"] = False
        if not verdict["blocked_reason"]:
            # Distinguish a genuine cross-model refutation from a no-adversary
            # fail-closed block (gate ON but no real 2nd model wired — audit A1),
            # so the operator can tell verification-fired from verification-unarmed.
            _cm_reason = str(cross_model.get("reason") or "refuted")
            # Name the SPECIFIC unwired cause (providers off / budget exhausted /
            # provider unavailable) instead of the generic wire message, so an
            # operator can tell "out of money" from "never configured".
            if _adversary is None and _adversary_status.startswith("none:"):
                _cm_reason = _adversary_status.split(":", 1)[1]
            verdict["blocked_reason"] = f"cross_model_{_cm_reason}"

    # __SLOT_F2_SANDBOX_VERIFY_2026_06_14__ ADVISORY sandbox execution signal.
    # Default-OFF (returns None) → byte-identical to the pre-F2 verdict. When
    # ON, the producer runs the selected unit and we record the telemetry +
    # the merged per-axis tokens for OBSERVABILITY ONLY. By itself this block
    # never mutates ``verdict["ok"]`` — sandbox stays purely advisory here.
    # __SLOT_SANDBOX_FAIL_CLOSED_2026_08_15__ UPDATE (2026-08-15): the
    # "SEPARATE future gate" this comment used to only promise has shipped as
    # ``AGI_V8_SI_SANDBOX_FAIL_CLOSED`` (revival v1 §1 L3, its own env knob,
    # below) — the block below fires ONLY when that gate is explicitly ON.
    # Its default remains OFF, so the advisory-only behaviour described above
    # is still the out-of-the-box posture; this comment's original promise is
    # the thing that changed, not the default.
    telemetry = _maybe_run_sandbox_verify(changes)
    if telemetry is not None:
        verdict["sandbox"] = telemetry.as_dict()
        _sandbox_stub = bool(getattr(telemetry, "stub", True))
        if not _sandbox_stub:
            # Build a minimal audit-shape finding block from the changed .py
            # paths so the axis scorer has positive text to anchor on; the
            # sandbox tokens are then merged via the keyword-only argument.
            changed_py = [
                str(getattr(c, "path", ""))
                for c in changes
                if str(getattr(c, "path", "")).endswith(".py")
            ]
            finding_block = {
                "summary": "sandbox advisory verify over: " + ", ".join(changed_py),
                "notes": changed_py,
            }
            from agi_v8_1.core.axis_scorer import extract_axis_evidence

            evidence = extract_axis_evidence(
                finding_block, sandbox_telemetry=telemetry
            )
            verdict["sandbox_axis_tokens"] = evidence.as_dict()

            # __SLOT_SANDBOX_FAIL_CLOSED_2026_08_15__ L3: promote the sandbox
            # exec signal from advisory to apply-blocking. Only reachable for
            # a REAL (non-stub) run. A stub run never blocks — but "stub" is
            # NOT a pure infrastructure-absence vocabulary (STUB_REASONS also
            # carries container_failed/timeout/exception); v1 keeps ALL stub
            # no-fire and counts the escape via ``sandbox_gate.stub_reason``
            # below (design doc §1 "stub≠실행 실패 구분 필수" + gate-slot
            # comment at the env definition for the open ``timeout`` question).
            _sandbox_failed = (
                int(getattr(telemetry, "exit_code", -1) or 0) != 0
                or int(getattr(telemetry, "pytest_failed", 0) or 0) > 0
            )
            if _si_sandbox_fail_closed_enabled() and _sandbox_failed:
                verdict["ok"] = False
                # Never overwrite an earlier stage's reason — the first block
                # to fire owns ``blocked_reason``.
                if not verdict["blocked_reason"]:
                    verdict["blocked_reason"] = "sandbox_fail_closed"

        # __SLOT_SANDBOX_FAIL_CLOSED_2026_08_15__ Armed-gate observability.
        # Present only when the gate is ON **and** F2 produced a telemetry —
        # gate OFF ⇒ key absent (byte-identical), and gate ON with the F2
        # verify gate OFF ⇒ ``telemetry is None`` ⇒ key equally absent (this
        # whole block is inside the ``telemetry is not None`` branch).
        # ``fired`` is forced False on a stub run regardless of its (not
        # meaningful) exec fields — see the vocabulary note above.
        if _si_sandbox_fail_closed_enabled():
            verdict["sandbox_gate"] = {
                "armed": True,
                "fired": bool(
                    (not _sandbox_stub)
                    and (
                        int(getattr(telemetry, "exit_code", -1) or 0) != 0
                        or int(getattr(telemetry, "pytest_failed", 0) or 0) > 0
                    )
                ),
                "stub": _sandbox_stub,
                "stub_reason": getattr(telemetry, "stub_reason", ""),
            }

    return verdict


# __SLOT_SI_WORK_PRODUCT_LANE_2026_08_02__ durable per-episode attempt log.
# Lives in the jail beside the artifacts it describes.
_WORK_PRODUCT_RUNS_REL = ("workspace", ".agent", "runs.jsonl")


def _work_product_history(state_dir: Path) -> tuple[int, dict[str, Any] | None]:
    """``(attempt_number, previous_attempt_feedback)`` from the jail's run log.

    Attempt N that cannot see attempt N-1's exit code and output is a fresh
    guess wearing a retry's clothes — this log is what makes it a revision.
    A missing log means attempt 1 with no feedback; a GARBLED log is reported
    as such (``read_error``) rather than silently read as "no previous attempt",
    because the two are operationally different and only one is good news.
    """
    path = Path(state_dir).joinpath(*_WORK_PRODUCT_RUNS_REL)
    try:
        rows = read_jsonl(path)
    except (OSError, json.JSONDecodeError) as exc:
        _swallowed(exc, site="self_improvement_v8._work_product_history",
                   category="verify")
        # This feedback is injected into the next provider prompt.  Treat it
        # as an external text sink at its birth rather than relying on a
        # downstream prompt builder to rediscover exception provenance.
        return 1, {
            "read_error": _format_exception_for_sink(
                exc, max_chars=512, one_line=True
            )
        }
    if not rows:
        return 1, None
    last = rows[-1]
    return len(rows) + 1, {
        k: last.get(k) for k in (
            "path", "returncode", "exec_reason", "stdout_tail", "stderr_tail",
        )
    }


def _record_work_product_runs(
    state_dir: Path, records: Sequence[Mapping[str, Any]],
) -> None:
    """Append this cycle's artifact-run records to the jail log (non-fatal)."""
    path = Path(state_dir).joinpath(*_WORK_PRODUCT_RUNS_REL)
    for rec in records:
        try:
            atomic_append_jsonl(path, dict(rec))
        except Exception as exc:  # noqa: BLE001 — logging never aborts a cycle
            _swallowed(exc, site="self_improvement_v8._record_work_product_runs",
                       category="verify")
            logger.warning(
                "F3 work-product run log append failed: %s",
                _format_exception_for_log(exc),
            )


# __SLOT_PROPOSAL_IDENTITY_2026_08_06__ 사다리로 넘어간 제안들의 **신원**.
_PROPOSAL_IDS_CAP = 20


def _proposal_ids(changes: "Sequence[Mapping[str, Any]] | None") -> list[str]:
    """제안 신원 목록(순서 보존 · 중복 제거 · 상한 ``_PROPOSAL_IDS_CAP``).

    ⚠️ ``proposal_id`` 가 없는 change 를 **조용히 버리지 않는다** — 그러면
    "제안이 0건이었다"와 "제안은 있었는데 신원이 없다"가 원장에서 같아진다.
    대신 경로를 실은 마커를 남긴다. (이 레포가 반복해서 데는 그 혼동이다.)
    """
    out: list[str] = []
    for ch in (changes or ()):
        if not isinstance(ch, Mapping):
            out.append("<not_a_mapping>")
        else:
            pid = str(ch.get("proposal_id") or "").strip()
            out.append(pid or f"<no_proposal_id:{str(ch.get('path') or '?')}>")
        if len(out) >= _PROPOSAL_IDS_CAP:
            break
    seen: set[str] = set()
    return [p for p in out if not (p in seen or seen.add(p))]


def _apply_si_session(*, applier_class: Any, state_dir: Path, dry_run: bool,
                      changes: Sequence[Any], verifier_id: str, decision_source: str) -> Any:
    """Public dry-run and safety seam; only real sessions require T9 operation code."""
    if dry_run:
        applier = applier_class(repo_root=state_dir, dry_run=True, audit_root=state_dir)
        return applier.apply_session(list(changes), verifier_id=verifier_id,
                                     decision_source=decision_source)
    from agi_v8_1.capabilities import PayloadPort, resolve_payload

    execute = resolve_payload(PayloadPort(
        9, "agi_v8_1.runtime.auto_apply_payload", "apply_session",
    ))
    return execute(applier_class=applier_class, state_dir=state_dir, changes=changes,
                   verifier_id=verifier_id, decision_source=decision_source)


def _run_apply_ladder(
    *,
    status: str,
    pod_a_block: Mapping[str, Any] | None,
    pod_b_block: Mapping[str, Any] | None,
    state_dir: Path,
    rubric_evaluation: Mapping[str, Any] | None = None,
    run_verify: bool = True,
    extra_changes: Sequence[Mapping[str, Any]] | None = None,
    extra_commands: Sequence[str] | None = None,
    extra_artifact_runs: Sequence[Mapping[str, Any]] | None = None,
    extra_work_changes: Sequence[Mapping[str, Any]] | None = None,
    extra_answer_changes: Sequence[Mapping[str, Any]] | None = None,
    approved_command_digests: frozenset[str] | None = None,
    verified_apply_confirmation_id: str | None = None,
) -> dict[str, Any]:
    """S3→S4→S5 apply ladder (replaces the placeholder ``stub_*`` theatre).

    Always returns a JSON-serialisable summary with REAL measured pre/post
    hashes (never the fixed ``stub_*`` placeholder sentinels). The closed loop
    ALWAYS completes:
      - verify (S3) runs locally + cheaply (rubric + g1-g8). Skippable via
        ``run_verify=False`` for the no-change advisory path (an empty change
        set has nothing for the gate to verify; running it fail-closed would
        block every advisory cycle, defeating "폐루프는 항상 완주").
      - apply (S4) runs SafeAutoApply.apply_session. dry_run = NOT
        write_enabled() — the 2-env operator write gate. dry_run computes the
        real on-disk aggregate hash without touching files.

    Summary keys: pre_apply_hash / post_apply_hash / rollback_latency_ms /
    changes_applied / change_count / dry_run / write_enabled / applied /
    reverted / verdict (the S3 verdict dict) / errors.

    ``extra_commands`` (__SLOT_SI_PROPOSED_COMMANDS_CHANNEL_2026_08_02__) is the
    proposer-side command channel: strings the F1 LLM lanes asked to RUN. They
    ride the synthetic proposer block on their own key, are never part of the
    SafeAutoApply file session, and are always re-validated by
    ``command_executor.plan_command`` here — proposing is not permission. Empty
    / None ⇒ no ``commands`` key on the summary (byte-identical to before).

    ``approved_command_digests`` (__SLOT_SI_EXECUTION_AUTHORITY_2026_08_17__,
    Round 5 §3 3-source fix): when NOT None, a command from ANY of the three
    ``_extract_commands`` sources (pod_a/pod_b/proposer) additionally needs
    its content digest to be a member of this set to be eligible to execute.
    ``run_one_si_cycle`` passes the inc5-reviewed ``proposer_commands`` digest
    set here only when the F1 inc5 verify gate is armed; every direct/test
    caller that omits it (the default) keeps the pre-existing behaviour where
    all three sources are equally eligible once planned+armed.
    """
    from agi_v8_1.enforcement.safe_auto_apply import SafeAutoApply
    from agi_v8_1.policy.execution_authority import (
        changes_digest as _changes_digest,
        command_digest as _command_digest,
        compute_authorization as _compute_authorization,
        execution_authority_enabled as _execution_authority_enabled,
    )

    # __SLOT_SI_CODE_PROPOSER_F1_2026_06_13__ F1 proposer output rides in as a
    # third synthetic block so caller-provided pod blocks stay unmutated. None
    # / empty → byte-identical to the pre-F1 two-block call.
    # __SLOT_SI_PROPOSED_COMMANDS_CHANNEL_2026_08_02__ the SAME synthetic block
    # now also carries ``proposed_commands`` — the key ``_extract_commands`` has
    # read since Stage 3 but which NO producer ever wrote (executor_log measured
    # command=0 against si_llm_propose=282 / safe_auto_apply=289). Each key is
    # added only when non-empty, so: no commands ⇒ the block is exactly the
    # historical ``{"proposed_file_changes": [...]}``; neither ⇒ still ``None``.
    proposer_block: dict[str, Any] | None = None
    if extra_changes or extra_commands:
        proposer_block = {}
        if extra_changes:
            proposer_block["proposed_file_changes"] = list(extra_changes)
        if extra_commands:
            proposer_block["proposed_commands"] = list(extra_commands)
    changes = _extract_file_changes(pod_a_block, pod_b_block, proposer_block)
    write_on = SafeAutoApply.write_enabled()
    consensus = status == "consensus"
    # __SLOT_CROSS_MODEL_CONSENSUS_ONLY_2026_09_11__ ``do_real_write`` below is
    # ``consensus and verify_pass and write_on`` — a non-consensus cycle is a
    # dry-run no matter what the paid 2nd opinion says. Under the gate, tell S3
    # so it skips that call; the reason is the loop's OWN status word, never
    # candidate text. Gate OFF ⇒ None ⇒ S3 byte-identical.
    _cm_skip = (
        f"status={status}"
        if (_cross_model_consensus_only_enabled() and not consensus)
        else None
    )

    summary: dict[str, Any] = {
        "change_count": len(changes),
        "consensus": consensus,
        "write_enabled": write_on,
        "applied": False,
        "reverted": False,
        "verdict": None,
        "errors": [],
    }

    # --- S3 verify (only when there is something to verify) ---
    verdict: dict[str, Any] | None = None
    if run_verify and changes:
        verdict = _run_verify_stage(
            changes,
            rubric_evaluation=rubric_evaluation,
            verified_apply_confirmation_id=verified_apply_confirmation_id,
            cross_model_skip_reason=_cm_skip,
        )
        summary["verdict"] = verdict

    # The apply step proceeds when consensus AND (no verify OR verify passed).
    # When the verify BLOCKS, we still run a dry_run session so S5 records a
    # real measured hash of the unchanged tree (the closed loop completes; the
    # disk is simply never touched). NO silent skip.
    verify_pass = verdict is None or bool(verdict.get("ok"))
    do_real_write = consensus and verify_pass and write_on

    # __SLOT_SI_EXECUTION_AUTHORITY_2026_08_17__ Round 5 §3 fix — ONE
    # authorization computed here, consulted (not re-derived) by the command
    # channel below. Gate default ON (security fix); explicit "false"/"0"
    # restores the exact pre-fix per-channel-boolean behaviour.
    _ea_on = _execution_authority_enabled()
    authorization = _compute_authorization(
        consensus=consensus, verify_pass=verify_pass, write_on=write_on,
    )
    # __SLOT_SI_VERIFY_APPLY_DIGEST_BIND_2026_08_17__ Round 5 §2 partial — the
    # apply step recomputes the SAME content digest the verdict was computed
    # over and refuses to write on a mismatch (fail-closed). ``changes`` is
    # never reassigned between the verify call above and here in THIS
    # function, so this never fires today; it is insurance against a future
    # refactor (e.g. re-reading changes from a store between verify and
    # apply) silently reintroducing verify/apply drift. NOT the full sealed-
    # bundle redesign (bundle_id/signature/nonce/expiry) — that is out of
    # scope for this round (see policy/execution_authority.py docstring).
    _digest_mismatch_error: str | None = None
    if _ea_on and verdict is not None:
        _verified_digest = verdict.get("verified_content_digest")
        _current_digest = _changes_digest(changes)
        if _verified_digest is not None and _verified_digest != _current_digest:
            do_real_write = False
            _digest_mismatch_error = (
                "content_digest_mismatch: verified content diverged from the "
                "content about to be applied (fail-closed, write refused)"
            )
            logger.critical(
                "SI apply ladder: verified_content_digest mismatch — refusing "
                "real write (verified=%s current=%s)",
                _verified_digest, _current_digest,
            )

    # __SLOT_SI_AUDIT_JAIL_ANCHOR_2026_08_09__ 원장 앵커를 **말한다**. 여기는
    # `repo_root` 가 이미 젤이지만, 그건 폴백이 `<젤>/state/si_audit` 를 낸다는
    # 뜻이라 판독기가 보는 `<젤>/si_audit` 와 어긋난다. env 핀이 없는 진입점
    # (tick 밖)에서 갈리므로 명시가 유일하게 안 갈리는 배선이다.
    result = _apply_si_session(
        applier_class=SafeAutoApply, state_dir=state_dir, dry_run=not do_real_write,
        changes=list(changes),
        verifier_id="si_v81",
        decision_source="si_loop_v81",
    )

    summary["pre_apply_hash"] = result.pre_apply_hash
    summary["post_apply_hash"] = result.post_apply_hash
    summary["rollback_latency_ms"] = result.rollback_latency_ms
    summary["changes_applied"] = result.changes_applied
    summary["dry_run"] = not do_real_write
    summary["applied"] = bool(result.success and do_real_write and changes)
    summary["reverted"] = bool(result.reverted)
    summary["errors"] = list(result.errors)
    if _digest_mismatch_error is not None:
        summary["errors"] = list(summary["errors"]) + [_digest_mismatch_error]

    # __SLOT_W_STAGE3_COMMAND_EXEC_2026_06_18__ observation-first command exec.
    # Commands ride a SEPARATE channel (never the SafeAutoApply file session).
    # Each command is ALWAYS planned (validated + logged); it is EXECUTED only
    # when the command seam gate AND the armed gate are both on AND the plan is
    # allowed. No commands proposed (the production default — pods don't emit
    # them) → no "commands" key → byte-identical to the pre-Stage-3 summary.
    #
    # __SLOT_SI_EXECUTION_AUTHORITY_2026_08_17__ Round 5 §3 fix: execution ALSO
    # requires ``authorization.allow_command_exec`` (consensus AND verify_pass
    # — see policy/execution_authority.py for why write_on is deliberately
    # excluded) and, when ``approved_command_digests`` was supplied, digest
    # membership — closing both halves of the §3 finding (consensus/
    # verify_pass being ignored, and pod_a/pod_b commands bypassing the inc5
    # review the proposer channel gets). Gate default ON; explicit OFF via
    # ``AGI_V8_SI_EXECUTION_AUTHORITY_ENABLED=false``/``0`` restores the exact
    # pre-fix ``seam_on and armed and plan.allowed`` decision, byte-identical.
    commands = _extract_commands(pod_a_block, pod_b_block, proposer_block)
    if commands:
        from agi_v8_1.enforcement.command_executor import (
            command_exec_armed,
            command_exec_enabled,
            execute_planned,
            mask_command_text,
            plan_command,
        )

        seam_on = command_exec_enabled()
        armed = command_exec_armed()
        cmd_records: list[dict[str, Any]] = []
        for raw in commands:
            # state_dir passed so path-operand containment can be checked at plan
            # time (adversarial-review fix). raw/argv are MASKED before they ride
            # into the durable apply_chain entry — a proposed command may embed a
            # credential (e.g. an Authorization header).
            plan = plan_command(str(raw), state_dir=state_dir)
            rec: dict[str, Any] = {
                "raw": mask_command_text(plan.raw),
                "argv": [mask_command_text(tok) for tok in plan.argv],
                "allowed": plan.allowed,
                "category": plan.category,
                "reason": plan.reason,
                "read_only": plan.read_only,
                "seam_enabled": seam_on,
                "executed": False,
                "returncode": None,
                "exec_reason": "plan_only",
            }
            would_execute = seam_on and armed and plan.allowed
            if would_execute and _ea_on and not authorization.allow_command_exec:
                rec["exec_reason"] = "denied: execution_authority (consensus/verify_pass)"
                would_execute = False
            elif (
                would_execute
                and _ea_on
                and approved_command_digests is not None
                and _command_digest(raw) not in approved_command_digests
            ):
                rec["exec_reason"] = "denied: unreviewed command source"
                would_execute = False
            if would_execute:
                res = execute_planned(plan, state_dir=state_dir)
                rec["executed"] = res.executed
                rec["returncode"] = res.returncode
                rec["exec_reason"] = res.reason
            cmd_records.append(rec)
        summary["commands"] = cmd_records

    # __SLOT_SI_WORK_PRODUCT_ARTIFACT_RUN_2026_08_02__ Stage 4, finally wired.
    # ``plan_artifact_run``/``execute_artifact_run`` shipped 2026-06-18 and had
    # ZERO production callers until now (FEATURE_MAP listed it "armed", which
    # was true of the gate and false of the capability). These are the loop's
    # hands: a *.py the loop just wrote, re-validated for containment here and
    # run with cwd pinned to the card workspace. Runs ONLY when the write
    # actually landed — executing an artifact from a dry-run session would
    # execute a STALE file from an earlier cycle and report it as this cycle's
    # work. Empty/None ⇒ no ``artifact_runs`` key (byte-identical summary).
    # __SLOT_WORK_PRODUCT_OWN_SESSION_2026_08_02__ The episode's DELIVERABLE
    # gets its own apply session, separate from the self-modification set.
    #
    # Measured 2026-08-02: bundling them meant a goal-card work product shared
    # an ATOMIC SafeAutoApply session and a UNANIMOUS (threshold 1.0)
    # cross-model review with unrelated ``si_proposed/*`` proposals. The
    # reviewer declined those proposals — a legitimate review decision — and
    # the task deliverable died with them (verdict cross_model_refuted, all
    # three paths ``mismatch:``, whole session dry-run). A work product is not
    # a self-edit: nothing about it concerns the live source tree, which is
    # also why it already bypasses the inc5 verify gate.
    #
    # Safety is unchanged and does NOT rest on that review: the path is
    # computed locally under ``workspace/`` (the model never picks it), the
    # write goes through SafeAutoApply with repo_root=state_dir (jail-only),
    # and execution needs the Stage 4 double gate plus the bwrap sandbox. So
    # this session is gated on the operator write gate alone.
    work_applied = False
    if extra_work_changes:
        work_result = _apply_si_session(
            applier_class=SafeAutoApply, state_dir=state_dir, dry_run=not write_on,
            changes=_extract_file_changes(None, None, {
                "proposed_file_changes": list(extra_work_changes)}),
            verifier_id="si_v81_work_product",
            decision_source="si_work_product_v81",
        )
        work_applied = bool(work_result.success and write_on)
        summary["work_product"] = {
            "change_count": len(list(extra_work_changes)),
            "changes_applied": work_result.changes_applied,
            "applied": work_applied,
            "dry_run": not write_on,
            "errors": list(work_result.errors)[:5],
        }

    # __SLOT_SI_ANSWER_PRODUCT_LANE_2026_08_06__ 답변 문서는 **세 번째** 세션이다.
    #
    # 왜 work_product 에 얹지 않았나: 위 SLOT 이 기록한 사고를 한 층 아래서
    # 그대로 재현하기 때문이다. ``apply_session`` 은 원자적이라, 한 세션에 답변
    # 문서와 산출물 스크립트를 같이 넣으면 스크립트가 거절되는 순간 **이미 돈을
    # 낸 답변까지 통째로 dry-run** 된다. 산출물끼리도 운명을 같이하면 안 된다.
    #
    # 실행 경로가 없다: 여기서 나온 change 는 ``extra_artifact_runs`` 에 절대
    # 실리지 않고, 파일이 ``.md`` 라서 ``plan_artifact_run`` 이 구조적으로
    # 거절한다(``not a .py artifact``). 답 하나 받자고 모델-저작 코드 실행
    # 표면을 사지 않는다.
    #
    # 안전 근거는 리뷰가 아니라 경로다(work_product 와 동일): 경로는 로컬 계산,
    # 쓰기는 repo_root=state_dir 로 젤 안뿐. 그래서 운영자 쓰기 게이트 하나에만
    # 걸린다.
    if extra_answer_changes:
        answer_result = _apply_si_session(
            applier_class=SafeAutoApply, state_dir=state_dir, dry_run=not write_on,
            changes=_extract_file_changes(None, None, {
                "proposed_file_changes": list(extra_answer_changes)}),
            verifier_id="si_v81_answer_product",
            decision_source="si_answer_product_v81",
        )
        _ans_paths = [str(c.get("path") or "") for c in extra_answer_changes]
        summary["answer_product"] = {
            "change_count": len(list(extra_answer_changes)),
            "changes_applied": answer_result.changes_applied,
            "applied": bool(answer_result.success and write_on),
            "dry_run": not write_on,
            # 🔑 경로는 **리포트가 답을 찾는 유일한 통로**다. 본문은 원장에 넣지
            # 않는다 — 원장이 비대해지고, 같은 진실이 두 곳에 살게 된다.
            "paths": _ans_paths,
            "errors": list(answer_result.errors)[:5],
        }

    if extra_artifact_runs and (work_applied or summary.get("applied")):
        from agi_v8_1.enforcement.command_executor import (
            artifact_sandbox_mode,
            execute_artifact_run,
            plan_artifact_run,
            py_artifact_run_enabled,
        )

        # __SLOT_SI_ARTIFACT_EXEC_AUTHORITY_2026_08_18__ ADV-R10 §5 wiring.
        # NOT the cycle-wide ``authorization`` computed above (line ~1861) —
        # that one's ``allow_artifact_exec`` mirrors ``allow_file_write``
        # (consensus AND verify_pass AND write_on), and this channel is the
        # goal-card WORK-PRODUCT deliverable path, deliberately gated on the
        # operator write gate alone since __SLOT_WORK_PRODUCT_OWN_SESSION_
        # 2026_08_02__ (see the block comment above ``work_applied``): a
        # self-mod cycle's own pods failing to reach consensus must not sink
        # an unrelated task deliverable. Forcing consensus=True, verify_pass=
        # True here makes ``allow_artifact_exec`` reduce to exactly
        # ``write_on`` — the SAME condition ``work_applied`` already uses —
        # so wiring this in is defense-in-depth inside the channel itself,
        # not a NEW dependency on self-mod consensus/verify.
        _artifact_authorization = _compute_authorization(
            consensus=True, verify_pass=True, write_on=write_on,
        )
        stage4_on = py_artifact_run_enabled()
        art_records: list[dict[str, Any]] = []
        for req in extra_artifact_runs:
            rel = str((req or {}).get("path") or "")
            cwd_rel = (req or {}).get("cwd")
            a_plan = plan_artifact_run(rel, state_dir=state_dir)
            a_rec: dict[str, Any] = {
                "path": rel,
                "cwd": cwd_rel,
                "allowed": a_plan.allowed,
                "reason": a_plan.reason,
                "stage4_enabled": stage4_on,
                "sandbox": artifact_sandbox_mode(),
                "executed": False,
                "returncode": None,
                "exec_reason": "plan_only",
                "stdout_tail": "",
                "stderr_tail": "",
            }
            if stage4_on and a_plan.allowed:
                a_res = execute_artifact_run(
                    a_plan, state_dir=state_dir,
                    cwd_rel=str(cwd_rel) if cwd_rel else None,
                    authorization=_artifact_authorization,
                )
                a_rec["executed"] = a_res.executed
                a_rec["returncode"] = a_res.returncode
                a_rec["exec_reason"] = a_res.reason
                # Tails are the ONLY channel by which the next attempt learns
                # what its own script did — truncated, already secret-masked by
                # the executor. Silence here is what made retries blind guesses.
                a_rec["stdout_tail"] = (a_res.stdout or "")[-2000:]
                a_rec["stderr_tail"] = (a_res.stderr or "")[-2000:]
                # __SLOT_SI_OUTPUT_SPILL_2026_08_23__ default-OFF; OFF adds
                # no keys (byte-identical). ON spills what the tail above
                # just dropped to a jail-local file instead of losing it.
                _apply_output_spill(
                    a_rec, stream="stdout", full_text=a_res.stdout or "",
                    state_dir=state_dir, req_rel=rel,
                )
                _apply_output_spill(
                    a_rec, stream="stderr", full_text=a_res.stderr or "",
                    state_dir=state_dir, req_rel=rel,
                )
                # __SLOT_SI_INDEPENDENT_REPRODUCTION_2026_08_18__ R13 P0-3
                # ("독립 검증자 증명" — GOVERNANCE.ko.md's value substitute for
                # the rejected 2-of-3 human-signature idea). Gate default-OFF;
                # gate OFF ⇒ ``reproduce_if_enabled`` returns None immediately
                # and ``a_rec`` gains no new key, so this block is
                # byte-identical to before it existed unless explicitly armed.
                # Runs ONLY when this cycle's own execution actually ran
                # (nothing to independently reproduce for a refused plan) —
                # re-executes the SAME artifact bytes in a brand-new
                # subprocess inside a throwaway workspace that is never
                # ``state_dir`` (the jail this run itself wrote into), and
                # compares outcome digests bit-for-bit. Fail-closed: any
                # mismatch, timeout, or sandbox-unavailability is
                # ``ok=False`` — never silently treated as reproduced.
                # Exact cheap precheck of the canonical reproduction gate:
                # OFF needs no T5 implementation or digest computation.
                if (a_res.executed and os.environ.get(
                        "AGI_V8_SI_INDEPENDENT_REPRODUCTION_ENABLED", "").strip().lower() in ("true", "1")):
                    from agi_v8_1.capabilities import PayloadPort, resolve_payload

                    _reproduction = resolve_payload(PayloadPort(
                        5, "agi_v8_1.si_lanes.verify_access_payload", "load_independent_reproduction"))()
                    _repro = _reproduction.reproduce_if_enabled(
                        source_path=Path(a_plan.abs_path),
                        expected_source_sha256=a_plan.sha256,
                        expected_outcome_digest=_reproduction.outcome_digest(
                            a_res.returncode, a_res.stdout, a_res.stderr),
                        args=a_plan.args,
                    )
                    if _repro is not None:
                        a_rec["independent_reproduction"] = {
                            "ok": _repro.ok,
                            "reason": _repro.reason,
                        }
            art_records.append(a_rec)
        summary["artifact_runs"] = art_records
    return summary


# --- R7 prompt evolution durable learning (C9 반증) --------------------------
# PART1 S2/S6 + ledger row 34. Audit claim C9 CONFIRMED: prompt-evolution
# durable accumulation is INACTIVE because the only path that reaches
# ``prompt_compactor_v8`` (Auto-applied block injection + compaction round-trip)
# is the dead ``phase_executors/`` glue — production never instantiates
# ``ReviewPhaseExecutorV8.run()``, so the compactor's read+write surface never
# fires in a real SI cycle. The compactor file existed but was wired to nothing.
#
# R7 wires it directly into ``run_one_si_cycle``'s S6 (reflect), no glue:
#
#   (a) advisory path (gate AGI_V8_PROMPT_COMPACTOR_ENABLED, default OFF):
#       every N cycles (cadence AGI_V81_PROMPT_COMPACTOR_CADENCE, default 10)
#       the SI loop compacts the cycle-event stream (via the SAME
#       cycle_logger.read_since the R1 observation stage feeds — prompt_compactor_v8
#       owns the read) into a RolePromptPacket and APPENDS the rendered rule
#       lines to a ``<role>.rules.txt`` sidecar UNDER ``state_dir/prompts/``
#       (an EVIDENCE area, never the canonical data/prompts tree). This is the
#       durable accumulation trace the audit found missing — it grows across
#       cycles without ever touching a real prompt file.
#
#   (b) write path (gate AGI_V8_SI_WRITE_PATH_ENABLED AND
#       AGI_V8_COMPACTOR_WRITE_PATH_ENABLED, BOTH default OFF): only when BOTH
#       operator envs are ON does the packet route through
#       ``prompt_compactor_v8.write_compacted`` → SafeAutoApply.apply_block,
#       injecting an ``# [Auto-applied: compactor_<role>]`` block into the
#       canonical allowlisted prompt file (data/prompts/<role>.txt) with REPLACE
#       semantics (deterministic proposal_id) — reusing the R3 SafeAutoApply
#       ladder, not a second writer. The block-marker contract gives the
#       compaction round-trip (write → re-read → re-compact) for free.
#
# Gate separation mirrors the rest of v8.1: the read/advisory loop is opt-in but
# write-free (the sidecar lives under state_dir, the SI's own evidence dir), and
# the canonical prompt-file mutation needs the SAME 2-env operator AND-gate the
# SI dispatcher's emit_write keys off (AGI_V8_SI_WRITE_PATH_ENABLED) PLUS the
# compactor's own write gate (AGI_V8_COMPACTOR_WRITE_PATH_ENABLED). NO mode enum:
# the helper branches on data (cadence hit + gate booleans), never a hardcoded
# mode string (feedback_no_mode_enums_2026_05_28).
_PROMPT_COMPACTOR_CADENCE_ENV = "AGI_V81_PROMPT_COMPACTOR_CADENCE"
_SI_WRITE_PATH_ENV = "AGI_V8_SI_WRITE_PATH_ENABLED"
_PROMPT_DIR_ENV = "AGI_V81_PROMPT_DIR"
_DEFAULT_PROMPT_COMPACTOR_CADENCE = 10

# The canonical prompt directory (matches SafeAutoApply.PROMPT_ALLOWLIST).
# Derive it from this imported snapshot rather than a live-host absolute path;
# it remains overridable via env so a
# test can redirect the write target into tmp_path WITHOUT touching the real
# prompt tree (mirrors the AGI_V8_STATE_DIR override the compactor's archive dir
# already honours). The PromptLoader reads <role>.rules.txt from this same dir,
# so a gate-ON sidecar written here is read straight back into the role prompt.
_DEFAULT_PROMPT_DIR = str(Path(__file__).resolve().parent / "data" / "prompts")

# The role whose prompt the SI loop evolves. The canonical prompt file
# (data/prompts/self_improvement.txt) is in SafeAutoApply.PROMPT_ALLOWLIST, so a
# gate-ON write path lands on an allowlisted target.
_SI_PROMPT_ROLE = "self_improvement"


def _canonical_prompt_dir() -> Path:
    """Resolve the canonical prompt dir (env-overridable for tests).

    # __SLOT_T3_PATH_CONTAINMENT_2026_08_08__ Deliberately left VERBATIM.
    # Round 1 folded (expanduser+resolve) this dir under the containment gate,
    # reasoning that `target`/`allow`/`repo_root` are all derived from it. That
    # is true — and it is exactly why folding here cannot be the containment
    # fix: a value derived from the caller's own input is not an anchor. The
    # anchor now lives in `SafeAutoApply.containment_root` (package root), and
    # measured 2026-08-08 the live-shape ancestor-symlink escape is denied with
    # OR without folding. Folding is strictly WEAKER — it collapses a symlinked
    # component before `_path_has_symlink_component` can refuse it — so the
    # unfolded spelling stays, and the gate never changes this function.
    """
    raw = os.environ.get(_PROMPT_DIR_ENV, "").strip()
    return Path(raw) if raw else Path(_DEFAULT_PROMPT_DIR)


def _prompt_compactor_cadence() -> int:
    """Resolve the compaction cadence N (every Nth cycle). Default 10.

    A non-positive / unparseable env falls back to the default so a misconfig
    can never make the cadence fire every cycle (cost) or never (silent).
    """
    raw = os.environ.get(_PROMPT_COMPACTOR_CADENCE_ENV, "").strip()
    try:
        n = int(raw) if raw else _DEFAULT_PROMPT_COMPACTOR_CADENCE
    except ValueError as _ff_exc:
        _swallowed(_ff_exc, site="self_improvement_v8._prompt_compactor_cadence:1180", category="verify")
        n = _DEFAULT_PROMPT_COMPACTOR_CADENCE
    return n if n > 0 else _DEFAULT_PROMPT_COMPACTOR_CADENCE


def _si_write_path_enabled() -> bool:
    """R7 write-path AND-gate: BOTH operator envs must be exactly 'true'.

    Reuses the compactor's own write gate (AGI_V8_COMPACTOR_WRITE_PATH_ENABLED)
    AND the SI dispatcher's write-path gate (AGI_V8_SI_WRITE_PATH_ENABLED). A
    single env on its own keeps the canonical prompt file UNCHANGED — the
    advisory sidecar still accumulates, but no allowlisted prompt is mutated.
    """
    from agi_v8_1.prompts.prompt_compactor_v8 import write_path_enabled

    return (
        os.environ.get(_SI_WRITE_PATH_ENV, "") == "true"
        and write_path_enabled()
    )


def _append_rules_sidecar(sidecar_path: Path, packet: Any) -> int:
    """Append a packet's compacted rule lines to a ``<role>.rules.txt`` sidecar.

    Durable accumulation: each compaction round appends a timestamped block of
    rule bullets so the sidecar GROWS across cycles (the v7.1 prompt-evolution
    accumulation the audit found missing). The format is the PromptLoader's
    ``.rules.txt`` contract (one rule per line; ``#``-prefixed lines are
    comments the loader skips), so a gate-ON sidecar under data/prompts would be
    read straight back into the composed prompt (item 3). Returns the count of
    rule lines appended this round (0 == nothing new survived compaction).
    """
    lines = [l for l in packet.compacted_history if l and l.strip()]
    if not lines:
        return 0

    # __SLOT_COMPACTOR_DEDUPE_2026_07_24__ Append ONLY rule lines the sidecar does
    # not already carry. Without this the sidecar grew a near-duplicate block on
    # every cadence hit (each round re-derived the same summaries), and this file
    # is the durable-learning artifact a loader composes back into a prompt — so
    # the duplication would land in the prompt itself and crowd out the core.
    # '#'-prefixed lines are loader comments, not rules, so they never dedupe.
    existing: set[str] = set()
    if sidecar_path.exists():
        try:
            for raw in sidecar_path.read_text(encoding="utf-8").splitlines():
                s = raw.strip()
                if s and not s.startswith("#"):
                    existing.add(s)
        except OSError as _ff_exc:
            _swallowed(_ff_exc, site="self_improvement_v8._append_rules_sidecar:1229", category="verify")
            existing = set()  # unreadable sidecar → fail open (append anyway)

    fresh: list[str] = []
    seen: set[str] = set()
    for line in lines:
        s = line.strip()
        if s in existing or s in seen:
            continue
        seen.add(s)
        fresh.append(s)
    if not fresh:
        return 0  # everything this round was already recorded

    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    block = [f"# compacted @ {ts} role={packet.role} retained={len(fresh)}"]
    block.extend(fresh)
    with sidecar_path.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(block) + "\n")
    return len(fresh)


# __SLOT_COMPACTOR_CURSOR_2026_07_24__ Compaction cursor persistence.
# Before this, the SI passed cursor=0.0 on EVERY cadence hit, so each round
# re-read the entire cycle-event stream from epoch and re-derived the same
# summaries (the duplication source the dedupe above also guards). Persisting
# the packet's next_cursor makes each round compact only NEW events, which also
# means the token budget is a per-slice ceiling instead of a whole-history one.
def _compact_cursor_path(state_dir: Path, role: str) -> Path:
    """Sibling of the rules sidecar, under the same state_dir/prompts jail."""
    return Path(state_dir) / "prompts" / f"{role}.compact_cursor.json"


def _read_compact_cursor(path: Path) -> float:
    """Read the persisted cursor. Any fault → 0.0 (recompact from epoch once)."""
    try:
        from agi_v8_1.state.store import read_json

        data = read_json(path, default={})
    except Exception as _ff_exc:  # noqa: BLE001 — cursor read must never abort a cycle
        _swallowed(_ff_exc, site="self_improvement_v8._read_compact_cursor:1269", category="verify")
        return 0.0
    if not isinstance(data, dict):
        return 0.0
    try:
        cur = float(data.get("cursor", 0.0))
    except (TypeError, ValueError) as _ff_exc:
        _swallowed(_ff_exc, site="self_improvement_v8._read_compact_cursor:1275", category="verify")
        return 0.0
    return cur if cur > 0.0 else 0.0


def _write_compact_cursor(path: Path, cursor: float, role: str) -> bool:
    """Persist the advanced cursor. Non-fatal: a failure just re-reads next time."""
    try:
        from agi_v8_1.state.store import atomic_write_json

        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, {"role": role, "cursor": float(cursor)})
        return True
    except Exception as _ff_exc:  # noqa: BLE001
        _swallowed(_ff_exc, site="self_improvement_v8._write_compact_cursor:1288", category="verify")
        return False


def _make_state_dir_archive_fn(state_dir: Path):
    """Return a write_compacted ``archive_fn`` that lands snapshots under the SI's
    own ``state_dir/prompt_compaction_archive`` (NOT the compactor's
    AGI_V8_STATE_DIR default, which would pollute the canonical tree).

    Delegates the JSON serialisation to the compactor's own ``_archive_packet``
    by temporarily honouring the SI state_dir — implemented WITHOUT mutating
    os.environ (anti-pattern): it re-implements only the directory choice and
    reuses the compactor's payload schema via a thin write.
    """
    import json as _json
    from datetime import datetime as _dt, timezone as _tz

    adir = Path(state_dir) / "prompt_compaction_archive"

    def _archive(target_file: str, proposal_id: str, packet: Any) -> Path:
        adir.mkdir(parents=True, exist_ok=True)
        ts = _dt.now(_tz.utc).strftime("%Y%m%dT%H%M%S%fZ")
        safe_role = "".join(
            c if c.isalnum() or c in "-_" else "_" for c in packet.role
        )
        archive_path = adir / f"{Path(target_file).name}.{safe_role}.{ts}.json"
        payload = {
            "schema": "prompt_compactor_v8.archive.v1",
            "ts_utc": _dt.now(_tz.utc).isoformat(),
            "target_file": str(target_file),
            "proposal_id": proposal_id,
            "role": packet.role,
            "base_prompt": packet.base_prompt,
            "compacted_history": list(packet.compacted_history),
            "retained_event_ids": list(packet.retained_event_ids),
            "dropped_event_count": packet.dropped_event_count,
            "token_budget": packet.token_budget,
            "estimated_tokens": packet.estimated_tokens,
        }
        archive_path.write_text(
            _json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return archive_path

    return _archive


def _run_prompt_evolution_advisory(
    *,
    cycle_n: int,
    state_dir: Path,
    cycle_logger: Any | None,
    cycle_id: str,
) -> Mapping[str, Any] | None:
    """R7 S6 hook: periodic prompt-history compaction + durable sidecar write.

    Returns a JSON-serialisable summary of the compaction round (cadence hit,
    rules appended, write-path outcome), or ``None`` when the advisory gate is
    OFF, the cadence has not elapsed, or there is no read surface. Advisory by
    construction: a fault never aborts the SI cycle.

    Pipeline:
      1. cadence check — fire only when ``cycle_n % N == 0`` (N from env).
      2. compact_prompts(logger=cycle_logger, role=...) reads the cycle-event
         stream via read_since and emits a RolePromptPacket (compactor owns the
         read; gate-OFF inside compact_prompts → empty packet, harmless).
      3. advisory write — append the compacted rules to
         ``state_dir/prompts/<role>.rules.txt`` (evidence area, always allowed
         when the compactor gate is ON; NOT the canonical prompt tree).
      4. operator write — only when ``_si_write_path_enabled()`` (2-env AND)
         route the packet through write_compacted → SafeAutoApply.apply_block
         on the canonical allowlisted prompt file (Auto-applied block).
    """
    from agi_v8_1.prompts.prompt_compactor_v8 import (
        compact_prompts,
        is_enabled as compactor_enabled,
        resolve_token_budget,
        write_compacted,
    )

    if not compactor_enabled():
        return None
    if cycle_logger is None:
        # No read surface → nothing to compact. Honour the gate (it IS on) by
        # returning an explicit no-read summary instead of None so the absence
        # is observable.
        return {"cadence_hit": False, "reason": "no_cycle_logger"}

    cadence = _prompt_compactor_cadence()
    if cycle_n <= 0 or cycle_n % cadence != 0:
        return {"cadence_hit": False, "cadence": cadence, "cycle_n": cycle_n}

    summary: dict[str, Any] = {
        "cadence_hit": True,
        "cadence": cadence,
        "cycle_n": cycle_n,
        "cycle_id": cycle_id,
        "role": _SI_PROMPT_ROLE,
        "rules_appended": 0,
        "sidecar_path": "",
        "write_path_enabled": False,
        "write_reason": "",
    }
    try:
        # __SLOT_COMPACTOR_CURSOR_2026_07_24__ Read the persisted cursor so this
        # round compacts only events NEWER than the last round (was cursor=0.0,
        # which re-read the whole stream every time and re-derived duplicates).
        cursor_path = _compact_cursor_path(Path(state_dir), _SI_PROMPT_ROLE)
        cursor_in = _read_compact_cursor(cursor_path)
        packet = compact_prompts(
            logger=cycle_logger,
            role=_SI_PROMPT_ROLE,
            cursor=cursor_in,
            token_budget=resolve_token_budget(),
        )
        summary["cursor_in"] = cursor_in
        summary["token_budget"] = getattr(packet, "token_budget", 0)
        # __SLOT_COMPACTOR_DROP_VISIBILITY_2026_07_24__ Surface the budget-bound
        # loss. The compactor already counted it, but it only reached the archive
        # JSON — so from the SI cycle record a compaction that silently discarded
        # most of the history looked identical to one that kept everything.
        summary["events_dropped"] = getattr(packet, "dropped_event_count", 0)
        summary["events_retained"] = len(getattr(packet, "retained_event_ids", ()) or ())
        summary["estimated_tokens"] = getattr(packet, "estimated_tokens", 0)
        # (a) advisory sidecar — durable accumulation under state_dir/prompts.
        sidecar = Path(state_dir) / "prompts" / f"{_SI_PROMPT_ROLE}.rules.txt"
        summary["rules_appended"] = _append_rules_sidecar(sidecar, packet)
        summary["sidecar_path"] = str(sidecar)
        # Advance the cursor only AFTER the sidecar write succeeded, so a crash
        # mid-round re-compacts the same slice instead of skipping it.
        cursor_out = float(getattr(packet, "next_cursor", cursor_in) or cursor_in)
        summary["cursor_out"] = cursor_out
        summary["cursor_persisted"] = _write_compact_cursor(
            cursor_path, cursor_out, _SI_PROMPT_ROLE
        )

        # (b) operator write path — canonical prompt file via SafeAutoApply.
        write_on = _si_write_path_enabled()
        summary["write_path_enabled"] = write_on
        if write_on:
            from agi_v8_1.enforcement.safe_auto_apply import SafeAutoApply

            prompt_dir = _canonical_prompt_dir()
            target = str(prompt_dir / f"{_SI_PROMPT_ROLE}.txt")
            # Allowlist the single role target under the resolved prompt dir.
            # The default dir == PROMPT_ALLOWLIST's, so production lands on the
            # already-allowlisted canonical file; an env-redirected dir
            # allowlists the redirected target (and ONLY that target).
            allow = frozenset({target})
            applier = SafeAutoApply(
                repo_root=prompt_dir,
                prompt_allowlist=allow,
                dry_run=False,
                # __SLOT_SI_AUDIT_JAIL_ANCHOR_2026_08_09__ 🔴 젤-밖 원장의 진원지.
                # `repo_root=prompt_dir` 라 폴백이 `data/prompts/state/si_audit/`
                # 를 냈다(08-08 라이브 12행, untracked). 바로 아래 `archive_fn` 이
                # 같은 이유로 이미 젤을 명시하고 있었다 — 원장만 빠져 있었다.
                audit_root=Path(state_dir),
            )
            write_res = write_compacted(
                target,
                packet,
                applier,
                allowlist=allow,
                # Co-locate the JSON archive snapshot with the SI's other state
                # (apply_chain / cycle_log) under state_dir, NOT the compactor's
                # AGI_V8_STATE_DIR-default (which would land in the canonical
                # installed package's state directory and pollute the tree). The archive is
                # a write-path provenance precondition, so it rides the SI's own
                # state dir for clean, per-run isolation.
                archive_fn=_make_state_dir_archive_fn(Path(state_dir)),
                verifier_id="si_v81",
                decision_source="si_loop_v81_prompt_evolution",
            )
            summary["write_reason"] = write_res.reason
            summary["write_target"] = write_res.target
            summary["write_proposal_id"] = write_res.proposal_id
            summary["wrote_prompt_block"] = bool(write_res.written)
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, ImportError) as _ff_exc:
        # Advisory compaction must never abort the SI cycle (mirrors the
        # logger-fault / dispatcher-fault contract).
        _swallowed(_ff_exc, site="self_improvement_v8._run_prompt_evolution_advisory:1462", category="verify")
        return summary
    return summary


# --- R6 escalation policy (C4 반증) -------------------------------------------
# PART1 §3 + S0/S7. The pre-R6 cycle had NO escalation policy at all (audit
# claim C4 CONFIRMED): v8 dropped v7.1's escalate_streak / force_replan /
# auto_head_reset machinery entirely. R6 revives it as a data-class policy
# (``core/cycle_policy_v81.py``) consumed at three points in the SI loop:
#
#   S0 (entry)   build_cycle_policy_v81(ring, prev_cycle_log, env) → snapshot.
#                escalate_streak = ring.trailing_escalated_count() (trailing
#                consecutive escalated entries — the v7.1 :265-271 semantics).
#   S6 (reflect) ring.append(status=...) after the cycle resolves, mapping the
#                SI status (consensus/split/blocked/advisory_stub) + any R3
#                rollback into a ring status (STATUS_ALIASES-compliant).
#   S7 (escal.)  when policy.force_replan, record phase_transition="forced_replan"
#                in the cycle_log payload AND surface it on SICycleOutcome so the
#                NEXT cycle's S2 propose stage can avoid the failing signature
#                (PART1 §3.3 — fixes v7.1's DEAD-OUTPUT defect where force_replan
#                was computed but never consumed).
#
# Gate: AGI_V81_CYCLE_POLICY_ENABLED (default-ON read loop). When OFF the policy
# returns a neutral snapshot (streak=0) and the ring append still honours the
# ring's OWN env gate (AGI_V8_CONTINUATION_RING_ENABLED, default-OFF) — so a
# fully-default cycle is byte-equivalent to the pre-R6 path (no ring history →
# streak=0 → never force_replan). NO mode enum: the status→ring mapping is a
# data tuple, the policy branches on data (ring counts + env), never a hardcoded
# mode string (feedback_no_mode_enums_2026_05_28).

# Data-driven SI-status → ring-status mapping (STATUS_ALIASES-compliant). The
# ring's canonical statuses are accepted / escalated / rejected /
# continuation_pending (see continuation_ring.STATUS_ALIASES). The SI cycle's
# own statuses (consensus/split/blocked/advisory_stub/rejected) map onto them:
#   consensus      → accepted              (cycle reached agreement)
#   blocked        → escalated             (axes failed / missing pod inputs)
#   split          → continuation_pending  (partial agreement → continue)
#   advisory_stub  → continuation_pending  (idle / no real evidence → continue)
#   rejected       → rejected              (ethics/auto-goal rejection, rare)
# A pure dict lookup (default continuation_pending) — NO enum branch.
_SI_STATUS_TO_RING_STATUS: Mapping[str, str] = {
    "consensus": "accepted",
    "blocked": "escalated",
    "split": "continuation_pending",
    "advisory_stub": "continuation_pending",
    "rejected": "rejected",
}
_RING_STATUS_DEFAULT = "continuation_pending"
# When an R3 apply-ladder session revert fired this cycle, the ring status is
# forced to ``escalated`` regardless of the SI status (PART1 §2.4 / task item 3):
# a rollback is an escalation event, so it feeds the force_replan streak.
_RING_STATUS_ON_ROLLBACK = "escalated"


def _map_si_status_to_ring_status(status: str, *, reverted: bool) -> str:
    """Map an SI cycle status (+ rollback flag) to a ring status.

    Data-driven (dict lookup), NO enum branch. A fired R3 session revert always
    escalates (overrides the status map) so the rollback feeds force_replan.
    """
    if reverted:
        return _RING_STATUS_ON_ROLLBACK
    return _SI_STATUS_TO_RING_STATUS.get(str(status), _RING_STATUS_DEFAULT)


# __SLOT_W2A2__ — W2.A2: migrate from weak core/apply_chain to strong
# enforcement/apply_chain_full (backward-scan tail read, strict tamper
# rejection, ``ts`` auto-injection, file_lock isolation). The on-disk
# filename remains ``apply_chain.jsonl`` (under ``state_dir``) — the
# additive ``chain_path`` kwarg on ``enforcement.append`` lets us keep it.
# Plan: integration/agi_v8_70lane_audit_2026_05_28/raw/w2_l8_apply_chain_migration.md
from agi_v8_1.enforcement.apply_chain_full import (  # __SLOT_W2A2__
    SCHEMA_VERSION as APPLY_CHAIN_SCHEMA,
    append as append_apply_entry,
)
from agi_v8_1.core.axis_scorer import (
    AXIS_KEYS,
    extract_axis_evidence,
    vote_per_axis,
    # __SLOT_R12_VACUOUS_2026_08_17__ the 5-state diagnosis (PASS/FAIL/
    # UNMEASURED/INELIGIBLE/EVALUATOR_ERROR) core/axis_scorer.py already ships
    # (R6 AX repair) but no production caller ever consumed — see the gate
    # docstring below for the wiring.
    DIAG_EVALUATOR_ERROR as _DIAG_EVALUATOR_ERROR,
    DIAG_INELIGIBLE as _DIAG_INELIGIBLE,
    DIAG_PASS as _DIAG_PASS,
    DIAG_UNMEASURED as _DIAG_UNMEASURED,
    diagnose_consensus_from_blocks as _diagnose_consensus_from_blocks,
)
from agi_v8_1.core.messages import (
    DecisionRecord,
    SIPolicyTicket,
    make_message_id,
)

if TYPE_CHECKING:
    from agi_v8_1.core.activation import ActivationDecision
    # __SLOT_F2_SANDBOX_VERIFY_2026_06_14__ type-only import (no runtime cost;
    # the producer is imported locally inside _maybe_run_sandbox_verify).
    from agi_v8_1.core.sandbox_runner import SandboxTelemetry


logger = logging.getLogger(__name__)

SCHEMA_VERSION = "agi_v8_si_cycle_v1"


# Status thresholds (axes_aligned out of 6 total)
_CONSENSUS_THRESHOLD = 5  # >=5 aligned -> consensus, unless inputs are stubs
_SPLIT_THRESHOLD = 3      # >=3 (and <5) -> split (R19 final_integrator)
                          # <3 -> blocked
_DEFAULT_STUB_REASON = "DEFAULT_POD_STUBS_NO_REAL_AGENT_EVIDENCE"

# __SLOT_R12_VACUOUS_2026_08_17__ round12 VACUOUS track — wires the existing
# (previously unconsumed) ``diagnose_consensus_from_blocks`` 5-state axis
# diagnosis into the ``status`` decision below, default ON.
#
# ## What was broken (audit-confirmed, file:line in the audit brief)
# ``_axis_aligned()`` (core/axis_scorer.py:542-545) legitimately scores a
# per-axis both-empty pair as ``(True, "neutral")`` — "no signal, no
# conflict" is a defensible LEAF rule. But that leaf climbs unchanged: 4 base
# axes empty-aligned -> novelty aligned (no diff) -> pareto aligned (all base
# aligned) -> 6/6 (or 7/7 with the causality axis) -> ``status="consensus"``
# below -> ``do_real_write`` in ``_run_apply_ladder``. The LESS evidence two
# pods produce, the EASIER the vote looks to pass — vacuous consensus.
#
# ## Chosen fix shape — fixed denominator, no auto-lowered threshold
# ``diagnose_consensus_from_blocks`` reports PASS only when BOTH axes_aligned
# reaches ``_CONSENSUS_THRESHOLD`` *and* at least one pod produced non-zero
# axis evidence overall (symmetric — UNMEASURED means literally zero
# evidence from both pods; INELIGIBLE means one pod produced zero while the
# other did not, an asymmetric imbalance that is equally untrustworthy as a
# consensus signal). We do NOT shrink the axis denominator to "however many
# axes had evidence" and we do NOT relax the threshold to match a smaller
# valid-axis count — the repo's own postmortem names that exact move
# ("유효 축 3개인데 3중3 자동완화") as the auto-threshold-lowering failure
# mode a prior audit already flagged. ``_CONSENSUS_THRESHOLD`` /
# ``_SPLIT_THRESHOLD`` and the total axis count are UNCHANGED by this slot.
#
# What changes: a cycle that reaches ``axes_aligned >= _CONSENSUS_THRESHOLD``
# ONLY because both pods stayed silent (UNMEASURED) or because one pod
# stayed silent while the other did not (INELIGIBLE), or where the
# evaluator itself faulted (EVALUATOR_ERROR, fail-closed — never counted as
# PASS), no longer reaches ``status="consensus"``. It falls through to the
# EXISTING ``elif axes_aligned >= _SPLIT_THRESHOLD`` branch using the
# UNADJUSTED axes_aligned count, so the cycle still completes as "split"
# (advisory-only, never gates ``do_real_write``) rather than being blocked
# outright — the closed loop keeps running; only the ability to WRITE on
# vacuous silence is what this slot removes. This is the deliberate
# too-tight-over-too-loose tradeoff: false "consensus" from silence is a
# write-authority defect (this is the single riskiest decision point in the
# repo — see round12 task brief); a cycle demoted to "split" is not a stall,
# because "split" already existed as a non-blocking, non-writing status.
#
# ## Gate — AGI_V8_CONSENSUS_DIAGNOSIS_WIRED, default ON (eval-integrity
# repair, same convention as core.axis_scorer's own
# AGI_V8_AXIS_ELIGIBILITY_ENABLED default-ON sibling gate). OFF reproduces
# the pre-slot behaviour byte-identically: the diagnosis is never computed,
# ``status`` is decided from ``axes_aligned`` alone exactly as before.
_CONSENSUS_DIAGNOSIS_WIRED_ENV = "AGI_V8_CONSENSUS_DIAGNOSIS_WIRED"


def _consensus_diagnosis_wired_enabled() -> bool:
    """Default-ON eval-integrity gate (strict ``"true"``/``"1"``, else OFF).

    OFF -> the status decision below skips the diagnosis call entirely and
    falls back to the legacy ``axes_aligned >= _CONSENSUS_THRESHOLD`` check,
    byte-identical to the pre-slot code path.
    """
    raw = os.environ.get(_CONSENSUS_DIAGNOSIS_WIRED_ENV, "true")  # tier: T1
    return (raw or "true").strip().lower() in ("true", "1")


def _agent_failure_metadata(agent_kind: str, phase: str, exc: Exception) -> dict[str, Any]:
    """Return structured failure metadata without echoing exception text."""
    return {
        "agent_kind": str(agent_kind),
        "phase": str(phase),
        "error_type": _safe_exception_type_name(exc),
        "error_message_redacted": True,
    }


@dataclass(frozen=True)
class SICycleOutcome:
    """Result of one SI cycle.

    Fields:
      status               consensus | split | blocked | advisory_stub | stub
      axes_aligned_count   0..6
      pod_a_evidence       AxisEvidence.as_dict() for Pod A
      pod_b_evidence       AxisEvidence.as_dict() for Pod B
      apply_chain_entry_hash  legacy prev_hash of the entry just written
      apply_chain_current_entry_hash  entry_hash of the exact row just written
      cycle_log_entries    count of cycle_log lines emitted (=1 per cycle)
      blocked_reason       human-readable reason if status='blocked'
    """
    schema_version: str = SCHEMA_VERSION
    cycle_id: str = ""
    status: str = "stub"
    axes_aligned_count: int = 0
    pod_a_evidence: dict[str, Any] = field(default_factory=dict)
    pod_b_evidence: dict[str, Any] = field(default_factory=dict)
    apply_chain_entry_hash: str = ""
    apply_chain_current_entry_hash: str = ""
    cycle_log_entries: int = 0
    blocked_reason: str = ""
    input_mode: str = ""
    agent_failures: tuple[Mapping[str, Any], ...] = ()
    # Lane B SIA: additive iteration summary surfaced to the orchestrator.
    # None when the SIA gate is OFF (default) or the hook is bypassed; a
    # mapping when the SIA controller ran an iteration this cycle. Kept as
    # ``Mapping | None`` to avoid pulling sia.* into the outcome's type
    # surface (the orchestrator only reads it as a dict).
    sia_iteration_summary: Mapping[str, Any] | None = None
    # R1 S1 observation stage: the ObservationBundle (trailing 3-cycle
    # context + prev-run baseline + drift keys) the cycle's evaluation
    # consumed. None when the AGI_V8_CYCLE_LOGGER_ENABLED gate is OFF
    # (default) or no cycle_logger was supplied — i.e. byte-equivalent
    # no-op. A mapping when the observation stage ran this cycle.
    observation: Mapping[str, Any] | None = None
    # __SLOT_BUS_AUTOWIRE_2026_06_16__ external falsifier-bus step (produce
    # trader wrong-signals via the pnl bridge → register a forward claim →
    # grade due predictions → digest). None when AGI_V8_BUS_AUTOWIRE_ENABLED is
    # OFF (default) — byte-equivalent no-op. A mapping when the bus step ran.
    bus_signal: Mapping[str, Any] | None = None
    # R2 (C13 반증): advisory summary of the SIDispatcherV8 7-stage dispatch
    # this cycle triggered. None when AGI_V8_SI_DISPATCHER_V8_ENABLED is OFF
    # (default) or the dispatch was bypassed/failed — byte-equivalent no-op.
    # A mapping (cycle_id / enabled / stage_count / chain_entries_written /
    # failures) when the dispatcher ran. The dispatcher persists its OWN
    # chain file under state_dir; it never touches the SI apply chain.
    si_dispatcher_summary: Mapping[str, Any] | None = None
    # R3 (C7 반증): summary of the apply ladder (S3 verify → S4 apply) this
    # cycle ran. ALWAYS present (never None) because the apply ladder always
    # runs — the closed loop completes every cycle. Carries the REAL measured
    # pre/post_apply_hash (never the fixed ``stub_*`` placeholders), dry_run flag (True when
    # the 2-env write gate is OFF == default), changes_applied, reverted, and
    # the S3 verdict (None when there were no concrete file changes to verify).
    apply_summary: Mapping[str, Any] | None = None
    # R6 (C4 반증): the S0 escalation-policy snapshot this cycle ran under
    # (escalate_streak / force_replan / head_auto_reset). ALWAYS present (never
    # None) — the policy is built unconditionally at cycle entry; a gate-OFF or
    # empty-ring cycle yields a neutral snapshot (streak=0, force_replan=False),
    # which is byte-equivalent to the pre-R6 behaviour. JSON-serialisable dict
    # (CyclePolicyV81.as_dict()).
    cycle_policy: Mapping[str, Any] | None = None
    # R6 S7 replan signal — the consumed force_replan flag, surfaced on the
    # outcome so the NEXT cycle / the orchestrator can branch its S2 propose
    # stage (PART1 §3.3 — the v7.1 defect was that force_replan was computed but
    # never consumed). True iff escalate_streak >= 3 this cycle. Default False.
    force_replan: bool = False
    # R6 S7 — the phase transition recorded when force_replan fired this cycle.
    # "forced_replan" when the streak hit the threshold, "" otherwise. Also
    # stamped into the cycle_log payload so the signal is durable + auditable.
    phase_transition: str = ""
    # R6 S7 — whether the head-state auto-reset signal fired (force_replan AND
    # the AGI_V81_HEAD_AUTO_RESET_ENABLED opt-out is ON). The actual destructive
    # reset is performed (with a backup) only when a state file exists. False by
    # default.
    head_auto_reset_fired: bool = False
    # R7 (C9 반증): summary of the prompt-evolution compaction round this cycle
    # ran (cadence_hit / rules_appended / sidecar_path / write_path_enabled /
    # write_reason). None when AGI_V8_PROMPT_COMPACTOR_ENABLED is OFF (default)
    # or the cadence has not elapsed — byte-equivalent no-op. A mapping when the
    # compactor ran this cycle. The advisory sidecar lives under
    # state_dir/prompts; the canonical prompt-file write needs the 2-env operator
    # AND-gate (AGI_V8_SI_WRITE_PATH_ENABLED + AGI_V8_COMPACTOR_WRITE_PATH_ENABLED).
    prompt_evolution_summary: Mapping[str, Any] | None = None


class SelfImprovementV8:
    """V8 SI cycle. Pod-distributed by construction.

    R18 시드 — :func:`run_one_si_cycle` wires:
      axis_scorer.extract_axis_evidence → vote_per_axis
      → status decision → apply_chain.append_apply_entry
      → cycle_log JSONL.

    Pod A/B finding blocks are fixtures in R18; R19 swaps them for real
    agent outputs (executor / planner / critic), R20 adds provider lane.
    """

    def __init__(
        self,
        *,
        state_dir: Path,
        continuation_ring: "Any | None" = None,
    ):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.apply_chain_path = self.state_dir / "apply_chain.jsonl"
        self.cycle_log_path = self.state_dir / "cycle_log.jsonl"
        # R6 (C4 반증): the escalation-history SSOT. When a ring is not supplied
        # the SI loop builds a default one keyed by this state_dir; the ring's
        # OWN env gate (AGI_V8_CONTINUATION_RING_ENABLED, default-OFF) governs
        # whether any append/read actually persists — so a default cycle's ring
        # stays empty (streak=0) and force_replan never fires. Injected for
        # tests so a pre-seeded history (3x escalated) can drive force_replan.
        if continuation_ring is None:
            from agi_v8_1.core.continuation_ring import StatusHistoryRing

            _ring_kwargs = dict(
                run_id="si_loop_v81",
                session_id="si_loop",
                pod="si",
                state_dir=self.state_dir / "continuation_ring",
            )
            # __SLOT_C1_RING_REHYDRATE_2026_07_25__ Gate ON → rebuild the ring
            # from its JSONL sidecar so the escalation streak survives process
            # boundaries (the armed tick model is one process per cycle, so a
            # fresh in-memory ring capped the streak at 1 and force_replan could
            # never fire). load_from_sidecar returns an EMPTY ring when the ring's
            # own env gate is off or no sidecar exists, and re-enables persistence
            # for subsequent appends, so arming this cannot lose or duplicate
            # entries. OFF (default) keeps the pre-fix empty-ring construction.
            if _continuation_ring_rehydrate_enabled():
                continuation_ring = StatusHistoryRing.load_from_sidecar(**_ring_kwargs)
            else:
                continuation_ring = StatusHistoryRing(**_ring_kwargs)
        self.continuation_ring = continuation_ring

    def run_one_si_cycle(
        self,
        cycle_id: str,
        *,
        pod_a_block: Mapping[str, Any] | None = None,
        pod_b_block: Mapping[str, Any] | None = None,
        activation_decision: "ActivationDecision | None" = None,
        critic: "Any | None" = None,
        cycle_logger: "Any | None" = None,
        objective: str = "",
    ) -> SICycleOutcome:
        """Run one SI cycle end-to-end.

        When *pod_a_block* / *pod_b_block* are None, identical R18 stub
        fixtures are used (both pods agree on a trivial finding → all 6
        axes align, but the cycle is reported as ``status='advisory_stub'``
        because no real Pod A/B evidence was supplied).

        R19 additive kwargs (backward compatible — None is the R18 default):
          critic         — optional CriticAgent that will evaluate any emitted
                           DecisionRecord. CriticTicket.severity is folded into
                           the cycle log (event_type="evidence_emit").
          cycle_logger   — optional CycleLogger; logs cycle_start / cycle_end /
                           si_policy events.
        """
        # __SLOT_OBJECTIVE_PATCH_MODE_EXCLUSIVE_2026_09_02__ Inc3 and inc4 are
        # alternative postimage producers.  Resolve their gates before stage
        # registration, observation, swarm dispatch, or any provider builder so
        # a conflicting operator configuration costs $0 and writes no cycle
        # evidence.  Keep the selected mode immutable for this whole cycle.
        if os.environ.get("AGI_V8_PUBLIC_TIER_GATE_ENABLED", "false").strip().lower() == "true":
            from agi_v8_1.policy.tier_gate import require_tier
            require_tier(1)
        objective_patch_mode = _objective_patch_mode.select_mode()
        t0 = time.monotonic()

        # META-SI round 3 MUT-2: defensively re-register the 5 SI
        # stage_ids in case a test fixture (or external caller) reset
        # the registry between module import and this cycle entry.
        # Idempotent: preserves any test-primed counts via
        # ``_CoverageRegistry.register``'s last-writer-wins-on-payload
        # contract (count is preserved across re-register). MUST run
        # before the stage-0 increment below so a mid-process
        # ``reset_registry()`` cannot desync the meter.
        _ensure_registered()

        # META-SI round 2 stage wire #1: cycle_start (entry). Increment
        # regardless of whether the optional cycle_logger is supplied —
        # the meter measures SI cycle reach, not logger reach.
        _coverage_increment_stage(SI_STAGE_IDS[0])

        # --- S0 (R6): escalation-policy snapshot (C4 반증) ---
        # Build the cycle policy BEFORE any work so its escalate_streak /
        # force_replan / head_auto_reset signals are available to S2 (propose)
        # and S7 (escalation). The policy consumes the ring's TRAILING escalated
        # count (v7.1 :265-271 semantics). Gate-OFF (AGI_V81_CYCLE_POLICY_ENABLED
        # false) or empty ring → neutral snapshot (streak=0) → byte-equivalent to
        # the pre-R6 path. prev_cycle_log seeds cycle_n only (best-effort read).
        from agi_v8_1.core.cycle_policy_v81 import (
            auto_reset_head_state,
            build_cycle_policy_v81,
        )

        # cycle_n is seeded from the durable cycle_log JSONL read DIRECTLY (via
        # state_store.read_jsonl) — NOT via cycle_logger.read_since, so the R1
        # observation stage stays the sole read_since caller (its once-per-cycle
        # contract is preserved). Best-effort: a missing/empty log → cycle_n=1.
        _prev_cycle_log: list[Any] = []
        try:
            _prev_cycle_log = read_jsonl(self.cycle_log_path)
        except (OSError, ValueError, TypeError) as _ff_exc:
            _swallowed(_ff_exc, site="self_improvement_v8.run_one_si_cycle:1779", category="verify")
            _prev_cycle_log = []
        cycle_policy = build_cycle_policy_v81(
            ring=self.continuation_ring,
            prev_cycle_log=_prev_cycle_log,
        )

        # R19: optional cycle_logger — cycle_start event.
        if cycle_logger is not None:
            try:
                cycle_logger.log_event(
                    event_type="cycle_start",
                    payload={
                        "schema_version": SCHEMA_VERSION,
                        "activation_mode": (
                            activation_decision.mode.value
                            if activation_decision is not None
                            else None
                        ),
                        # MUT-2 (META-SI): stamp the threshold pair into
                        # cycle_start so drift becomes auditable post-hoc
                        # without re-running the cycle. Additive fields,
                        # ints, no env gate (the existing typed except below
                        # still guards logger faults).
                        "consensus_threshold": _CONSENSUS_THRESHOLD,
                        "split_threshold": _SPLIT_THRESHOLD,
                    },
                    cycle_id=cycle_id,
                )
            # META-SI round 2 MUT-3 site :181 (cycle_start ONLY): replace
            # the bare ``except Exception: pass`` with a typed handler for
            # the realistic logger-fault surface. ``logging.Handler``
            # callbacks raise TypeError / AttributeError when payload keys
            # collide with a structured formatter, ValueError when a level
            # name is malformed, and OSError when a file/stream handler's
            # backing fd is closed. Anything else (e.g. KeyboardInterrupt,
            # SystemExit, RuntimeError from a custom CycleLogger contract
            # violation) MUST still propagate so the cycle does not paper
            # over genuine bugs. Per AP-6, sites :331/:351/:367 (sia/critic
            # /si_policy/cycle_end) stay bare for round 2; they migrate in
            # round 3 once this site's behaviour is in-tree for >= 1 week.
            except (TypeError, AttributeError, ValueError, OSError) as _ff_exc:
                # Record the swallowed fault in the data-driven coverage
                # registry so the silent-drop becomes observable. No
                # re-raise — preserves the original "never block SI cycle
                # on log failure" invariant from R19.
                _swallowed(_ff_exc, site="self_improvement_v8.run_one_si_cycle:1820", category="verify")
                increment_logger_drop_total()

        # --- H1 (R5): external-change-detect at SI cycle entry ---
        # PART2 §3 H1: fire the external-change-detect hook so a human edit to
        # TaskBundle JSON / swarm_v8 internals between cycles is fingerprinted
        # into the router_freeze ledger as a ChangeReport (C5 reversal). The
        # bridge wrapper is imported lazily so the SI module's import graph stays
        # byte-identical when the gate is OFF — the hook itself returns None
        # WITHOUT importing the detector unless
        # ``AGI_V8_EXTERNAL_CHANGE_DETECT_ENABLED`` is exactly "true"/"1"
        # (gate-OFF == no-op, no network, no disk). A detector fault never
        # blocks the SI cycle (typed except mirrors the logger-fault contract).
        try:
            from agi_v8_1.bridge.change_detect_wire import run_change_detect

            run_change_detect("si_start")
        except (ImportError, OSError, ValueError, TypeError) as _ff_exc:
            # Never block the SI cycle on a change-detect fault.
            _swallowed(_ff_exc, site="self_improvement_v8.run_one_si_cycle:1841", category="verify")
            pass

        # --- S1 (R1): observation stage — read_since first production use ---
        # C6 fix: when AGI_V8_CYCLE_LOGGER_ENABLED is ON *and* a cycle_logger
        # is supplied, rehydrate the trailing 3-cycle context (+ prev-run
        # baseline + drift keys) into an ObservationBundle and inject it into
        # the cycle's evaluation context. Gate-OFF (default) or no logger →
        # observation stays None → byte-equivalent no-op vs the pre-R1 path.
        observation_bundle: dict[str, Any] | None = None
        if cycle_logger is not None and _cycle_logger_observation_enabled():
            observation_bundle = _build_observation_bundle(
                cycle_logger,
                cycle_id=cycle_id,
                state_dir=self.state_dir,
            )

        # --- bench backlog #3: durable objective reinjection (S1 bundle attach, T9) ---
        # __SLOT_DURABLE_GOAL_2026_08_22__ Reinjects the persisted durable
        # objective (runtime/durable_goal.py) into THIS cycle's S1
        # observation bundle, untrusted-wrapped, and stamps a NAMED
        # cycle_logger row every armed cycle — never silent, whether or not
        # anything was actually reinjected (see module docstring "no silent
        # reinjection"). Gated on BOTH this gate AND
        # ``AGI_V8_CYCLE_LOGGER_ENABLED`` (no bundle ⇒ nowhere to attach the
        # reinjection ⇒ no-op), the same AND-of-two-gates shape as backlog
        # #2's progress oracle below. A fault anywhere in this block is
        # swallowed whole — a durable-goal fault must never break the SI
        # cycle it is merely carrying context into.
        if (
            cycle_logger is not None
            and observation_bundle is not None
            and _goal_reinject_enabled()
        ):
            try:
                from agi_v8_1.runtime import durable_goal as _dg

                _dg_state = _dg.load_state(self.state_dir)
                _dg_payload: dict[str, Any] = {
                    "prior_status": _dg_state["status"],
                    "prior_read_error": _dg_state.get("read_error"),
                }
                if _dg_state["status"] == _dg.STATUS_ACTIVE:
                    # A durable goal is already active: reinject IT,
                    # regardless of what (if anything) this cycle's own
                    # ``objective`` kwarg says — that is exactly what
                    # "durable" guards against (a caller passing a
                    # different/empty objective this cycle must never
                    # silently erase the standing goal; only
                    # ``mark_complete`` may).
                    _dg_text = _dg_state["objective"]
                    observation_bundle["durable_goal"] = {
                        "status": _dg.STATUS_ACTIVE,
                        "objective_wrapped": _dg.wrap_for_injection(_dg_text),
                        "set_at_cycle": _dg_state["set_at_cycle"],
                        "set_reason": _dg_state["set_reason"],
                    }
                    _dg_payload.update({
                        "reinjected": True,
                        "source": "active_persisted",
                        "objective": _dg_text,
                        "reason": "durable goal already active; reinjected unchanged",
                    })
                elif _dg_state["status"] == _dg.STATUS_COMPLETE:
                    observation_bundle["durable_goal"] = {
                        "status": _dg.STATUS_COMPLETE,
                        "objective_wrapped": None,
                        "set_at_cycle": _dg_state["set_at_cycle"],
                        "set_reason": _dg_state["set_reason"],
                    }
                    _dg_payload.update({
                        "reinjected": False,
                        "source": "complete",
                        "objective": None,
                        "reason": (
                            "durable goal marked complete at cycle "
                            f"{_dg_state.get('completed_at_cycle')!r}; not reinjected"
                        ),
                    })
                elif objective and _dg_state.get("read_error") is not None:
                    # R19-10 fix: status=="none" here does NOT mean "cold
                    # start" — load_state() ALSO fail-closes an unreadable
                    # or malformed state file to status=="none" (see its
                    # own docstring). Adopting *objective* via set_goal()
                    # in that case would silently clobber whatever active
                    # goal genuinely exists on disk, just because ONE read
                    # happened to fail — the injection-side "모름 접혀 0"
                    # mistake, but on the write side. So: read_error present
                    # + an objective was supplied ⇒ do NOT descend into the
                    # adopt/overwrite branch below. Record — named, never a
                    # silent skip — that this cycle gave up on writing
                    # because the prior state is unknown, and try again next
                    # cycle once the read succeeds (write-side fail-closed).
                    observation_bundle["durable_goal"] = {
                        "status": _dg.STATUS_NONE,
                        "objective_wrapped": None,
                        "set_at_cycle": None,
                        "set_reason": None,
                    }
                    _dg_payload.update({
                        "reinjected": False,
                        "source": "read_error",
                        "objective": None,
                        "reason": (
                            "durable-goal state file failed to read "
                            f"({_dg_state.get('read_error')!r}); refusing to "
                            "adopt this cycle's objective over an unknown "
                            "prior state (would risk silently overwriting a "
                            "genuinely active goal lost to a transient read "
                            "fault) — will retry adoption once the read "
                            "succeeds"
                        ),
                    })
                elif objective:
                    # No durable goal yet, but this cycle supplied one:
                    # adopt it as the new durable goal and reinject it in
                    # this SAME cycle (so the very cycle that first
                    # supplies an objective already sees it echoed back
                    # wrapped, satisfying "present in bundle" on cycle one).
                    # Only reached when read_error is None — i.e. a genuine
                    # cold start (missing state file), not a masked read
                    # fault (see the read_error branch immediately above).
                    _dg_new = _dg.set_goal(
                        self.state_dir, objective, cycle_id=cycle_id,
                        reason="adopted_from_cycle_objective",
                    )
                    observation_bundle["durable_goal"] = {
                        "status": _dg.STATUS_ACTIVE,
                        "objective_wrapped": _dg.wrap_for_injection(_dg_new["objective"]),
                        "set_at_cycle": _dg_new["set_at_cycle"],
                        "set_reason": _dg_new["set_reason"],
                    }
                    _dg_payload.update({
                        "reinjected": True,
                        "source": "adopted",
                        "objective": _dg_new["objective"],
                        "reason": "no durable goal was active; adopted this cycle's objective",
                    })
                else:
                    observation_bundle["durable_goal"] = {
                        "status": _dg.STATUS_NONE,
                        "objective_wrapped": None,
                        "set_at_cycle": None,
                        "set_reason": None,
                    }
                    _dg_payload.update({
                        "reinjected": False,
                        "source": "none",
                        "objective": None,
                        "reason": "no durable goal active and no objective supplied this cycle",
                    })
                cycle_logger.log_event(
                    event_type=_dg.EVENT_TYPE,
                    payload=_dg_payload,
                    cycle_id=cycle_id,
                )
            except Exception as _dg_exc:  # noqa: BLE001 — reinjection observes/
                # carries context, it must never be able to break the cycle.
                _swallowed(
                    _dg_exc,
                    site="self_improvement_v8.run_one_si_cycle:durable_goal",
                    category=_FF_TELEMETRY,
                )

        # --- 1. Pod block fixtures (R18; R19 real agents) ---
        # META-SI round 2 stage wire #2: pod_block_fill (default fixtures
        # or provided real-agent blocks both count as a stage visit; the
        # meter distinguishes pod_defaulted via MUT-1's counter, not here).
        _coverage_increment_stage(SI_STAGE_IDS[1])
        pod_a_defaulted = pod_a_block is None
        pod_b_defaulted = pod_b_block is None
        if pod_a_block is None:
            pod_a_block = {
                "findings": [{"text": "R18 stub finding"}],
                "reviewer_acceptance": "accept",
            }
        if pod_b_block is None:
            pod_b_block = {
                "findings": [{"text": "R18 stub finding"}],
                "reviewer_acceptance": "accept",
            }
        if pod_a_defaulted and pod_b_defaulted:
            input_mode = "default_stub"
        elif pod_a_defaulted or pod_b_defaulted:
            input_mode = "partial_stub"
        else:
            input_mode = "provided"

        # MUT-1 (META-SI): emit pod_defaulted flags to MetricLog so the
        # default-stub vs provided ratio becomes observable. Gated by the
        # existing AGI_V8_OBSERVABILITY_ENABLED env knob (MetricLog.emit
        # is itself a no-op when disabled). Import is best-effort: if the
        # observability module is unavailable for any reason we skip
        # silently so the SI cycle remains unaffected. Additive only.
        if input_mode != "provided":
            try:
                from agi_v8_1.observability.metric_log import (
                    get_default_metric_log,
                )

                _ml = get_default_metric_log()
                _ml.emit(
                    "metric_log.cycle.pod_defaulted_total",
                    value=int(pod_a_defaulted) + int(pod_b_defaulted),
                    tags={
                        "cycle_id": str(cycle_id),
                        "input_mode": input_mode,
                        "pod_a_defaulted": str(pod_a_defaulted).lower(),
                        "pod_b_defaulted": str(pod_b_defaulted).lower(),
                    },
                )
            except (ImportError, OSError, ValueError, TypeError) as exc:
                # Never block the SI cycle on observability faults — but record
                # them, so "the logger was broken all along" is discoverable
                # from census() instead of being invisible.
                _swallowed(
                    exc, site="si.cycle_logger.observability",
                    category=_FF_TELEMETRY,
                )

        # --- 2-3. 6-axis evidence + vote ---
        # META-SI round 2 stage wire #3: axis_scorer (extract + vote both
        # roll into one stage visit — the apply_chain entry already records
        # axes_aligned for downstream attribution).
        _coverage_increment_stage(SI_STAGE_IDS[2])
        a_ev = extract_axis_evidence(pod_a_block)
        b_ev = extract_axis_evidence(pod_b_block)
        alignment, _detail = vote_per_axis(a_ev, b_ev)
        axes_aligned = sum(1 for v in alignment.values() if v)

        # Observability (audit A2): count the total axis evidence both pods
        # produced. The axis keywords are DEFECT signals (incorrect/wrong/bug/
        # secret/injection/…), so an empty axis legitimately means "no concern
        # flagged", and both-pods-empty is a clean mutual approval → consensus
        # is the CORRECT status (NOT a false green — the audit A2 "silence =
        # agreement" claim was a false positive against a defect-only scanner).
        # We do NOT change the decision on this; we only STAMP the count into the
        # durable records so the frequency of all-empty ("vacuous") cycles is
        # auditable post-hoc. Counts exactly the token axes vote_per_axis saw
        # (AXIS_KEYS + causal_chain). Additive int, no env gate (mirrors MUT-2).
        axis_evidence_total = 0
        for _ev in (a_ev, b_ev):
            for _ax in AXIS_KEYS:
                axis_evidence_total += len(getattr(_ev, _ax, ()) or ())
            axis_evidence_total += len(getattr(_ev, "causal_chain", ()) or ())

        # __SLOT_R12_VACUOUS_2026_08_17__ 5-state evidence diagnosis. Computed
        # ONLY on the branch it can actually change (both pods provided real,
        # non-defaulted input) — the missing-input branches below decide
        # status from pod_a_defaulted/pod_b_defaulted alone and never
        # consult it, so skipping it there keeps their durable-record shape
        # (cycle_log / apply_chain / si_policy) untouched by this slot, not
        # just their status value. OFF -> None, and the elif below falls
        # back to the legacy axes_aligned-only check verbatim.
        consensus_diagnosis = (
            _diagnose_consensus_from_blocks(
                pod_a_block, pod_b_block, threshold=_CONSENSUS_THRESHOLD,
            )
            if _consensus_diagnosis_wired_enabled()
            and not (pod_a_defaulted or pod_b_defaulted)
            else None
        )
        consensus_diagnosis_status = (
            consensus_diagnosis.status if consensus_diagnosis is not None else ""
        )
        # PASS only when the diagnosis explicitly agrees evidence was real and
        # symmetric on both pods. UNMEASURED (both silent) / INELIGIBLE (one
        # pod silent) / EVALUATOR_ERROR (fail-closed) never satisfy this, even
        # when the raw axes_aligned count alone would have reached threshold —
        # this is the "vacuous consensus" hole this slot closes. When the gate
        # is OFF, `consensus_diagnosis is None` short-circuits back to the
        # pre-slot behaviour byte-identically.
        _diagnosis_blocks_consensus = (
            consensus_diagnosis is not None
            and consensus_diagnosis.status != _DIAG_PASS
        )

        # --- 4. Status decision ---
        if pod_a_defaulted and pod_b_defaulted:
            status = "advisory_stub"
            blocked_reason = _DEFAULT_STUB_REASON
        elif pod_a_defaulted or pod_b_defaulted:
            status = "blocked"
            blocked_reason = "MISSING_POD_INPUTS_DEFAULT_STUB_USED"
        elif axes_aligned >= _CONSENSUS_THRESHOLD and not _diagnosis_blocks_consensus:
            status = "consensus"
            blocked_reason = ""
        elif axes_aligned >= _SPLIT_THRESHOLD:
            # Also reached when axes_aligned>=_CONSENSUS_THRESHOLD but the
            # evidence diagnosis above vetoed "consensus" (vacuous/asymmetric/
            # faulted) — demoted to split (advisory, non-writing), never
            # blocked outright, so the closed loop still completes.
            status = "split"  # R19 final_integrator
            blocked_reason = (
                f"consensus_diagnosis={consensus_diagnosis_status}"
                if axes_aligned >= _CONSENSUS_THRESHOLD and _diagnosis_blocks_consensus
                else ""
            )
        else:
            status = "blocked"
            blocked_reason = f"axes_aligned<{_SPLIT_THRESHOLD}"

        wall_clock_ms = (time.monotonic() - t0) * 1000.0

        # --- R3: apply ladder (S3 verify → S4 apply) — C7 theatre 제거 -------
        # Replaces the pre-R3 fixed ``stub_*`` placeholder snapshot hashes
        # apply theatre with a REAL SafeAutoApply.apply_session run. verify
        # (rubric_emergency + g1-g8) runs only when there are concrete file
        # changes; apply runs always (dry_run when the 2-env write gate is OFF
        # — the default), producing a REAL measured aggregate hash. The closed
        # loop always completes; only disk WRITE is operator-gated.
        # --- S2' (F1): failure-signature proposer — the missing PRODUCER of
        # proposed_file_changes (break site was _extract_file_changes:345).
        # Gated default-OFF; reuses the observation bundle (no extra read_since);
        # repo_root == state_dir so proposals can only target the SI state dir.
        # __SLOT_SI_CODE_PROPOSER_F1_2026_06_13__
        proposer_changes: list[Mapping[str, Any]] = []
        # __SLOT_SI_PROPOSED_COMMANDS_CHANNEL_2026_08_02__ the OTHER half of the
        # propose end: commands the LLM lanes ask for when a work product needs
        # an observation that writing files cannot provide. Accumulated exactly
        # like proposer_changes and handed to the SAME apply-ladder call, but on
        # a distinct key so it never touches the SafeAutoApply file session (a
        # command has no file to back up) and never passes through the inc5
        # verify gate (which verifies PATCHES). Empty unless the lane-side gate
        # AGI_V8_SI_PROPOSED_COMMANDS_ENABLED is on AND a model asked; execution
        # still requires the executor's own two gates.
        proposer_commands: list[str] = []
        # Best-of-N inc4 may expose its independently materialised candidates
        # only to the already-armed inc5 gate. ``None`` means the optional API
        # hand-off was absent (single-shot / breadth OFF / verify OFF), so the
        # historical one-batch path below remains untouched. An empty list is
        # distinct: breadth ran but no finitely scored candidate was eligible.
        _inc4_ranked_candidate_sets: list[dict[str, Any]] | None = None
        _inc4_archive_changes: list[Any] = []
        if observation_bundle is not None and _si_code_proposer_enabled():
            try:
                from agi_v8_1.si_lanes.failure_proposer import (
                    propose_from_observation,
                )

                proposer_changes = propose_from_observation(
                    observation_bundle, repo_root=self.state_dir,
                )
            except Exception as exc:  # noqa: BLE001 — proposer never aborts a cycle
                _swallowed(exc, site="self_improvement_v8.run_one_si_cycle:1982", category="verify")
                logger.warning(
                    "F1 proposer failed (non-fatal): %s",
                    _format_exception_for_log(exc),
                )
                proposer_changes = []
            if proposer_changes:
                logger.info(
                    "F1 proposer emitted %d proposed_file_change(s) (cycle %s)",
                    len(proposer_changes), cycle_id,
                )

        # --- S2'' (F1 inc2): LLM proposal-artifact producer. Separately gated;
        # APPENDS to proposer_changes so both inc1 + inc2 flow through the SAME
        # apply ladder call below (one ladder, no second call). The LLM output
        # lives only inside each artifact's JSON content as DATA — the path
        # (si_proposed/patches/<sig>.proposal.json, state_dir-relative) and
        # action ("create") are computed locally, never from the model.
        # __SLOT_SI_LLM_PROPOSER_F1_INC2_2026_06_14__
        if observation_bundle is not None and _si_llm_proposer_enabled():
            try:
                from agi_v8_1.si_lanes.llm_failure_proposer import (
                    propose_from_observation as _llm_propose,
                )

                _propose_fn = _build_si_llm_propose_fn(self.state_dir)
                if _propose_fn is not None:
                    _r = _llm_propose(
                        observation_bundle,
                        repo_root=self.state_dir,
                        propose_fn=_propose_fn,
                    )
                    proposer_changes = list(proposer_changes) + list(
                        _r["proposed_file_changes"]
                    )
                    proposer_commands += list(_r.get("proposed_commands") or ())
                    logger.info(
                        "F1-inc2 LLM proposer: diagnoses=%d proposals=%d "
                        "no_op=%s cost=%s commands=%d",
                        _r["diagnoses"], _r["proposals"], _r["no_op"], _r["cost"],
                        len(_r.get("proposed_commands") or ()),
                    )
            except Exception as exc:  # noqa: BLE001 — inc2 never aborts a cycle
                _swallowed(exc, site="self_improvement_v8.run_one_si_cycle:2019", category="verify")
                logger.warning(
                    "F1-inc2 LLM proposer failed (non-fatal): %s",
                    _format_exception_for_log(exc),
                )

        # --- S2''' (F1 inc3): OBJECTIVE-driven code proposer. Separately gated;
        # APPENDS to proposer_changes so inc1+inc2+inc3 all flow through the SAME
        # apply ladder call below. Unlike inc1/inc2 (failure-reactive, advisory),
        # inc3 turns the cycle's objective into REAL full-file ``create`` changes
        # under a bounded shadow dir (si_proposed/objective/<objhash>/, state_dir
        # -relative) — a previewable patch in the jail, never the live tree. The
        # LLM supplies only ``content`` (syntax + size validated); the path is
        # shadow-prefixed + basename-sanitised and the action is a hardcoded
        # ``create``. objective threads in from the orchestrator's CycleSeed.
        # __SLOT_SI_OBJECTIVE_PROPOSER_F1_INC3_2026_07_02__
        if (
            objective
            and objective_patch_mode == _objective_patch_mode.MODE_PROPOSER
        ):
            try:
                from agi_v8_1.runtime import daily_cost_cap
                from agi_v8_1.si_lanes.objective_proposer import (
                    propose_from_objective as _obj_propose,
                )

                _propose_fn = _build_si_llm_propose_fn(self.state_dir)
                if _propose_fn is not None:
                    # Per-day cost cap gates the (billable) provider call BEFORE
                    # it happens — a standing objective under a recurring driver
                    # can only re-bill while the day's spend is under the floor.
                    _sd = self.state_dir

                    def _inc3_budget_ok() -> bool:
                        return bool(daily_cost_cap.check(_sd)["allowed"])

                    _inc3_targets = _si_objective_targets()
                    _r = _obj_propose(
                        objective,
                        repo_root=self.state_dir,
                        source_root=_si_cycle_source_root(
                            self.state_dir, _inc3_targets
                        ),
                        target_files=_inc3_targets,
                        propose_fn=_propose_fn,
                        budget_check=_inc3_budget_ok,
                    )
                    proposer_changes = list(proposer_changes) + list(
                        _r["proposed_file_changes"]
                    )
                    proposer_commands += list(_r.get("proposed_commands") or ())
                    logger.info(
                        "F1-inc3 objective proposer: proposals=%d no_op=%s "
                        "cost=%s commands=%d",
                        _r["proposals"], _r["no_op"], _r["cost"],
                        len(_r.get("proposed_commands") or ()),
                    )
            except Exception as exc:  # noqa: BLE001 — inc3 never aborts a cycle
                _swallowed(exc, site="self_improvement_v8.run_one_si_cycle:2064", category="verify")
                logger.warning(
                    "F1-inc3 objective proposer failed (non-fatal): %s",
                    _format_exception_for_log(exc),
                )

        # --- S2'''' (F1 inc4): mutually exclusive anchored-diff alternative to
        # inc3. The LLM emits find/replace edits (not whole files); the editor
        # materialises them against the real source in memory and emits a
        # ``create`` of the patched file under a distinct shadow dir
        # (si_proposed/objective_edits/<objhash>/). Untouched bytes are preserved
        # verbatim (no full-file-rewrite drift), so this is the path intended for
        # eventual live-tree arming. Same jail + budget_check as inc3.
        # __SLOT_SI_OBJECTIVE_EDITOR_F1_INC4_2026_07_02__
        if (
            objective
            and objective_patch_mode == _objective_patch_mode.MODE_EDITOR
        ):
            try:
                from agi_v8_1.runtime import daily_cost_cap
                from agi_v8_1.si_lanes.objective_editor import (
                    propose_edits_from_objective as _obj_edit,
                )

                _edit_fn = _build_si_llm_propose_fn(self.state_dir)
                if _edit_fn is not None:
                    _sd4 = self.state_dir

                    def _inc4_budget_ok() -> bool:
                        return bool(daily_cost_cap.check(_sd4)["allowed"])

                    _inc4_targets = _si_objective_targets()
                    _inc4_handoff_kwargs = (
                        {"include_ranked_candidate_sets": True}
                        if _si_verify_gate_enabled() else {}
                    )
                    _re = _obj_edit(
                        objective,
                        repo_root=self.state_dir,
                        source_root=_si_cycle_source_root(
                            self.state_dir, _inc4_targets
                        ),
                        target_files=_inc4_targets,
                        propose_fn=_edit_fn,
                        budget_check=_inc4_budget_ok,
                        **_inc4_handoff_kwargs,
                    )
                    if "ranked_candidate_sets" in _re:
                        # This shape is produced locally by objective_editor,
                        # not accepted from the model. Still validate it at the
                        # privilege boundary: malformed internal wiring must
                        # suppress the whole candidate batch, never fall back
                        # to the heuristic winner.
                        _raw_ranked = _re["ranked_candidate_sets"]
                        if not isinstance(_raw_ranked, list):
                            raise ValueError(
                                "inc4 ranked_candidate_sets must be a list"
                            )
                        _parsed_ranked: list[dict[str, Any]] = []
                        for _candidate in _raw_ranked:
                            if not isinstance(_candidate, Mapping):
                                raise ValueError(
                                    "inc4 ranked candidate must be a mapping"
                                )
                            _candidate_changes = _candidate.get(
                                "proposed_file_changes"
                            )
                            _candidate_commands = _candidate.get(
                                "proposed_commands", ()
                            )
                            if (
                                not isinstance(_candidate_changes, (list, tuple))
                                or not _candidate_changes
                                or any(
                                    not isinstance(_change, Mapping)
                                    for _change in _candidate_changes
                                )
                                or not isinstance(
                                    _candidate_commands, (list, tuple)
                                )
                                or any(
                                    not isinstance(_command, str)
                                    for _command in _candidate_commands
                                )
                            ):
                                raise ValueError(
                                    "inc4 ranked candidate payload is invalid"
                                )
                            _parsed_ranked.append({
                                "proposed_file_changes": [
                                    dict(_change)
                                    for _change in _candidate_changes
                                ],
                                "proposed_commands": list(_candidate_commands),
                            })
                        _inc4_ranked_candidate_sets = _parsed_ranked
                        # Preserve the pre-existing proposal-archive contract:
                        # until inc5 chooses another candidate, the scorer's
                        # top-ranked legacy view is the archived proposal.
                        _inc4_archive_changes = list(
                            _re["proposed_file_changes"]
                        )
                    else:
                        proposer_changes = list(proposer_changes) + list(
                            _re["proposed_file_changes"]
                        )
                        proposer_commands += list(
                            _re.get("proposed_commands") or ()
                        )
                    logger.info(
                        "F1-inc4 objective editor: proposals=%d edits_applied=%d "
                        "edits_dropped=%d no_op=%s cost=%s commands=%d",
                        _re["proposals"], _re["edits_applied"], _re["edits_dropped"],
                        _re["no_op"], _re["cost"],
                        len(_re.get("proposed_commands") or ()),
                    )
            except Exception as exc:  # noqa: BLE001 — inc4 never aborts a cycle
                _swallowed(exc, site="self_improvement_v8.run_one_si_cycle:2106", category="verify")
                logger.warning(
                    "F1-inc4 objective editor failed (non-fatal): %s",
                    _format_exception_for_log(exc),
                )

        # __SLOT_SI_PROPOSAL_ARCHIVE_WIRE_2026_08_24__ Preserve the proposal
        # objects *before* inc5 replaces ``proposer_changes`` with only the
        # accepted subset (or clears it on a verifier error).  Copy each
        # mapping so the archived full-file ``content`` remains the value the
        # proposer emitted even if a verifier implementation later annotates
        # its input mappings in place.  This snapshot is telemetry only: it is
        # never handed to the apply ladder.
        _proposed_for_archive: list[Any] = [
            dict(change) if isinstance(change, Mapping) else change
            for change in [*proposer_changes, *_inc4_archive_changes]
        ]
        _proposal_archive_verdict: Mapping[str, Any] | None = None
        _proposal_archive_summary: dict[str, Any] | None = None

        # --- S3-verify (F1 inc5): worktree test-verification gate. When armed,
        # the proposed code patches (inc3/inc4) are run against the real test
        # suite in an isolated raw-HEAD snapshot BEFORE the apply ladder.
        # Candidate-controlled pytest is diagnostic only: mapped patches remain
        # blocked until an authority outside that Python trust domain reviews
        # them. Only the closed advisory allowlist can pass through untouched.
        # __SLOT_SI_VERIFY_GATE_F1_INC5_2026_07_02__
        # __SLOT_SI_CMD_VERIFY_GATE_2026_08_02__ adversarial-review-3 fix (F2).
        # The gate used to filter ONLY ``proposer_changes``: a RED verdict that
        # dropped 100% of the patches still handed ``proposer_commands`` — the
        # HIGHER-privilege payload from the SAME untrusted responses — to the
        # ladder, which planned and (armed) executed them. Commands now require
        # an out-of-domain verified run: RED, gate error, advisory-only, or an
        # in-process rc0 all clear them.
        _verify_gate_on = bool(objective and _si_verify_gate_enabled())
        _commands_verified = False
        _apply_confirmation_id: str | None = None
        # __SLOT_VERIFY_RESULT_EVENT_2026_08_22__ `_v` 는 아래 try 스코프에만
        # 살아서 사이클 끝까지 못 온다. 요약을 밖에 두어 성공/에러 양쪽에서
        # 채우고, 관문이 아예 안 돈 경우는 None 으로 남긴다(0 아님).
        _verify_summary: dict[str, Any] | None = None
        # 게이트 판정은 runtime.card_targets 정본을 쓴다 — 사본 정의 금지.
        try:
            from agi_v8_1.runtime.card_targets import gate_enabled as _ct_gate_enabled
        except ImportError as _ct_exc:
            _swallowed(_ct_exc,
                       site="self_improvement_v8.run_one_si_cycle:card_targets_import",
                       category="config")

            def _ct_gate_enabled() -> bool:   # 모듈 부재 = OFF (fail-closed)
                return False
        if _verify_gate_on and (
            proposer_changes or _inc4_ranked_candidate_sets
        ):
            # __SLOT_CARD_SCOPED_TARGETS_2026_08_22__ 표적을 **한 번** 정하고 그
            # 개수를 요약에 남긴다. 0 은 측정된 사실이다("아무도 표적을 주지
            # 않았다") — 그 상태에서 verify_gate 는 코드 패치를 전부 unknown
            # path 로 떨어뜨려 ``unverifiable_changes_dropped`` RED 를 내는데,
            # 원장만 보면 그게 능력 부족과 같은 모양이었다.
            _targets = _si_objective_targets()
            _active_verify_count = len(proposer_changes)
            if _inc4_ranked_candidate_sets:
                _active_verify_count += len(
                    _inc4_ranked_candidate_sets[0]["proposed_file_changes"]
                )
            try:
                from agi_v8_1.si_lanes import verify_gate_log as _vgl
                from agi_v8_1.si_lanes import completion_authority as _completion
                from agi_v8_1.capabilities import PayloadPort, resolve_payload

                verify_changes = resolve_payload(PayloadPort(
                    5, "agi_v8_1.si_lanes.verify_access_payload", "verify_changes"))
                _discard_apply_confirmation = resolve_payload(PayloadPort(
                    5, "agi_v8_1.si_lanes.verify_access_payload", "confirm_verified_apply"))

                _verify_source_root = _si_cycle_source_root(
                    self.state_dir, _targets
                )
                _protected_contract = None
                from agi_v8_1.runtime import workspace_snapshot as _wsnap

                if _wsnap.episode_repo_name_from_env():
                    _protected_contract = (
                        _completion.current_episode_protected_contract()
                    )
                    if (
                        _completion.completion_authority_enabled()
                        and _protected_contract is None
                    ):
                        raise RuntimeError(
                            "episode protected oracle contract was not captured"
                        )

                # __SLOT_SI_INC5_ORDERED_FIRST_GREEN_2026_09_04__ The breadth
                # scorer is only an ordering heuristic.  When inc4 supplied
                # independently materialised candidate sets, verify each full
                # transaction in score order and stop at the first result that
                # carries out-of-domain completion authority.  Never combine
                # two alternatives targeting the same file into one batch.
                _ordered_inc4 = bool(_inc4_ranked_candidate_sets)
                _base_changes = list(proposer_changes)
                _base_commands = list(proposer_commands)
                if _ordered_inc4:
                    _verify_batches = [
                        (
                            [
                                *_base_changes,
                                *list(_candidate["proposed_file_changes"]),
                            ],
                            list(_candidate["proposed_commands"]),
                            list(_candidate["proposed_file_changes"]),
                        )
                        for _candidate in _inc4_ranked_candidate_sets or ()
                    ]
                else:
                    _verify_batches = [(list(proposer_changes), None, None)]

                _v: dict[str, Any] | None = None
                _before = len(proposer_changes)
                _active_verify_count = _before
                _selected_rank: int | None = None
                for _rank, (
                    _candidate_batch,
                    _candidate_commands,
                    _candidate_archive_changes,
                ) in enumerate(_verify_batches):
                    _active_verify_count = len(_candidate_batch)
                    _candidate_v = verify_changes(
                        _candidate_batch,
                        source_root=_verify_source_root,
                        target_files=_targets,
                        apply_root=self.state_dir,
                        protected_contract=_protected_contract,
                    )
                    _candidate_confirmation_id = _candidate_v.pop(
                        "_apply_confirmation_id", None
                    )
                    _v = _candidate_v
                    _before = len(_candidate_batch)
                    if not _ordered_inc4:
                        if isinstance(_candidate_confirmation_id, str):
                            _apply_confirmation_id = (
                                _candidate_confirmation_id
                            )
                        break

                    _accepted = _candidate_v.get("accepted_changes")
                    _trusted_green = bool(
                        _candidate_v.get("ok")
                        and _candidate_v.get("ran")
                        and _candidate_v.get("completion_verified")
                        and isinstance(_candidate_confirmation_id, str)
                        and _candidate_confirmation_id
                        and isinstance(_accepted, (list, tuple))
                        and _accepted
                    )
                    if _trusted_green:
                        _selected_rank = _rank
                        _apply_confirmation_id = _candidate_confirmation_id
                        proposer_changes = list(_accepted)
                        proposer_commands = [
                            *_base_commands,
                            *(_candidate_commands or ()),
                        ]
                        # Archive the raw selected postimage rather than the
                        # heuristic candidate zero when a later rank wins.
                        _proposed_for_archive = [
                            dict(_change)
                            if isinstance(_change, Mapping) else _change
                            for _change in [
                                *_base_changes,
                                *(_candidate_archive_changes or ()),
                            ]
                        ]
                        logger.info(
                            "F1-inc5 ordered candidates: rank=%d GREEN; "
                            "stopping before %d later candidate(s)",
                            _rank,
                            len(_verify_batches) - _rank - 1,
                        )
                        break

                    # A confirmation handle on a non-GREEN result is invalid
                    # for this transaction. Consume it without advancing the
                    # trusted apply chain instead of leaving reusable state.
                    if isinstance(_candidate_confirmation_id, str):
                        _discard_apply_confirmation(
                            _candidate_confirmation_id, applied=False
                        )
                    logger.info(
                        "F1-inc5 ordered candidates: rank=%d not GREEN "
                        "(ok=%s ran=%s completion_verified=%s reason=%s)",
                        _rank,
                        bool(_candidate_v.get("ok")),
                        bool(_candidate_v.get("ran")),
                        bool(_candidate_v.get("completion_verified")),
                        _candidate_v.get("reason"),
                    )

                if _v is None:
                    raise RuntimeError("inc5 received no candidate batch")
                if _ordered_inc4 and _selected_rank is None:
                    # No ranked alternative earned trusted completion. Even
                    # an advisory accepted subset from a RED attempt cannot
                    # launder the candidate transaction into apply.
                    proposer_changes = []
                    # The single durable verdict below is the final attempted
                    # candidate's verdict, so archive that same raw candidate
                    # rather than falsely pairing it with rank zero's body.
                    _proposed_for_archive = [
                        dict(_change)
                        if isinstance(_change, Mapping) else _change
                        for _change in [
                            *_base_changes,
                            *(_candidate_archive_changes or ()),
                        ]
                    ]
                # Keep the exact normal-path verdict before filtering destroys
                # the per-proposal accepted/blocked attribution at this seam.
                # On the exception path this deliberately stays None: a gate
                # error did not measure a per-proposal disposition.
                _proposal_archive_verdict = _v
                if not _ordered_inc4:
                    proposer_changes = list(_v["accepted_changes"])
                # Commands are higher privilege than advisory file notes.
                # ``nothing_to_verify`` is ok=True but ran=False, and candidate
                # Python cannot certify its own completion.  Require all three
                # axes so an allowlisted advisory cannot launder commands.
                _commands_verified = bool(
                    _v.get("ok")
                    and _v.get("ran")
                    and _v.get("completion_verified")
                    and (
                        not _ordered_inc4
                        or _selected_rank is not None
                    )
                )
                logger.info(
                    "F1-inc5 verify gate: ok=%s ran=%s passed=%d failed=%d "
                    "accepted=%d/%d reason=%s",
                    _v["ok"], _v["ran"], _v["passed"], _v["failed"],
                    len(proposer_changes), _before, _v["reason"],
                )
                # __SLOT_SI_VERIFY_GATE_LOG_P1_2026_07_31__ durable verdict row
                # (default-OFF): the W1 first-run wrapper halts on consecutive
                # RED by reading these — the logger.info line above is dropped
                # on the unconfigured tick path and survives nowhere else.
                _vgl.record(
                    self.state_dir,
                    cycle_id=cycle_id,
                    objective=objective,
                    verdict=_v,
                    proposed=_before,
                    accepted=len(proposer_changes),
                )
                # __SLOT_VERIFY_RESULT_EVENT_2026_08_22__ 되먹임용 요약.
                # `ran=False`(nothing_to_verify 등)면 통과/실패 수는 측정된
                # 값이 아니라 **재보지 않은 값**이므로 None 으로 둔다.
                _ran = bool(_v.get("ran"))
                _verify_summary = {
                    # 🔴 적대검증·조사 실측(08-23): pytest 출력이 캡을 넘으면
                    # verify_isolation._TailBuffer 가 스트림을 **통째로 폐기**하고도
                    # ran=True 를 유지한다 → _parse_summary 가 빈 텍스트에서
                    # passed=0/failed=0 을 만든다. 그걸 measured=True 로 적으면
                    # 결정안 B 가 "실패 0개"라는 **거짓 신호**를 투표에 주입한다
                    # (ctl1 cycle2 실측). ran 만으로는 부족하다.
                    "measured": bool(_ran) and not bool(_v.get("output_truncated")),
                    "output_truncated": bool(_v.get("output_truncated")),
                    "ran": _ran,
                    "ok": bool(_v.get("ok")),
                    "passed": _v.get("passed") if _ran else None,
                    "failed": _v.get("failed") if _ran else None,
                    "mapped": _v.get("mapped"),
                    "dropped": _v.get("dropped"),
                    "backend": _v.get("backend"),
                    "reason": _v.get("reason"),
                    "failed_tests": [
                        str(t) for t in (_v.get("failed_tests") or ())
                    ][:_VERIFY_FAILED_TESTS_CAP],
                    "proposed": _before,
                    "accepted": len(proposer_changes),
                    **({"targets_declared": len(_targets)}
                       if _ct_gate_enabled() else {}),
                    # __SLOT_VERIFY_STAMP_GOAL_2026_09_11__ 목표 검사 결과(고정 어휘)
                    # — OFF 면 키 자체가 없다(행 스키마 불변).
                    **({"public": _public_stamp(_v.get("public_goal_checks"))}
                       if _verify_stamp_goal_enabled() else {}),
                }
            except Exception as exc:  # noqa: BLE001 — fail-closed: drop unverified
                # The durable RED row below owns the detailed, masked text.
                # Keep this pre-record logger type-only so neither hostile
                # stringification nor a strict masker failure can erase the row
                # or replace ``exc`` before it is persisted.
                logger.warning(
                    "F1-inc5 verify gate errored — dropping code patches "
                    "(fail-closed): error_type=%s",
                    _safe_exception_type_name(exc),
                )
                # The verifier owns the *only* closed advisory allowlist.  If
                # it raises, recreating an older basename heuristic here would
                # preserve arbitrary unknown paths (including novel ``.py``)
                # and turn an operational failure into an apply bypass.  The
                # transaction therefore retains no proposer file payload at
                # all; work/answer lanes below have separate typed channels.
                _n_prior = _active_verify_count
                proposer_changes = []
                # __SLOT_VERIFY_RESULT_EVENT_2026_08_22__ 에러도 판정이다 — 다만
                # **RED 이면서 미측정**이다. 통과/실패 수를 0 으로 적으면 다음
                # 사이클이 "깨끗했다"로 읽는다. 그래서 전부 None + measured=False.
                _verify_summary = {
                    "measured": False,
                    "ran": None,
                    "ok": False,
                    "passed": None,
                    "failed": None,
                    "mapped": None,
                    "dropped": None,
                    "backend": None,
                    "reason": "verify_gate_errored",
                    "failed_tests": [],
                    "proposed": _n_prior,
                    "accepted": 0,
                    **({"targets_declared": len(_targets)}
                       if _ct_gate_enabled() else {}),
                    # __SLOT_VERIFY_STAMP_GOAL_2026_09_11__ 관문이 죽었으면 목표
                    # 검사도 없었다 — None 이지 unknown 스탬프도 아니다.
                    **({"public": None}
                       if _verify_stamp_goal_enabled() else {}),
                }
                # __SLOT_SI_VERIFY_GATE_LOG_P1_2026_07_31__ the error path is a
                # RED verdict too — W1 must see "could not verify" as red. The
                # record is written BEFORE the ``_swallowed`` below: under
                # strict fail-fast that call re-raises, and a RED row that only
                # existed after it would never be written (adversarial-review
                # finding). The guard warns instead of routing through
                # ``swallowed``: re-raising a LOGGING failure here would mask
                # the original ``exc`` and misattribute the abort.
                try:
                    from agi_v8_1.si_lanes import verify_gate_log as _vgl_err

                    _vgl_err.record(
                        self.state_dir,
                        cycle_id=cycle_id,
                        objective=objective,
                        verdict=None,
                        proposed=_n_prior,
                        accepted=len(proposer_changes),
                        # Pre-format through the non-throwing transaction
                        # adapter; the writer applies the same critical text
                        # boundary again, while the bare primary object remains
                        # available for ``_swallowed`` below to re-raise.
                        error=_format_exception_for_critical_record(exc),
                    )
                except Exception as _vgl_exc:  # noqa: BLE001 — never mask ``exc``
                    logger.warning(
                        "F1-inc5 verdict record failed on the error path "
                        "(not masking the verify error): error_type=%s",
                        _safe_exception_type_name(_vgl_exc),
                    )
                # Counted last, deliberately: under AGI_V8_STRICT_FAIL_FAST
                # this re-raises ``exc`` (loud abort) — everything above must
                # already be on disk by then.
                _swallowed(exc, site="self_improvement_v8.run_one_si_cycle:2133", category="verify")

        # __SLOT_SI_PROPOSAL_ARCHIVE_WIRE_2026_08_24__ Archive after inc5 has
        # produced its verdict but from the pre-filter snapshot above.  The
        # archive is non-authoritative telemetry, so neither an import failure
        # nor a persistence failure may abort the SI cycle -- including under
        # strict fail-fast.  ``archive_cycle`` already reports normal write
        # failures as a typed summary; this outer guard also isolates the
        # import/call seam itself.  Gate OFF does not call the writer and leaves
        # both the filesystem and the apply-entry schema unchanged.
        _proposal_archive_gate_on = False
        try:
            from agi_v8_1.si_lanes import proposal_archive as _proposal_archive

            _proposal_archive_gate_on = _proposal_archive.enabled()
            if _proposal_archive_gate_on:
                _archive_result = _proposal_archive.archive_cycle(
                    self.state_dir,
                    cycle_id=cycle_id,
                    changes=_proposed_for_archive,
                    verdict=_proposal_archive_verdict,
                    objective=objective,
                )
                if isinstance(_archive_result, Mapping):
                    _proposal_archive_summary = dict(_archive_result)
                else:
                    logger.warning(
                        "proposal archive returned a non-mapping summary "
                        "(ignored): result_type=%s",
                        type(_archive_result).__name__,
                    )
        except Exception as _pa_exc:  # noqa: BLE001 -- telemetry never aborts SI
            logger.warning(
                "proposal archive wiring failed (non-fatal): error_type=%s",
                _safe_exception_type_name(_pa_exc),
            )
            if _proposal_archive_gate_on:
                _proposal_archive_summary = {
                    "archived": False,
                    "reason": (
                        "archive_wiring_error:"
                        f"{_safe_exception_type_name(_pa_exc)}"
                    ),
                    "written": None,
                    "bodies_written": None,
                    "truncated": None,
                    "redacted": None,
                    "sensitive_skipped": None,
                    "over_count_cap": None,
                    "index_path": None,
                }

        # __SLOT_VERIFY_RESULT_EVENT_2026_08_22__ 관문 판정을 **이벤트 스트림**에
        # 싣는다. 여기가 유일한 방출 지점이다 — 되먹임(_build_observation_bundle)
        # 은 `read_since` 로 이 스트림만 읽고, cycle_log.jsonl 은 안 본다.
        # 게이트 OFF 면 이벤트가 아예 안 생겨 스트림이 종전과 byte-identical 이다.
        # ⛔ 관측 실패가 사이클을 죽이지 않는다 — 다만 조용히 넘기지도 않는다.
        if (
            _verify_summary is not None
            and cycle_logger is not None
            and _verify_stamp_enabled()
        ):
            try:
                cycle_logger.log_event(
                    event_type="verify_result",
                    payload=dict(_verify_summary),
                    cycle_id=str(cycle_id),
                )
            except (AttributeError, OSError, ValueError, TypeError) as _vs_exc:
                _swallowed(
                    _vs_exc,
                    site="self_improvement_v8.run_one_si_cycle:verify_result_event",
                    category=_FF_TELEMETRY,
                )

        # __SLOT_SI_CMD_VERIFY_GATE_2026_08_02__ (F2) fail-closed command gating.
        # Reached on EVERY path — including "the gate never ran because there
        # were no patches to verify", where nothing about this cycle's model
        # output was ever verified. The gate is itself default-OFF, so with it
        # off the commands flow exactly as before (and the executor's own two
        # gates still decide execution).
        #
        # Adversarial review (CC-4): this interacts with the OBSERVATION-ONLY
        # case that ``objective_proposer``/``objective_editor`` bless — a lane
        # legitimately returning commands and ZERO file changes. With the verify
        # gate armed that cycle can never produce a GREEN verdict (the gate is
        # skipped for want of patches), so 100% of its commands are dropped
        # here. That is the fail-closed side and it stays; what changed is that
        # the two lane comments now SAY so instead of asserting the opposite
        # contract. If this is ever relaxed, the relaxation belongs here, in one
        # place, with its own gate — not by loosening the lanes.
        if _verify_gate_on and proposer_commands and not _commands_verified:
            logger.warning(
                "F1-inc5 verify gate lacks trusted completion — dropping %d proposed "
                "command(s) (fail-closed; commands are the higher-privilege "
                "payload of the same responses)",
                len(proposer_commands),
            )
            proposer_commands = []

        # __SLOT_SI_CMD_GLOBAL_CAP_2026_08_02__ adversarial-review-3 fix (F6).
        # ``MAX_COMMANDS`` is enforced PER LANE, and inc2/inc3/inc4 each append
        # to the same list — so the seam could receive 3×8 = 24 commands per
        # cycle with no cross-lane dedup, 3× the bound the channel docstring
        # claims. Re-running the lane normaliser over the CONCATENATION applies
        # the cap and the dedup globally (single source of truth — no second
        # copy of the bound to drift). Imported lazily: with the channel gate
        # off this list is always empty, so the module is never imported.
        if proposer_commands:
            from agi_v8_1.si_lanes.command_channel import (
                normalize_commands as _normalize_commands,
            )

            _cmds_before = len(proposer_commands)
            proposer_commands = _normalize_commands(proposer_commands)
            if len(proposer_commands) != _cmds_before:
                logger.info(
                    "SI command channel: %d cross-lane command(s) → %d after "
                    "global dedup + cap",
                    _cmds_before, len(proposer_commands),
                )

        # --- S2''' (F3): work-product lane. Deliberately placed AFTER the inc5
        # verify gate rather than inside it: that gate runs the real test suite
        # in a worktree to judge patches against the LIVE source tree, and a
        # jailed workspace script is not that — routing it through the gate
        # would spend a full suite run to verify a file the suite never sees.
        # It rides the same ladder call on the same synthetic block.
        # __SLOT_SI_WORK_PRODUCT_LANE_2026_08_02__
        work_changes: list[Mapping[str, Any]] = []
        work_runs: list[Mapping[str, Any]] = []
        if objective:
            try:
                from agi_v8_1.si_lanes import work_product as _wp

                if _wp.enabled():
                    _wp_fn = _build_si_llm_propose_fn(self.state_dir)
                    if _wp_fn is None:
                        logger.info(
                            "F3 work-product: providers OFF → lane is a no-op"
                        )
                    else:
                        _attempt, _fb = _work_product_history(self.state_dir)
                        _wr = _wp.propose_work_product(
                            objective=objective,
                            cycle_id=cycle_id,
                            attempt=_attempt,
                            propose_fn=_wp_fn,
                            feedback=_fb,
                        )
                        work_changes = list(_wr["proposed_file_changes"])
                        work_runs = list(_wr["proposed_artifact_runs"])
                        logger.info(
                            "F3 work-product: attempt=%d no_op=%s changes=%d "
                            "runs=%d had_feedback=%s cost=%s",
                            _attempt, _wr["no_op"], len(work_changes),
                            len(work_runs), _fb is not None, _wr.get("cost"),
                        )
            except Exception as exc:  # noqa: BLE001 — F3 never aborts a cycle
                _swallowed(exc, site="self_improvement_v8.run_one_si_cycle:work_product",
                           category="verify")
                logger.warning(
                    "F3 work-product lane failed (non-fatal): %s",
                    _format_exception_for_log(exc),
                )
                work_changes, work_runs = [], []

        # --- S2'''' (F3-A): 답변 레인. 질문형 목표는 code-change proposer 레인에
        # 태우면 **답이 아니라 .py 모듈**이 나온다(측정: breadth scorer 가 파싱되는
        # 파이썬에 +1000, 산문에 -500 을 준다). 그래서 여기서 별도로, 사람이 읽는
        # 문서를 낸다. 실행 없음 · 자기 세션 · 젤 안 쓰기.
        # __SLOT_SI_ANSWER_PRODUCT_LANE_2026_08_06__
        answer_changes: list[Mapping[str, Any]] = []
        if objective:
            try:
                from agi_v8_1.si_lanes import answer_product as _ans

                if _ans.enabled():
                    _ans_fn = _build_si_llm_propose_fn(self.state_dir)
                    if _ans_fn is None:
                        logger.info(
                            "F3-A answer: providers OFF → lane is a no-op"
                        )
                    else:
                        _ar = _ans.propose_answer(
                            objective=objective,
                            cycle_id=cycle_id,
                            propose_fn=_ans_fn,
                        )
                        answer_changes = list(_ar["proposed_answer_changes"])
                        logger.info(
                            "F3-A answer: no_op=%s reason=%s path=%s chars=%d "
                            "cost=%s",
                            _ar["no_op"], _ar["reason"], _ar.get("path"),
                            _ar.get("chars") or 0, _ar.get("cost"),
                        )
            except Exception as exc:  # noqa: BLE001 — F3-A never aborts a cycle
                _swallowed(exc, site="self_improvement_v8.run_one_si_cycle:answer",
                           category="verify")
                logger.warning(
                    "F3-A answer lane failed (non-fatal): %s",
                    _format_exception_for_log(exc),
                )
                answer_changes = []

        # __SLOT_SI_EXECUTION_AUTHORITY_2026_08_17__ Round 5 §3 3-source fix:
        # when the inc5 verify gate is armed (``_verify_gate_on``), the digest
        # set of the commands it actually reviewed (``proposer_commands``,
        # already filtered by ``_commands_verified`` above) becomes the SOLE
        # authorized command set for THIS cycle's apply ladder — binding
        # pod_a_block/pod_b_block command sources to the same review the
        # proposer channel got, not a second unreviewed path to the same
        # executor. Gate OFF (default posture, or the gate never armed this
        # cycle) ⇒ None ⇒ ``_run_apply_ladder`` applies no source restriction
        # (byte-identical to pre-fix).
        _approved_cmd_digests: frozenset[str] | None = None
        if _verify_gate_on:
            from agi_v8_1.policy.execution_authority import (
                command_digest as _cmd_digest,
            )

            _approved_cmd_digests = frozenset(
                _cmd_digest(c) for c in proposer_commands
            )

        apply_summary = _run_apply_ladder(
            status=status,
            pod_a_block=pod_a_block,
            pod_b_block=pod_b_block,
            state_dir=self.state_dir,
            extra_changes=proposer_changes or None,
            # 산출물은 자가수정 제안과 **운명을 같이하지 않는다**(위 SLOT 참조).
            extra_work_changes=work_changes or None,
            extra_artifact_runs=work_runs or None,
            # 답변도 산출물 스크립트와 운명을 같이하지 않는다(세 번째 세션).
            extra_answer_changes=answer_changes or None,
            # __SLOT_SI_PROPOSED_COMMANDS_CHANNEL_2026_08_02__ empty (the
            # default) ⇒ no ``proposed_commands`` key on the synthetic block ⇒
            # ``_extract_commands`` returns [] ⇒ no ``commands`` key on the
            # summary ⇒ byte-identical apply summary + apply_chain entry.
            extra_commands=proposer_commands or None,
            approved_command_digests=_approved_cmd_digests,
            verified_apply_confirmation_id=_apply_confirmation_id,
        )
        if _apply_confirmation_id is not None:
            try:
                from agi_v8_1.capabilities import PayloadPort, resolve_payload

                _confirm_verified_apply = resolve_payload(PayloadPort(
                    5, "agi_v8_1.si_lanes.verify_access_payload", "confirm_verified_apply"))

                apply_summary["verified_apply_chain_advanced"] = (
                    _confirm_verified_apply(
                        _apply_confirmation_id,
                        applied=bool(apply_summary.get("applied")),
                    )
                )
            except Exception as exc:  # noqa: BLE001 - observability, fail closed
                _swallowed(
                    exc,
                    site=(
                        "self_improvement_v8.run_one_si_cycle:"
                        "confirm_verified_apply"
                    ),
                    category="apply",
                )
                apply_summary["verified_apply_chain_advanced"] = False
        # __SLOT_SI_WORK_PRODUCT_LANE_2026_08_02__ persist BEFORE the apply-chain
        # append: this log is the next attempt's only memory, and a cycle that
        # dies later must not cost the loop that memory.
        if apply_summary.get("artifact_runs"):
            _record_work_product_runs(
                self.state_dir, apply_summary["artifact_runs"]
            )

        # --- 5. Apply chain append (R12 Merkle) ---
        # META-SI round 2 stage wire #4: apply_chain. Incremented before
        # the actual append so a mid-write crash still leaves the meter
        # showing "reached the stage" — useful for partial-cycle audits.
        _coverage_increment_stage(SI_STAGE_IDS[3])
        apply_entry: dict[str, Any] = {
            "schema_version": APPLY_CHAIN_SCHEMA,
            "timestamp": time.time(),
            "cycle_id": cycle_id,
            "decision_source": "self_improvement_v8",
            "verifier_id": "axis_scorer",
            # R3: REAL measured snapshot hashes from the apply ladder
            # (SafeAutoApply._aggregate_hash over the on-disk snapshot). NEVER
            # the fixed ``stub_*`` placeholder sentinels again — that was the
            # C7 theatre.
            "pre_apply_hash": apply_summary["pre_apply_hash"],
            "post_apply_hash": apply_summary["post_apply_hash"],
            "rollback_latency_ms": apply_summary["rollback_latency_ms"],
            "status": status,
            "axes_aligned": axes_aligned,
            "axis_evidence_total": axis_evidence_total,  # A2: 0 == vacuous
            "input_mode": input_mode,
            "stub_inputs": pod_a_defaulted or pod_b_defaulted,
            "wall_clock_ms": round(wall_clock_ms, 3),
            # R3 apply-ladder provenance (additive; observability_first).
            "apply_dry_run": apply_summary["dry_run"],
            "apply_changes_applied": apply_summary["changes_applied"],
            "apply_change_count": apply_summary["change_count"],
            # __SLOT_PROPOSAL_IDENTITY_2026_08_06__ 🔴 **거절이 무엇에 대한
            # 거절인지** 원장이 말하지 못했다.
            #
            # 실측(2026-08-06): 46일 4,448 tick 동안 제안이 나온 건 **단 1건**
            # (cycle ``tick_1785997207_q1785996``)이고 그건 ``cross_model_refuted``
            # 로 막혔다. 그런데 원장에는 *"1건이 막혔다"* 만 남고 **무엇이**
            # 막혔는지가 없다. 바로 위 SLOT 이 "어느 게이트가 막았나"를 남긴 것과
            # 같은 종류의 구멍이 하나 더 있었던 셈이다 — 그건 *누가* 막았는지고
            # 이건 *무엇을* 막았는지다. 둘 다 있어야 한 줄로 재구성된다.
            #
            # 왜 이게 아픈가: proposer 의 **유일한 멱등 장치가 착지한 원장의
            # ``# sig:<id>`` 마커**다. 거절된 제안은 원장에 안 닿으므로 마커가
            # 안 생기고, ``si_lanes/`` 전체에 거절을 읽는 코드는 0개다.
            #
            # ⚠️ **계측만이다.** 이 값을 읽어 재제안을 막는 코드는 넣지 않는다
            #    (계측 먼저, 강제 나중). 재제안이 실제로 관측된 적은 아직 없고,
            #    관측 없이 기계를 만들면 그게 우리가 동결하려는 "할 일 없는
            #    신규 기능"이다.
            #
            # 이 시점의 ``proposer_changes`` = **게이트를 통과해 사다리로 넘어간**
            # 것들(2755 의 ``extra_changes`` 와 같은 객체). 로컬 verify 게이트가
            # 떨어뜨린 것들은 별개 축이고 ``si_verify_gate_log.jsonl`` 소관이다.
            "apply_proposal_ids": _proposal_ids(proposer_changes),
            # __SLOT_DRY_RUN_ATTRIBUTION_2026_08_02__ WHY the write did not land.
            # ``do_real_write = consensus and verify_pass and write_enabled`` —
            # recording only ``apply_dry_run`` says a cycle produced nothing
            # without saying which of the three refused, so the operator (and
            # the next session) has to re-derive it by re-running the stage by
            # hand. Measured cost of that gap: a whole diagnosis pass spent
            # guessing between a policy block and a missing API key. All three
            # are already computed; this just stops throwing them away.
            "apply_consensus": apply_summary["consensus"],
            "apply_write_enabled": apply_summary["write_enabled"],
            "apply_verify_blocked_reason": (
                (apply_summary.get("verdict") or {}).get("blocked_reason") or ""
            ),
            # __SLOT_CROSS_MODEL_REVIEWER_DETAIL_2026_09_13__ "cross_model_refuted" 의
            # **사유** — 게이트 ON 일 때만 키가 생긴다(OFF = 행 모양 불변).
            **(
                {"apply_cross_model": _cross_model_detail(apply_summary.get("verdict"))}
                if _cross_model_reviewer_detail_enabled() else {}
            ),
            "apply_errors": list(apply_summary.get("errors") or ())[:5],
            # 산출물 세션은 자가수정과 별개다 — 별개면 원장에도 별개로 보여야
            # 한다. 없으면 "레인이 침묵했다"는 사실 자체가 기록이다.
            "work_product": apply_summary.get("work_product"),
            # __SLOT_SI_ANSWER_PRODUCT_LANE_2026_08_06__ 답변 레인의 **경로만**
            # 남긴다. 본문은 젤의 ``workspace/answers/*.md`` 에 있고 리포트가
            # 거기서 읽는다 — 원장에 본문을 복사하면 같은 진실이 두 곳에 산다.
            # ``None`` = "이 사이클에 답변 레인 기록이 없다"이고, 그건 게이트 OFF
            # 와 레인이 예외로 죽은 것을 **구분하지 못한다**(리포트가 그렇게 적는다).
            "answer_product": apply_summary.get("answer_product"),
            "artifact_runs": apply_summary.get("artifact_runs"),
            # __SLOT_F2_SANDBOX_VERIFY_2026_06_14__ advisory sandbox telemetry
            # provenance (additive; None unless the F2 gate is ON and a real run
            # produced signals). Carries the same serialized dict the S3 verdict
            # holds so the Merkle chain records what the sandbox observed.
            "sandbox": (
                apply_summary.get("verdict", {}).get("sandbox")
                if apply_summary.get("verdict")
                else None
            ),
        }
        # __SLOT_R12_VACUOUS_2026_08_17__ additive-only when the gate is ON
        # (consensus_diagnosis is not None) -- OFF leaves apply_entry with
        # exactly the pre-slot key set, byte-identical.
        if consensus_diagnosis is not None:
            apply_entry["consensus_diagnosis"] = consensus_diagnosis_status
        # __SLOT_SI_PROPOSAL_ARCHIVE_WIRE_2026_08_24__ Additive only while the
        # archive gate is ON.  OFF therefore preserves the historical Merkle
        # entry schema exactly; ON records only archive_cycle's compact result,
        # never a second copy of proposal bodies.
        if _proposal_archive_summary is not None:
            apply_entry["proposal_archive"] = _proposal_archive_summary
        # __SLOT_W2A2__  enforcement.append signature is
        # (record, state_dir=None, *, chain_path=None) and returns the
        # freshly-computed ``entry_hash`` (str), not the full dict. The
        # caller's ``apply_entry`` is mutated in place with prev_hash,
        # ts, schema_version, entry_hash.
        written_entry_hash = append_apply_entry(
            apply_entry, chain_path=self.apply_chain_path
        )  # __SLOT_W2A2__

        # R3 §2.4: when a SESSION revert fired (partial apply rolled back),
        # append a SEPARATE decision_source="rollback" chain entry so the audit
        # trail records both the attempted apply AND its rollback (parity with
        # apply_block's _audit("reverted") pattern). Only on a real revert —
        # dry_run sessions never revert, so this stays a no-op by default.
        if apply_summary.get("reverted"):
            append_apply_entry(
                {
                    "schema_version": APPLY_CHAIN_SCHEMA,
                    "timestamp": time.time(),
                    "cycle_id": cycle_id,
                    "decision_source": "rollback",
                    "verifier_id": "si_v81",
                    "pre_apply_hash": apply_summary["post_apply_hash"],
                    "post_apply_hash": apply_summary["pre_apply_hash"],
                    "rollback_latency_ms": apply_summary["rollback_latency_ms"],
                    "status": status,
                    "reverted_from_entry_hash": apply_entry.get("entry_hash", ""),
                },
                chain_path=self.apply_chain_path,
            )

        # __SLOT_W_STAGE3_COMMAND_EXEC_2026_06_18__ When the cycle proposed any
        # shell command, append a SEPARATE decision_source="command_exec" chain
        # entry recording each command's validated argv + plan/exec verdict (no
        # stdout — masked output is not chained). Mirrors the rollback-entry
        # pattern. No commands → this block is skipped → byte-identical.
        if apply_summary.get("commands"):
            append_apply_entry(
                {
                    "schema_version": APPLY_CHAIN_SCHEMA,
                    "timestamp": time.time(),
                    "cycle_id": cycle_id,
                    "decision_source": "command_exec",
                    "verifier_id": "si_v81",
                    "commands": apply_summary["commands"],
                    "status": status,
                },
                chain_path=self.apply_chain_path,
            )

        # --- S7 (R6): escalation — consume force_replan (C4 반증) ---
        # PART1 §3.3: v7.1 computed force_replan but NEVER consumed it
        # (DEAD-OUTPUT defect). v8.1 turns it into a REAL signal recorded into
        # the cycle_log payload + surfaced on SICycleOutcome so the NEXT cycle's
        # propose stage (and the orchestrator) can branch on it. When the policy
        # gate is OFF or the streak < 3 this stays "" / False — byte-equivalent.
        force_replan = bool(cycle_policy.force_replan)
        phase_transition = "forced_replan" if force_replan else ""
        head_auto_reset_fired = False
        if cycle_policy.head_auto_reset:
            # Destructive head-state reset — ALWAYS backs up first (v7.1
            # :203-224 port). Only resets when a head-state file exists; a
            # missing file is a no-op (returns False). Never raises into the
            # cycle (typed except mirrors the logger-fault contract).
            try:
                head_auto_reset_fired = auto_reset_head_state(
                    self.state_dir / "current_state_auto.json"
                )
            except (OSError, ValueError, TypeError) as _ff_exc:
                _swallowed(_ff_exc, site="self_improvement_v8.run_one_si_cycle:2260", category="verify")
                head_auto_reset_fired = False

        # --- 6. Cycle log JSONL ---
        cycle_log_entry = {
            "schema_version": SCHEMA_VERSION,
            "timestamp": time.time(),
            "cycle_id": cycle_id,
            "status": status,
            "axes_aligned": axes_aligned,
            "axis_evidence_total": axis_evidence_total,  # A2: 0 == vacuous
            "input_mode": input_mode,
            "stub_inputs": pod_a_defaulted or pod_b_defaulted,
            "pod_a_correctness_signals": list(a_ev.correctness),
            "pod_b_correctness_signals": list(b_ev.correctness),
        }
        # __SLOT_R12_VACUOUS_2026_08_17__ additive-only when the gate is ON,
        # mirrors the R1/R6 stamp convention immediately below.
        if consensus_diagnosis is not None:
            cycle_log_entry["consensus_diagnosis"] = consensus_diagnosis_status
        # R6 S0/S7: stamp the escalation-policy snapshot + the consumed replan
        # signal into the durable cycle_log so the streak/force_replan decision
        # is auditable post-hoc without re-running the cycle. Additive — and ONLY
        # when there is actual escalation signal (streak>0 or force_replan), so a
        # neutral cycle (the default idle/no-history case) keeps the pre-R6 row
        # shape BYTE-IDENTICAL (mirrors the R1 observation-stamp convention).
        if force_replan or cycle_policy.escalate_streak > 0:
            cycle_log_entry["escalate_streak"] = cycle_policy.escalate_streak
            cycle_log_entry["force_replan"] = force_replan
            cycle_log_entry["phase_transition"] = phase_transition
        # R1: when the observation stage ran, stamp the rehydrated context
        # counts into the cycle_log entry so the 3-cycle read is reflected
        # in the durable cycle result (auditable post-hoc). Additive — only
        # present when the gate is ON, so gate-OFF stays byte-equivalent.
        if observation_bundle is not None:
            cycle_log_entry["observed_cycle_count"] = observation_bundle[
                "observed_count"
            ]
            cycle_log_entry["observed_cycle_ids"] = list(
                observation_bundle["recent_cycle_ids"]
            )
            cycle_log_entry["policy_drift_keys"] = list(
                observation_bundle["policy_drift_keys"]
            )
            # __SLOT_PREV_RUN_LEDGER_STAMP_2026_08_10__ 🔴 bundle 의 prev_run
            # 두 키는 여기 안 실려서 **메모리에서 소멸**했다 —
            # `_previous_run_id` 독스트링은 *"없다는 사실이 원장에 이름으로
            # 남는다"* 고 약속하는데, 실제로는 SICycleOutcome.observation
            # (프로세스 수명)에만 있었고 어느 원장에도 없었다(2026-08-10
            # contracts_map §7 실측: `prev_run*` 키를 가진 라이브 행 0/2280).
            # 같은 additive 관례를 따른다: 게이트 OFF(bundle None)면 이 블록
            # 자체가 안 돌아 행 모양이 byte-identical 이다.
            # ⛔ `recent_cycles`(raw 이벤트 본문)는 스탬프하지 않는다 — 원장에
            # 본문을 복제하면 같은 진실이 두 곳에 산다(answer_product 관례).
            cycle_log_entry["prev_run_context_status"] = observation_bundle[
                "prev_run_context_status"
            ]
            cycle_log_entry["prev_run_best_score"] = observation_bundle[
                "prev_run_best_score"
            ]
        atomic_append_jsonl(self.cycle_log_path, cycle_log_entry)

        # --- S6 (R6): reflect — append cycle status to the escalation ring ---
        # PART1 §2.4 + task item 3: map the SI status (+ any R3 session revert)
        # onto a STATUS_ALIASES-compliant ring status and append it. The ring's
        # OWN env gate (AGI_V8_CONTINUATION_RING_ENABLED, default-OFF) makes this
        # a no-op unless the operator turned the ring on — so a default cycle
        # never grows the ring (streak stays 0). A fired R3 rollback forces
        # status="escalated" (overrides the status map) so it feeds force_replan
        # on the NEXT cycle. Never raises into the cycle.
        ring_reverted = bool(
            apply_summary.get("reverted") if apply_summary else False
        )
        ring_status = _map_si_status_to_ring_status(
            status, reverted=ring_reverted
        )
        try:
            self.continuation_ring.append(
                cycle=cycle_policy.cycle_n,
                status=ring_status,
                decision=status,
            )
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            # Ring persistence faults must never abort the SI cycle — but they
            # must not be SILENT either. This is the exact handler whose silence
            # let the escalation ring stay unrehydrated (force_replan unreachable)
            # without a single log line. Now it is counted + named, and
            # AGI_V8_STRICT_FAIL_FAST=true re-raises it.
            _swallowed(
                exc, site="si.continuation_ring.append", category=_FF_PERSIST,
                detail=f"cycle={cycle_policy.cycle_n} status={ring_status}",
            )

        # --- S6 (R7): prompt-evolution durable compaction (C9 반증) ---
        # PART1 S2/S6: periodic (every N cycles) compaction of the cycle-event
        # stream into a RolePromptPacket, appended to a state_dir/prompts
        # sidecar (advisory) and — only under the 2-env operator AND-gate —
        # injected into the canonical allowlisted prompt file via the R3
        # SafeAutoApply ladder. Gate-OFF (AGI_V8_PROMPT_COMPACTOR_ENABLED false,
        # default) → None → byte-equivalent no-op. This is the FIRST production
        # caller of prompt_compactor_v8 (the dead phase_executors glue is absent
        # from this tree). Advisory: never aborts the cycle.
        prompt_evolution_summary = _run_prompt_evolution_advisory(
            cycle_n=cycle_policy.cycle_n,
            state_dir=self.state_dir,
            cycle_logger=cycle_logger,
            cycle_id=cycle_id,
        )

        # Lane B (SIA) + R19 (critic) both append into ``agent_failures``,
        # so initialize the list before either hook fires. Order: SIA hook
        # runs first (post-apply-chain durability), critic runs afterwards.
        critic_ticket_id: str = ""
        agent_failures: list[Mapping[str, Any]] = []

        # --- Lane B: SIA self-rewrite hook (default OFF, additive) ---------
        # Runs after the apply_chain entry is durable so the SIA chain
        # entries can reference the just-written cycle. The controller's
        # own R12 chain (sia_chain.jsonl) is independent from apply_chain.
        # When the gate is OFF, no import, no instantiation, no chain
        # write. NO mode enum — the gate is a single env check.
        sia_summary_mapping: Mapping[str, Any] | None = None
        if _sia_enabled():
            try:
                # Lazy import keeps the SI module light when SIA is OFF
                # (the default case in tests + production until a SIA
                # rollout is approved).
                from agi_v8_1.capabilities import PayloadPort, resolve_payload

                SIASelfRewriteController = resolve_payload(PayloadPort(
                    9, "agi_v8_1.si_lanes.si_autonomy_payload", "self_rewrite_controller_class"))()

                sia_controller = SIASelfRewriteController()
                sia_summary = sia_controller.maybe_run_iteration(
                    cycle_id,
                    si_status=status,
                )
                sia_summary_mapping = sia_summary.model_dump()
            except (ImportError, OSError, ValueError, TypeError) as exc:
                # Typed except — never `except Exception: pass`. Failure
                # rides into the cycle's agent_failures so the orchestrator
                # can surface it without crashing the cycle.
                _swallowed(exc, site="self_improvement_v8.run_one_si_cycle:2377", category="verify")
                agent_failures.append(
                    _agent_failure_metadata("sia_controller", "sia_iteration", exc)
                )

        # --- R19: optional critic evaluation (advisory only) ---
        if critic is not None:
            try:
                # Build a synthetic DecisionRecord from this SI cycle's status
                # so the critic can score it deterministically.
                synth_decision = DecisionRecord(
                    message_id=make_message_id("dec"),
                    created_at_unix=time.time(),
                    decision_id=f"si_decision_{cycle_id}",
                    evidence_ids=(),
                    outcome=status,
                    rationale=blocked_reason or "si_cycle",
                )
                ticket = critic.evaluate_decision(synth_decision, ())
                if ticket is not None:
                    critic_ticket_id = ticket.ticket_id
                    if cycle_logger is not None:
                        try:
                            cycle_logger.log_event(
                                event_type="evidence_emit",
                                payload={
                                    "critic_ticket_id": ticket.ticket_id,
                                    "severity": ticket.severity,
                                    "target_id": ticket.target_id,
                                },
                                cycle_id=cycle_id,
                            )
                        # META-SI round 3 MUT-3 (critic_evidence_emit ONLY):
                        # replace the bare ``except Exception: pass`` with the
                        # same typed handler shape established at round 2's
                        # cycle_start site (now at :234). Round 3 brief named
                        # ":331" / "pod_block_fill" — that ledger number is
                        # stale (predates MUT-1/MUT-2 LOC growth). The actual
                        # next-in-queue bare-except after round-2 :181 is this
                        # critic-evidence-emit site (was :427 at brief time,
                        # now at this offset). Per AP-6, MUT-3 covers this
                        # site ONLY in round 3; the remaining bare-except
                        # sites in si_policy / cycle_end (further down) stay
                        # bare until round 4+. The per-site sub-counter
                        # payload makes the swallow attributable.
                        except (TypeError, AttributeError, ValueError, OSError) as _ff_exc:
                            # Record the swallowed fault with a per-site
                            # discriminator so the same well-known stage_id
                            # (LOGGER_DROP_TOTAL_STAGE) still aggregates the
                            # total, while audits can split by ``site`` tag.
                            # Additive-only extension of the round-2 contract
                            # (AP-11): wrapper signature accepts an optional
                            # payload kwarg without breaking existing callers.
                            _swallowed(_ff_exc, site="self_improvement_v8.run_one_si_cycle:2425", category="verify")
                            increment_logger_drop_total(
                                payload={"site": "critic_evidence_emit"}
                            )
            except Exception as exc:
                _swallowed(exc, site="self_improvement_v8.run_one_si_cycle:2436", category="verify")
                agent_failures.append(
                    _agent_failure_metadata("critic", "si_critic_evaluate", exc)
                )

        # --- R2: advisory SIDispatcherV8 7-stage dispatch (C13 반증) ---
        # Direct call (no phase_executors glue — that module is absent from
        # this tree). Gate-OFF (default) → None → byte-equivalent no-op. The
        # dispatcher writes its own chain JSONL under state_dir; the SI apply
        # chain is untouched. Surfaced as an advisory summary only.
        si_dispatcher_summary = _run_si_dispatcher_advisory(
            cycle_id=cycle_id,
            state_dir=self.state_dir,
            status=status,
            input_mode=input_mode,
            pod_ids=(),
        )

        # --- R19: optional cycle_logger — si_policy + cycle_end ---
        if cycle_logger is not None:
            try:
                si_policy_payload: dict[str, Any] = {
                    "status": status,
                    "axes_aligned": axes_aligned,
                    "axis_evidence_total": axis_evidence_total,  # A2: 0 == vacuous
                    "blocked_reason": blocked_reason,
                }
                # __SLOT_R12_VACUOUS_2026_08_17__ additive-only when the gate
                # is ON, same convention as the two stamp sites above.
                if consensus_diagnosis is not None:
                    si_policy_payload["consensus_diagnosis"] = (
                        consensus_diagnosis_status
                    )
                # R1: surface how many prior cycles the observation stage
                # rehydrated — ONLY when the stage actually ran. Gate-OFF
                # (observation_bundle is None) leaves the payload byte-
                # identical to the pre-R1 si_policy event.
                if observation_bundle is not None:
                    si_policy_payload["observed_cycle_count"] = (
                        observation_bundle["observed_count"]
                    )
                # R6 S7: surface the consumed replan signal in the durable
                # si_policy event ONLY when it fired — gate-OFF / streak<3
                # leaves the payload byte-identical to the pre-R6 event.
                if force_replan:
                    si_policy_payload["force_replan"] = True
                    si_policy_payload["phase_transition"] = phase_transition
                    si_policy_payload["escalate_streak"] = (
                        cycle_policy.escalate_streak
                    )
                cycle_logger.log_event(
                    event_type="si_policy",
                    payload=si_policy_payload,
                    cycle_id=cycle_id,
                )
            except Exception as _ff_exc:
                _swallowed(_ff_exc, site="self_improvement_v8.run_one_si_cycle:2485", category="verify")
                pass
            try:
                cycle_logger.log_event(
                    event_type="cycle_end",
                    payload={
                        "status": status,
                        # __SLOT_W2A2__  predecessor's entry_hash now lives
                        # on the mutated apply_entry dict (set by
                        # enforcement.append before return).
                        "apply_chain_entry_hash": apply_entry.get("prev_hash", ""),
                        # Exact identity of THIS cycle's row.  Keep the legacy
                        # predecessor field above for compatibility, but a
                        # consumer proving this cycle landed must join here.
                        "apply_chain_current_entry_hash": written_entry_hash,
                        "critic_ticket_id": critic_ticket_id,
                        "agent_failures": list(agent_failures),
                        "wall_clock_ms": round(wall_clock_ms, 3),
                        # R2: surface dispatcher reach ONLY when it ran. Gate-OFF
                        # (summary is None) leaves the cycle_end payload byte-
                        # identical to the pre-R2 event.
                        **(
                            {"si_dispatcher_chain_entries": si_dispatcher_summary[
                                "chain_entries_written"
                            ]}
                            if si_dispatcher_summary is not None
                            else {}
                        ),
                        # R7: surface prompt-compaction reach ONLY when it ran
                        # AND the cadence fired. Gate-OFF / cadence-miss leaves
                        # the cycle_end payload byte-identical to the pre-R7 event.
                        **(
                            {
                                "prompt_rules_appended": prompt_evolution_summary[
                                    "rules_appended"
                                ]
                            }
                            if prompt_evolution_summary is not None
                            and prompt_evolution_summary.get("cadence_hit")
                            else {}
                        ),
                    },
                    cycle_id=cycle_id,
                )
            except Exception as _ff_exc:
                _swallowed(_ff_exc, site="self_improvement_v8.run_one_si_cycle:2525", category="verify")
                pass

        # --- bench backlog #2: progress ledger oracle (record-only, T9) ---
        # __SLOT_PROGRESS_ORACLE_2026_08_22__ Adapts Magentic-One's progress-
        # ledger judge (is_progress_being_made / is_in_loop) to the SI cycle
        # (see runtime/progress_oracle.py module docstring for the full
        # adaptation). Runs AT MOST once per cycle, strictly AFTER the S1
        # observation bundle — its only input — so this is gated on BOTH
        # this gate AND ``AGI_V8_CYCLE_LOGGER_ENABLED`` (no bundle ⇒ nothing
        # to summarize ⇒ no call; same AND-of-two-gates shape as
        # ``runtime/work_feeder.py``'s stall+refeed gates). RECORD-ONLY: the
        # verdict is logged and NEVER consulted below — no branch in this
        # function reads ``_po_row``'s content. Wiring it into stall/refeed
        # decisions is backlog #5, explicitly out of scope here.
        if (
            cycle_logger is not None
            and observation_bundle is not None
            and _progress_oracle_enabled()
        ):
            try:
                from agi_v8_1.runtime import progress_oracle as _po

                _po_model = _po.resolve_model()
                _po_call_fn, _po_unavailable = _build_progress_oracle_call_fn(
                    model=_po_model, state_dir=self.state_dir,
                )
                _po_row = _po.run_progress_verdict(
                    bundle=observation_bundle,
                    call_fn=_po_call_fn,
                    model=_po_model,
                    unavailable_reason=_po_unavailable,
                )
                cycle_logger.log_event(
                    event_type=_po.EVENT_TYPE,
                    payload=_po_row,
                    cycle_id=cycle_id,
                )
            except Exception as _po_exc:  # noqa: BLE001 — the oracle observes the
                # cycle, it must never be able to break it.
                _swallowed(
                    _po_exc,
                    site="self_improvement_v8.run_one_si_cycle:progress_oracle",
                    category=_FF_TELEMETRY,
                )

        # META-SI round 2 stage wire #5: cycle_end. Incremented just
        # before the SICycleOutcome construction so any return-path
        # exception (e.g. dataclass field validation) leaves the meter
        # showing the stage entered but the cycle not completed — a
        # signal worth keeping for the round-3 outcome correlation work.
        _coverage_increment_stage(SI_STAGE_IDS[4])

        # __SLOT_BUS_AUTOWIRE_2026_06_16__ external falsifier-bus step.
        # Gated default-OFF; observation-only (fills the bus from the trader,
        # registers a forward claim on a change-producing cycle, grades due
        # predictions against the bus, surfaces a digest). OFF or ANY failure
        # leaves bus_signal=None → byte-equivalent to the pre-wire cycle. It is
        # the LAST pre-return step so it can never perturb the verdict/apply.
        bus_signal: Mapping[str, Any] | None = None
        try:
            from agi_v8_1.bus.cycle_wire import bus_autowire_enabled

            if bus_autowire_enabled():
                from agi_v8_1.bus.cycle_wire import run_bus_cycle_step

                bus_signal = run_bus_cycle_step(
                    cycle_id=cycle_id,
                    now_ts=time.time(),
                    state_dir=self.state_dir,
                    change_count=int(apply_summary.get("change_count", 0) or 0),
                    applied=bool(apply_summary.get("changes_applied"))
                    and not bool(apply_summary.get("dry_run")),
                )
        except Exception as exc:  # noqa: BLE001 — bus wiring never breaks a cycle
            _swallowed(exc, site="self_improvement_v8.run_one_si_cycle:2556", category="verify")
            logger.warning(
                "bus auto-wire step failed (non-fatal): %s",
                _format_exception_for_log(exc),
            )
            bus_signal = None

        return SICycleOutcome(
            cycle_id=cycle_id,
            status=status,
            axes_aligned_count=axes_aligned,
            pod_a_evidence=a_ev.as_dict(),
            pod_b_evidence=b_ev.as_dict(),
            # __SLOT_W2A2__  predecessor's entry_hash via apply_entry's
            # prev_hash field (mutated in place by enforcement.append).
            apply_chain_entry_hash=apply_entry.get("prev_hash", ""),
            apply_chain_current_entry_hash=written_entry_hash,
            cycle_log_entries=1,
            blocked_reason=blocked_reason,
            input_mode=input_mode,
            agent_failures=tuple(agent_failures),
            sia_iteration_summary=sia_summary_mapping,
            observation=observation_bundle,
            bus_signal=bus_signal,
            si_dispatcher_summary=si_dispatcher_summary,
            apply_summary=apply_summary,
            # R6 (C4 반증): S0 policy snapshot + S7 consumed replan signal.
            cycle_policy=cycle_policy.as_dict(),
            force_replan=force_replan,
            phase_transition=phase_transition,
            head_auto_reset_fired=head_auto_reset_fired,
            # R7 (C9 반증): prompt-evolution compaction round (None when gate-OFF
            # or cadence not elapsed — byte-equivalent no-op).
            prompt_evolution_summary=prompt_evolution_summary,
        )


# ===== R18.5 SI 12-lane surface (additive over R18 seed) =====
#
# SI promotes from a single cycle (R18 seed) to a 12-lane structure that runs
# only when ``ActivationDecision.si_lanes > 0`` (i.e. ``full`` mode). All lanes
# are advisory: they only emit :class:`SIPolicyTicket` records. No provider
# calls, no shell, no apply.

class SILaneKind(StrEnum):
    """The four SI lane kinds inside the 12-lane SI layer."""

    PROPOSER = "proposer"
    ROLLBACK_GUARD = "rollback_guard"
    OUTCOME_OBSERVER = "outcome_observer"
    TICKET_WRITER = "ticket_writer"


# 4 + 4 + 2 + 2 = 12 lanes in full mode.
SI_LANE_ALLOCATION: Mapping[SILaneKind, int] = {
    SILaneKind.PROPOSER: 4,
    SILaneKind.ROLLBACK_GUARD: 4,
    SILaneKind.OUTCOME_OBSERVER: 2,
    SILaneKind.TICKET_WRITER: 2,
}

assert sum(SI_LANE_ALLOCATION.values()) == 12, "SI 12-lane allocation must sum to 12"


@dataclass(frozen=True, slots=True)
class SILaneResult:
    lane_kind: str
    lane_index_within_kind: int
    tickets_emitted: tuple[SIPolicyTicket, ...]
    notes: str


def _scale_si_allocation(total_si_lanes: int) -> Mapping[SILaneKind, int]:
    """Scale the 4+4+2+2 ratio down for partial SI budgets.

    Each kind gets at most ``ceil(total_si_lanes/12 * base)`` lanes. We round
    down deterministically and clamp to total. If ``total_si_lanes == 12``,
    returns the canonical 4+4+2+2.
    """

    if total_si_lanes >= 12:
        return dict(SI_LANE_ALLOCATION)
    if total_si_lanes <= 0:
        return {k: 0 for k in SI_LANE_ALLOCATION}
    # Proportional floor allocation, then distribute remainder by priority
    # order (proposer > rollback_guard > outcome_observer > ticket_writer).
    out: dict[SILaneKind, int] = {}
    for k, base in SI_LANE_ALLOCATION.items():
        out[k] = (base * total_si_lanes) // 12
    deficit = total_si_lanes - sum(out.values())
    priority = (
        SILaneKind.PROPOSER,
        SILaneKind.ROLLBACK_GUARD,
        SILaneKind.OUTCOME_OBSERVER,
        SILaneKind.TICKET_WRITER,
    )
    i = 0
    while deficit > 0:
        out[priority[i % len(priority)]] += 1
        deficit -= 1
        i += 1
    return out


def run_si_layer(
    *,
    decision_records: Sequence[DecisionRecord],
    outcome_log_path: Path,
    activation_decision: "ActivationDecision",
) -> Sequence[SILaneResult]:
    """Run the SI 12-lane layer.

    Returns a sequence of :class:`SILaneResult` (one per lane). The layer is
    a no-op (returns ``[]``) when ``activation_decision.si_lanes <= 0``.

    Lane semantics:
      proposer       — emit one :class:`SIPolicyTicket` per DecisionRecord
                       (proposed_changes empty when no recent outcomes).
      rollback_guard — score each proposer ticket's rollback_risk (deterministic
                       stub: 0.5 default, 0.2 when proposed_changes empty).
      outcome_observer — read tail of outcome_log JSONL; notes = summary string.
      ticket_writer  — stable-sort tickets emitted upstream by ticket_id.

    All four lane kinds are deterministic and have no side effects beyond
    optionally reading ``outcome_log_path`` via state_store.read_jsonl.
    """

    si_total = int(activation_decision.si_lanes)
    if si_total <= 0:
        return ()

    allocation = _scale_si_allocation(si_total)
    results: list[SILaneResult] = []

    # --- Pass 1: proposer lanes ---
    proposer_tickets: list[SIPolicyTicket] = []
    proposer_count = allocation[SILaneKind.PROPOSER]
    decisions = list(decision_records)
    now = time.time()
    for lane_idx in range(1, proposer_count + 1):
        emitted: list[SIPolicyTicket] = []
        for d in decisions:
            ticket = SIPolicyTicket(
                message_id=make_message_id("si_prop"),
                created_at_unix=now,
                ticket_id=f"si_prop_{lane_idx:02d}_{d.decision_id}",
                source_decision_id=d.decision_id,
                config_namespace="agi_v8_1.si",
                # R18.5: proposed_changes stays empty until R19 outcome
                # reader. Tuple of (key, value_json) pairs.
                proposed_changes=(),
                rollback_risk=0.5,
                lane_kind=SILaneKind.PROPOSER.value,
            )
            emitted.append(ticket)
            proposer_tickets.append(ticket)
        results.append(
            SILaneResult(
                lane_kind=SILaneKind.PROPOSER.value,
                lane_index_within_kind=lane_idx,
                tickets_emitted=tuple(emitted),
                notes=f"proposed {len(emitted)} tickets from {len(decisions)} decisions",
            )
        )

    # --- Pass 2: rollback_guard lanes ---
    guard_count = allocation[SILaneKind.ROLLBACK_GUARD]
    for lane_idx in range(1, guard_count + 1):
        guard_tickets: list[SIPolicyTicket] = []
        for src in proposer_tickets:
            # Deterministic stub: empty proposed_changes → lower risk (0.2);
            # otherwise default 0.5. R19+ replaces with real outcome regression.
            risk = 0.2 if not src.proposed_changes else 0.5
            guard_tickets.append(
                SIPolicyTicket(
                    message_id=make_message_id("si_guard"),
                    created_at_unix=now,
                    ticket_id=f"si_guard_{lane_idx:02d}_{src.ticket_id}",
                    source_decision_id=src.source_decision_id,
                    config_namespace=src.config_namespace,
                    proposed_changes=src.proposed_changes,
                    rollback_risk=risk,
                    lane_kind=SILaneKind.ROLLBACK_GUARD.value,
                )
            )
        results.append(
            SILaneResult(
                lane_kind=SILaneKind.ROLLBACK_GUARD.value,
                lane_index_within_kind=lane_idx,
                tickets_emitted=tuple(guard_tickets),
                notes=f"guarded {len(guard_tickets)} proposer tickets",
            )
        )

    # --- Pass 3: outcome_observer lanes ---
    observer_count = allocation[SILaneKind.OUTCOME_OBSERVER]
    try:
        outcomes = read_jsonl(outcome_log_path)
    except Exception as _ff_exc:  # pragma: no cover — read_jsonl already tolerates missing
        _swallowed(_ff_exc, site="self_improvement_v8.run_si_layer:2750", category="verify")
        outcomes = []
    tail = outcomes[-10:] if outcomes else []
    for lane_idx in range(1, observer_count + 1):
        results.append(
            SILaneResult(
                lane_kind=SILaneKind.OUTCOME_OBSERVER.value,
                lane_index_within_kind=lane_idx,
                tickets_emitted=(),
                notes=f"observed {len(tail)} recent outcome entries (of {len(outcomes)} total)",
            )
        )

    # --- Pass 4: ticket_writer lanes ---
    writer_count = allocation[SILaneKind.TICKET_WRITER]
    all_upstream = list(proposer_tickets)
    # Also include guard tickets from results above.
    for r in results:
        if r.lane_kind == SILaneKind.ROLLBACK_GUARD.value:
            all_upstream.extend(r.tickets_emitted)
    all_upstream.sort(key=lambda t: t.ticket_id)
    sorted_tuple = tuple(all_upstream)
    for lane_idx in range(1, writer_count + 1):
        results.append(
            SILaneResult(
                lane_kind=SILaneKind.TICKET_WRITER.value,
                lane_index_within_kind=lane_idx,
                tickets_emitted=sorted_tuple,
                notes=f"stable-sorted {len(sorted_tuple)} upstream tickets by ticket_id",
            )
        )

    return tuple(results)


# ===== R20 W1 SI port — enriched lane logic over R18.5 stubs ============
#
# v7.1 self_improvement_agent.py (1,617 LoC) → V8 SI 12-lane redistribution.
# Each lane kind now has a dedicated free-function in agi_v8_1/si_lanes/ that
# encapsulates the deterministic logic. ``run_si_layer_v2`` runs the lanes
# using those functions instead of the R18.5 inline stubs.
#
# Backward compat: ``run_si_layer`` (the R18.5 entry point) still works.
# ``run_si_layer_v2`` is the R20 W1 enriched variant. R20 callers can opt
# in by switching the call.


def run_si_layer_v2(
    *,
    decision_records: Sequence[DecisionRecord],
    outcome_log_path: Path,
    activation_decision: "ActivationDecision",
    revert_history: Sequence[Mapping[str, Any]] = (),
) -> Sequence[SILaneResult]:
    """R20 W1 enriched SI 12-lane layer.

    Differences vs R18.5 ``run_si_layer``:
      - proposer lanes call ``si_lanes.propose_tickets`` (uses recent
        outcomes to derive proposed_changes; R18.5 always emitted empty)
      - rollback_guard lanes call ``si_lanes.score_rollback_risk``
        (deterministic risk formula with prior-revert history)
      - outcome_observer lanes call ``si_lanes.observe_outcomes``
        (read-only; same as R18.5 but explicit)
      - ticket_writer lanes call ``si_lanes.stable_sort_tickets``

    All four lane kinds remain advisory only — no provider calls, no
    shell, no apply. Tests/v8/test_r20_w1_si_*.py verify each behavior.
    """

    from agi_v8_1.si_lanes import (
        observe_outcomes,
        propose_tickets,
        score_rollback_risk,
        stable_sort_tickets,
    )

    si_total = int(activation_decision.si_lanes)
    if si_total <= 0:
        return ()

    allocation = _scale_si_allocation(si_total)
    results: list[SILaneResult] = []
    now = time.time()

    # --- Pass 0: outcome_observer reads recent outcomes once (shared). ---
    recent_outcomes = observe_outcomes(outcome_log_path)

    # --- Pass 1: proposer lanes ---
    proposer_count = allocation[SILaneKind.PROPOSER]
    all_proposer_tickets: list[SIPolicyTicket] = []
    for lane_idx in range(1, proposer_count + 1):
        emitted = propose_tickets(
            decisions=decision_records,
            recent_outcomes=recent_outcomes,
            config_namespace="agi_v8_1.si",
            lane_index=lane_idx,
            now_unix=now,
        )
        all_proposer_tickets.extend(emitted)
        results.append(
            SILaneResult(
                lane_kind=SILaneKind.PROPOSER.value,
                lane_index_within_kind=lane_idx,
                tickets_emitted=tuple(emitted),
                notes=(
                    f"proposed {len(emitted)} tickets from "
                    f"{len(decision_records)} decisions, "
                    f"recent_outcomes={len(recent_outcomes)}"
                ),
            )
        )

    # --- Pass 2: rollback_guard lanes ---
    guard_count = allocation[SILaneKind.ROLLBACK_GUARD]
    for lane_idx in range(1, guard_count + 1):
        scored = score_rollback_risk(
            proposer_tickets=tuple(all_proposer_tickets),
            history=tuple(revert_history),
            lane_index=lane_idx,
            now_unix=now,
        )
        results.append(
            SILaneResult(
                lane_kind=SILaneKind.ROLLBACK_GUARD.value,
                lane_index_within_kind=lane_idx,
                tickets_emitted=tuple(scored),
                notes=(
                    f"scored {len(scored)} proposer tickets against "
                    f"{len(revert_history)} prior revert events"
                ),
            )
        )

    # --- Pass 3: outcome_observer lanes (advisory note only) ---
    observer_count = allocation[SILaneKind.OUTCOME_OBSERVER]
    for lane_idx in range(1, observer_count + 1):
        results.append(
            SILaneResult(
                lane_kind=SILaneKind.OUTCOME_OBSERVER.value,
                lane_index_within_kind=lane_idx,
                tickets_emitted=(),
                notes=(
                    f"observed {len(recent_outcomes)} recent outcome entries"
                ),
            )
        )

    # --- Pass 4: ticket_writer lanes ---
    writer_count = allocation[SILaneKind.TICKET_WRITER]
    all_tickets: list[SIPolicyTicket] = list(all_proposer_tickets)
    for r in results:
        if r.lane_kind == SILaneKind.ROLLBACK_GUARD.value:
            all_tickets.extend(r.tickets_emitted)
    sorted_tickets = stable_sort_tickets(all_tickets)
    for lane_idx in range(1, writer_count + 1):
        results.append(
            SILaneResult(
                lane_kind=SILaneKind.TICKET_WRITER.value,
                lane_index_within_kind=lane_idx,
                tickets_emitted=sorted_tickets,
                notes=(
                    f"stable-sorted {len(sorted_tickets)} upstream tickets"
                ),
            )
        )

    return tuple(results)


__all__ = [
    "SelfImprovementV8",
    "SICycleOutcome",
    "SCHEMA_VERSION",
    # R18.5 SI 12-lane surface
    "SILaneKind",
    "SI_LANE_ALLOCATION",
    "SILaneResult",
    "run_si_layer",
    # R20 W1 enriched variant
    "run_si_layer_v2",
    # __SLOT_VERIFY_FEEDBACK_ARM_2026_08_23__ shared verify-history fold,
    # reused by bridge.si_evidence to inject vote-input feedback.
    "recent_verify_history",
]
