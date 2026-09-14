"""external_falsifier_bus_v1 — the single append-only ledger of external
"you were wrong" signals for agi_v8_1.

# __SLOT_FALSIFIER_BUS_2026_06_16__

WHY (the bottleneck this seam dissolves): the SI loop is idea-starved because
every external scoreboard Doha owns — live trader PnL, git history, Seoul
open-data, robot telemetry — is a SEPARATE ISLAND the loop cannot reach
(``agi_v8_1`` imports the trader ``real_investment`` package exactly 0 times,
by design; the trader is a different repo). This module is the DECOUPLING
SEAM: producers append observation-only events here; SI-side consumers read
them. NO cross-import, NO execution — just an append-only JSONL the two sides
both touch.

SAFETY INVARIANTS (load-bearing — enforced by ``validate_event``, fail-closed):
  1. ``observation_only`` is ALWAYS True. An event that sets it False is
     REJECTED (dropped, never written).
  2. ``non_executable`` is ALWAYS True. Likewise REJECTED if False.
  3. The bus NEVER opens, imports, evals, or executes anything. ``artifact_path``
     is an OPAQUE reference STRING the bus stores verbatim and never touches.
     There is no execution surface here; a consumer that chooses to follow the
     reference must do its own sandboxing.
  4. Append-only: there is exactly ONE writer primitive (``append_event``) and
     it only appends. No update/delete API exists.
  5. Every string field is run through the canonical composed secret masker
     (``policy.secret_masker.mask_secrets_strict``) BEFORE serialization, and
     structured fields through ``mask_obj`` — a trader row or stack frame that
     echoes a key never lands in the ledger. BEFORE-serialization is necessary
     but NOT sufficient: an object leaf's leaking string does not exist yet at
     masking time, it is MINTED later by ``str()``. So a second masking layer
     stands AT the stringification boundary too — ``policy.secret_masker.
     jsonify_masked`` (__SLOT_SHARED_STRINGIFY_MASK_2026_08_10__), shared with
     ``enforcement.executor_log`` rather than copied.
  6. Containment: the default ledger path is under the state_dir (same jail as
     ``SafeAutoApply`` repo_root), resolved exactly like ``executor_log`` /
     ``apply_chain_full``.

⚠️ SCOPE OF (5) — 무엇이 **안** 닫혔는지 먼저 적는다(2026-08-10, R2 적대검증).
   위 두 층이 닫는 것은 **버스 원장 행과 이 모듈의 로그**다. 같은 예외 문자열이
   나가는 다른 싱크는 이 파일 밖이고 아직 열려 있다:
     · ``policy.fail_fast`` 의 swallow census — 예외 **원문**을 ``_last`` 에 담고,
       ``runtime/cli.py`` 가 ``swallowed_census`` 로 orchestrator digest(stdout +
       디스크 리포트)에 싣는다. HEAD 부터 그랬고 이 판이 바꾸지 않았다(회귀 아님).
     · ``runtime/cli.py`` 의 ``error_detail`` — ``str(exc)[:300]`` 을 같은 digest 에
       마스킹 없이 싣는다.
   두 자리 모두 이 판의 소유 밖이라 손대지 않았다. "동종 유출 폐쇄"라는 제목은
   **원장 축에 한정**이다 — 후속 판의 몫으로 여기 명시해 둔다.

This is a PASSIVE library: importing it has no side effect, and it writes
nothing until a producer calls ``append_event``. The first producer (the
read-only PnL-surprise bridge) and the first consumer (``predicted_delta``
grading) are SEPARATE modules — this file is only the schema + validator + I/O.

Non-fatal (DEFAULT MODE): ``append_event`` is fully wrapped — a producer bug or
disk error NEVER crashes the caller; the event is dropped with a WARNING.
⚠️ ``AGI_V8_STRICT_FAIL_FAST=true`` 는 그 계약을 의도적으로 뒤집는다(전역 디버그
모드, default-OFF). 자세한 판정 근거는 ``__SLOT_STRICT_IS_NOT_OURS_TO_BEND_2026_08_10__``.
"""

from __future__ import annotations

# __SLOT_FALSIFIER_BUS_2026_06_16__
import dataclasses
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Module-top imports are pure stdlib-regex / IO — safe to import eagerly.
# (mask_secrets_strict lazy-imports providers.base internally, so importing
# this module never pulls providers at import time.)
# __SLOT_SHARED_STRINGIFY_MASK_2026_08_10__ 문자열화 경계(``_jsonify`` 가 주조하는
# 문자열)의 마스킹 정본은 secret_masker 다 — 여기 사본을 두지 않는다.
from agi_v8_1.policy.secret_masker import (
    jsonify_masked,
    mask_obj_strict_guarded,
    mask_secrets_strict_guarded,
    mask_stringified,
    stabilize_object_repr_addresses as _row_stabilize_addresses,
)
from agi_v8_1.state.store import atomic_append_jsonl
from agi_v8_1.state.path_guard import resolve_sink

# __SLOT_FAIL_FAST_2026_07_25__ Swallowed failures route through one choke
# point: counted + named always, re-raised under AGI_V8_STRICT_FAIL_FAST.
from agi_v8_1.policy.fail_fast import (
    format_exception_for_log,
    swallowed as _swallowed,
)

logger = logging.getLogger(__name__)

# Single source of truth — mirrors executor_log / apply_chain_full SCHEMA_VERSION.
BUS_SCHEMA_VERSION = "agi_v8_falsifier_bus_v1"

# Env knobs (single source of truth).
_ENV_ENABLED = "AGI_V8_FALSIFIER_BUS_ENABLED"
_ENV_BUS_PATH = "AGI_V8_FALSIFIER_BUS_PATH"
_ENV_MAX_BYTES = "AGI_V8_FALSIFIER_BUS_MAX_BYTES"

_DEFAULT_MAX_BYTES = 16_384  # per-event ledger-line ceiling (post-mask)

# Soft allowlist of known sources. NOT enforced (a new producer may add its own
# source) — purely documentary + used to tag an "unknown" source for triage.
# This is a data list, never a control-flow switch (no per-source branching).
KNOWN_SOURCES = (
    "trader_real",
    "trader_mock",
    "git_history",
    "seoul_subway",
    "seoul_opendata",
    "robot_telemetry",
    "si_self",
    "test",
)


# --- record schema ---------------------------------------------------------
# __SLOT_FALSIFIER_BUS_2026_06_16__ ONE frozen dataclass. ``source`` and
# ``producer`` are free-string discriminators — NEVER switched on.
@dataclass(frozen=True, slots=True)
class FalsifierEvent:
    """One immutable external-falsification event.

    Required (no default): the four fields that make an event meaningful —
    where it came from, what was predicted, what happened, and whether that
    counts as a wrong signal — plus the producer that emitted it and a wrongness
    magnitude.
    """

    source: str            # where the signal came from (free string)
    producer: str          # which producer module emitted it (free string)
    prediction: Any        # what the system expected (JSON-able)
    outcome: Any           # what actually happened (JSON-able)
    is_wrong: bool         # the falsification bit
    severity: float        # magnitude of wrongness; clamped to [0.0, 1.0]

    schema_version: str = BUS_SCHEMA_VERSION
    ts: float = field(default_factory=time.time)
    event_id: str = ""     # content hash; auto-filled by append_event if empty
    control_effect: Any = None     # optional: did an intervention change outcome
    artifact_path: str | None = None  # OPAQUE reference — the bus NEVER opens it
    # --- invariants (must stay True; an event that flips either is rejected) --
    observation_only: bool = True
    non_executable: bool = True
    tags: dict = field(default_factory=dict)


# --- env knobs -------------------------------------------------------------
def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no"}


def _enabled() -> bool:
    # Default TRUE — append unless explicitly turned off (kill switch).
    return _env_flag(_ENV_ENABLED, default=True)


def _max_bytes() -> int:
    raw = os.environ.get(_ENV_MAX_BYTES)
    if raw is None:
        return _DEFAULT_MAX_BYTES
    try:
        val = int(raw.strip())
    except (TypeError, ValueError) as _ff_exc:
        _swallowed(_ff_exc, site="bus.falsifier_bus._max_bytes:138", category="persist")
        return _DEFAULT_MAX_BYTES
    return val if val > 0 else _DEFAULT_MAX_BYTES


# --- path resolution -------------------------------------------------------
# __SLOT_FALSIFIER_BUS_2026_06_16__ mirrors executor_log._resolve_log_path.
def resolve_bus_path() -> Path:
    """Resolve the falsifier-bus JSONL path.

    Precedence:
      1. ``AGI_V8_FALSIFIER_BUS_PATH`` (explicit override) → that file.
      2. ``${AGI_V8_STATE_DIR or AGI_STATE_DIR}/bus/falsifier_events.jsonl``.
      3. ``agi_v8_1/state/bus/falsifier_events.jsonl`` (repo-local default).

    No separate mkdir is needed — ``atomic_append_jsonl`` mkdirs the parent.
    """
    override = os.environ.get(_ENV_BUS_PATH)
    if override:
        # __SLOT_LEDGER_SINK_2026_08_08__ 정규화(+AGI_V8_LEDGER_SINK_STRICT 시 containment).
        return resolve_sink(override, what="falsifier bus")
    env_dir = os.environ.get("AGI_V8_STATE_DIR") or os.environ.get("AGI_STATE_DIR")
    if env_dir:
        state_dir = Path(env_dir)
    else:
        # agi_v8_1/bus/falsifier_bus.py -> agi_v8_1/state
        state_dir = Path(__file__).resolve().parent.parent / "state"
    return state_dir / "bus" / "falsifier_events.jsonl"


# --- validation (THE SAFETY GATE) ------------------------------------------
def _asdict_tolerant(obj: Any, _depth: int = 0) -> Any:
    """``dataclasses.asdict`` 와 같은 **구조**를 만들되 ``deepcopy`` 는 안 한다.

    # __SLOT_SHARED_STRINGIFY_MASK_2026_08_10__ R1 적대검증이 잡은 스키마 표류:
    # deepcopy 불가 잎(memoryview·핸들·락) 하나가 끼면 종전 폴백은 **얕은 필드
    # 추출**로 내려가서, 같은 생산자·같은 필드인데 중첩 dataclass 가 dict 가 아니라
    # ``"Inner(user='u', pnl=1.5)"`` repr 문자열로 착지했다(행 타입 계약이 조용히
    # dict→str 로 바뀐다). 드롭보다야 낫지만, 구조는 보존할 수 있다 —
    # ``asdict`` 가 던지는 유일한 이유가 잎의 deepcopy 이기 때문이다. 잎을 복사하지
    # 않고 **참조로** 넘기면 같은 승격(dataclass→dict)을 그대로 얻는다. 안전한
    # 이유: 이 dict 는 읽기 전용으로 쓰이고(최상위 키만 재대입) 마스커는 입력을
    # 변형하지 않으며, ``FalsifierEvent`` 는 frozen 이다.
    #
    # 깊이 상한은 순환 참조 방어(``asdict`` 도 순환에서는 재귀 폭발한다).
    """
    if _depth > 12:
        return obj
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {
            f.name: _asdict_tolerant(getattr(obj, f.name), _depth + 1)
            for f in dataclasses.fields(obj)
        }
    if isinstance(obj, dict):
        return {k: _asdict_tolerant(v, _depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        items = [_asdict_tolerant(v, _depth + 1) for v in obj]
        if isinstance(obj, tuple) and hasattr(obj, "_fields"):  # namedtuple
            return type(obj)(*items)
        return type(obj)(items) if isinstance(obj, tuple) else items
    return obj


def _as_event_dict(event: "FalsifierEvent | dict") -> dict:
    if isinstance(event, FalsifierEvent):
        # __SLOT_SHARED_STRINGIFY_MASK_2026_08_10__ 판2 적대 가족이 잡은 동종 결함
        # (마스킹이 아니라 **직렬화 전단**): ``dataclasses.asdict`` 는 잎을
        # ``copy.deepcopy`` 한다 ⇒ deepcopy 불가 잎(memoryview·열린 핸들·락·소켓
        # — 트레이더/로봇 텔레메트리에 충분히 실린다) 하나가 ``TypeError`` 를
        # 던지고, ``validate_event`` 가 그걸 "type_error" 로 접어 **이벤트를 통째로
        # 드롭**했다(재현: "cannot pickle memoryview objects" → append_event=None).
        # append-only 원장에서 증발한 falsification 은 되찾을 길이 없다.
        # 계약 보존: asdict 가 성공하는 입력은 종전과 **byte-identical**(중첩
        # dataclass 의 dict 승격 포함). 실패하는 입력만 얕은 필드 추출로 구조한다
        # — 이 dict 는 읽기 전용으로 쓰이고(최상위 키만 재대입), 마스커는 입력을
        # 변형하지 않으므로 참조 공유가 안전하다.
        try:
            return dataclasses.asdict(event)
        except Exception as _ff_exc:  # noqa: BLE001 — 드롭보다 구조 보존 폴백이 낫다
            # __SLOT_STRICT_IS_NOT_OURS_TO_BEND_2026_08_10__ strict fail-fast ON
            # 에서는 ``_swallowed`` 가 재발화하고 이 폴백에 닿지 못한다. 그게
            # 의도된 동작이다(근거: policy/secret_masker.py 의 같은 슬롯 주석 —
            # strict 는 default-OFF 디버그 모드이고, 면제 스위치를 심는 자리는
            # 573 사이트가 물린 전역 초크포인트라 이 판의 소유 밖이다).
            _swallowed(_ff_exc, site="bus.falsifier_bus._as_event_dict:asdict",
                       category="persist")
            try:
                # 1차 폴백: 같은 구조(중첩 dataclass→dict), deepcopy 없이.
                return _asdict_tolerant(event)
            except Exception as _ff_exc2:  # noqa: BLE001 — 병리적 잎(__iter__/__hash__)
                _swallowed(_ff_exc2, site="bus.falsifier_bus._as_event_dict:tolerant",
                           category="persist")
                # 2차 폴백: 얕은 필드 추출. 스키마는 표류하지만 행은 산다.
                return {f.name: getattr(event, f.name) for f in dataclasses.fields(event)}
    if isinstance(event, dict):
        return dict(event)
    raise TypeError(f"event must be FalsifierEvent or dict, got {type(event).__name__}")


def validate_event(event: "FalsifierEvent | dict") -> tuple[bool, str]:
    """Fail-closed structural + invariant validation.

    Returns ``(ok, reason)``. ``ok=False`` means the event MUST NOT be written.
    The two invariant checks (observation_only / non_executable) are the
    load-bearing ones: a producer can never sneak an executable/active event
    onto the bus, because flipping either flag is a hard reject.
    """
    try:
        d = _as_event_dict(event)
    except TypeError as exc:
        _swallowed(exc, site="bus.falsifier_bus.validate_event:186", category="persist")
        return False, f"type_error: {exc}"

    # required, non-empty string fields
    for key in ("source", "producer"):
        val = d.get(key)
        if not isinstance(val, str) or not val.strip():
            return False, f"missing_or_empty: {key}"

    if "prediction" not in d:
        return False, "missing: prediction"
    if "outcome" not in d:
        return False, "missing: outcome"

    if not isinstance(d.get("is_wrong"), bool):
        return False, "is_wrong_not_bool"

    sev = d.get("severity")
    if isinstance(sev, bool) or not isinstance(sev, (int, float)):
        return False, "severity_not_number"
    try:
        sevf = float(sev)
    except (TypeError, ValueError) as _ff_exc:
        _swallowed(_ff_exc, site="bus.falsifier_bus.validate_event:208", category="persist")
        return False, "severity_not_number"
    if sevf != sevf or sevf in (float("inf"), float("-inf")):  # NaN / inf
        return False, "severity_not_finite"

    # --- the load-bearing invariants ---------------------------------------
    if d.get("observation_only", True) is not True:
        return False, "invariant: observation_only must be True"
    if d.get("non_executable", True) is not True:
        return False, "invariant: non_executable must be True"

    ap = d.get("artifact_path", None)
    if ap is not None and not isinstance(ap, str):
        return False, "artifact_path_not_str"

    return True, "ok"


def _clamp_severity(sev: Any) -> float:
    try:
        s = float(sev)
    except (TypeError, ValueError) as _ff_exc:
        _swallowed(_ff_exc, site="bus.falsifier_bus._clamp_severity:229", category="persist")
        return 0.0
    if s != s:  # NaN
        return 0.0
    return max(0.0, min(1.0, s))


# __SLOT_EVENT_ID_IS_DETERMINISTIC_2026_08_10__ 🔴 R2 적대검증이 잡은 결함:
# "드롭보다 행 생존"이 **새 결함을 낳았다**.
#
# 종전 ``_compute_event_id`` 는 ``json.dumps(basis, default=str)`` 였다. 낯선 잎을
# ``str()`` 로 넘기는데, 파이썬 기본 ``__repr__``/``__str__`` 은 **객체 주소**를
# 물고 있다(``<memory at 0x7f…>``). 주소는 프로세스마다 다르다 ⇒ 같은 논리 이벤트를
# 두 번 발행하면 **매번 다른 event_id** 를 받는다.
#
# HEAD 에서는 이 면이 안 보였다. deepcopy 불가 잎이 ``asdict`` 를 터뜨려
# ``validate_event`` 가 이벤트를 통째로 **드롭**했기 때문이다(rows 0). 판1 수리가
# 그 드롭을 없애 행을 살리자 비결정적 id 가 원장에 착지하기 시작했다:
#   조용한 드롭 → 조용한 **무한 중복**(하류 dedup 은 event_id 로 접으므로 영영
#   안 접힌다). 이 판이 그렇게 길게 방어한 중복제거/커서 계약이 바로 그 입력에서
#   죽는다. 실측(R2): 같은 이벤트 2회 append → ids 2종, rows 2.
#
# ⇒ 해시 기저에서 **비결정성만** 제거한다. 판별력은 버리지 않는다.
#   · 주소(``… at 0x7f…``): 유일하게 증명 가능한 비결정 원천 ⇒ 고정 토큰으로 치환.
#     좁은 앵커(``(?<= at )0x…``)를 쓴다 — 내용에 든 임의 hex 는 안 건드린다.
#   · ``memoryview``: 주소만 남고 **내용은 repr 에 아예 없다** ⇒ 치환만 하면 서로
#     다른 버퍼가 같은 id 로 접힌다(중복제거가 진짜 사건을 먹는다). 내용 해시로
#     신원을 되살린다.
#   · ``set``/``frozenset``: 문자열/바이트 원소의 반복 순서는 ``PYTHONHASHSEED``
#     에 걸려 **프로세스마다 다르다** ⇒ 정렬해 고정한다.
# 비-exotic 이벤트(오늘의 생산자 전부)는 ``default=`` 를 아예 안 타고 정규식도 안
# 물리므로 id 는 **byte-identical** 이다 — 라이브 원장 156행(tick_jail/si_jail,
# 전부 생산자 지정 ``fe_pnl_…``)은 애초에 이 함수를 타지도 않는다.
#
# __SLOT_B4_ID_BASIS_ROW_ANCHOR_PARITY_2026_08_10__ 🔴 판B4 재검증 major #2 —
# 이 자리는 종전 **자기 앵커**(``(?<= at )0x[0-9a-fA-F]+``, ``(?=>)`` 없음)를
# 썼다. 같은 라운드가 행 값 층(``policy.secret_masker._STRINGIFY_ADDRESS``)은
# 프로세스 산문 파괴(major #2 본 결함)를 막으려고 ``(?=>)`` 로 좁혔는데, 이
# id 기저 층은 안 좁혀서 **두 층이 서로 다른 판정**을 내리는 새 비대칭이 생겼다
# (재현: ``class Conn:\n  def __repr__(self): return "Conn at " + hex(id(self))``
# — id 기저는 접어 event_id 가 프로세스 간 안정인데, 행 값은 안 접어 저장
# ``prediction`` 바이트가 프로세스마다 달랐다. 바로 이 판이 "event_id 는
# 결정론인데 행 바이트는 비결정"이라고 부른 자기모순의 정확한 재발생 지점).
#
# 수리는 앵커를 좁히는 게 아니라 **공유**다 — 정규식 사본을 또 뜨면 다음에도
# 갈라진다(같은 duplicate_prompt_definition_trap). ``policy.secret_masker.
# stabilize_object_repr_addresses`` 를 그대로 위임한다: 두 층이 이제 문자
# 그대로 같은 앵커를 쓰므로, 어느 쪽이 접으면 다른 쪽도 반드시 접는다(또는
# 둘 다 안 접는다) — "id 만 접힌다"는 비대칭이 구조적으로 불가능해진다.
#
# ⚠️ 정직 고지(잔여): CPython 기본 repr(``... at 0x<hex>>``, 주소 바로 뒤 ``>``)
# 이 아닌 커스텀 repr(위 ``Conn`` 처럼 각괄호 없이 "X at 0x…"만 내는 모양)은
# 이제 **id 기저와 행 값 둘 다** 안 접는다 — 그런 잎이 실제로 쓰이면 두 층
# 모두 프로세스 간 비결정으로 남는다(레포의 실제 생산자들은 이 모양을 안 낸다
# — memoryview/기본 object repr/Exception repr 전부 각괄호로 닫히거나 아예
# 주소가 없다). 판별력보다 **두 층의 일치**를 우선한 트레이드오프다: 자기
# 모순으로 조용히 갈라지느니, 알려진 채로 함께 남는 잔여가 낫다.
def _stabilize_addresses(text: str) -> str:
    """기본 ``__repr__`` 이 물고 나오는 객체 주소를 고정 토큰으로 접는다.

    ``policy.secret_masker.stabilize_object_repr_addresses`` 로 위임 —
    행 값 층과 **문자 그대로 같은 앵커**를 쓴다(위 슬롯 참조, id/행 비대칭
    재발 방지).
    """
    return _row_stabilize_addresses(text)


def _id_basis_memoryview(value: memoryview) -> str:
    """memoryview 의 **내용**으로 신원을 짓는다(repr 에는 주소만 있고 내용이 없다)."""
    try:
        return "memoryview:sha256:" + hashlib.sha256(
            value.tobytes()).hexdigest()[:16]
    except Exception as _ff_exc:  # noqa: BLE001 — 비연속 버퍼 등
        _swallowed(_ff_exc, site="bus.falsifier_bus._id_basis_leaf:memoryview",
                   category="persist")
        return f"memoryview:<uncastable:{value.format}:{value.nbytes}>"


def _id_basis_leaf(value: Any) -> Any:
    """``json.dumps(default=…)`` 훅 — 낯선 잎을 **결정론적으로** 문자열화한다.

    ⛔ 이 함수의 출력은 원장에 안 실린다(해시 기저일 뿐). 그래서 마스킹 대상이
    아니고, 반대로 **판별력**(서로 다른 사건 → 서로 다른 id)이 유일한 요구사항이다.
    """
    if isinstance(value, (set, frozenset)):
        # 원소 순서는 프로세스마다 다르다 — 정렬로 고정.
        return sorted(_stabilize_addresses(str(v)) for v in value)
    if isinstance(value, memoryview):
        # repr 에 내용이 없다 ⇒ 주소만 접으면 서로 다른 버퍼가 한 id 로 붙는다.
        return _id_basis_memoryview(value)
    return _stabilize_addresses(str(value))


# __SLOT_EVENT_ID_FALLBACK_IS_NORMALIZED_TOO_2026_08_10__ 🔴 R3 적대검증이 잡은
# **이 판의 신규 회귀**: 위 정규화(세트 정렬·주소 안정화·memoryview 내용화)는
# ``json.dumps`` 의 ``default=`` 훅 **안에만** 있었다. 그 훅은 dumps 가 낯선 잎을
# 만났을 때 불리는 것이라, **dumps 자체가 던지면 한 번도 도달하지 않는다**
# (비-str dict 키 = ``TypeError: keys must be str…`` 가 대표 사례).
#
# 그때 내려가던 폴백은 필드마다 ``repr(value)`` 였고, ``repr(set)`` 의 원소 순서는
# ``PYTHONHASHSEED`` 에 걸린다 ⇒ **프로세스마다 다른 event_id**. 실측(R3 재현,
# fresh process ×4, PYTHONHASHSEED 미지정 = 실운영 조건):
#   prediction={'set': {...5개...}}, outcome={'bad': __str__ 이 던지는 객체}
#   → fe_e995878c…/fe_2a842112…/fe_08958229…/fe_ea1db69c… (4프로세스 4종)
# HEAD 는 이 입력을 **드롭**했으므로(asdict 폭발) 비교 대상이 없었다. 판1/판2 가
# 드롭을 없애 행을 살리자 비결정 id 가 원장에 착지했다:
#   조용한 드롭 → 조용한 **무한 중복**(하류는 event_id 로 접으므로 영영 안 접힌다).
#   소비자 실측: ``rl_accum/grounding.py:141`` 은 read_events 전건을 dedup 없이
#   평균내고 ``bus/cycle_wire.py:150`` 은 ``n_recent_wrong`` 으로 센다.
#
# ⇒ 수리는 "폴백을 조금 더 안정화"가 아니라 **경로 단일화**다. 아래 canon 은 같은
#   세 규칙을 **모든 층에** 적용한다(훅은 잎에서만 불린다는 것이 결함의 뿌리였다).
#   추가 이득: 종전 폴백은 한 잎이 병리적이면 **필드 통째로** ``<unrepresentable:
#   dict>`` 로 떨어져 판별력을 크게 잃었다. canon 은 그 잎만 떨어뜨린다.
_ID_BASIS_MAX_DEPTH = 12


def _id_basis_canon(value: Any, _depth: int = 0, _seen: frozenset | None = None) -> Any:
    """해시 기저를 **완전히 JSON-native + 결정론적**인 형태로 접는다.

    ``_id_basis_leaf`` 와 같은 정규화를 쓰되 잎이 아니라 **모든 층**에 적용한다.

    반환은 None/bool/int/float/str/list 뿐이다(dict 는 ``[[key, value], …]`` 정렬
    쌍 목록으로 내린다 — 비-str 키를 str 로 접을 때 서로 다른 두 키가 한 키로
    **조용히 합쳐지는 것**을 막고, ``sort_keys`` 가 혼합 타입에서 던지는 것도
    피한다). 그래서 이 출력에 대한 ``json.dumps`` 는 던질 수 없다.

    ⛔ 원장에 안 실린다(해시 기저일 뿐) ⇒ 마스킹 대상이 아니다. 요구사항은
    **결정성 + 판별력** 둘뿐이다.
    """
    if _depth > _ID_BASIS_MAX_DEPTH:
        return "<max_depth>"
    # str 은 이미 결정론적이다 — 주소 안정화를 걸지 않는다(판별력 보존, 그리고
    # ``default=`` 경로도 native str 은 훅을 안 태운다는 사실과 일치).
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, memoryview):
        return _id_basis_memoryview(value)
    if isinstance(value, (bytes, bytearray)):
        return f"{type(value).__name__}:sha256:" + hashlib.sha256(
            bytes(value)).hexdigest()[:16]
    if isinstance(value, (dict, list, tuple, set, frozenset)):
        if _seen is None:
            _seen = frozenset()
        oid = id(value)
        if oid in _seen:
            return "<cycle>"
        _seen = _seen | {oid}
    if isinstance(value, dict):
        pairs = []
        for k, v in value.items():
            pairs.append([
                _id_basis_stable_str(_id_basis_canon(k, _depth + 1, _seen)),
                _id_basis_canon(v, _depth + 1, _seen),
            ])
        # 삽입 순서는 신원이 아니다(같은 매핑이 다른 순서로 지어질 수 있다) ⇒ 정렬.
        pairs.sort(key=_id_basis_stable_str)
        return pairs
    if isinstance(value, (set, frozenset)):
        # 반복 순서가 PYTHONHASHSEED 의존 — 정규화된 문자열로 정렬해 고정한다.
        return sorted(
            _id_basis_stable_str(_id_basis_canon(v, _depth + 1, _seen))
            for v in value
        )
    if isinstance(value, (list, tuple)):
        # 순서가 신원인 컨테이너 — 정렬하지 않는다.
        return [_id_basis_canon(v, _depth + 1, _seen) for v in value]
    # 낯선 잎: ``str`` → 실패하면 ``repr`` → 그것도 실패하면 타입 이름.
    # 주소만 고정하고 내용은 남긴다(판별력).
    for _cast in (str, repr):
        try:
            return _stabilize_addresses(_cast(value))
        except Exception as _ff_exc:  # noqa: BLE001 — 병리적 __str__/__repr__ 은 실재한다
            _swallowed(_ff_exc, site="bus.falsifier_bus._id_basis_canon:cast",
                       category="persist")
    return f"<unrepresentable:{type(value).__name__}>"


def _id_basis_stable_str(value: Any) -> str:
    """canon 출력(=JSON-native)을 결정론적 문자열로. 정렬 키 겸 키 자리 주조용."""
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    except Exception as _ff_exc:  # noqa: BLE001 — canon 출력은 native 라 도달 불가
        _swallowed(_ff_exc, site="bus.falsifier_bus._id_basis_stable_str",
                   category="persist")
        return f"<unstringable:{type(value).__name__}>"


def _compute_event_id(d: dict) -> str:
    """Deterministic content hash → idempotency / dedup key.

    Same (source, producer, ts, prediction, outcome, is_wrong) → same id, so a
    producer that re-emits an event it already wrote can be deduped downstream.

    # __SLOT_SHARED_STRINGIFY_MASK_2026_08_10__ 판정: **id 는 마스킹 前 값으로
    # 계산한다(기존 순서 유지)**. 근거를 명시해 둔다 — 이 자리는 "마스킹 뒤로
    # 옮기면 더 안전해 보이는" 함정이다.
    #
    #   (a) 유출면이 아니다. ``blob`` 은 디스크에 안 실린다. 실리는 건
    #       sha256 앞 16 자리 hex 뿐이고, 그건 내용 공개가 아니다(preimage).
    #       원문이 실리는 자리는 ``entry`` 이며 거기는 전부 마스킹을 거친다.
    #   (b) 마스킹은 **단사가 아니다**. ``sk-FAKEAAA…`` 와 ``sk-FAKEBBB…`` 는
    #       둘 다 ``***MASKED***`` 로 접힌다 ⇒ 마스킹 後 해시는 서로 다른 두
    #       falsification 에 **같은 id** 를 주고, 중복제거 소비자가 진짜 사건
    #       하나를 조용히 버린다. 안전 게이트가 데이터 손실로 fail-open 하는 꼴.
    #   (c) 소비자 계약이 "원사건의 신원"이라고 말한다. 생산자들은 id 를 원천
    #       식별자에서 직접 만든다(``git_history_bridge:278`` = ``fe_git_<sha12>``,
    #       ``pnl_surprise_bridge:194`` = ``fe_pnl_<trade_id>``) — 그 경로는
    #       ``_compute_event_id`` 를 아예 안 탄다. 즉 id 는 **저장된 행의 해시가
    #       아니라 원사건의 이름**이다. 자동 계산분만 마스킹 後로 옮기면 두 경로가
    #       서로 다른 규칙을 갖는다.
    #   (d) 원장은 append-only 다. 이미 착지한 행들의 id 는 마스킹 前 기준이라,
    #       기준을 바꾸면 같은 사건의 재발행이 **다른 id** 를 받아 ``read_events``
    #       의 ``after_event_id`` 커서와 하류 중복제거가 동시에 죽는다.
    #
    # ⇒ 순서는 그대로. 대신 fallback 이 행을 죽이지 못하게 fail-closed 로 만든다.
    """
    basis = {
        "schema_version": d.get("schema_version", BUS_SCHEMA_VERSION),
        "source": d.get("source"),
        "producer": d.get("producer"),
        "ts": d.get("ts"),
        "prediction": d.get("prediction"),
        "outcome": d.get("outcome"),
        "is_wrong": d.get("is_wrong"),
    }
    try:
        blob = json.dumps(basis, sort_keys=True, ensure_ascii=False,
                          default=_id_basis_leaf)
    except Exception as _ff_exc:  # noqa: BLE001 — never let id-gen crash the append
        # __SLOT_STRICT_IS_NOT_OURS_TO_BEND_2026_08_10__ strict ON 이면 여기서
        # 재발화한다(아래 대체 기저에 안 닿는다) — 의도된 동작.
        _swallowed(_ff_exc, site="bus.falsifier_bus._compute_event_id:dumps",
                   category="persist")
        # __SLOT_EVENT_ID_FALLBACK_IS_NORMALIZED_TOO_2026_08_10__ 종전엔 필드마다
        # ``repr`` 이었고 그게 R3 가 잰 비결정성의 자리다(``repr(set)`` 순서 =
        # PYTHONHASHSEED). 이제 **같은 정규화**를 모든 층에 적용한 canon 을 쓴다 —
        # 정상 경로와 폴백 경로가 서로 다른 안정성 등급을 갖지 않게.
        # id 는 원장 행이 아니므로 유보할 대상이 없다. canon 은 native 만 내보내
        # 아래 dumps 는 던질 수 없지만, 계약을 코드로도 닫아 둔다.
        try:
            blob = json.dumps(_id_basis_canon(basis), sort_keys=True,
                              ensure_ascii=False)
        except Exception as _ff_exc2:  # noqa: BLE001 — id 생성이 행을 먹으면 안 된다
            _swallowed(_ff_exc2, site="bus.falsifier_bus._compute_event_id:canon",
                       category="persist")
            # 최후 기저: 필드 이름 + 타입 이름. 판별력은 크게 잃지만 결정론적이고,
            # 무엇보다 행을 죽이지 않는다.
            blob = "|".join(
                f"{_k}=<unrepresentable:{type(_v).__name__}>"
                for _k, _v in sorted(basis.items())
            )
    return "fe_" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


# __SLOT_EVENT_ID_NO_RAW_SECRET_2026_08_10__ 식별자 면의 유출 폐쇄.
_EVENT_ID_HASH_PREFIX = "fe_masked_"

# __SLOT_EVENT_ID_GRAMMAR_ALLOWLIST_2026_08_10__ 🔴 철회 기록: 종전의
# ``_only_length_catchall`` 카브아웃(= "마스커 흔적이 ``<MASKED-TOKEN>`` 하나뿐이면
# 원문 유지")은 **자기모순**이라 걷어냈다. R3 적대검증이 잰 결과 + 재측정:
#
#   (1) 카브아웃이 실제로 연 구멍(R3 실측, 이 판이 만든 것):
#       event_id  = fe_Zq7Kp2Lm9Xw4Rt6Yn1Bv8Cd3Gh5Jk0Fs2Ae7Uo9Pi4   ← 원문 착지
#       tags      = {'trade_id': '<MASKED-TOKEN>'}
#       artifact  = trader_outcomes:<MASKED-TOKEN>
#       같은 바이트열이 **같은 행 안에서** 형제 필드는 마스킹되고 id 만 원문이었다.
#       ``fe_`` + 64hex 계열도 동일.
#
#   (2) 카브아웃의 **명시 근거를 재측정했더니 자기 발동 영역에서 거짓**이었다.
#       근거 원문: "그 행에서 같은 값은 tags.trade_id / artifact_path 에 원문 그대로
#       남는다 ⇒ 은닉 이득 0". 실측(scratchpad/pan2/measure_carveout_rationale.py):
#         body 36자(uuid)  → 형제 RAW    ← 근거 참. 그러나 catch-all 이 애초에 안 문다
#         body 42자(opaque)→ 형제 MASKED ← 근거 거짓
#         body 64자(hex)   → 형제 MASKED ← 근거 거짓
#       길이 catch-all 은 40자 이상에서만 문다 ⇒ 카브아웃이 **실제로 발동하는 영역
#       전체에서 그 근거가 거짓**이다. 근거가 참인 영역에서는 발동하지도 않는다.
#
# ⇒ 축을 바꾼다. 판별을 마스커의 **출력 어휘**(catch-all 인가?)에 걸면 "우리 id"와
#   "40자 opaque 시크릿"을 원리적으로 구분할 수 없다 — 둘 다 같은 흔적을 남긴다.
#   대신 **원본 문자열이 우리가 주조하는 id 형식인가**로 판별한다. 이 축은 시크릿
#   모양을 하나도 안 받아들이는 것이 **기계로 증명 가능**하다(아래 앵커 테스트가
#   시크릿 가족 전체를 각 grammar 에 먹여 매치 0 을 요구한다).
#
# 🔴 B4 (2026-08-10) — 위 "기계로 증명 가능"의 **정확한 범위**를 적는다(과장
#   정정): 그 증명은 **현재 아래 4개 grammar 한정**이다. allowlist 축에 다섯
#   번째 항목이 미래에 추가되면, 그 새 항목은 자동으로 이 증명에 안 들어온다 —
#   ``test_no_secret_family_can_satisfy_any_id_grammar`` 가 알려진 시크릿 가족
#   전체를 "그 시점의" ``_KNOWN_EVENT_ID_GRAMMARS`` 전항목에 먹이므로 새 항목도
#   같은 테스트 실행에서 같이 검증되긴 하지만, 그 항목 자체가 시크릿 모양을
#   받아들이는 정규식으로 지어지면(예: 폭넓은 catch-all) 테스트는 **그 항목을
#   추가한 사람이 케이스를 빠뜨리지 않는 한에서만** 잡는다 — 기계 증명은
#   "이미 있는 규칙이 시크릿을 안 받아들인다"이지 "미래에 추가되는 어떤 규칙도
#   안전하다"가 아니다. 신규 항목은 여전히 사람이 앵커+시크릿-가족 회귀를
#   갖춰야 한다(아래 "앵커 없는 형식은 추가 금지" 참조).
#
# ⚠️ 정직한 계측 결과 — 이 allowlist 는 **오늘 no-op** 이다:
#   라이브 원장 156행(78 distinct, tick_jail+si_jail)의 event_id 는 전부 ≤29자이고
#   composed 마스커를 **한 건도 안 물린다**(실측: fired=0/78). 즉 카브아웃이 지키던
#   집단은 공집합이었다.
#   ⇒ 이 변경의 순효과 = **유출면 폐쇄**, 정상 id 영향 0. 그 사실을 테스트로 고정한다.
#
# 각 항목은 실측/생산자 앵커를 달고 들어온다. ⛔ 앵커 없는 형식은 추가 금지 —
# ``kis-order-…``/``swebench__…`` 는 R2 보고서에 예시로 등장했지만 레포 전수 grep
# 결과 **이 판의 테스트 픽스처에만** 존재했다(생산자 0, 라이브 행 0). 그런 것을
# allowlist 에 넣으면 테스트가 계약이 아니라 구현을 따라간 것이 된다.
#
# 🔴 B4 (2026-08-10) — 그 기준을 **자기 항목에도** 적용한다. 아래 ``fe_git_``
#   grammar 는 종전 ``{7,40}`` 이었는데, 트리 전수 grep 결과 실제 생산자
#   (``bus/producers/git_history_bridge.py:254/278``)는 항상 ``sha[:12]``(정확히
#   12 hex)만 낸다 — 13~40 자 구간은 위 kis-order 와 **같은 처지**(생산자 0,
#   라이브 행 0)였다. 판2가 자기 판정 기준을 자기 항목에는 안 적용한 자기모순.
#   ⇒ 좁힌다: 생산자가 실제로 내는 길이만 grammar 에 남긴다. 풀 sha(40 hex)로
#   넓히는 날이 오면, 그날의 커밋이 새 앵커(파일:줄) + 시크릿-가족 회귀를 같이
#   가져와야 한다 — "언젠가 넓어질 수 있으니 미리 열어 둔다"는 근거는 이 파일이
#   위에서 이미 한 번 거짓으로 판정한 패턴이다.
_KNOWN_EVENT_ID_GRAMMARS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # 라이브 실측: 156행 전건(78 distinct)이 이 형식. 본문 = 종목코드/티커 + 날짜 +
    # 시각. 앵커: bus/producers/pnl_surprise_bridge.py:194 (``fe_pnl_{trade_id}``).
    ("bus/producers/pnl_surprise_bridge.py:194",
     re.compile(r"\Afe_pnl_[A-Za-z0-9]{1,12}_[0-9]{8}_[0-9]{6}\Z")),
    # 트리의 실제 생산자는 항상 정확히 sha12(``sha[:12]``, id 전체 19자)만 낸다
    # (실측: bus/producers/git_history_bridge.py:254 ``sha12 = sha[:12]`` →
    # :278 ``event_id=f"fe_git_{sha12}"`` — 가변 길이를 내는 코드 경로가 없다).
    # 오늘은 마스커를 안 물린다(=no-op). B4(2026-08-10): 종전 ``{7,40}`` 은 이
    # 생산자가 한 번도 낸 적 없는 폭(13~40자)까지 미리 열어 둔 카브아웃이었다 —
    # 앵커: bus/producers/git_history_bridge.py:278.
    ("bus/producers/git_history_bridge.py:278",
     re.compile(r"\Afe_git_[0-9a-f]{12}\Z")),
    # 이 모듈이 스스로 주조하는 두 형식(자기일관성/멱등성).
    # 앵커: bus/falsifier_bus.py:_compute_event_id (``fe_`` + sha256[:16]).
    ("bus/falsifier_bus.py:_compute_event_id",
     re.compile(r"\Afe_[0-9a-f]{16}\Z")),
    # 앵커: bus/falsifier_bus.py:_sanitize_event_id (``fe_masked_`` + sha256[:16]).
    ("bus/falsifier_bus.py:_sanitize_event_id",
     re.compile(r"\Afe_masked_[0-9a-f]{16}\Z")),
)


def _is_known_event_id_grammar(raw: str) -> bool:
    """원본 문자열이 **우리가 주조하는** event_id 형식인가.

    ⛔ 마스커 출력이 아니라 **원본**을 본다. 출력 어휘로 판별하면 "우리 id"와
    "40자 opaque 시크릿"이 같은 흔적(``<MASKED-TOKEN>``)을 남겨 구분이 불가능하다.

    각 grammar 의 본문 문자집합은 순수 hex 이거나 (대문자·숫자 티커 + 고정 자릿수
    날짜/시각)이라, 알려진 시크릿 서명(``sk-``/``AKIA``/``AIza``/``gh?_``/``xox``/
    JWT/PEM/base64 덩어리)은 **어느 것도 만족할 수 없다**. 이 성질은 선언이 아니라
    앵커 테스트가 시크릿 가족 전체로 기계 검증한다.

    ⚠️ 범위(B4 2026-08-10, 과장 정정): 이 기계 검증은 **현재 4개 grammar 한정**
    이다 — ``_KNOWN_EVENT_ID_GRAMMARS`` 축에 미래에 새 항목이 추가되면, 그 항목이
    실제로 시크릿 모양을 배제하는지는 **그 커밋이 자기 시크릿-가족 회귀를 새로
    가져와야만** 증명된다. 이 함수/테스트가 "allowlist 축에 무엇이 와도 안전하다"
    를 보장하지는 않는다.
    """
    return any(pat.match(raw) for _anchor, pat in _KNOWN_EVENT_ID_GRAMMARS)


def _sanitize_event_id(event_id: Any) -> str:
    """식별자 필드에 실린 시크릿을 **치환 없이** 닫는다.

    R1 적대검증(2026-08-10)이 잰 유일한 잔여 유출면: ``FalsifierEvent(event_id=
    "fe_" + "sk-…")`` 는 20종 가족 중 혼자 원문 착지했다(19/20 clean). 종전 코드는
    이 자리를 BOUNDARY 로 **선언**만 했다 — 선언은 정확했지만 "동종 유출 폐쇄"라는
    제목이 이 면을 포함하지 않았다.

    ⛔ 왜 마스킹(치환)이 아니라 해시인가. id 는 중복제거 키이자 커서다. 마스커는
    단사가 아니라서 ``sk-AAA…``/``sk-BBB…`` 두 id 가 같은 ``***MASKED***`` 로
    접힌다(=서로 다른 사건이 같은 키). 대신 sha256 대체를 쓰면:
      · 유출 0 (원문은 한 글자도 안 실린다)
      · 충돌 0 (서로 다른 id → 서로 다른 해시)
      · 재발행 안정 (같은 id → 항상 같은 해시 ⇒ 중복제거/커서 계약 보존)
    ``append_event`` 의 반환값과 원장 행이 **같은 값**이 되도록 여기 한 자리에서만
    정한다(반환값이 곧 커서라는 계약).

    발동 조건은 위 ``__SLOT_EVENT_ID_GRAMMAR_ALLOWLIST_2026_08_10__`` 판정대로
    **fail-closed 기본값 + 실측 grammar allowlist** 다: 마스커가 무엇이든 물면
    해시로 대체하고, 원본이 우리가 주조하는 id 형식일 때만 원문을 지킨다.
    (종전의 "길이 catch-all 은 봐준다" 카브아웃은 자기 발동 영역에서 근거가
    거짓이라 철회했다 — 위 슬롯의 재측정 기록 참조.)
    """
    try:
        raw = event_id if isinstance(event_id, str) else str(event_id)
    except Exception as _ff_exc:  # noqa: BLE001 — 병리적 ``__str__``
        _swallowed(_ff_exc, site="bus.falsifier_bus._sanitize_event_id:str",
                   category="persist")
        return _EVENT_ID_HASH_PREFIX + hashlib.sha256(
            repr(type(event_id)).encode("utf-8")).hexdigest()[:16]
    masked = mask_secrets_strict_guarded(raw)
    if masked == raw:
        return raw
    # __SLOT_EVENT_ID_GRAMMAR_ALLOWLIST_2026_08_10__ 마스커가 물었다 ⇒ 기본값은
    # 해시(fail-closed). 예외는 하나 — **원본이 우리가 주조하는 id 형식**일 때.
    if _is_known_event_id_grammar(raw):
        return raw
    return _EVENT_ID_HASH_PREFIX + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _jsonify(value: Any) -> Any:
    """Recursively coerce a (already-masked) structure to JSON-safe leaves.

    ``atomic_append_jsonl`` calls json.dumps with no ``default=``, so a stray
    non-serializable leaf would raise. We stringify such leaves rather than let
    the append blow up — drop nothing, never raise.

    # __SLOT_SHARED_STRINGIFY_MASK_2026_08_10__ 이 함수는 종전에
    # ``executor_log._jsonify`` 의 **옛 사본**이었고("Same idiom as
    # executor_log._jsonify"), 그 사본은 객체 잎을 마스킹 **뒤에** ``str()`` 해서
    # ``tags={"exc": Exception("boom sk-…")}`` 류가 두 마스커를 다 통과해 버스
    # 원장에 원문 착지했다(mask_obj_strict 는 비문자열·비컨테이너 잎을 무변화
    # 통과시킨다 — 유출 문자열은 여기서 비로소 주조된다).
    # 수리는 사본 재생산이 아니라 **정본 임포트**:
    # ``policy.secret_masker.jsonify_masked`` 가 주조하는 모든 문자열(객체 잎 +
    # 비-str dict 키)에 composed 마스커를 세운다. 얇은 위임으로 남겨 두는 이유는
    # 이 모듈 안의 호출부/테스트가 이름을 계속 쓸 수 있게 하기 위함이고, 로직은
    # 여기 없다(사본 없음).
    """
    return jsonify_masked(value)


# --- the single writer primitive (APPEND-ONLY) -----------------------------
# __SLOT_FALSIFIER_BUS_2026_06_16__
def append_event(
    event: "FalsifierEvent | dict",
    *,
    bus_path: Path | str | None = None,
) -> str | None:
    """Validate → mask → append ONE event to the bus. Returns its ``event_id``.

    Fail-closed + non-fatal:
      - bus disabled via env → no-op, returns None, NO file created.
      - validation fails (incl. the observation_only / non_executable
        invariants) → event DROPPED with a WARNING, returns None.
      - any masking / IO error → swallowed with a WARNING, returns None.

    There is intentionally NO update or delete counterpart: the ledger is
    append-only.

    # __SLOT_STRICT_IS_NOT_OURS_TO_BEND_2026_08_10__ ⚠️ 축을 밝힌다: "절대 호출자를
    # 크래시시키지 않는다"는 **무조건형이 아니라 기본 모드(strict OFF)의 계약**이다.
    # ``AGI_V8_STRICT_FAIL_FAST=true`` 는 삼킨 실패를 전부 재발화시키는 프로세스
    # 전역 디버그 모드이고, 그 모드에서는 이 함수도 던진다 — 마스킹/직렬화 방어층도
    # 예외가 아니다. 그렇게 둔 근거는 ``policy/secret_masker.py`` 의 같은 슬롯
    # 주석에 있다(요지: strict 는 default-OFF 이고 레포 어디서도 안 켜며, 면제
    # 스위치를 심을 자리는 573 사이트가 물린 전역 초크포인트라 이 판의 소유 밖이고,
    # 그 재설계는 이미 gc-119 로 기입돼 있다).
    # ⇒ 이 판이 고친 것은 **strict OFF 에서의 행 증발**이다: 마스킹이 터져도 행은
    #   살고 내용만 이름 붙여 유보된다. strict ON 에서는 크게 터지는 것이 의도다.
    """
    try:
        if not _enabled():
            return None

        ok, reason = validate_event(event)
        if not ok:
            # ``reason`` 은 상수 어휘가 대부분이지만 ``type_error: {exc}`` 한 갈래가
            # 예외 문자열을 실어 나른다 — 같은 층을 세운다(정상 어휘는 무변화).
            logger.warning("falsifier_bus: dropped invalid event (%s)",
                           mask_secrets_strict_guarded(reason))
            return None

        d = _as_event_dict(event)

        # normalize / fill derived fields
        d["schema_version"] = d.get("schema_version") or BUS_SCHEMA_VERSION
        if not isinstance(d.get("ts"), (int, float)) or isinstance(d.get("ts"), bool):
            d["ts"] = time.time()
        d["severity"] = _clamp_severity(d.get("severity"))
        # hard re-assert the invariants on the persisted record (defence in
        # depth — validate already rejected False, but never persist anything
        # but the canonical True).
        d["observation_only"] = True
        d["non_executable"] = True
        event_id = _sanitize_event_id(d.get("event_id") or _compute_event_id(d))
        d["event_id"] = event_id

        # --- mask everything BEFORE serialization (the safety gate) --------
        # __SLOT_SHARED_STRINGIFY_MASK_2026_08_10__ 이 dict 안의 ``str(...)`` 두
        # 자리도 주조면이다(dict 형태 이벤트는 이 두 필드에 임의 객체를 실을 수
        # 있다). 판정이 갈린다:
        #   - ``schema_version`` 은 조인/중복제거에 안 쓰이는 서술 필드 ⇒ 마스킹
        #     한다. 정본 값(``agi_v8_falsifier_bus_v1``)은 어느 패턴에도 안 걸려
        #     오늘의 출력은 byte-identical 이다.
        #   - ``event_id`` 는 ⛔ **치환식 마스킹을 하지 않는다**. 이건 중복제거
        #     키이자 ``read_events(after_event_id=…)`` 커서라, 서로 다른 두 값이
        #     같은 ``***MASKED***``/``<MASKED-TOKEN>`` 으로 접히면 다른 사건이
        #     같은 키를 갖고 커서는 영영 자기 행을 못 찾는다 — 안전 게이트가
        #     데이터 손실로 fail-open 하는 꼴. 그래서 유출은 ``_sanitize_event_id``
        #     가 **결정론적 해시 대체**로 닫는다(치환 아님: 충돌 0, 재발행 안정).
        entry: dict[str, Any] = {
            "schema_version": mask_secrets_strict_guarded(d["schema_version"]),
            "event_id": str(event_id),
            "ts": float(d["ts"]),
            "source": mask_secrets_strict_guarded(d.get("source", "")),
            "producer": mask_secrets_strict_guarded(d.get("producer", "")),
            "is_wrong": bool(d.get("is_wrong")),
            "severity": float(d["severity"]),
            "prediction": mask_obj_strict_guarded(d.get("prediction")),
            "outcome": mask_obj_strict_guarded(d.get("outcome")),
            "control_effect": mask_obj_strict_guarded(d.get("control_effect")),
            "artifact_path": (
                mask_secrets_strict_guarded(d["artifact_path"])
                if isinstance(d.get("artifact_path"), str)
                else None
            ),
            "observation_only": True,
            "non_executable": True,
            "tags": mask_obj_strict_guarded(d.get("tags") or {}),
        }
        # __SLOT_SHARED_STRINGIFY_MASK_2026_08_10__ 위 마스커들은 전부 fail-closed
        # 래퍼다: 마스킹이 터져도 **행은 살고 내용만 이름 붙여 유보**된다(무가드
        # 직호출이면 예외가 append_event 바깥 try 로 흘러 이벤트가 통째로 증발했다
        # — 원장은 append-only 라 증발한 falsification 은 되찾을 길이 없다).
        entry = _jsonify(entry)

        # size guard — never let one runaway event bloat the ledger.
        limit = _max_bytes()
        try:
            blob = json.dumps(entry, ensure_ascii=False)
        except Exception as _ff_exc:  # noqa: BLE001
            _swallowed(_ff_exc, site="bus.falsifier_bus.append_event:347", category="persist")
            blob = ""
        if blob and len(blob.encode("utf-8")) > limit:
            entry["prediction"] = _truncate_repr(entry.get("prediction"), limit)
            entry["outcome"] = _truncate_repr(entry.get("outcome"), limit)
            entry["control_effect"] = None
            entry["tags"] = {"truncated": True}
            entry["truncated"] = True

        # __SLOT_LEDGER_JOIN_2026_08_08__ ⛔ ``event_id`` 계산 **뒤**에 넣는다 —
        # 그 해시는 고정 6필드(source·producer·ts·prediction·outcome·is_wrong)라
        # 조인키가 기여하지 않지만, 순서로 그 사실을 못박는다. 앞에 넣으면 나중에
        # 해시 기준이 넓어지는 날 **같은 사건이 사이클마다 다른 id 를 받아** 중복
        # 제거가 조용히 죽는다.
        try:
            from agi_v8_1.runtime.ledger_join import join_keys

            entry.update(join_keys())
        except Exception as _join_exc:  # noqa: BLE001
            _swallowed(_join_exc, site="bus.falsifier_bus.append_event:join_keys",
                       category="telemetry")

        path = Path(bus_path) if bus_path is not None else resolve_bus_path()
        atomic_append_jsonl(path, entry)
        return event_id
    except Exception as exc:  # noqa: BLE001 — append must never crash a caller
        _swallowed(exc, site="bus.falsifier_bus.append_event:359", category="persist")
        # __SLOT_CENSUS_MESSAGE_MASKED_2026_08_10__ 로그도 원장과 같은 급의 싱크다
        # (파일·stdout 으로 나간다). 원문 ``%s`` 로 예외를 찍으면 마스킹 층을 옆문
        # 으로 우회한다 — 여기 오는 예외는 마스킹/직렬화가 터진 바로 그 예외라
        # 시크릿을 물고 있을 확률이 가장 높다.
        # ⛔ ``f"{exc}"`` 로 쓰지 않는다 — 병리적 ``__str__`` 이면 **바깥 except 본문
        # 에서** 새 예외가 나가 "안 터진다"는 계약 자체가 깨진다(fail_fast 가 같은
        # 이유로 고쳐진 자리다). 타입 이름은 절대 안 터지고 ``mask_stringified`` 는
        # str() 실패를 자기 안에서 ``<unserializable>`` 로 접는다.
        logger.warning(
            "falsifier_bus append failed: %s", format_exception_for_log(exc)
        )
        return None


def _truncate_repr(value: Any, limit: int) -> str:
    # __SLOT_SHARED_STRINGIFY_MASK_2026_08_10__ ``default=`` 는 여기서 또 하나의
    # str() 주조면이다. 호출부(``append_event`` 크기 가드)는 ``_jsonify`` 뒤라
    # 잎이 전부 JSON-native 여서 오늘은 **도달하지 않는다** — 그래서 ``mask_
    # stringified`` 로 바꿔도 오늘의 출력은 byte-identical 이다. 바꾸는 이유는
    # 호출 순서가 언젠가 뒤집혔을 때 이 자리가 조용한 유출면으로 부활하지 않게
    # 하기 위함이다(사본이 썩는 방식과 같은 부류의 사고).
    s = (
        json.dumps(value, ensure_ascii=False, default=mask_stringified)
        if not isinstance(value, str)
        else value
    )
    cap = max(64, limit // 4)
    if len(s) <= cap:
        return s
    return s[:cap] + f"...(+{len(s) - cap} more chars)"


# --- the read-only reader (for SI-side consumers) --------------------------
# __SLOT_FALSIFIER_BUS_2026_06_16__
def read_events(
    *,
    bus_path: Path | str | None = None,
    source: str | None = None,
    is_wrong: bool | None = None,
    producer: str | None = None,
    since_ts: float | None = None,
    after_event_id: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    """Read events back (read-only). Empty list if the ledger does not exist.

    Tolerant: a single malformed/torn line is skipped (with a WARNING), never
    raised — a consumer must not die on one bad row. Filters are ANDed.

    Cursor semantics:
      - ``since_ts``: keep events with ``ts > since_ts``.
      - ``after_event_id``: keep events that appear AFTER the row whose
        ``event_id`` matches; if the cursor id is not found, ALL events are
        returned (conservative — a consumer that lost its cursor re-reads
        rather than silently skipping; downstream dedups on ``event_id``).
    """
    path = Path(bus_path) if bus_path is not None else resolve_bus_path()
    if not path.exists():
        return []

    rows: list[dict] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError as _ff_exc:
                    _swallowed(_ff_exc, site="bus.falsifier_bus.read_events:409", category="persist")
                    logger.warning("falsifier_bus: skipped malformed line")
                    continue
    except OSError as exc:
        _swallowed(exc, site="bus.falsifier_bus.read_events:412", category="persist")
        # __SLOT_CENSUS_MESSAGE_MASKED_2026_08_10__ 위와 같은 이유(로그=싱크).
        logger.warning(
            "falsifier_bus read failed: %s", format_exception_for_log(exc)
        )
        return []

    if after_event_id is not None:
        idx = next((i for i, r in enumerate(rows) if r.get("event_id") == after_event_id), None)
        if idx is not None:
            rows = rows[idx + 1:]

    out: list[dict] = []
    for r in rows:
        if source is not None and r.get("source") != source:
            continue
        if producer is not None and r.get("producer") != producer:
            continue
        if is_wrong is not None and bool(r.get("is_wrong")) != is_wrong:
            continue
        if since_ts is not None:
            try:
                if float(r.get("ts", 0.0)) <= float(since_ts):
                    continue
            except (TypeError, ValueError) as _ff_exc:
                _swallowed(_ff_exc, site="bus.falsifier_bus.read_events:433", category="persist")
                continue
        out.append(r)

    if limit is not None and limit >= 0:
        out = out[:limit]
    return out
