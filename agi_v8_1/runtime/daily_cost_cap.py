# __SLOT_TICK_DAILY_CAP_2026_06_20__ multi-turn steering — per-day cost floor.
"""Per-day cumulative cost cap for the tick (multi-turn steering) loop.

The cost caps that already exist (``AGI_V8_SWARM_MAX_USD`` /
``AGI_V8_COST_BUDGET_USD``) are **per-cycle** only. An autonomous tick loop runs
many cycles per day, so a per-cycle cap *multiplies* — N ticks × $cap/cycle —
which violates the cost-as-survival constraint. This module adds the missing
per-**day** cumulative ceiling.

It is read-only over the jail's ``runtime_logs/executor_log.jsonl``: it sums the
real executor spend recorded since local midnight and reports whether another
armed cycle may start. There is intentionally **no enable/disable gate on the
cap itself** — a daily cost floor is always in force wherever the tick loop
consults it; only the cap *value* is configurable
(``AGI_V8_SI_TICK_DAILY_USD_CAP``, default $10). Optionally
(``AGI_V8_COST_CAP_INCLUDE_CROSS_MODEL``, default OFF — see
:func:`cross_model_included`, __SLOT_CAP_CROSS_MODEL_2026_08_19__) the numerator
also sums ``runtime_logs/cross_model_verify_spend.jsonl`` — the same second
ledger ``runtime.episode_ledger.episode_spend()`` already folds into an
episode's real spend.

Conservative by construction:
  - rows with no parseable timestamp are counted as "today" (can't prove old);
  - a missing/empty log reads as $0 spent (normal cold start) but the cap still
    bounds the cycle;
  - a log that EXISTS but fails to read/parse (``OSError``,
    ``UnicodeDecodeError``, malformed JSON) is a different fact from "nothing
    was spent" — under the default-ON ``AGI_V8_COST_CAP_LEDGER_FAILCLOSED``
    gate (__SLOT_R14_COSTCAP_2026_08_17__, mirrors ``tick_runner``'s R13
    ledger-failclosed treatment) that fact denies the next armed cycle instead
    of silently reading as $0 — a corrupted/unreadable ledger must never
    defeat the daily cap. ``=false`` restores the pre-patch byte-identical
    behaviour (a garbled log folds into $0 spent);
  - the boundary is local-calendar midnight (matches the user's "자정 이후 합").
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from agi_v8_1.state.store import read_jsonl

# __SLOT_FAIL_FAST_2026_07_25__ Swallowed failures route through one choke
# point: counted + named always, re-raised under AGI_V8_STRICT_FAIL_FAST.
from agi_v8_1.policy.fail_fast import (
    safe_exception_type_name as _safe_exception_type_name,
    swallowed as _swallowed,
)

# __SLOT_CAP_CROSS_MODEL_2026_08_19__ (path, key) 는 episode_ledger.LEDGERS 에서
# 그대로 재사용한다(SSOT 1곳, 사본 금지) — ``episode_ledger.episode_spend()`` 는
# 이 두 원장의 합을 "에피소드 실지출"로 정의하는데 이 캡은 지금까지 첫 번째
# (executor_log)만 봤다. 2-tuple 언패킹이 곧 계약 검사다: episode_ledger 가 이
# 두 원장 이외의 것을 정의하도록 바뀌면(늘거나 순서가 바뀌면) 조용히 $0 을
# 더하는 대신 여기서 바로 ``ValueError`` 로 터진다(import 시점).
from agi_v8_1.runtime.episode_ledger import LEDGERS as _EPISODE_LEDGERS

# __SLOT_SPEND_RESERVATION_2026_08_19__ 이 캡은 executor_log(+opt-in
# cross-model)만 봐서 "이미 착지한 지출"만 안다 — dispatch **진행 중**에
# reviewer/hetero/재현이 쓰는 돈은 그게 executor_log 에 착지하는 순간까지
# 이 캡에 안 보인다. spend_reservation 모듈이 그 창을 메운다(모듈
# docstring 참조). SSOT 는 그 모듈 하나 — 여기서는 호출만 한다.
from agi_v8_1.runtime import spend_reservation as _spend_reservation
from agi_v8_1.verifier.cross_model_budget import (
    charge_attribution,
    read_spend_rows,
    resolve_ledger_path,
    transaction_charge_rows,
)

DEFAULT_DAILY_CAP_USD = 10.0
_CAP_ENV = "AGI_V8_SI_TICK_DAILY_USD_CAP"
_TS_KEYS = ("timestamp", "timestamp_unix", "ts", "generated_at")
_EXECUTOR_LOG_REL = ("runtime_logs", "executor_log.jsonl")

_EXECUTOR_LEDGER_DEF, _CROSS_MODEL_LEDGER_DEF = _EPISODE_LEDGERS
_CROSS_MODEL_REL = tuple(_CROSS_MODEL_LEDGER_DEF[0].split("/"))
_CROSS_MODEL_KEY = _CROSS_MODEL_LEDGER_DEF[1]

# __SLOT_CAP_CROSS_MODEL_2026_08_19__ 캡의 분자에 cross-model 검증 원장도 더할지
# (default False — opt-in). ``runtime.cost_reconcile`` 의
# ``__SLOT_POD_B_COST_CONSUMED_2026_08_09__`` 주석이 이미 "캡이 읽는 것은
# executor_log 뿐이다"라고 적어놨다 — 두 SSOT가 갈리는 방향은 캡이
# **under-count**(실제보다 헐거움)다. 그런데도 default-OFF 인 이유:
# cross-model 검증은 일부 배포에서만 켜져 있는 별도 스펜드 카테고리라, 이미
# 그 원장에 지출이 쌓인 배포에서 이 게이트를 갑자기 default-ON 으로 켜면
# "어제까지 캡의 관찰 대상이 아니던 돈"이 오늘 갑자기 잡혀 예상 못 한 차단이
# 뜬다 — ``AGI_V8_SI_TICK_DAILY_CAP_STRICT_UNMEASURED``(2026-08-03, 바로 아래)
# 와 같은 "정상 동작 중 흔한 회색지대" 라 그 게이트의 관례(default-OFF
# opt-in)를 따른다. ``=true`` 로 켜면 원장 판독 실패도 기존
# ``AGI_V8_COST_CAP_LEDGER_FAILCLOSED`` fail-closed 사슬에 그대로 얽힌다(원장
# 종류가 하나 늘 뿐 계약은 같다). OFF(기본)는 이 원장을 아예 안 읽는다 —
# ``check()``/``spent_today()`` 의 반환값에 새 키가 하나도 안 생기고 분자도
# 늘지 않는다(byte-identical).
_CROSS_MODEL_INCLUDED_ENV = "AGI_V8_COST_CAP_INCLUDE_CROSS_MODEL"


def cross_model_included() -> bool:
    """True면 :func:`check`/:func:`spent_today` 가 cross-model 원장도 합산한다."""
    return os.environ.get(_CROSS_MODEL_INCLUDED_ENV, "").strip().lower() in (
        "true", "1", "yes", "on",
    )


# __SLOT_R14_COSTCAP_2026_08_17__ 판독 실패("못 읽었다")와 "비어 있음"(파일
# 없음/빈 파일 = 정상 콜드스타트)을 구분할지 — ``runtime/tick_runner.py`` 의
# ``AGI_V8_TICK_LEDGER_FAILCLOSED``/``REASON_LEDGER_READ_FAILED``/
# ``_consumed_ids`` (R13, 같은 날)와 **같은 모양**(같은 관례, 다른 원장). 기본
# ON(안전한 방향): ``OSError``/``UnicodeDecodeError``/JSON 파싱 실패로 오늘 지출
# 원장(``executor_log.jsonl``)을 못 읽으면 "지출 0"으로 접지 않고 이번 캡
# 판정을 거부한다(fail-closed) — "판독 못 했다"가 "지출이 0이다"로 접히면
# 손상되거나 권한이 없는 원장이 일일 비용 캡을 **무력화**하고 실제 돈이 나간다.
# ``=false`` 면 :func:`check` 는 패치 이전과 byte-identical(관대한 삼킴 — 손상
# 원장은 지출 0 으로 조용히 접혀 캡 판정에 들어간다).
_LEDGER_FAILCLOSED = "AGI_V8_COST_CAP_LEDGER_FAILCLOSED"
_LEDGER_FAILCLOSED_FALSY = ("false", "0")

#: :func:`check` 가 원장 판독 실패로 거부할 때 싣는 이유 — ``tick_runner.
#: REASON_LEDGER_READ_FAILED`` 와 같은 문자열(다른 원장, 같은 의미: 파일이
#: 없는 게 아니라 있는데 못 읽음). 값을 두 모듈이 각자 상수로 들되 문자열은
#: 맞춘다 — 원장 종류가 달라 소비자가 서로 갈리므로 import 로 묶지 않는다.
REASON_LEDGER_READ_FAILED = "ledger_read_failed"


def ledger_failclosed_enabled() -> bool:
    """원장 판독 실패를 "지출 미상"으로 fail-closed 할지 (default True).

    ⚠️ ON 이 안전한 방향이다 — OFF 로 낮추면 :func:`_executor_rows` 의 관대한
    예외 삼킴(``except Exception: return []``)이 어떤 후단 검사도 없이 그대로
    :func:`check` 의 분자로 들어간다. 즉 손상되거나 권한이 없는
    ``executor_log.jsonl`` 이 "오늘 지출 0"으로 읽혀 일일 캡이 무력화되고
    실제 돈이 나간다.
    """
    v = os.environ.get(_LEDGER_FAILCLOSED, "true").strip().lower()
    return v not in _LEDGER_FAILCLOSED_FALSY


def cap_usd() -> float:
    """The per-day cap in USD. Invalid/negative env → the default floor."""
    raw = os.environ.get(_CAP_ENV)
    if raw is None:
        return DEFAULT_DAILY_CAP_USD
    try:
        val = float(raw)
    except (TypeError, ValueError) as _ff_exc:
        _swallowed(_ff_exc, site="runtime.daily_cost_cap.cap_usd:43", category="config")
        return DEFAULT_DAILY_CAP_USD
    # A negative cap is nonsensical and would read as "block everything by a
    # weird path"; clamp to 0 so the meaning is explicit (0 = freeze spend).
    return val if val >= 0.0 else DEFAULT_DAILY_CAP_USD


def _row_ts(row: dict[str, Any]) -> float | None:
    for k in _TS_KEYS:
        v = row.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    return None


def day_start_epoch(now_ts: float) -> float:
    """Epoch seconds at local-calendar midnight of the day containing *now_ts*."""
    lt = time.localtime(now_ts)
    midnight = (lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0,
                lt.tm_wday, lt.tm_yday, -1)
    return time.mktime(midnight)


# __SLOT_R14_COSTCAP_2026_08_17__
def _read_ledger_rows(path: Path) -> "tuple[list[dict[str, Any]], str | None]":
    """원장 파일 하나를 읽는다 ⇒ ``(행들, 판독 실패의 안전한 이름 | None)``.

    ⚠️ **첫 번째 값은 항상 리스트다** — 판독 실패에도 ``[]``(원 함수의
    OFF-parity 계약). ``read_jsonl`` 은 파일 없음/빈 파일을 예외 없이 ``[]``
    로 주므로(``state/store.py::read_jsonl`` — 정상 콜드스타트), 그 경우 두
    번째 값은 ``None``. ``OSError``/``UnicodeDecodeError``/JSON 파싱 실패로
    **있는데 못 읽으면** 두 번째 값에 :func:`safe_exception_type_name` 이 매긴
    타입명이 실린다 — :func:`_tally_today`/:func:`check` 가 게이트 ON 일 때만
    그 사실을 fail-closed 판정에 쓴다(이 함수 자신은 게이트를 안 본다 — 사실만
    돌려주고, 그 사실을 어떻게 쓸지는 호출부의 몫이다).

    __SLOT_CAP_CROSS_MODEL_2026_08_19__ 원래 이름은 ``_executor_rows`` 였고
    executor_log 하나만 읽었다 — cross-model 원장도 같은 계약(판독 실패 이름표
    포함)으로 읽어야 해서 경로를 인자로 받게 일반화했다. 이 함수는 모듈
    내부에서만 쓰인다(호출부 감사 완료: ``_tally_today`` 둘) — 시그니처를
    바꿔도 이 모듈 밖 계약은 안 깨진다.
    """
    try:
        return read_jsonl(path), None
    except Exception as _ff_exc:  # noqa: BLE001 — a garbled log must never crash the cap
        _swallowed(_ff_exc, site="runtime.daily_cost_cap._read_ledger_rows:read", category="config")
        return [], _safe_exception_type_name(_ff_exc)


def _executor_rows(
    state_dir: Path | str,
) -> "tuple[list[dict[str, Any]], str | None]":
    """오늘 지출을 셀 executor_log 행들. :func:`_read_ledger_rows` 의 얇은 래퍼
    (경로만 고정) — 계약은 위 docstring 참조."""
    return _read_ledger_rows(Path(state_dir).joinpath(*_EXECUTOR_LOG_REL))


# __SLOT_CAP_READS_UNMEASURED_2026_08_02__ The numerator can be INCOMPLETE, and
# the cap has to admit it.
#
# ``spent_today`` sums ``cost_usd``. A call whose token usage was never observed
# writes ``cost_usd: 0.0`` — indistinguishable, to this sum, from a genuinely
# free turn. So an outage that fails calls after the API already billed them
# does not just lose money, it makes the cap look LOOSER than it is. The spend
# ledger already marks those rows (``output_summary.usage_measured is False``,
# ``enforcement.si_spend_ledger``); until now nothing read the mark.
#
# The cap reads it now. It cannot invent the missing dollars — writing a guessed
# number would be exactly the "unknown recorded as known" failure the marker
# exists to prevent — so it reports the count and declares its own numerator
# incomplete. ``remaining_usd`` is then a *ceiling* on the headroom, not a fact.
# Opting into ``AGI_V8_SI_TICK_DAILY_CAP_STRICT_UNMEASURED`` turns that into a
# hard block; default-OFF, so ``allowed`` is unchanged for every existing caller.
_STRICT_UNMEASURED_ENV = "AGI_V8_SI_TICK_DAILY_CAP_STRICT_UNMEASURED"


def strict_unmeasured() -> bool:
    """True when an unmeasured spend row must block the next armed cycle."""
    return os.environ.get(_STRICT_UNMEASURED_ENV, "").strip().lower() in (
        "true", "1", "yes", "on",
    )


def _row_is_unmeasured(row: dict[str, Any]) -> bool:
    """True only for a row that EXPLICITLY declared its usage unobserved.

    Absence of the marker is not evidence of anything (most executors never
    supply it), so it is not counted — this measures declared ignorance, never
    infers it. 마커가 **없는** 행은 :func:`_row_is_unmarked` 로 따로 센다.
    """
    out = row.get("output_summary")
    return isinstance(out, dict) and out.get("usage_measured") is False


def _row_is_unmarked(row: dict[str, Any]) -> bool:
    """마커를 아예 안 단 지출 행.

    __SLOT_CAP_COUNTS_ITS_OWN_DENOMINATOR_2026_08_03__ 마커 부재를 무지로
    추론하지 않는 것은 옳다. 하지만 그 부재를 **세지도 않으면** 새로 추가된
    원장 기록자가 마커를 잊어도 아무 표면에 안 드러난다 — 실제로
    ``tick_review`` 가 그랬다. 그래서 "선언된 무지"(unmeasured)와 "선언 자체가
    없음"(unmarked)을 **분리해서 센다**. 판정에는 쓰지 않고 보고만 한다.
    """
    if not isinstance(row.get("cost_usd"), (int, float)):
        return False
    out = row.get("output_summary")
    return not isinstance(out, dict) or "usage_measured" not in out


def _row_is_price_model_mismatch(row: dict[str, Any]) -> bool:
    """단가가 붙은 모델과 **답한** 모델이 갈린 행.

    __SLOT_SERVED_MODEL_IS_THE_PRICED_MODEL_2026_08_09__ 이 행의 ``cost_usd`` 는
    *요청한* 모델 단가로 매긴 값이다(원장이 그 사실을 ``price_model_mismatch`` 로
    적는다 — `enforcement.si_spend_ledger`). deepseek pro↔flash 는 12.4배 차이라
    한 행이 그만큼 틀릴 수 있고, 방향이 나쁜 쪽(요청=싼 모델)이면 분자가
    **과소** 계상되어 캡이 헐거워진다.

    ⛔ 판정에는 쓰지 않는다 — 세기만 한다. ``_row_is_unmarked`` 와 같은 이유다:
    새로 생긴 종류의 무지가 어느 표면에도 안 드러나면 다음 세션이 또 발견한다.
    강제는 원장 쪽 default-OFF 게이트(``price_model_strict``)가 이 행의
    ``usage_measured`` 를 False 로 내리는 경로로만 일어나고, 그러면 이 행은
    ``unmeasured`` 로도 세어진다(두 칸에 다 뜬다 — 겹침이지 이중계상이 아니다,
    둘은 서로 다른 질문의 답이다).
    """
    out = row.get("output_summary")
    return isinstance(out, dict) and out.get("price_model_mismatch") is True


# __SLOT_UNMEASURED_ACK_2026_08_03__ STRICT 게이트의 탈출구.
#
# STRICT 가 ON 이면 미측정 행 **하나**가 그 로컬 하루 전체를 막는다. 원장은
# append-only 라 자정까지 ``allowed=False`` 가 고정되고, 그 뒤 아무리 완벽히
# 측정된 행이 쌓여도 복구되지 않는다. 트리거는 흔하다 — provider 예외 한 번이면
# 미측정 행이 생긴다. 즉 네트워크 글리치 하나가 자율 루프를 최대 24시간 세우고,
# 운영자가 env 를 내리거나 **로그를 손대는 것** 말고는 해제 수단이 없었다.
# 둘 다 나쁘다: env 를 내리면 방어가 통째로 꺼지고, 로그를 고치면 원장이 원장이 아니다.
#
# 대신 **기록되는 승인**을 둔다. 운영자가 ack 행을 남기면 그 시각 이전의 미측정
# 행은 "사람이 보고 넘어가기로 했다"가 되어 판정에서 빠진다. fail-closed 는 그대로고
# (기본은 차단), 탈출은 명시적이고 원장에 남으며 누가 언제 왜 넘어갔는지가 보인다.
_ACK_EXECUTOR = "spend_unmeasured_ack"


def _ack_through_ts(rows: "list[dict[str, Any]]", start: float) -> float:
    """오늘 남은 ack 중 가장 나중의 ``ack_through_ts`` (없으면 하루 시작 이전)."""
    best = start - 1.0
    for r in rows:
        if r.get("executor") != _ACK_EXECUTOR:
            continue
        ts = r.get("ack_through_ts")
        if isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts > best:
            best = float(ts)
    return best


def _tally_today(state_dir: Path | str, now: float) -> dict[str, Any]:
    """One pass over today's rows → 오늘의 분자와 **그 분자에 대해 아는 것**."""
    start = day_start_epoch(now)
    rows, ledger_read_failed = _executor_rows(state_dir)
    ack_through = _ack_through_ts(rows, start)
    total = 0.0
    considered = unmeasured = unmeasured_acked = unmarked = 0
    price_mismatch = 0
    for r in rows:
        if r.get("executor") == _ACK_EXECUTOR:
            continue
        ts = _row_ts(r)
        if ts is not None and ts < start:
            continue
        considered += 1
        c = r.get("cost_usd")
        if isinstance(c, (int, float)) and not isinstance(c, bool):
            total += float(c)
        if _row_is_unmeasured(r):
            # ts 를 못 읽는 행은 ack 대상이 아니다(언제 것인지 모르므로).
            if ts is not None and ts <= ack_through:
                unmeasured_acked += 1
            else:
                unmeasured += 1
        elif _row_is_unmarked(r):
            unmarked += 1
        # ⚠️ elif 사슬에 넣지 않는다 — 이건 marker 3분법과 **직교하는** 질문이다.
        if _row_is_price_model_mismatch(r):
            price_mismatch += 1
    out = {
        "spent": round(total, 6),
        "unmeasured": unmeasured,
        "unmeasured_acked": unmeasured_acked,
        "price_model_mismatch": price_mismatch,
        "unmarked": unmarked,
        "rows_considered": considered,
        # __SLOT_R14_COSTCAP_2026_08_17__ ``None`` = 정상 판독(빈 원장 포함).
        # 안전한 예외 타입명 문자열 = 원장을 못 읽었다(파일 없음이 아님) — 위
        # 합계는 그 실패한 읽기에서 나온 ``[]`` 로 계산됐으므로 **분자가
        # 미상**이다. :func:`check` 가 게이트 ON 일 때 이 값을 fail-closed
        # 판정에 쓴다.
        "ledger_read_failed": ledger_read_failed,
    }
    # __SLOT_CAP_CROSS_MODEL_2026_08_19__ default OFF: 이 원장을 아예 안 읽는다,
    # ``out`` 에 새 키가 안 생긴다, ``spent`` 도 executor_log 만의 값 그대로
    # (byte-identical). ON 이면 오늘 창의 cross-model ``usd`` 를 같은 자정
    # 경계로 분자에 더한다 — executor_log 전용 마커(unmeasured/unmarked/
    # price_mismatch) 는 이 원장의 행 모양이 달라(``kind``/``model``/``detail``,
    # 스키마는 ``cost_reconcile.py`` 참조) 적용하지 않는다(세지 않는다).
    if cross_model_included():
        try:
            cm_rows = read_spend_rows(resolve_ledger_path(state_dir))
            cm_read_failed = None
        except Exception as exc:  # noqa: BLE001 — strict parse failure must deny
            _swallowed(
                exc,
                site="runtime.daily_cost_cap._tally_today:cross_model_read",
                category="config",
            )
            cm_rows = []
            cm_read_failed = _safe_exception_type_name(exc)
        cm_total = 0.0
        cm_considered = 0
        cm_window_charges: list[dict[str, Any]] = []

        # Keep this API as a physical-row count even though dollars below are
        # transaction-attributed.
        for r in cm_rows:
            ts = _row_ts(r)
            if ts is not None and ts < start:
                continue
            cm_considered += 1

        try:
            cm_charges = transaction_charge_rows(cm_rows)
        except Exception as exc:  # noqa: BLE001 — semantic damage must deny
            _swallowed(
                exc,
                site="runtime.daily_cost_cap._tally_today:cross_model_attribution",
                category="config",
            )
            cm_charges = []
            cm_read_failed = _safe_exception_type_name(exc)

        for charge in cm_charges:
            ts = _row_ts(charge)
            if ts is not None and ts < start:
                continue
            cm_window_charges.append(charge)
            value = charge.get(_CROSS_MODEL_KEY)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                cm_total += float(value)
        out["spent"] = round(total + cm_total, 6)
        out["cross_model_spent"] = round(cm_total, 6)
        out["cross_model_rows_considered"] = cm_considered
        out["cross_model_read_failed"] = cm_read_failed
        out["cross_model_attribution"] = charge_attribution(cm_window_charges)
    return out


def spent_today(state_dir: Path | str, *, now_ts: float | None = None) -> float:
    """Sum of the executor ``cost_usd`` KNOWN since local midnight.

    Rows without a parseable timestamp are counted (conservative). A row with a
    non-numeric ``cost_usd`` contributes 0.

    __SLOT_CAP_READS_UNMEASURED_2026_08_02__ This is a lower bound, not the
    whole bill: a row that declared its usage unobserved contributes its
    recorded ``0.0``. Use :func:`unmeasured_today` (or ``check()``'s
    ``spend_is_complete``) to learn whether that happened today.
    """
    now = float(now_ts) if now_ts is not None else time.time()
    return _tally_today(state_dir, now)["spent"]


def unmeasured_today(state_dir: Path | str, *, now_ts: float | None = None) -> int:
    """How many of today's rows admit their spend was never observed.

    운영자 ack 로 넘어간 행은 빠진다(``check()`` 의 ``unmeasured_acked_rows``).
    """
    now = float(now_ts) if now_ts is not None else time.time()
    return _tally_today(state_dir, now)["unmeasured"]


def ack_unmeasured(
    state_dir: Path | str, *, reason: str, through_ts: float | None = None,
    now_ts: float | None = None,
) -> dict[str, Any]:
    """미측정 지출을 **운영자 판단으로** 넘긴다 — 원장에 기록하면서.

    __SLOT_UNMEASURED_ACK_2026_08_03__ STRICT 차단의 유일한 정당한 해제 경로다.
    지출을 지우지도, 없던 일로 만들지도 않는다(``spent_usd`` 는 그대로) — 다만
    "이 시각까지의 미측정 행은 사람이 보고 진행하기로 했다"를 원장에 남긴다.
    ``reason`` 은 필수이고 최소 길이가 있다: 이유 없는 해제는 env 를 내리는 것과
    같아서 기록의 의미가 없다.
    """
    if len(str(reason).strip()) < 12:
        raise ValueError("ack 사유는 최소 12자 — 무엇을 확인했는지 적어야 기록이 된다")
    now = float(now_ts) if now_ts is not None else time.time()
    through = float(through_ts) if through_ts is not None else now
    row = {
        "executor": _ACK_EXECUTOR,
        "ts": now,
        "ack_through_ts": through,
        "reason": str(reason).strip(),
        "schema_version": "agi_v8_unmeasured_ack_v1",
    }
    path = Path(state_dir).joinpath(*_EXECUTOR_LOG_REL)
    path.parent.mkdir(parents=True, exist_ok=True)
    from agi_v8_1.state.store import atomic_append_jsonl

    atomic_append_jsonl(path, row)
    return row


def remaining(state_dir: Path | str, *, now_ts: float | None = None) -> float:
    """Headroom left under today's cap (never negative)."""
    return max(0.0, round(cap_usd() - spent_today(state_dir, now_ts=now_ts), 6))


def check(state_dir: Path | str, *, now_ts: float | None = None) -> dict[str, Any]:
    """Decide whether another armed cycle may start under the per-day cap.

    Returns a JSON-able verdict dict. ``allowed`` is False once the day's
    recorded spend reaches (>=) the cap; a cap of 0 blocks unconditionally.

    __SLOT_CAP_READS_UNMEASURED_2026_08_02__ ``unmeasured_rows`` /
    ``spend_is_complete`` report whether today's numerator is whole. When it is
    not, ``spent_usd`` is a lower bound and ``remaining_usd`` an upper bound —
    the verdict says so rather than presenting a partial sum as the total.
    ``allowed`` is only affected under the default-OFF
    ``AGI_V8_SI_TICK_DAILY_CAP_STRICT_UNMEASURED`` gate, so every existing
    caller keeps its decision unchanged.
    """
    now = float(now_ts) if now_ts is not None else time.time()
    cap = cap_usd()
    t = _tally_today(state_dir, now)
    spent, unmeasured = t["spent"], t["unmeasured"]
    rem = max(0.0, round(cap - spent, 6))
    allowed = spent < cap
    complete = unmeasured == 0
    if allowed:
        reason = "ok" if complete else "ok_spend_incomplete"
    else:
        reason = "daily_cap_reached"
    if allowed and not complete and strict_unmeasured():
        allowed = False
        reason = "unmeasured_spend"

    # __SLOT_R14_COSTCAP_2026_08_17__ 원장 판독 자체가 실패했나(파일 없음/빈
    # 파일과 다른 사실 — ``_tally_today``/``_executor_rows`` 참조). 있으면
    # 이게 **최종 발언권**을 가진다: 위 판정이 무엇이었든(``spent`` 는 실패한
    # 읽기에서 나온 ``[]`` 로 계산된 미상의 값이므로) "지출 미상"은 "지출
    # 허용"을 뒤집는다(fail-closed) — 손상되거나 권한이 없는 원장이 캡을
    # 무력화하고 실제 돈이 나가는 사슬을 끊는다.
    #
    # ⛔ **영구 브릭 아님**: 다음 호출이 같은 검사를 다시 돌린다. 손상된 파일이
    #    수리/교체/삭제되면(``read_jsonl`` 이 다시 예외 없이 읽으면) 그 다음
    #    호출부터 자동으로 다시 허용된다 — 코드 재배포도, 게이트 조작도 필요
    #    없다. 여기서 파일을 지우거나 고치지 않는다(사실만 판정한다).
    #
    # OFF(``AGI_V8_COST_CAP_LEDGER_FAILCLOSED=false``)면 이 블록은 아예 안
    # 돌고 위 판정이 그대로 나간다 — 반환 딕셔너리에 ``ledger_read_failed``
    # 키조차 안 생긴다(패치 이전과 byte-identical: 손상 원장은 지출 0 으로
    # 조용히 접힌 채 판정된다).
    ledger_read_failed = t["ledger_read_failed"]
    out = {
        "allowed": allowed,
        "spent_usd": spent,
        "cap_usd": cap,
        "remaining_usd": rem,
        "unmeasured_rows": unmeasured,
        "spend_is_complete": complete,
        # __SLOT_CAP_COUNTS_ITS_OWN_DENOMINATOR_2026_08_03__ ``spend_is_complete``
        # 만 보면 "선언된 무지가 없다"가 "분자가 온전하다"로 승격된다. 그 값은
        # 행이 **0개**인 하루(원장 게이트 OFF=기본값 / executor log 비활성 /
        # 젤이 아예 없음)에서도 True 다. 그래서 무엇을 보고 그렇게 말하는지를
        # 같이 싣는다 — 0 이 "깨끗함"인지 "안 봤음"인지 구분하는 것은 이 세션이
        # 오라클 앵커에서 배운 것과 같은 규칙이다(``verified``).
        "rows_considered": t["rows_considered"],
        "unmarked_rows": t["unmarked"],
        "unmeasured_acked_rows": t["unmeasured_acked"],
        # __SLOT_SERVED_MODEL_IS_THE_PRICED_MODEL_2026_08_09__ 이 분자 안에
        # **다른 모델 단가로 매긴 금액**이 몇 행 섞였나. 보고만 하고 ``allowed`` 는
        # 안 건드린다(강제는 원장의 default-OFF 게이트 경로로만).
        "price_model_mismatch_rows": t["price_model_mismatch"],
        "day_start": day_start_epoch(now),
        "now_ts": now,
        "reason": reason,
    }
    if ledger_failclosed_enabled():
        # __SLOT_R14_COSTCAP_2026_08_17__ 게이트가 이 필드의 존재 자체를
        # 지킨다 — OFF 에서는 아래 키가 반환 딕셔너리에 안 생기고 ``allowed``/
        # ``reason`` 도 위 값 그대로 나간다(byte-identical). ON 이면 항상
        # 싣는다(실패가 없을 때도 ``None`` — 스키마가 안정적이어야 관측
        # 소비자가 매번 ``.get`` 없이 이 키를 믿을 수 있다).
        out["ledger_read_failed"] = ledger_read_failed
        if ledger_read_failed:
            out["allowed"] = False
            out["reason"] = REASON_LEDGER_READ_FAILED
    # __SLOT_CAP_CROSS_MODEL_2026_08_19__ OFF(기본)면 이 블록 전체가 안 돈다 —
    # 위 키들에 cross_model_* 이 하나도 안 생기고 ``allowed``/``reason`` 도
    # 그대로(byte-identical). ON 이면 ``spent``/``remaining_usd`` 는 이미
    # ``_tally_today`` 에서 cross-model 원장을 더한 값이고, 여기서는 그 내역
    # (``cross_model_spent_usd``/``cross_model_rows_considered``)과 판독 실패
    # 여부만 보탠다. cross-model 원장 판독 실패도 executor_log 판독 실패와
    # **같은 fail-closed 사슬**(``ledger_failclosed_enabled()``)에 얽힌다 —
    # 원장 종류가 하나 늘 뿐 "손상/판독불가 원장이 캡을 무력화하면 안 된다"는
    # 계약은 그대로다.
    if cross_model_included():
        cm_read_failed = t.get("cross_model_read_failed")
        out["cross_model_spent_usd"] = t.get("cross_model_spent", 0.0)
        out["cross_model_rows_considered"] = t.get("cross_model_rows_considered", 0)
        out["cross_model_read_failed"] = cm_read_failed
        out["cross_model_attribution"] = t.get("cross_model_attribution")
        if cm_read_failed and ledger_failclosed_enabled():
            out["allowed"] = False
            out["reason"] = REASON_LEDGER_READ_FAILED

    # __SLOT_SPEND_RESERVATION_2026_08_19__ OFF(기본)면 이 블록 전체가 안 돈다 —
    # 위 키들에 reservation_* 이 하나도 안 생기고 ``allowed``/``reason`` 도
    # 그대로(byte-identical). ON 이면 판정 축에 "아직 executor_log 에 안
    # 착지했지만 지금 열려 있는 예약"을 더한다.
    #
    # ⚠️ **``spent_usd``/``remaining_usd`` 자체는 절대 안 건드린다** — 그건
    # "실지출"이라는 뜻을 그대로 유지해야, 이 블록이 없던 시절의 소비자(다른
    # 원장/리포트)가 계속 같은 숫자를 본다. 여기서 바꾸는 것은 ``allowed``/
    # ``reason`` 뿐이고, 오직 **True → False 방향으로만** 뒤집는다(이미 다른
    # 이유로 False 인 판정을 다시 True 로 되돌리지 않는다).
    #
    # 이중계상 — 정산 후엔 없음, 진행 중엔 있을 수 있음(방향은 항상 안전):
    # ``open_reservations()`` 는 ``status=="open"``(아직 settle/release 안 됨)인
    # 예약만 합산한다 — **정산이 끝나면** 그 돈은 executor_log 에 착지해
    # ``spent`` 축이 이어받고, 이 예약은 원장에서 "closed"로 읽혀 더 이상 여기
    # 안 잡힌다(이 방향은 구조적으로 보장).
    #
    # ⚠️ 2026-08-19 적대검증 수리 — 정정: 예약이 **아직 열려 있는 동안**은 이
    # 보장이 없다. 그 예약이 감싸는 dispatch 자신의 서브콜(reviewer/hetero/
    # 재현)이 settle() 전에 자기 지출을 executor_log 에 먼저 착지시킬 수
    # 있어서, 그 창에서는 같은 달러가 ``spent_usd``(이미 착지)와
    # ``open_reservations_usd``(예약의 전체 expected_usd, 감액 없음) 양쪽에
    # 동시에 잡힌다 — ``spend_reservation.py`` 모듈 docstring "이중계상" 절
    # 참조. 방향은 항상 안전 측(과대평가, 캡을 더 일찍 잠글 뿐 뚫리게는 안
    # 함)이고, 정산되면 스스로 바로잡는다. ``spent_plus_reserved_usd >=
    # spent_usd`` 는 ``open_usd>=0`` 이면 항상 참인 항등식이라 그 자체로는
    # "이중계상 부재"의 증명이 아니다 — 실제 안전성(과소평가 없음)의 증거는
    # ``test_cap_gate_on_own_open_reservation_mid_flight_sub_spend_double_counts_but_stays_safe_direction``
    # (진행 중 시나리오를 직접 재현) 쪽을 본다.
    if _spend_reservation.enabled():
        _res = _spend_reservation.open_reservations(state_dir, now_ts=now)
        open_usd = _res["open_usd"]
        spent_plus_reserved = round(out["spent_usd"] + open_usd, 8)
        out["open_reservations_usd"] = open_usd
        out["open_reservations_count"] = _res["open_count"]
        out["spent_plus_reserved_usd"] = spent_plus_reserved
        out["reservation_ledger_read_failed"] = _res["read_failed"]
        if _res["read_failed"] and ledger_failclosed_enabled():
            out["allowed"] = False
            out["reason"] = REASON_LEDGER_READ_FAILED
        elif out["allowed"] and spent_plus_reserved >= cap:
            out["allowed"] = False
            out["reason"] = "daily_cap_reached_with_reservations"
    return out


__all__ = [
    "DEFAULT_DAILY_CAP_USD",
    "REASON_LEDGER_READ_FAILED",
    "ack_unmeasured",
    "cap_usd",
    "cross_model_included",
    "day_start_epoch",
    "ledger_failclosed_enabled",
    "spent_today",
    "unmeasured_today",
    "strict_unmeasured",
    "remaining",
    "check",
]
