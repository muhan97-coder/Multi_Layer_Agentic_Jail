"""Shared executor observability log helper for agi_v8_1.

# __SLOT_EXECUTOR_LOG_2026_06_14__ — ONE shared sink so all four executor
surfaces (swarm ``executors`` / ``executor_v20`` / ``safe_auto_apply`` /
``agents.executor``) write a single, frozen-schema JSONL trail with airtight
secret masking. There is exactly ONE record schema and ZERO per-executor
branching: the executor name is a free-string discriminator, never a switch.

Safety gate (load-bearing):
  - Every persisted field is masked BEFORE serialization. There is NO env knob
    to disable masking — masking ALWAYS runs.
  - ``raw_dump`` and ``error`` pass through
    ``policy.fail_fast.format_text_for_critical_record`` in full: safe
    stringify, bounded
    whole-value withhold, composed canonical masking, bare-base64 scrub, and
    unclosed-block guard. Only then is the historical head budget applied.
  - Inputs too large for bounded full-value masking are withheld whole behind
    a named sentinel. They are NEVER pre-truncated into a fragment that could
    separate a block-secret opener from its closing marker.

Import discipline (load-bearing):
  - ``policy.secret_masker`` + ``state.store`` are pure stdlib-regex / IO and
    are imported at module top.
  - ``providers.base._mask_secret`` is imported LAZILY inside the masking
    function (NOT at module top). A future ``import executor_log`` from
    ``executor_v20`` must NOT transitively pull ``providers`` at v20's
    module-import time — that would break the R-Demo-Env-Fix AST invariant
    (executor_v20 stays free of a top-level ``providers`` import).

Logging is non-fatal: ``_emit`` is fully wrapped — a logging failure NEVER
crashes a caller.
"""

from __future__ import annotations

# __SLOT_EXECUTOR_LOG_2026_06_14__
import dataclasses
import logging
import os
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

# Module-top imports are pure stdlib-regex / IO — safe to import eagerly.
# (mask_secrets_strict lazy-imports providers.base internally, so importing
# this module never pulls providers at import time — R-Demo-Env-Fix AST inv.)
from agi_v8_1.policy.secret_masker import (
    mask_obj_strict,
    mask_secrets_strict,
    stabilize_object_repr_addresses,
    _row_sort_key as _shared_row_sort_key,
    _dict_insert_no_loss as _shared_dict_insert,
)
from agi_v8_1.state.store import atomic_append_jsonl
from agi_v8_1.state.path_guard import resolve_sink

# __SLOT_FAIL_FAST_2026_07_25__ Swallowed failures route through one choke
# point: counted + named always, re-raised under AGI_V8_STRICT_FAIL_FAST.
from agi_v8_1.policy.fail_fast import (
    SINK_MASK_INPUT_MAX_CHARS,
    format_exception_for_sink,
    format_exception_for_log,
    format_exception_for_critical_record,
    format_text_for_critical_record,
    record_critical_failure,
    safe_exception_type_name,
    swallowed as _swallowed,
)

logger = logging.getLogger(__name__)

# __SLOT_EXECUTOR_LOG_2026_06_14__ schema id — mirrors the cycle_logger /
# apply_chain_full SCHEMA_VERSION pattern (single source of truth).
SCHEMA_VERSION = "agi_v8_executor_log_v1"

# __SLOT_EXECUTOR_LOG_2026_06_14__ env-knob names (single source of truth).
_ENV_ENABLED = "AGI_V8_EXECUTOR_LOG_ENABLED"
_ENV_VERBOSE = "AGI_V8_EXECUTOR_LOG_VERBOSE"
_ENV_MAX_CHARS = "AGI_V8_EXECUTOR_LOG_MAX_CHARS"
_ENV_LOG_PATH = "AGI_V8_EXECUTOR_LOG_PATH"

_DEFAULT_MAX_CHARS = 2000

# __SLOT_EXECUTOR_LOG_MASK_BEFORE_TRUNCATE_2026_08_11__ The canonical PEM
# masker needs the complete opener-to-footer string. The central sink adapter's
# INPUT cap bounds regex work; crossing it withholds the whole field. The local
# ``MAX_CHARS`` value is a safe-source head budget followed by a bounded
# ``...(+N more chars)`` metadata suffix, not a hard total persisted length.
# Compatibility alias for callers/tests that imported the old local bound.
# The bound and all sentinel wording now belong to policy.fail_fast's single
# sink-formatting contract; executor_log must not maintain a second masker.
_MASK_INPUT_MAX_CHARS = SINK_MASK_INPUT_MAX_CHARS


# --- record schema ---------------------------------------------------------
# __SLOT_EXECUTOR_LOG_2026_06_14__ ONE frozen dataclass, NO per-executor
# branch. ``executor`` and ``phase``/``status`` are free strings — never
# switched on.
@dataclass(frozen=True, slots=True)
class ExecutorLogRecord:
    """One immutable executor-observability record (start | success | failure)."""

    ts: float
    executor: str
    invocation_id: str
    phase: str  # start / success / failure — free string, NEVER branched on
    status: str  # start / ok / error — free string, NEVER branched on
    schema_version: str = SCHEMA_VERSION
    input_summary: dict = dataclasses.field(default_factory=dict)
    output_summary: dict = dataclasses.field(default_factory=dict)
    cost_usd: float | None = None
    wall_ms: int | None = None
    error: str | None = None
    raw_dump: str | None = None
    tags: dict = dataclasses.field(default_factory=dict)


# --- env knobs -------------------------------------------------------------
# __SLOT_EXECUTOR_LOG_2026_06_14__
def _env_flag(name: str, default: bool) -> bool:
    """Parse a boolean env knob. Disabled iff value lower-cases to 0/false/no."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no"}


def _logging_enabled() -> bool:
    # Default TRUE — enabled unless explicitly turned off.
    return _env_flag(_ENV_ENABLED, default=True)


def _verbose() -> bool:
    # Default FALSE — raw_dump on the success path only when verbose.
    return _env_flag(_ENV_VERBOSE, default=False)


def _max_chars() -> int:
    raw = os.environ.get(_ENV_MAX_CHARS)
    if raw is None:
        return _DEFAULT_MAX_CHARS
    try:
        val = int(raw.strip())
    except (TypeError, ValueError) as _ff_exc:
        _swallowed(_ff_exc, site="enforcement.executor_log._max_chars:113", category="telemetry")
        return _DEFAULT_MAX_CHARS
    return val if val > 0 else _DEFAULT_MAX_CHARS


# __SLOT_PYTEST_LIVE_GUARD_2026_08_08__ P0-a (전수 감사 2026-08-08).
# 맨 ``python3 -m pytest`` 가 _resolve_log_path 의 3순위 폴백(소스트리 state/)으로
# 떨어져 라이브 원장에 테스트 행을 append 했다(2026-06-15~08-08, 613행 실측 —
# 전부 safe_auto_apply 경유, 발생원 test_r7_prompt_evolution 류 게이트-온 테스트).
# 계약: pytest 문맥에서 라이브 폴백은 닫힌다. 명시적 싱크(비어있지 않은
# AGI_V8_EXECUTOR_LOG_PATH / AGI_V8_STATE_DIR / AGI_STATE_DIR, 또는 호출자의
# ``log_path`` 인자 — _emit 쪽에서 판정)가 있을 때만 쓴다. pytest 밖에서는 이
# 술어가 항상 False 라 종전과 byte-identical.
# ⛔ 빈 문자열 핀 = 미설정(레포 전역 관례). ⛔ 새 env 게이트 없음 — pytest 탐지는
# PYTEST_CURRENT_TEST(자식 프로세스로 상속됨) + sys.modules(수집 단계 커버) 둘 다.
def _pytest_unpinned_live_fallback(
    env: Mapping[str, str] | None = None,
    modules: Mapping[str, Any] | None = None,
) -> bool:
    """True → pytest 문맥인데 명시적 싱크 핀이 하나도 없다 (라이브 폴백 차단)."""
    e: Mapping[str, str] = os.environ if env is None else env
    m: Mapping[str, Any] = sys.modules if modules is None else modules
    in_pytest = ("PYTEST_CURRENT_TEST" in e) or ("pytest" in m)
    if not in_pytest:
        return False
    return not (
        e.get(_ENV_LOG_PATH)
        or e.get("AGI_V8_STATE_DIR")
        or e.get("AGI_STATE_DIR")
    )


# --- path resolution -------------------------------------------------------
# __SLOT_EXECUTOR_LOG_2026_06_14__ mirrors apply_chain_full._resolve_chain_path.
def _resolve_log_path() -> Path:
    """Resolve the executor-log JSONL path.

    Precedence:
      1. ``AGI_V8_EXECUTOR_LOG_PATH`` (explicit override) → that file.
      2. ``${AGI_V8_STATE_DIR or AGI_STATE_DIR}/runtime_logs/executor_log.jsonl``.
      3. ``<this file>.parent.parent/state/runtime_logs/executor_log.jsonl``.

    No separate mkdir is needed — ``atomic_append_jsonl`` mkdirs the parent.
    """
    override = os.environ.get(_ENV_LOG_PATH)
    if override:
        # __SLOT_LEDGER_SINK_2026_08_08__ 정규화(+AGI_V8_LEDGER_SINK_STRICT 시 containment).
        # ⛔ truthiness 는 위 ``if override`` 그대로 — _pytest_unpinned_live_fallback
        # 과 갈라지면 P0-a 라이브 오염이 재발한다.
        return resolve_sink(override, what="executor log")
    env_dir = os.environ.get("AGI_V8_STATE_DIR") or os.environ.get("AGI_STATE_DIR")
    if env_dir:
        state_dir = Path(env_dir)
    else:
        # agi_v8_1/enforcement/executor_log.py -> agi_v8_1/state
        state_dir = Path(__file__).resolve().parent.parent / "state"
    return state_dir / "runtime_logs" / "executor_log.jsonl"


# --- masking + truncation (THE SAFETY GATE) --------------------------------
# __SLOT_EXECUTOR_LOG_2026_06_14__
def _truncate(text: Any, limit: int) -> str:
    """Head-keeping truncation for text that is already safe to persist.

    Sandbox_runner._tail keeps the TAIL (``str(text)[-limit:]``); here we keep
    the HEAD (``str(text)[:limit]``) because a raw_dump's most diagnostic bytes
    (the start of the response / stack frame) are at the front. The flip is
    intentional — same idiom, opposite slice.
    """
    s = str(text)
    if len(s) <= limit:
        return s
    n = len(s) - limit
    return s[:limit] + f"...(+{n} more chars)"


def _mask_obj_guarded(obj: Any) -> Any:
    """``mask_obj_strict`` 의 fail-closed 래퍼 — ``_emit`` 이 직접 부르는 세 자리
    (input_summary/output_summary/tags) 전용.

    # __SLOT_OBJ_MASK_GUARD_2026_08_10__ 적대검증 소견: 문자열 깔때기
    # (``_compose_secret_mask``)에는 가드가 섰는데 이 직호출 세 곳은 무가드라,
    # 상태 의존 ``__hash__`` 같은 병리적 입력에서 마스커가 던지면 ``_emit`` 바깥
    # try 가 행 전체를 삼켜 cost_usd 가 증발했다(재현: row_exists=False, 9.99
    # 증발). 내용은 named sentinel 로 유보하고 행은 산다 — 같은 교리:
    # never half-masked, never raw. ``_swallowed`` 계수(strict 재발화).
    """
    try:
        return mask_obj_strict(obj)
    except BaseException as exc:  # noqa: BLE001 — hostile object protocol boundary
        # This is a transaction-boundary recorder: strict mode may not turn a
        # secondary object-mask failure into loss of the primary money/audit
        # row.  Count a type-only synthetic event, then locally absorb the
        # strict rethrow.  Neither ``str(exc)`` nor ``repr(exc)`` is invoked.
        record_critical_failure(
            exc,
            site="enforcement.executor_log._mask_obj_guarded",
            category="telemetry",
        )
        return (
            "<content withheld: object mask failed "
            f"({safe_exception_type_name(exc)}) — fail-closed, "
            "__SLOT_OBJ_MASK_GUARD_2026_08_10__>"
        )


def _prepare_structured_sink(obj: Any) -> Any:
    """Sanitize every minted/native text leaf before structured masking.

    The composed object masker is key-aware, but its canonical string pass can
    shred a bare-base64 body before the exception-sink scrub sees it.  Pre-
    jsonify only the three untrusted structured fields with the full sink
    adapter, then retain ``mask_obj_strict`` for sensitive-key context.
    """
    return _mask_obj_guarded(_jsonify(obj, _sanitize_native_strings=True))


def _compose_secret_mask_result(text: Any) -> tuple[str, bool]:
    """Return ``(masked_text, failed)`` without ever returning raw fallback.

    The status bit lets the durable-text path preserve the complete named
    failure sentinel even when the configured display limit is very small.
    ``_compose_secret_mask`` below keeps the established string-only API for
    the JSON stringification call sites.
    """
    try:
        masked = mask_secrets_strict(text)
        if type(masked) is not str:
            raise TypeError("canonical secret masker returned a non-string")
        return masked, False
    except Exception as exc:  # noqa: BLE001 — masking failure must not eat the row
        record_critical_failure(
            exc,
            site="enforcement.executor_log._compose_secret_mask",
            category="telemetry",
        )
        return (
            "<content withheld: composed mask failed "
            f"({safe_exception_type_name(exc)}) — fail-closed, "
            "__SLOT_OBJ_LEAF_STRINGIFY_MASK_2026_08_10__>",
            True,
        )


def _compose_secret_mask(text: Any) -> str:
    """Strict composed string mask — delegates to the SINGLE canonical impl
    ``policy.secret_masker.mask_secrets_strict`` (mask_text → _mask_secret).

    Used for structured ``_jsonify`` stringification (an object's ``__str__``
    can echo a short key). ``raw_dump`` and ``error`` deliberately use the
    broader central sink adapter below because those free-text fields also
    need bare-base64 and unclosed-block guards.

    # __SLOT_OBJ_LEAF_STRINGIFY_MASK_2026_08_10__ fail-closed guard: this is
    # the masking layer every persisted string funnels through, and it runs
    # INSIDE ``_emit``'s try — if it raised, the whole row (cost_usd included)
    # evaporated (돈행 증발, 2026-08-10 재현). ``mask_secrets_strict`` already
    # fail-closes its own lazy import; this guard covers everything else.
    # Content is WITHHELD behind a named sentinel — never half-masked, never
    # raw (same doctrine as ``_strict_masker_unavailable``) — and the failure
    # is counted via ``_swallowed`` (re-raised under strict fail-fast).
    """
    return _compose_secret_mask_result(text)[0]


def _mask_complete_then_truncate(text: Any, limit: int) -> str:
    """Apply the repository sink SSOT to the complete value, then display-cut.

    The critical-record adapter owns safe stringification, the 32 KiB
    whole-value withhold guard, canonical masking, the bare-base64 scrub, and
    the unclosed secret-block guard. Only after that complete pass may
    executor_log apply its historical ``MAX_CHARS`` head budget and bounded
    metadata suffix. Strict formatter failures become a raw-free sentinel so
    the audit/cost row is not erased.
    """
    masked = format_text_for_critical_record(
        text,
        max_chars=SINK_MASK_INPUT_MAX_CHARS,
        one_line=False,
        keep="head",
    )
    return _truncate(masked, limit)


def _mask_raw_dump(text: Any, limit: int) -> str:
    """Compatibility-named raw helper: complete mask, then output truncation."""
    return _mask_complete_then_truncate(text, limit)


def _format_failure_error(exc: BaseException) -> str:
    """Preserve ledger newlines while using the central fail-closed adapter.

    🔴 2026-08-11 (Codex 감사 이어받음, 타워가 완성) — 이 자리는 실패 원장
    행(ExecutorLogSpan._finish_failure)을 쓰는 그 exc 를 포맷한다 =
    critical-record 경로다. ``format_exception_for_sink`` 는 stringify
    실패를 strict 에서 재던지는데, 그러면 이 함수 자체가 raise 하고 —
    ① `_emit(...)` 호출에 도달 못해 실패 행이 원장에 아예 안 남고
    ② `executor_log_span` 의 except 블록에서 진짜 원인(`exc`) 대신
    합성 포맷팅 실패가 그 자리를 대신 전파한다(정체성 치환).
    ``format_exception_for_critical_record`` 는 절대 안 던지므로 행은
    항상 쓰이고, 원래 `exc` 는 호출자의 `raise` 가 그대로 재던진다.
    """
    return format_exception_for_critical_record(
        exc,
        max_chars=SINK_MASK_INPUT_MAX_CHARS,
        one_line=False,
    )


# __SLOT_B4_MONEY_FIELD_TYPE_SAFETY_2026_08_10__ 🔴 판2 잔여 적대검증 major #1
# (2026-08-10, 라이브 재현): ``cost_usd``/``wall_ms`` 는 스키마상 숫자|``None``
# 전용 필드다(``ExecutorLogRecord`` 타입 힌트). 병리적 호출자(순환 list/dict)가
# 이 자리에 비-숫자를 실으면 종전엔 ``_jsonify`` 의 순환/과깊이 sentinel
# (``['<MASKED:cycle>']`` / ``{'self': '<MASKED:cycle>'}``)이 그대로 착지했다
# — 행은 살았지만 **숫자 필드가 list/dict 로 오염**됐다. 그 행을 읽는 실소비자
# ``observability.briefing_builder.build_briefing`` 은 ``cost_usd is not None``
# 이면 ``float(...)`` 를, ``wall_ms`` 는 ``int(r.get("wall_ms") or 0)`` 를
# 부른다 — list/dict 에서 둘 다 ``TypeError`` 로 죽는다. append-only 원장이라
# 이 오염 행은 지울 수 없고, 브리핑은 그 행을 만날 때마다 **영구히** 크래시
# 한다(검증자 재현: HEAD 모양은 rows=0 이라 브리핑은 무사했는데, B4 는 rows=1
# 이지만 브리핑을 죽였다 — "행을 살렸다"는 목표가 소비자 계약을 어겼다).
#
# 수리: 숫자 필드는 ``_jsonify`` 에 아예 안 보낸다. 숫자가 아니면 이미 원장
# 어휘에 있는 "미측정" 표현(``None`` — ``briefing_builder`` 가
# ``rows_unmeasured`` 로 안전하게 센다)으로 접는다. 병리적 구조를 뜯어보려는
# 시도(순환 방어 재사용 등)는 하지 않는다 — 숫자 아니면 즉시 None, 그걸로
# 끝이다. 정상 경로(``float(x)``/``int(...)`` 로 이미 캐스팅된 값)는
# byte-identical.
# __SLOT_B4_NUMERIC_COERCION_IS_NOT_LOSSY_2026_08_10__ 🔴 B4 판2 잔여 적대검증
# major (2026-08-10, 라이브 재현) — 위 게이트가 첫 버전에서는 ``int``/``float``
# 만 받고 그 밖은 전부 None 이었다. 그런데 HEAD 부터 이 필드는 str(``'0.05'``)/
# ``Decimal('0.05')`` 도 정상적으로 받아 원장에 실었다(``float(str)``/
# ``float(Decimal)`` 은 모두 유효한 캐스팅) — 순환/과깊이(list/dict) 방어가
# 목적이었는데, 방어망이 str/Decimal 처럼 **원래 쓰던 값**까지 걸러 조용히
# None(=미측정)으로 접었다. ``_swallowed`` 신호도 없어 브리핑의 executor_cost_usd
# 합계에서 실제 지출 달러가 소리 없이 빠진다 — 이 라운드가 막으려던 것과 같은
# 부류의 결함(측정치 증발)을 다른 축에서 새로 냈다.
#
# 수리: list/dict/set 등 **컨테이너**만 걸러낸다(``__float__`` 이 없다 ⇒
# ``float(value)`` 를 아예 안 부르므로 RecursionError 재발 없음). 그 밖의
# 값은 ``float(value)`` 를 시도하고, 실패하면(진짜 숫자가 아님) named
# ``_swallowed`` 로 세고 None. NaN/±inf 는 "숫자지만 무의미"라 None(기존
# ``validate_event`` 의 severity 방어와 같은 어휘).
#
# __SLOT_B4_R4_NUMERIC_SWALLOW_DISCIPLINE_2026_08_10__ 🔴 타워 R4 major — 이
# 함수가 ``None``(=미측정)으로 접는 자리 중 **두 부류**가 이 모듈 전체가 세우는
# 규율("실패하면 named ``_swallowed`` 로 센다")을 어기고 있었다:
#   (1) 컨테이너 분기(list/dict/set/frozenset/tuple)가 무카운트로 조용히
#       None을 반환했다 — 돈 값이 이 함수를 통과하는 유일한 관문인데, 여기서
#       나는 드롭은 어떤 표면에도 신호가 없었다.
#   (2) NaN/±inf **성공적으로 파싱된 뒤** 거부되는 분기(문자열 파싱/``__float__``
#       캐스트 둘 다)가 "파싱 실패"의 ``except`` 블록만 세고, "파싱은 됐지만
#       무의미"라는 별도 사유는 안 셌다.
#   (3) 그리고 진짜 회귀: ``int``/``float`` 를 곧장 반환하는 최상단 분기는
#       NaN/±inf 필터를 아예 안 거쳤다 — ``_numeric_or_none(float('nan'))``
#       이 ``None`` 이 아니라 ``nan`` 그대로 새 나갔다(재현: 문자열로 온
#       ``"nan"`` 은 걸렀는데 리터럴 ``float('nan')`` 은 안 걸렀다 — 같은
#       계약의 갈라진 두 경로). 이 리크는 이 함수의 유일한 소비자
#       (``_emit`` 의 ``entry["cost_usd"]``/``entry["wall_ms"]``)를 거쳐
#       ``json.dumps`` 가 기본적으로 ``NaN``/``Infinity`` 를 유효하지 않은
#       JSON 리터럴로 쓰는 문제로 그대로 이어진다.
# 수리: 정상 유한 int/float/str-수/``__float__`` 값은 전과 byte-identical로
# 보존한다(회귀 없음 — 아래 테스트가 그 보존을 잡는다). None 으로 접는 모든
# 분기는 이제 예외 없이 ``_swallowed`` 로 세어진다. ⛔ ``value or 0`` 류로
# None 을 0 에 접지 않는다 — "미측정"(None)과 "측정된 0"은 다른 사실이다.
def _numeric_or_none(value: Any) -> float | int | None:
    """``cost_usd``/``wall_ms`` 전용: 숫자로 안전 변환, 아니면 None(=미측정).

    ``None`` 으로 접는 모든 **거부/실패** 분기는 ``_swallowed`` 로 세어진다 —
    돈 값의 드롭이 무신호로 증발하지 않는다
    (__SLOT_B4_R4_NUMERIC_SWALLOW_DISCIPLINE_2026_08_10__). 단, ``None``
    **입력** 자체는 거부가 아니다 — 이미 이 함수 계약의 "미측정" 어휘 그
    자체(span 시작 등 비과금 이벤트의 정상 입력)라 무카운트로 그대로 통과한다
    (이걸 세면 정상 트래픽의 절대다수가 strict 모드에서 계속 재발화한다 —
    R4 회귀 재현·수리: 처음 버전은 이 분기를 빠뜨려 ``record_llm_call`` 의
    ``None`` cost 정상 경로가 strict 에서 매번 raise 했다).

    🔴 R5 타워 정정 — 이 함수 몸체는 ``isinstance``/``getattr(type(value),
    ...)`` 를 여러 번 부르는데, 그 둘 다 ``value.__class__`` 를 내부에서
    읽는다. ``__class__`` 가 예외를 던지는 property 인 병리적 객체(포이즌드
    클래스)가 오면 첫 ``isinstance`` 호출에서 바로 이 함수 **밖으로** 예외가
    새 나가 돈 행 전체가 증발했다(``_emit`` 의 일반 sink 로만 잡혀 이름 없는
    카운트였다) — 위 독스트링의 "숫자 아니면 즉시 None, 그걸로 끝이다" 계약을
    정면 위반. 관문 하나로 막는다: 몸체 전체를 감싸 **어떤** 예외도 이름 붙은
    swallow 로 접고 None 을 반환한다(개별 분기의 세부 사유는 유지 — 안쪽
    ``_swallowed`` 호출들이 여전히 더 구체적인 site 로 먼저 잡는다).
    """
    try:
        return _numeric_or_none_inner(value)
    except Exception as exc:  # noqa: BLE001 — __class__ 자체가 poison 된 값
        _swallowed(exc, site="enforcement.executor_log._numeric_or_none:poisoned_class",
                   category="telemetry")
        return None


def _numeric_or_none_inner(value: Any) -> float | int | None:
    if value is None:
        return None
    if isinstance(value, bool):  # bool은 int 서브클래스라 별도 배제
        _swallowed(TypeError(f"bool is not a money value: {value!r}"),
                   site="enforcement.executor_log._numeric_or_none:bool",
                   category="telemetry")
        return None
    if isinstance(value, (int, float)):
        # int는 NaN/Inf 상태가 구조적으로 없다 — float일 때만 검사(정상 int는
        # 이 분기를 안 타 byte-identical, __float__() 재캐스트 없이 원값 그대로).
        if isinstance(value, float) and (
            value != value or value in (float("inf"), float("-inf"))
        ):
            _swallowed(ValueError(f"non-finite float money value: {value!r}"),
                       site="enforcement.executor_log._numeric_or_none:nonfinite_float",
                       category="telemetry")
            return None
        return value
    if isinstance(value, str):
        try:
            f = float(value)
        except (TypeError, ValueError) as exc:
            _swallowed(exc, site="enforcement.executor_log._numeric_or_none:str",
                       category="telemetry")
            return None
        if f != f or f in (float("inf"), float("-inf")):
            _swallowed(ValueError(f"non-finite numeric string: {value!r}"),
                       site="enforcement.executor_log._numeric_or_none:str_nonfinite",
                       category="telemetry")
            return None
        return f
    # 컨테이너(list/dict/set/tuple/순환 구조 포함)는 ``__float__`` 이 없다 —
    # 여기서 걸러야 ``float(value)`` 를 아예 안 불러 RecursionError 재발이
    # 구조적으로 불가능하다.
    if isinstance(value, (list, dict, set, frozenset, tuple)):
        _swallowed(TypeError(f"container is not a money value: {type(value).__name__}"),
                   site="enforcement.executor_log._numeric_or_none:container",
                   category="telemetry")
        return None
    to_float = getattr(type(value), "__float__", None)
    if not callable(to_float):
        _swallowed(TypeError(f"no numeric coercion for {type(value).__name__}"),
                   site="enforcement.executor_log._numeric_or_none:uncoercible",
                   category="telemetry")
        return None
    try:
        f = float(value)
    except Exception as exc:  # noqa: BLE001 — 병리적 __float__ 는 실재한다
        _swallowed(exc, site="enforcement.executor_log._numeric_or_none:cast",
                   category="telemetry")
        return None
    if f != f or f in (float("inf"), float("-inf")):
        _swallowed(ValueError(f"non-finite cast money value: {value!r}"),
                   site="enforcement.executor_log._numeric_or_none:cast_nonfinite",
                   category="telemetry")
        return None
    return f


# --- the single private sink -----------------------------------------------
# __SLOT_EXECUTOR_LOG_2026_06_14__
def _emit(record: ExecutorLogRecord, log_path: Path | None,
          tee_episode: bool = False) -> None:
    """The ONE private sink both public funcs funnel into.

    Masks every field BEFORE serialization, then appends one JSONL line.
    Fully non-fatal: any failure is logged at WARNING and swallowed — logging
    must NEVER crash a caller.
    """
    try:
        if not _logging_enabled():
            return  # disabled => NO file created.

        # __SLOT_PYTEST_LIVE_GUARD_2026_08_08__ P0-a: ``log_path`` 인자는 명시적
        # 싱크라 존중한다(test_ledger_join 류 인자-전용 호출 보존). 인자가 없고
        # pytest 문맥 + env 핀 전무일 때만 — 즉 라이브 폴백으로 떨어질 바로 그
        # 경우에만 — 조용히 끈다. _logging_enabled 의 조용한 return 과 같은 부류의
        # 정책 게이트다(실패 삼킴 아님).
        if log_path is None and _pytest_unpinned_live_fallback():
            return

        path = Path(log_path) if log_path is not None else _resolve_log_path()
        limit = _max_chars()

        # --- mask everything BEFORE serialization (the safety gate) --------
        masked_raw: str | None = None
        if record.raw_dump is not None:
            masked_raw = _mask_complete_then_truncate(record.raw_dump, limit)

        masked_error: str | None = None
        if record.error is not None:
            # error can also echo a short key (e.g. an API error repeating it),
            # so it gets the SAME composed mask as raw_dump (mask_text alone
            # misses short sk-{6,}). The shared helper masks the COMPLETE value
            # before the persisted output truncation and withholds oversize
            # input wholesale.
            masked_error = _mask_complete_then_truncate(record.error, limit)

        # __SLOT_B4_MONEY_FIELD_TYPE_SAFETY_2026_08_10__ 숫자 아니면 None —
        # _jsonify 의 순환/과깊이 sentinel(list/dict)이 숫자 필드에 실려
        # 실소비자(briefing_builder)를 크래시시키지 못하게 여기서 막는다.
        # __SLOT_B4_MONEY_ROW_TEE_PARITY_2026_08_10__ 🔴 재검증 major — 한 번만
        # 계산해 아래 entry 와 tee_episode_cost 양쪽에 **같은 값**을 쓴다(변수를
        # 안 나누고 ``record.cost_usd`` 원본을 tee 쪽에 다시 넘기면, str/Decimal
        # 입력이 실행기 원장엔 float 로 착지하고 계약 원장엔 문자열/Decimal 로
        # 착지해 두 원장이 같은 콜에 대해 다른 타입/값을 갖는 대조-장식 결함이
        # 재발한다 — 이 판이 다른 곳에서 이미 "대조 장치가 장식이면 안 된다"고
        # 못 박은 것과 같은 축).
        coerced_cost = _numeric_or_none(record.cost_usd)
        coerced_wall = _numeric_or_none(record.wall_ms)
        entry: dict[str, Any] = {
            "schema_version": record.schema_version,
            "ts": float(record.ts),
            "executor": str(record.executor),
            "invocation_id": str(record.invocation_id),
            "phase": str(record.phase),
            "status": str(record.status),
            "input_summary": _prepare_structured_sink(record.input_summary or {}),
            "output_summary": _prepare_structured_sink(record.output_summary or {}),
            "cost_usd": coerced_cost,
            "wall_ms": coerced_wall,
            "error": masked_error,
            "raw_dump": masked_raw,
            "tags": _prepare_structured_sink(record.tags or {}),
        }

        # __SLOT_LEDGER_JOIN_2026_08_08__ 조인축. 이 원장은 375행 전부
        # ``cycle_id`` 가 없어서 **일일 캡이 읽는 값과 계약 원장의 값을 사이클
        # 단위로 대조할 수 없었다**. 게이트 OFF 면 빈 dict 라 행이 종전과
        # byte-identical 이다. ⛔ 마스킹 **뒤**에 넣는다 — 조인키는 우리가 만든
        # 값이라 마스킹 대상이 아니고, 마스커가 id 를 훼손하면 조인이 죽는다.
        try:
            from agi_v8_1.runtime.ledger_join import join_keys

            entry.update(join_keys())
        except Exception as exc:  # noqa: BLE001 — 조인키가 원장을 죽이면 안 된다
            _swallowed(exc, site="enforcement.executor_log._emit:join_keys",
                       category="telemetry")

        # Defend against non-serializable leaves in masked containers (e.g. a
        # lambda passed in tags survives masking as a callable). atomic_append_
        # _jsonl uses json.dumps WITHOUT a default=, so stringify defensively
        # here — never raise.
        entry = _jsonify(entry)

        atomic_append_jsonl(path, entry)

        # __SLOT_SWARM_EPISODE_TEE_2026_08_07__ 계약 원장으로 한 벌 더.
        # ⛔ **명시 opt-in 일 때만.** ``record_llm_call`` 경유 경로는 이미
        #    ``_tee_episode_cost`` 를 따로 부르므로, 여기서 무조건 tee 하면
        #    같은 콜이 COST 행 두 개를 받아 이중계상이 된다.
        # ⚠️ 비용이 ``None`` 인 행(= span start, 비-provider 실행)은 과금 콜이
        #    아니다. 계약의 "모든 과금 콜은 행을 받는다"에 안 걸린다.
        # __SLOT_B4_MONEY_ROW_TEE_PARITY_2026_08_10__ ``coerced_cost``(entry 에
        # 실제로 쓴 바로 그 값)로 판정 + 전달한다. 원본 ``record.cost_usd`` 로
        # 판정하면 str/Decimal 은 여기선 "값 있음"으로 새 tee 되는데 실행기
        # 원장 쪽 entry 는 None 으로 착지해(구 버전) 두 원장이 같은 콜에 대해
        # 서로 다른 값을 갖는다 — 그리고 병리적(list/dict) cost_usd 는 진성이
        # 아닌데도 tee 를 시도해 내부에서 매번 실패-후-swallow 왕복만 했다.
        if tee_episode and coerced_cost is not None:
            from agi_v8_1.enforcement.si_spend_ledger import tee_episode_cost

            tee_episode_cost(
                cost=coerced_cost, out_summary=dict(record.output_summary or {}),
                executor=record.executor,
                agent_name=str((record.tags or {}).get("lane_id") or "lane"),
                wall_ms=coerced_wall, error=record.error, log_path=path,
                # __SLOT_TRACK2_WORKER_CALL_ID_2026_08_29__ The span already
                # minted one stable identity before dispatch.  Reuse that
                # exact identity in the contract COST row; a fresh UUID here
                # would make the two ledgers impossible to join, while the
                # episode context is legitimately absent on this async swarm
                # path (20260829a: 31/31 worker COST rows had call_id=None).
                call_id=record.invocation_id)
    except Exception as exc:  # noqa: BLE001 — logging must never crash a caller
        _swallowed(exc, site="enforcement.executor_log._emit:227", category="telemetry")
        logger.warning("executor_log emit failed: %s", format_exception_for_log(exc))


# __SLOT_B4_EXECUTOR_LOG_JSONIFY_CYCLE_2026_08_10__ interpreter-recursion
# safety margin only (mirrors ``policy.secret_masker._JSONIFY_MAX_DEPTH`` —
# NOT the masking depth cap; this function runs AFTER masking).
_JSONIFY_MAX_DEPTH = 64


def _jsonify(
    value: Any,
    _depth: int = 0,
    _seen: frozenset | None = None,
    *,
    _sanitize_native_strings: bool = False,
) -> Any:
    """Recursively coerce a (already-masked) structure to JSON-safe leaves.

    json.dumps in atomic_append_jsonl has no ``default=`` fallback, so a stray
    non-serializable leaf (lambda, set, custom object) would raise. We stringify
    such leaves rather than let the append blow up — drop nothing, never raise.

    # __SLOT_OBJ_LEAF_STRINGIFY_MASK_2026_08_10__ C1 R3: "already-masked" was
    # a lie for object leaves. ``mask_obj``/``mask_obj_strict`` pass
    # non-string, non-container leaves through UNCHANGED — masking never saw
    # the string that ``str(value)`` mints here, AFTER the maskers ran. A
    # short key riding an object's ``__str__``/``__repr__`` (custom object,
    # Exception instance) therefore landed RAW in the ledger (2026-08-09
    # 재현: tags={"obj": Sneaky()} / tags={"exc": SneakyExc("boom sk-…")}).
    # Fix: every string this function MINTS (object-leaf fallback + non-str
    # dict keys — the two ``str()`` sites) goes through ``_compose_secret_
    # mask``. Strings that arrive here already masked are NOT re-masked
    # (byte-parity: JSON-native rows never touch the new path; secret-free
    # stringifications pass the regexes unchanged).

    # __SLOT_B4_EXECUTOR_LOG_JSONIFY_CYCLE_2026_08_10__ 🔴 이 사본은 순환/
    # 과깊이 가드가 없었다(``policy.secret_masker.jsonify_masked`` 은 R3/B4
    # 에서 이미 받은 가드). ``cost_usd``/``wall_ms`` 는 ``_mask_obj_guarded``
    # (input_summary/output_summary/tags 전용)를 안 거치고 이 함수에 무가드로
    # 실린다 — 그래서 병리적(순환) ``cost_usd`` 가 ``RecursionError`` 를 냈고,
    # 그 예외가 ``_emit`` 바깥 try 로 흘러 **돈 행이 통째로 증발**했다(재현:
    # ``log_executor_event(cost_usd=<자기참조 list>)`` → rows=0). ``policy.
    # secret_masker.jsonify_masked`` 와 같은 어휘(``<MASKED:cycle>`` /
    # ``<MASKED:max_depth>`` / ``<MASKED:iteration_failed:...>``)로 접는다 —
    # 알고리즘을 통째로 위임하지는 않는다(항목6 지시: 동기화 앵커 "확장"은
    # 제안일 뿐, 이 판이 선취하지 않는다 — 여러 실행기가 공유하는 원장이라
    # 국소 통합의 폭발반경을 신중히 본다). 여기서 닫는 것은 확인된 행-증발
    # 결함 하나뿐이다.
    #
    # 🔴 B4 재검증(2026-08-10, major #1) — 위 수리는 "행이 산다"만 지켰지
    # **행이 살아도 되는 모양으로 산다**는 안 지켰다: 순환 sentinel
    # (``['<MASKED:cycle>']``)이 ``cost_usd`` 자리에 그대로 착지해, 그 행을
    # 읽는 실소비자 ``observability.briefing_builder.build_briefing`` 이
    # ``float(list)`` 로 영구 크래시했다(append-only라 그 행은 못 지운다).
    # ⇒ ``cost_usd``/``wall_ms`` 는 이제 이 함수(``_jsonify``)에 아예 안
    # 들어온다 — ``_emit`` 이 그 두 필드만 ``_numeric_or_none`` 으로 먼저
    # 걸러 숫자|None 만 보낸다(__SLOT_B4_MONEY_FIELD_TYPE_SAFETY_2026_08_10__
    # 참조). 이 함수의 순환/과깊이/순회-실패 가드는 여전히 유효하다 —
    # ``input_summary``/``output_summary``/``tags`` 는 ``_mask_obj_guarded``
    # 를 거쳐 이미 문자열로 평평해진 뒤 여기 오지만(직접 순환에 안 부딪힘),
    # 이 함수 자체를 직접 호출하는 경로(테스트, 향후 호출자)는 여전히 이
    # 가드에 의존한다.
    """
    if _depth > _JSONIFY_MAX_DEPTH:
        return "<MASKED:max_depth>"
    if isinstance(value, str):
        if not _sanitize_native_strings:
            return value
        return format_text_for_critical_record(
            value,
            max_chars=SINK_MASK_INPUT_MAX_CHARS,
            one_line=False,
            keep="head",
        )
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (dict, list, tuple, set, frozenset)):
        if _seen is None:
            _seen = frozenset()
        oid = id(value)
        if oid in _seen:
            return "<MASKED:cycle>"
        _seen = _seen | {oid}
    if isinstance(value, dict):
        try:
            items = list(value.items())
        except Exception as _ff_exc:  # noqa: BLE001 — 순회 프로토콜이 던지는 매핑
            _swallowed(_ff_exc, site="enforcement.executor_log._jsonify:dict_items",
                       category="telemetry")
            return f"<MASKED:iteration_failed:{_mask_stringified(type(value).__name__)}>"
        out: dict[str, Any] = {}
        for idx, item in enumerate(items):
            try:
                k, v = item
            except Exception as _ff_exc:  # noqa: BLE001 — 2-튜플이 아닌 원소
                _swallowed(_ff_exc, site="enforcement.executor_log._jsonify:dict_unpack",
                           category="telemetry")
                return f"<MASKED:iteration_failed:{_mask_stringified(type(value).__name__)}>"
            # __SLOT_B4_STR_SUBCLASS_KEY_IS_NOT_PLAIN_STR_2026_08_10__ 정본
            # (``policy.secret_masker.jsonify_masked``)과 같은 좁힘 —
            # ``type(k) is str`` 로 str 서브클래스(포이즌드 __hash__/__eq__ 가능)
            # 를 걸러 ``_mask_stringified`` 로 정화한다. 상세 근거는 정본 슬롯 주석.
            key = (
                format_text_for_critical_record(
                    k,
                    max_chars=SINK_MASK_INPUT_MAX_CHARS,
                    one_line=False,
                    keep="head",
                )
                if type(k) is str and _sanitize_native_strings
                else (k if type(k) is str else _mask_stringified(k))
            )
            # __SLOT_B4_DICT_INSERT_IS_LOSSLESS_2026_08_10__ 정본
            # (``policy.secret_masker._dict_insert_no_loss``) 을 그대로 임포트해
            # 쓴다 — 무가드 ``out[key] =`` 는 키 충돌(원소 증발)/포이즌드
            # str-서브클래스 키(예외 전파) 둘 다 냈다(재검증 major, 라이브 재현).
            _shared_dict_insert(
                out,
                key,
                _jsonify(
                    v,
                    _depth + 1,
                    _seen,
                    _sanitize_native_strings=_sanitize_native_strings,
                ),
                idx,
            )
        return out
    if isinstance(value, (list, tuple, set, frozenset)):
        try:
            items2 = list(value)
        except Exception as _ff_exc:  # noqa: BLE001
            _swallowed(_ff_exc, site="enforcement.executor_log._jsonify:iter",
                       category="telemetry")
            return f"<MASKED:iteration_failed:{_mask_stringified(type(value).__name__)}>"
        masked_items = [
            _jsonify(
                v,
                _depth + 1,
                _seen,
                _sanitize_native_strings=_sanitize_native_strings,
            )
            for v in items2
        ]
        if isinstance(value, (set, frozenset)):
            # __SLOT_B4_EXECUTOR_LOG_JSONIFY_CYCLE_2026_08_10__ 반복 순서가
            # PYTHONHASHSEED 의존 — 저장 직전 정렬해 행 바이트를 프로세스 간
            # 결정론으로 만든다(secret_masker.jsonify_masked 와 같은 정렬 키).
            # __SLOT_B4_SORT_KEY_IS_FAIL_CLOSED_2026_08_10__ 정렬 키 자체는
            # ``policy.secret_masker._row_sort_key`` 를 그대로 공유한다(사본을
            # 또 뜨지 않는다 — 이 함수도 같은 ``sort_keys=True`` TypeError 신규
            # 회귀에 물려 있었다. 상세 근거는 그 함수 독스트링/슬롯 주석 참조).
            masked_items.sort(key=_shared_row_sort_key)
        return masked_items
    # Anything else (callable, bytes, custom object) -> stringified + masked.
    return _mask_stringified(value)


def _mask_stringified(value: Any) -> str:
    """Stringify a non-JSON-native leaf, then mask the MINTED string.

    # __SLOT_OBJ_LEAF_STRINGIFY_MASK_2026_08_10__ the one place ``_jsonify``
    # turns an unmasked object into a string — so the composed masker stands
    # AFTER ``str()`` too. ``_compose_secret_mask`` is itself fail-closed
    # (named sentinel, never raises), so a masking failure withholds content
    # instead of killing the caller's row.

    B4(2026-08-10): the minted string is first run through
    ``stabilize_object_repr_addresses`` (imported from ``policy.secret_masker``
    — no local regex copy) so a default ``__repr__``'s memory address does not
    make the row byte-nondeterministic across processes (mirrors the same fix
    in ``policy.secret_masker.mask_stringified``).
    """
    text = format_text_for_critical_record(
        value,
        max_chars=SINK_MASK_INPUT_MAX_CHARS,
        one_line=False,
        keep="head",
    )
    # Address stabilization only substitutes a fixed non-secret token; it
    # cannot introduce new credential material, so do not send already-safe
    # text through a second truncating formatter.
    return stabilize_object_repr_addresses(text)


# --- public API ------------------------------------------------------------
# __SLOT_EXECUTOR_LOG_2026_06_14__
class ExecutorLogSpan:
    """Mutable handle yielded by :func:`executor_log_span`.

    Call sites accumulate output / cost / raw onto the span; the terminal
    record (success or failure) is written exactly once when the span closes
    (or via an explicit :meth:`fail` for manual-mode call sites that catch
    their own exceptions instead of raising).
    """

    __slots__ = (
        "executor",
        "invocation_id",
        "_input_summary",
        "_tags",
        "_log_path",
        "_tee_episode",
        "_time_fn",
        "_perf_start",
        "_output_summary",
        "_cost_usd",
        "_raw_dump",
        "_finalized",
    )

    def __init__(
        self,
        *,
        executor: str,
        invocation_id: str,
        input_summary: Mapping[str, Any] | None,
        tags: Mapping[str, Any] | None,
        log_path: Path | None,
        time_fn: Callable[[], float],
        perf_start: float,
        tee_episode: bool = False,
    ) -> None:
        self.executor = str(executor)
        self.invocation_id = str(invocation_id)
        self._input_summary = dict(input_summary) if input_summary else {}
        self._tags = dict(tags) if tags else {}
        self._log_path = log_path
        # __SLOT_SWARM_EPISODE_TEE_2026_08_07__ 계약 원장으로 한 벌 더 낼지.
        # ⛔ 기본 False — 켜는 쪽이 '나는 record_llm_call 을 안 거친다'를 선언한다.
        self._tee_episode = bool(tee_episode)
        self._time_fn = time_fn
        self._perf_start = perf_start
        self._output_summary: dict[str, Any] = {}
        self._cost_usd: float | None = None
        self._raw_dump: str | None = None
        self._finalized = False

    # -- mutators -----------------------------------------------------------
    def set_output(self, **kw: Any) -> None:
        """Merge keyword fields into the output_summary."""
        self._output_summary.update(kw)

    def set_cost(self, x: float | None) -> None:
        """Record the call's cost in USD."""
        self._cost_usd = None if x is None else float(x)

    def set_raw(self, text: Any) -> None:
        """Record the last raw dump (masked + truncated at emit time)."""
        # Keep the complete value until the single sink can stringify, bound,
        # mask, and truncate it in the required order.  Eager ``str()`` here
        # both bypassed the sink guard and could erase the terminal row.
        self._raw_dump = text

    def _wall_ms(self) -> int:
        return int((time.perf_counter() - self._perf_start) * 1000)

    def fail(self, exc: BaseException | str, raw: Any = None) -> None:
        """Manually emit a failure record.

        For call sites (e.g. the swarm executor) that catch their own
        exceptions and RETURN an error result rather than re-raising. Writes
        the terminal failure record exactly once; subsequent context-exit
        will not double-write.
        """
        if self._finalized:
            return
        if raw is not None:
            self._raw_dump = raw
        self._finalized = True
        manual_error = format_text_for_critical_record(
            exc,
            max_chars=SINK_MASK_INPUT_MAX_CHARS,
            one_line=False,
            keep="head",
        )
        _emit(
            ExecutorLogRecord(
                ts=self._time_fn(),
                executor=self.executor,
                invocation_id=self.invocation_id,
                phase="failure",
                status="error",
                input_summary=self._input_summary,
                output_summary=self._output_summary,
                cost_usd=self._cost_usd,
                wall_ms=self._wall_ms(),
                # Manual fail historically stores only the message (without a
                # class prefix).  Keep that byte behavior, but fail closed if
                # the exception's own ``__str__`` is hostile.
                error=manual_error,
                raw_dump=self._raw_dump,
                tags=self._tags,
            ),
            self._log_path,
            self._tee_episode,
        )

    # -- terminal writers (called by the context manager) -------------------
    def _finish_success(self) -> None:
        if self._finalized:
            return
        self._finalized = True
        raw = self._raw_dump if _verbose() else None
        _emit(
            ExecutorLogRecord(
                ts=self._time_fn(),
                executor=self.executor,
                invocation_id=self.invocation_id,
                phase="success",
                status="ok",
                input_summary=self._input_summary,
                output_summary=self._output_summary,
                cost_usd=self._cost_usd,
                wall_ms=self._wall_ms(),
                error=None,
                raw_dump=raw,
                tags=self._tags,
            ),
            self._log_path,
            self._tee_episode,
        )

    def _finish_failure(self, exc: BaseException) -> None:
        if self._finalized:
            return
        self._finalized = True
        _emit(
            ExecutorLogRecord(
                ts=self._time_fn(),
                executor=self.executor,
                invocation_id=self.invocation_id,
                phase="failure",
                status="error",
                input_summary=self._input_summary,
                output_summary=self._output_summary,
                cost_usd=self._cost_usd,
                wall_ms=self._wall_ms(),
                error=_format_failure_error(exc),
                raw_dump=self._raw_dump,  # last set_raw, masked at emit
                tags=self._tags,
            ),
            self._log_path,
            self._tee_episode,
        )


def _new_invocation_id() -> str:
    import uuid

    return uuid.uuid4().hex


@contextmanager
def executor_log_span(
    *,
    executor: str,
    invocation_id: str | None = None,
    input_summary: Mapping[str, Any] | None = None,
    tags: Mapping[str, Any] | None = None,
    log_path: Path | None = None,
    time_fn: Callable[[], float] | None = None,
    tee_episode_cost: bool = False,
) -> Iterator[ExecutorLogSpan]:
    """Bracket an executor invocation with a start + one terminal record.

    On enter: writes ``phase="start"`` / ``status="start"`` and starts a
    ``time.perf_counter`` clock. Yields an :class:`ExecutorLogSpan` handle.

    On normal exit: writes ``phase="success"`` / ``status="ok"`` with
    ``wall_ms`` filled (unless ``.fail()`` was already called). ``raw_dump`` is
    included on the success path ONLY when verbose.

    On exception exit: writes ``phase="failure"`` / ``status="error"`` (error
    from the exception, raw_dump from the last ``.set_raw``) and RETURNS False
    so the exception is NOT suppressed — it propagates to the caller.

    Exactly ONE terminal record is written (start + one of success/failure),
    tracked by the span's ``_finalized`` flag.
    """
    tfn = time_fn or time.time
    inv_id = invocation_id or _new_invocation_id()
    perf_start = time.perf_counter()

    # Start record (non-fatal — _emit swallows its own errors).
    _emit(
        ExecutorLogRecord(
            ts=tfn(),
            executor=str(executor),
            invocation_id=str(inv_id),
            phase="start",
            status="start",
            input_summary=dict(input_summary) if input_summary else {},
            output_summary={},
            cost_usd=None,
            wall_ms=None,
            error=None,
            raw_dump=None,
            tags=dict(tags) if tags else {},
        ),
        log_path,
    )

    span = ExecutorLogSpan(
        executor=str(executor),
        invocation_id=str(inv_id),
        input_summary=input_summary,
        tags=tags,
        log_path=log_path,
        time_fn=tfn,
        perf_start=perf_start,
        tee_episode=tee_episode_cost,
    )
    try:
        yield span
    except BaseException as exc:  # noqa: BLE001 — write terminal then re-raise
        # The write itself is inside _emit's try/except-swallow. With a
        # @contextmanager generator, re-raising (NOT ``return False``) is what
        # propagates the exception — a generator return value is ignored by the
        # contextmanager machinery, so a bare ``return`` would SUPPRESS it. We
        # MUST NOT suppress: write the terminal failure record, then re-raise.
        span._finish_failure(exc)
        raise
    else:
        span._finish_success()


def log_executor_event(
    *,
    executor: str,
    phase: str,
    status: str,
    invocation_id: str | None = None,
    input_summary: Mapping[str, Any] | None = None,
    output_summary: Mapping[str, Any] | None = None,
    cost_usd: float | None = None,
    wall_ms: int | None = None,
    error: str | None = None,
    raw_dump: str | None = None,
    tags: Mapping[str, Any] | None = None,
    log_path: Path | None = None,
    time_fn: Callable[[], float] | None = None,
) -> None:
    """One-shot event writer for call sites that already build structured dicts.

    Funnels into the SAME private ``_emit`` sink as :func:`executor_log_span`,
    so masking/truncation/path resolution are identical. ``phase`` / ``status``
    are free strings (never branched on).
    """
    tfn = time_fn or time.time
    _emit(
        ExecutorLogRecord(
            ts=tfn(),
            executor=str(executor),
            invocation_id=str(invocation_id) if invocation_id else _new_invocation_id(),
            phase=str(phase),
            status=str(status),
            input_summary=dict(input_summary) if input_summary else {},
            output_summary=dict(output_summary) if output_summary else {},
            cost_usd=cost_usd,
            wall_ms=wall_ms,
            error=error,
            raw_dump=raw_dump,
            tags=dict(tags) if tags else {},
        ),
        log_path,
    )


__all__ = [
    "SCHEMA_VERSION",
    "ExecutorLogRecord",
    "ExecutorLogSpan",
    "executor_log_span",
    "log_executor_event",
]
