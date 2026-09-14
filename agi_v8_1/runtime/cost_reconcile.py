# __SLOT_COST_RECONCILE_W3_2026_07_31__ first-run W3 — internal-ledger ↔ dashboard diff.
"""Reconcile internal spend ledgers against the provider dashboard (spec §2 (e)).

READ-ONLY over two jail ledgers (no gate needed — this module writes nothing):

  1. ``runtime_logs/executor_log.jsonl``   — ``cost_usd`` rows (all executors,
     incl. the P2 ``si_llm_propose`` / ``tick_review`` estimates)
  2. ``runtime_logs/cross_model_verify_spend.jsonl`` — ``usd`` rows (the
     cross-model reviewer's own durable ledger)

The provider dashboard number is NOT machine-readable (DeepSeek/Anthropic
consoles have no billing API wired here), so it is a manual CLI input:
``--dashboard-usd``.

Exit contract (2026-08-09 정정 — 초판 서술은 "인자 없으면 무조건 exit 0" 과
"대칭 허용오차 초과면 exit 1" 을 주장했는데 둘 다 낡았다. 코드가 정본:
``main`` 은 ``0 if out["ok"] else 1`` 이고 ``ok`` 는 두 팔의 AND 다):

  * 대시보드 팔 — ``--dashboard-usd`` 를 줬을 때만 돈다. **비대칭**이다:
    ``internal - dashboard < -tolerance`` (지출이 미기록되는 심각한 방향)만
    실패다. internal 이 tolerance 이상 **커도** exit 0 — 추정치는 의도적
    과계상이라 note 로만 경고한다.
  * pod B 대조 팔 (:func:`pod_b_crosscheck`) — **항상** 돈다. ``--dashboard-usd``
    없이도 이 팔이 깨지면 exit 1 이다.
  * 둘 다 조용하면 exit 0 (인자 없으면 내부 내역만 찍는다). 그 nonzero exit 이
    DoD (e) 실패 신호다.

Honest-accounting note: P2 rows are token×price ESTIMATES that deliberately
over-count cache-hit input. A small positive (internal > dashboard) diff is the
expected direction; internal < dashboard means something is NOT being recorded
— treat that as the serious direction.

__SLOT_POD_B_COST_CONSUMED_2026_08_09__ 세 번째 팔 — **pod B 대조**.

``bridge/si_evidence.py`` 는 사이클마다 ``runtime_logs/si_swarm_evidence.jsonl`` 에
``pod_b_cost_usd`` 를 적는다. 같은 달러가 ``bridge/hetero_pod.py:_record_spend`` 를
거쳐 ``executor_log.jsonl`` 에 ``executor="hetero_pod"`` 행으로도 적힌다. **캡이 읽는
것은 후자뿐이다**(``daily_cost_cap`` 은 executor_log 만 본다).

두 기록은 같은 ``usage`` 에서 나오지만 **쓰기 경로가 다르고 둘 다 독립적으로 실패할
수 있다** — ``_record_spend`` 의 except 는 ``_swallowed`` 라 조용하다. 그 팔이 죽으면
캡의 분자에서 pod B 달러가 사라지는데, 지금까지 **두 값을 비교하는 코드가 0개**여서
아무 표면에도 안 드러났다. 여기가 그 비교다.

⛔ ``pod_b_cost_usd`` 를 ``internal_total_usd`` 에 **더하지 않는다** — hetero_pod 행은
   이미 ``executor_spend`` 에 들어 있어서 더하면 **이중계상**이다. 이 팔은 합계가
   아니라 **대조**다.
⚠️ 허용오차는 대시보드 것($0.01)을 쓰지 않는다. 이 둘은 같은 숫자에서 나온 내부 원장
   두 개라 정당한 오차는 행당 ``round(x, 6)`` 뿐이다 — $0.01 을 쓰면 통째로 빠진
   ~$0.0018 짜리 행 하나가 오차에 묻힌다(임계는 분모가 정한다).

__SLOT_R11_COST_2026_08_17__ Round 11 COST — 대조 장치 자체의 두 구멍.

1. ``_rows()`` 가 원장 판독 **실패**(손상된 JSON, 권한 오류 등)를 조용히 빈 목록
   `[]` 로 접었다 — "지출이 없었다"와 "원장을 못 읽었다"가 같은 숫자가 된다.
   ⛔ ``read_jsonl`` 자신은 "파일이 아예 없음"을 이미 정당한 `[]` 로 처리한다
   (그 파일의 계약) — 여기서 새로 잡는 것은 **파일은 있는데 못 읽은** 경우뿐이다.
   "없다"·"손상됐다"·"비었다"는 서로 다른 사실이고 이 셋을 접지 않는 것이 이
   트랙의 주제다.
2. 대시보드 값이 없을 때 내부 원장(4팔: executor_log/cross_model/si_evidence/
   hetero_pod)이 **전부** 0행이어도 기존 코드는 ``ok=True`` 로 끝났다 — 아무것도
   안 봤는데 "대조했다"가 된다.

게이트 ``AGI_V8_COST_RECONCILE_STRICT``(기본 true)가 이 두 구멍을 닫는다:

  * 판독 실패는 ``read_error`` 로 이름 붙여 각 팔의 리포트에 남고, ``reconcile()``
    은 이를 모아 ``read_errors`` 로 노출한다. 하나라도 있으면 ``status`` 는
    ``EVALUATOR_ERROR`` 이고 ``ok`` 는 무조건 False — 손상된 원장이 깨끗한 통과로
    보이면 안 된다.
  * 대시보드가 없고 네 팔이 전부 0행이면 ``status`` 는 ``EVALUATOR_ERROR`` 가
    아니라 ``UNMEASURED`` (판독은 성공했지만 잴 것이 없었다는 뜻 — 다른 사실),
    ``ok`` 는 False. 대시보드가 주어졌으면 내부가 비어도 대시보드와 비교할
    앵커가 있으므로 UNMEASURED 아님(기존 diff 판정 그대로).
  * ``status`` 어휘는 이 레포가 이미 쓰는 5상태(``core/axis_scorer.py``:
    ``DIAG_PASS/DIAG_FAIL/DIAG_UNMEASURED/DIAG_EVALUATOR_ERROR/DIAG_INELIGIBLE``)
    에서 그대로 빌린다 — 새 어휘를 발명하지 않는다. ``INELIGIBLE`` 은 "짝이 비대칭
    으로 비었다"는 axis_scorer 고유 구조라 여기엔 자연스러운 대응이 없어 안 쓴다.
  * ``pod_b_crosscheck()`` 자신의 ``ok``/``note`` 는 **건드리지 않는다** — 그 팔은
    2026-08-09 계약(빈 두 팔은 "비교할 게 없다"는 로컬 판단으로 ok=True 유지)이
    이미 테스트로 고정돼 있다. 이 트랙이 여는 것은 ``reconcile()`` 최상위가 그
    로컬 판단을 **네 팔 전체 시야 없이** 그대로 물려받던 지점뿐이다.
  * OFF 는 패치 이전과 byte-identical — 새로 추가되는 키(``read_error``,
    ``read_errors``, ``status``, ``logged``)는 전부 게이트 뒤에 있다. ``ok`` 값도
    OFF 에서는 예전 계산 그대로.

⚠️ 자기비판(리포트에 상술): 이 수리는 이 CLI 를 더 자주 fail-closed 시킨다. 하지만
   ``main()`` 의 exit code 를 프로그램적으로 소비하는 자동 파이프라인은 없다(수동
   DoD (e) 체크 도구) — ``executor_spend``/``cross_model_spend`` 는 라이브에서
   ``budget_governor.py`` 가 쓰지만 그 호출부는 ``["total_usd"]`` 키만 읽고 자체
   try/except 로 감싸여 있어 이 변경(추가 키, 내부 반환 타입) 의 영향을 받지 않는다.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import os

from agi_v8_1.bridge.si_evidence import LEDGER_REL as _SI_EVIDENCE_REL
from agi_v8_1.policy.fail_fast import (
    safe_exception_type_name as _safe_exception_type_name,
    swallowed as _swallowed,
)
# __SLOT_R11_COST_2026_08_17__ 5상태 어휘는 여기서 발명하지 않는다 — 이 레포가
# 이미 쓰는 곳(core/axis_scorer.py, Round 6 P0 #2)의 상수를 그대로 빌린다.
from agi_v8_1.core.axis_scorer import (
    DIAG_EVALUATOR_ERROR as _DIAG_EVALUATOR_ERROR,
    DIAG_FAIL as _DIAG_FAIL,
    DIAG_PASS as _DIAG_PASS,
    DIAG_UNMEASURED as _DIAG_UNMEASURED,
)
# __SLOT_MISMATCH_ROWS_CONSUMED_2026_08_09__ 술어는 캡의 것을 그대로 쓴다 —
# "단가가 붙은 모델과 답한 모델이 갈린 행"이라는 **같은 사실**을 두 소비자
# (캡=오늘 창, 이 리포트=since_ts 창)가 읽는다. 사본을 두면 검사 안 받는 쪽이
# 썩는다(private 이름 import 는 그 대가다).
from agi_v8_1.runtime.daily_cost_cap import (
    _row_is_price_model_mismatch,
    day_start_epoch,
)
from agi_v8_1.state.path_guard import resolve_sink
from agi_v8_1.state.store import read_jsonl
from agi_v8_1.verifier.cross_model_budget import (
    charge_attribution,
    read_spend_rows,
    resolve_ledger_path,
    transaction_charge_rows,
)

_EXECUTOR_LOG_REL = ("runtime_logs", "executor_log.jsonl")
_CROSS_MODEL_REL = ("runtime_logs", "cross_model_verify_spend.jsonl")
# The writers honour these overrides (executor_log._resolve_log_path /
# cross_model_budget._resolve_ledger_path). Reading the fixed jail-relative
# path while a writer is redirected would silently reconcile against the
# WRONG file (adversarial-review finding) — so the same precedence applies
# here, and the output names any override in ``path_overrides``.
_EXECUTOR_LOG_ENV = "AGI_V8_EXECUTOR_LOG_PATH"
_CROSS_MODEL_ENV = "AGI_V81_CROSS_MODEL_VERIFY_LEDGER"
DEFAULT_TOLERANCE_USD = 0.01

# __SLOT_R11_COST_2026_08_17__ 기본 ON — read-failure-as-empty / measured-nothing
# -as-ok 두 구멍을 닫는 게이트. OFF 는 패치 이전과 byte-identical(테스트로 증명).
_STRICT_ENV = "AGI_V8_COST_RECONCILE_STRICT"


def _strict_enabled() -> bool:
    raw = os.environ.get(_STRICT_ENV, "true")  # tier: T4
    return (raw or "true").strip().lower() in ("true", "1", "yes", "on")

# __SLOT_POD_B_COST_CONSUMED_2026_08_09__ pod B 대조 상수.
# ``executor`` 이름은 ``bridge/hetero_pod.py:_record_spend`` 가 적는 문자열이다.
_HETERO_EXECUTOR = "hetero_pod"
#: __SLOT_OPENROUTER_LANE_POD_A_2026_08_22__ pod A 는 별도 executor 이름으로
#: 분리 기록된다(bridge/hetero_pod.py:_record_spend — pod B 는 옛 행·소비자
#: 연속성 때문에 무접미 이름을 유지). 이 분리가 없으면 pod A 지출이
#: :func:`pod_b_crosscheck` 의 logged 팔에 합산돼 대조기가 눈을 잃는다
#: (적대검증 HIGH 재현).
_HETERO_EXECUTOR_POD_A = "hetero_pod_a"
#: 행당 정당한 오차. 생산자는 ``round(cost, 6)`` 한 값을 증거 원장에 적고
#: executor_log 에는 반올림 전 값이 간다 ⇒ 행당 최대 5e-7. 1e-6/행은 그 상한이다.
#: ⚠️ 이 값을 키우면 통째로 누락된 행이 오차에 묻힌다 — 실측 pod B 행은 ~$0.0015.
POD_B_ROW_TOLERANCE_USD = 1e-6


def _executor_log_path(state_dir: Path | str) -> Path:
    # __SLOT_LEDGER_SINK_2026_08_08__ 리더는 라이터와 **같은 리졸버**를 타야 한다.
    # 술어도 라이터(executor_log._resolve_log_path 의 원시 ``if override:``)와 같게
    # 둔다 — 여기만 ``.strip()`` 하면 공백만 값에서 라이터는 핀을 쓰고 리더는
    # state_dir 기본값을 읽어 **다른 파일**을 대조한다.
    override = os.environ.get(_EXECUTOR_LOG_ENV)
    if override:
        return resolve_sink(override, what="executor log")
    return Path(state_dir).joinpath(*_EXECUTOR_LOG_REL)


def _cross_model_path(state_dir: Path | str) -> Path:
    # __SLOT_LEDGER_SINK_2026_08_08__ writer와 소비자가 같은
    # override/state-root 선택을 쓴다. 술어를 복사하지 않는다.
    return resolve_ledger_path(state_dir)


def _rows(path: Path) -> tuple[list[dict[str, Any]], str | None]:
    """Read a ledger jsonl. Returns ``(rows, read_error)``.

    __SLOT_R11_COST_2026_08_17__ ``read_jsonl`` itself already treats a
    MISSING file as legitimately empty (its own contract, no exception) — a
    jail that never wrote this ledger yet still reads as ``([], None)``,
    same as always. ``read_error`` is non-None ONLY when the file EXISTS but
    reading/parsing it raised (malformed JSON line, permission error, …) —
    that is a DIFFERENT fact from "nothing was spent" and callers under the
    STRICT gate must not fold the two together.
    """
    try:
        return read_jsonl(path), None
    except Exception as exc:  # noqa: BLE001 — a garbled ledger reads as empty, but now SAYS SO
        _swallowed(exc, site="runtime.cost_reconcile._rows:read", category="telemetry")
        # __SLOT_R11_COST_TYPEONLY_2026_08_17__ 🔴 **예외 원문을 넣지 않는다.**
        # ``OSError``/``PermissionError`` 의 ``str(exc)`` 는 **실패한 절대경로를
        # 그대로 담는다**. 이 값은 대조 결과에 실려 원장/리포트로 흘러가므로,
        # 원문을 담으면 사설 경로가 산출물에 새고(공개 경계 결함) 이 레포의
        # non-logger 예외 sink 규율(원문 금지, 타입만)도 어긴다 — 같은 날 아침
        # 래칫에 정당화한 7개 sink 전부 ``type(exc).__name__`` 만 쓴다.
        # 부차 효과로 ``str(exc)`` 가 스스로 raise 하는 병적 예외에도 안전해진다.
        return [], type(exc).__name__


def _row_ts(row: dict[str, Any]) -> float | None:
    for k in ("ts", "timestamp", "timestamp_unix", "generated_at"):
        v = row.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    return None


def executor_spend(state_dir: Path | str, *, since_ts: float | None = None) -> dict[str, Any]:
    """Sum executor-log ``cost_usd`` (total + per-executor). Conservative:
    rows without a parseable timestamp are INCLUDED when filtering by time.

    __SLOT_MISMATCH_ROWS_CONSUMED_2026_08_09__ ``price_model_mismatch_rows`` —
    이 합 안에 **다른 모델 단가로 매긴 금액**이 몇 행 섞였나. 생산은 원장
    (``si_spend_ledger``)이, 오늘-창 카운트는 캡(``daily_cost_cap.check``)이
    이미 하고 있었지만 **소비자가 0** 이었다. 대시보드와의 diff 를 읽는 사람이
    이 수를 모르면 12.4배짜리(deepseek pro↔flash) 어긋남의 원인을 못 찾는다.
    ⛔ 카운트일 뿐이다 — 어떤 달러 합에도 안 들어가고(이중계상 없음), 판정
    (``ok``)도 안 건드린다(강제는 원장의 default-OFF ``price_model_strict``
    게이트 몫 — 캡 쪽과 같은 계약).
    """
    total = 0.0
    by_executor: dict[str, float] = {}
    n = 0
    price_mismatch = 0
    rows, read_error = _rows(_executor_log_path(state_dir))
    for r in rows:
        if since_ts is not None:
            ts = _row_ts(r)
            if ts is not None and ts < since_ts:
                continue
        c = r.get("cost_usd")
        if isinstance(c, (int, float)) and not isinstance(c, bool):
            total += float(c)
            n += 1
            key = str(r.get("executor", "unknown"))
            by_executor[key] = round(by_executor.get(key, 0.0) + float(c), 8)
        if _row_is_price_model_mismatch(r):
            price_mismatch += 1
    out = {"total_usd": round(total, 8), "rows_with_cost": n,
           "by_executor": by_executor,
           "price_model_mismatch_rows": price_mismatch}
    if _strict_enabled():  # __SLOT_R11_COST_2026_08_17__ additive-only under the gate
        out["read_error"] = read_error
    return out


def cross_model_spend(state_dir: Path | str, *, since_ts: float | None = None) -> dict[str, Any]:
    """Sum cross-model spend by reserve-anchored accounting transaction.

    ``rows`` remains the number of physical numeric rows in the requested
    window.  Only the dollar total is transaction-attributed, so a reconcile
    after the cutoff cannot enter alone as a negative refund.
    """
    total = 0.0
    n = 0
    no_ts = 0
    try:
        rows = read_spend_rows(_cross_model_path(state_dir))
        read_error = None
    except Exception as exc:  # noqa: BLE001 — strict parse failure is typed below
        _swallowed(
            exc,
            site="runtime.cost_reconcile.cross_model_spend:read",
            category="telemetry",
        )
        rows = []
        read_error = _safe_exception_type_name(exc)

    # Preserve the public physical-row counters byte-for-byte.
    for r in rows:
        ts = _row_ts(r)
        if ts is None:
            no_ts += 1  # pre-2026-07-31 rows have no ts — included regardless
        elif since_ts is not None and ts < since_ts:
            continue
        u = r.get("usd")
        if isinstance(u, (int, float)) and not isinstance(u, bool):
            n += 1

    try:
        charges = transaction_charge_rows(rows)
    except Exception as exc:  # noqa: BLE001 — semantic corruption is a read defect
        _swallowed(
            exc,
            site="runtime.cost_reconcile.cross_model_spend:transaction_attribution",
            category="telemetry",
        )
        charges = []
        read_error = _safe_exception_type_name(exc)

    window_charges = []
    for charge in charges:
        ts = _row_ts(charge)
        if ts is not None and since_ts is not None and ts < since_ts:
            continue
        window_charges.append(charge)
        usd = charge.get("usd")
        if isinstance(usd, (int, float)) and not isinstance(usd, bool):
            total += float(usd)
    out = {"total_usd": round(total, 8), "rows": n,
           "rows_without_ts_included": no_ts,
           "attribution": charge_attribution(window_charges)}
    if _strict_enabled():  # __SLOT_R11_COST_2026_08_17__ additive-only under the gate
        out["read_error"] = read_error
    return out


# __SLOT_POD_B_COST_CONSUMED_2026_08_09__ ------------------------------------


def _si_evidence_path(state_dir: Path | str) -> Path:
    """증거 원장 경로. 전용 override env 가 **없다**(생산자도 젤 상대경로만 쓴다).

    경로 조각은 생산자에게서 import 한다 — 여기에 문자열을 다시 적으면 한 값이 두
    곳에 살게 되고, 검사 안 받는 쪽이 썩는다.
    """
    return Path(state_dir).joinpath(*_SI_EVIDENCE_REL)


def pod_b_evidence_spend(
    state_dir: Path | str, *, since_ts: float | None = None,
) -> dict[str, Any]:
    """증거 원장이 **주장하는** pod B 지출.

    ⛔ null 접기 금지: ``pod_b_cost_usd`` 가 없는 dispatched 행(2026-08-07 필드 도입
    이전 스키마)은 0 으로 세지 않고 ``rows_missing_field`` 로 따로 신고한다. 0 으로
    접으면 "옛 행"과 "진짜 공짜"가 같은 숫자가 된다.
    """
    total = 0.0
    rows_with_cost = dispatched = considered = missing = local_free = 0
    rows, read_error = _rows(_si_evidence_path(state_dir))
    for r in rows:
        if since_ts is not None:
            ts = _row_ts(r)
            if ts is not None and ts < since_ts:
                continue
        considered += 1
        if not r.get("dispatched"):
            continue        # 게이트차단·쿨다운 행은 애초에 이 필드를 안 갖는다
        dispatched += 1
        c = r.get("pod_b_cost_usd")
        if isinstance(c, (int, float)) and not isinstance(c, bool):
            total += float(c)
            rows_with_cost += 1
            if str(r.get("pod_b_cost_basis") or "").startswith("local_free"):
                # 로컬 GPU = 과금 콜이 아니라 executor_log 행이 **없는 게 정상**이다.
                local_free += 1
        else:
            missing += 1
    out = {
        "total_usd": round(total, 8),
        "rows_with_cost": rows_with_cost,
        "rows_missing_field": missing,
        "local_free_rows": local_free,
        "dispatched_rows": dispatched,
        # 🔑 0 이 "깨끗함"인지 "안 봤음"인지 구분되게 — daily_cost_cap 이 배운 것과 같다.
        "rows_considered": considered,
    }
    if _strict_enabled():  # __SLOT_R11_COST_2026_08_17__ additive-only under the gate
        out["read_error"] = read_error
    return out


def pod_a_evidence_spend(
    state_dir: Path | str, *, since_ts: float | None = None,
) -> dict[str, Any]:
    """증거 원장이 **주장하는** pod A 지출 — :func:`pod_b_evidence_spend` 의 미러.

    ⚠️ 의도적 사본이다(공용화 리팩토링 금지 아님, 단 pod B 쪽은 래칫된 계약이라
    무터치가 우선). ``pod_a_cost_usd`` 는 pod A 게이트 ON 행에만 실리므로
    (si_evidence 의 OFF-parity 계약), OFF 기간의 행은 전부
    ``rows_missing_field`` 로 잡힌다 — 그 수치는 "옛 스키마"가 아니라
    "게이트 OFF 기간"으로 읽어라.
    """
    total = 0.0
    rows_with_cost = dispatched = considered = missing = local_free = 0
    rows, read_error = _rows(_si_evidence_path(state_dir))
    for r in rows:
        if since_ts is not None:
            ts = _row_ts(r)
            if ts is not None and ts < since_ts:
                continue
        considered += 1
        if not r.get("dispatched"):
            continue
        dispatched += 1
        c = r.get("pod_a_cost_usd")
        if isinstance(c, (int, float)) and not isinstance(c, bool):
            total += float(c)
            rows_with_cost += 1
            if str(r.get("pod_a_cost_basis") or "").startswith("local_free"):
                local_free += 1
        else:
            missing += 1
    out = {
        "total_usd": round(total, 8),
        "rows_with_cost": rows_with_cost,
        "rows_missing_field": missing,
        "local_free_rows": local_free,
        "dispatched_rows": dispatched,
        "rows_considered": considered,
    }
    if _strict_enabled():
        out["read_error"] = read_error
    return out


def hetero_pod_a_logged_spend(
    state_dir: Path | str, *, since_ts: float | None = None,
) -> dict[str, Any]:
    """executor_log 의 ``hetero_pod_a`` 행 합 — :func:`hetero_pod_logged_spend` 미러."""
    total = 0.0
    n = 0
    rows, read_error = _rows(_executor_log_path(state_dir))
    for r in rows:
        if r.get("executor") != _HETERO_EXECUTOR_POD_A:
            continue
        if since_ts is not None:
            ts = _row_ts(r)
            if ts is not None and ts < since_ts:
                continue
        c = r.get("cost_usd")
        if isinstance(c, (int, float)) and not isinstance(c, bool):
            total += float(c)
            n += 1
    out = {"total_usd": round(total, 8), "rows": n}
    if _strict_enabled():
        out["read_error"] = read_error
    return out


def pod_a_crosscheck(
    state_dir: Path | str,
    *,
    since_ts: float | None = None,
    tolerance_usd: float | None = None,
) -> dict[str, Any]:
    """증거 원장 ``pod_a_cost_usd`` ↔ ``hetero_pod_a`` 행 대조 — pod B 팔의 미러.

    방향 비대칭·창 합 대조·이중계상 금지 규칙 전부 :func:`pod_b_crosscheck`
    와 동일. pod A 가 한 번도 안 켜졌으면 양팔 다 0 이고 ``rows_considered``
    로 "안 봤음"과 구분한다.
    """
    ev = pod_a_evidence_spend(state_dir, since_ts=since_ts)
    lg = hetero_pod_a_logged_spend(state_dir, since_ts=since_ts)
    tol = (float(tolerance_usd) if tolerance_usd is not None
           else POD_B_ROW_TOLERANCE_USD * max(1, ev["rows_with_cost"]))
    delta = round(ev["total_usd"] - lg["total_usd"], 10)
    out: dict[str, Any] = {
        "evidence_usd": ev["total_usd"],
        "logged_usd": lg["total_usd"],
        "delta_usd": delta,
        "tolerance_usd": tol,
        "evidence": ev,
        "logged_rows": lg["rows"],
        "ok": True,
        "note": None,
    }
    if _strict_enabled():
        out["logged"] = lg
    if delta > tol:
        out["ok"] = False
        out["note"] = (
            f"pod A UNRECORDED: 증거 원장은 ${ev['total_usd']} 를 썼다고 하는데 "
            f"executor_log 의 hetero_pod_a 행은 ${lg['total_usd']} 뿐이다 "
            f"(delta ${delta}) — hetero_pod._record_spend(pod='A') 가 삼켜졌는지 본다"
        )
    elif delta < -tol:
        out["note"] = (
            f"executor_log 의 hetero_pod_a 행(${lg['total_usd']})이 증거 원장 "
            f"신고(${ev['total_usd']})보다 크다 (delta ${delta}) — 과소신고"
        )
    return out


def hetero_pod_logged_spend(
    state_dir: Path | str, *, since_ts: float | None = None,
) -> dict[str, Any]:
    """executor_log 의 ``hetero_pod`` 행 합 — 캡의 분자로 가는 **쓰기 경로** 쪽 팔.

    라이터와 같은 리졸버(:func:`_executor_log_path`)를 탄다 — 즉
    ``AGI_V8_EXECUTOR_LOG_PATH`` override 를 존중한다.

    ⚠️ 초판 독스트링은 이 값을 캡이 그대로 읽는 값이라고 단정했는데 과대였다
    (2026-08-09 정정):
    ``runtime.daily_cost_cap`` 은 그 override 를 **안 보고** 젤 상대경로
    (``state_dir/runtime_logs/executor_log.jsonl``)만 읽는다. override 가 없는
    기본 환경에서만 두 값이 같은 파일에서 나온다. 이 함수가 보장하는 것은
    "라이터가 적은 파일의 hetero_pod 행 합"까지다 — 코드가 하는 만큼만.
    """
    total = 0.0
    n = 0
    rows, read_error = _rows(_executor_log_path(state_dir))
    for r in rows:
        if r.get("executor") != _HETERO_EXECUTOR:
            continue
        if since_ts is not None:
            ts = _row_ts(r)
            if ts is not None and ts < since_ts:
                continue
        c = r.get("cost_usd")
        if isinstance(c, (int, float)) and not isinstance(c, bool):
            total += float(c)
            n += 1
    out = {"total_usd": round(total, 8), "rows": n}
    if _strict_enabled():  # __SLOT_R11_COST_2026_08_17__ additive-only under the gate
        out["read_error"] = read_error
    return out


def pod_b_crosscheck(
    state_dir: Path | str,
    *,
    since_ts: float | None = None,
    tolerance_usd: float | None = None,
) -> dict[str, Any]:
    """증거 원장의 ``pod_b_cost_usd`` ↔ 캡이 읽는 ``hetero_pod`` 행을 댄다.

    ``delta_usd = evidence - logged``.

    방향은 **비대칭**이다(이 모듈의 대시보드 비교와 같은 철학):

    * ``delta > tol`` — pod B 가 썼다고 증거 원장은 아는데 executor_log 에 그만큼이
      없다 ⇒ **캡의 분자에서 그 달러가 빠져 있다**. 심각한 방향이고 ``ok=False``.
    * ``delta < -tol`` — 반대. 캡은 이미 보수적으로(더 많이) 세고 있어서 예산을
      위협하진 않지만 증거 원장이 자기 지출을 과소신고한다 ⇒ 신고만 하고 ``ok`` 유지.

    ⛔ 여기서 나온 합은 ``internal_total_usd`` 에 **더하지 않는다**(이중계상).
    ⚠️ 행 단위 ``cycle_id`` 조인은 안 쓴다 — 라이브 실측에서 hetero_pod 행 하나가
       ``cycle_id=null`` 이었다(생산자가 cycle 문맥을 늦게 얻는 경로). 순번도 신원도
       못 되는 축으로 조인하면 멀쩡한 대조가 거짓 불일치를 낸다. 창(window) 합끼리 댄다.
    """
    ev = pod_b_evidence_spend(state_dir, since_ts=since_ts)
    lg = hetero_pod_logged_spend(state_dir, since_ts=since_ts)
    # 허용오차는 **비교한 행 수**가 정한다 — 행마다 반올림 하나씩.
    tol = (float(tolerance_usd) if tolerance_usd is not None
           else POD_B_ROW_TOLERANCE_USD * max(1, ev["rows_with_cost"]))
    delta = round(ev["total_usd"] - lg["total_usd"], 10)
    out: dict[str, Any] = {
        "evidence_usd": ev["total_usd"],
        "logged_usd": lg["total_usd"],
        "delta_usd": delta,
        "tolerance_usd": tol,
        "evidence": ev,
        "logged_rows": lg["rows"],
        "ok": True,
        "note": None,
    }
    if _strict_enabled():
        # __SLOT_R11_COST_2026_08_17__ full ``lg`` dict (carries read_error)
        # so reconcile() can see it — this key is additive-only, and does
        # NOT change ok/note above (those stay the 2026-08-09 contract).
        out["logged"] = lg
    if delta > tol:
        out["ok"] = False
        out["note"] = (
            f"pod B UNRECORDED: 증거 원장은 ${ev['total_usd']} 를 썼다고 하는데 "
            f"executor_log 의 hetero_pod 행은 ${lg['total_usd']} 뿐이다 "
            f"(delta ${delta}). daily_cost_cap 은 executor_log 만 읽으므로 그 차액만큼 "
            "캡이 실제보다 헐겁다 — hetero_pod._record_spend 가 삼켜졌는지 본다"
        )
    elif delta < -tol:
        out["note"] = (
            f"executor_log 의 hetero_pod 행(${lg['total_usd']})이 증거 원장이 신고한 "
            f"pod B 지출(${ev['total_usd']})보다 크다 (delta ${delta}). 캡은 보수적인 "
            "쪽이라 예산은 안전하지만 증거 원장이 자기 지출을 과소신고하고 있다"
        )
    elif ev["rows_missing_field"]:
        out["note"] = (
            f"{ev['rows_missing_field']} 개 dispatched 행에 pod_b_cost_usd 가 없다"
            "(필드 도입 이전 스키마) — 그 행들은 합에 안 들어갔다"
        )
    elif ev["rows_considered"] == 0 and lg["rows"] == 0:
        # __SLOT_POD_B_SAW_NOTHING_2026_08_09__ 못 본 것과 봤는데 0 은 다른
        # 사실이다 — 빈/부재 원장(또는 since_ts 창 밖)의 0 을 "대조해서 깨끗함"
        # 으로 읽지 않게 note 로 못박는다. ``ok`` 는 그대로 True: 표본이 없다는
        # **보고**지 실패 판정이 아니다(캡의 ``rows_considered`` 와 같은 규칙).
        out["note"] = (
            "looked at nothing: 증거 원장에서 고려한 행 0 · executor_log 의 "
            "hetero_pod 행 0 — 이 0 은 '대조해서 깨끗함'이 아니라 '대조할 것이 "
            "없었음'이다(빈/부재 원장 또는 since_ts 창 밖)"
        )
    return out


def reconcile(
    state_dir: Path | str,
    *,
    dashboard_usd: float | None = None,
    tolerance_usd: float = DEFAULT_TOLERANCE_USD,
    since_ts: float | None = None,
) -> dict[str, Any]:
    ex = executor_spend(state_dir, since_ts=since_ts)
    cm = cross_model_spend(state_dir, since_ts=since_ts)
    # __SLOT_POD_B_COST_CONSUMED_2026_08_09__ ⛔ 이 값은 internal 에 **안 더한다** —
    # hetero_pod 행은 이미 ``ex`` 안에 있다. 대조 전용 팔이다.
    pod_b = pod_b_crosscheck(state_dir, since_ts=since_ts)
    pod_a = pod_a_crosscheck(state_dir, since_ts=since_ts)
    internal = round(ex["total_usd"] + cm["total_usd"], 8)
    overrides = {
        k: v for k, v in (
            ("executor_log", os.environ.get(_EXECUTOR_LOG_ENV, "").strip()),
            ("cross_model", os.environ.get(_CROSS_MODEL_ENV, "").strip()),
        ) if v
    }
    out: dict[str, Any] = {
        "state_dir": str(state_dir),
        "since_ts": since_ts,
        "executor_log": ex,
        "cross_model": cm,
        "path_overrides": overrides or None,
        "internal_total_usd": internal,
        # __SLOT_POD_B_COST_CONSUMED_2026_08_09__ 대조 전용(합계에 미포함).
        "pod_b_crosscheck": pod_b,
        # __SLOT_OPENROUTER_LANE_POD_A_2026_08_22__ 대조 전용(합계 미포함) —
        # additive 키. pod A 미가동이면 양팔 0 으로 조용히 참이다.
        "pod_a_crosscheck": pod_a,
        "dashboard_usd": dashboard_usd,
        "diff_usd": None,
        "tolerance_usd": tolerance_usd,
        "ok": pod_b["ok"],
        "note": None,
    }
    if dashboard_usd is not None:
        diff = round(internal - float(dashboard_usd), 8)
        out["diff_usd"] = diff
        # ASYMMETRIC on purpose: the P2 rows are deliberate OVER-estimates
        # (cache-hit input billed at the miss rate), so internal > dashboard
        # is the expected direction and stays ok (with a note to sanity-check
        # the magnitude). internal < dashboard beyond tolerance means spend is
        # going UNRECORDED — that is the failure signal (DoD (e)).
        # __SLOT_POD_B_COST_CONSUMED_2026_08_09__ 두 판정의 **AND** — 대시보드가
        # 맞아도 pod B 대조가 깨졌으면 ok 가 아니다(``= diff >= ...`` 로 덮어쓰면
        # 세 번째 팔이 조용히 사라진다).
        out["ok"] = (diff >= -float(tolerance_usd)) and pod_b["ok"]
        if diff < 0:
            out["note"] = ("internal < dashboard: spend is going UNRECORDED "
                           "— the serious direction (estimates should "
                           "over-count, not under-count)")
        elif diff > float(tolerance_usd):
            out["note"] = (f"internal exceeds dashboard by ${diff} — expected "
                           "direction (conservative over-estimate); confirm "
                           "the magnitude matches cache-hit share + pricing "
                           "tier before signing off DoD (e)")
    else:
        out["note"] = "no --dashboard-usd given: internal breakdown only"
    # __SLOT_POD_B_COST_CONSUMED_2026_08_09__ pod B 대조의 말은 **덮이지 않는다** —
    # ok 를 뒤집어 놓고 이유가 note 에 없으면 exit 1 의 근거를 못 읽는다.
    if pod_b["note"]:
        out["note"] = (f"{out['note']} | {pod_b['note']}" if out["note"]
                       else pod_b["note"])
    # __SLOT_MISMATCH_ROWS_CONSUMED_2026_08_09__ 리포트 축(판정 아님): 분자 안에
    # 다른 모델 단가로 매긴 행이 섞였으면 diff 를 읽는 사람에게 말한다. ``ok`` 는
    # 안 건드린다 — 강제는 원장의 default-OFF ``price_model_strict`` 게이트 몫.
    if ex["price_model_mismatch_rows"]:
        mm_note = (
            f"{ex['price_model_mismatch_rows']} executor row(s) carry "
            "price_model_mismatch: their cost_usd was priced against a model "
            "the server did not serve — internal totals can be off by the "
            "model-pair price ratio (deepseek pro↔flash: 12.4x)"
        )
        out["note"] = f"{out['note']} | {mm_note}" if out["note"] else mm_note

    # __SLOT_R11_COST_2026_08_17__ ------------------------------------------
    # STRICT 팔: 판독 실패 이름표 + "아무것도 못 읽음"을 UNMEASURED 로 승격.
    # OFF(false)면 이 블록 전체가 안 돈다 — ok/note 는 위에서 이미 확정된 값
    # 그대로, 패치 이전과 byte-identical.
    if _strict_enabled():
        ev = pod_b["evidence"]
        lg = pod_b.get("logged") or {}
        read_errors: dict[str, str] = {}
        if ex.get("read_error"):
            read_errors["executor_log"] = ex["read_error"]
        if cm.get("read_error"):
            read_errors["cross_model"] = cm["read_error"]
        if ev.get("read_error"):
            read_errors["si_evidence"] = ev["read_error"]
        if lg.get("read_error"):
            read_errors["hetero_pod_logged"] = lg["read_error"]
        out["read_errors"] = read_errors or None

        # "안 봤음"은 네 팔(executor_log/cross_model/si_evidence/hetero_pod)
        # 전체가 0행이고 대시보드도 없을 때만이다 — 대시보드가 있으면 내부가
        # 비어도 비교할 앵커가 있으므로 UNMEASURED 가 아니라 위 diff 판정이
        # 이미 유효한 PASS/FAIL 이다.
        nothing_considered = (
            dashboard_usd is None
            and ex["rows_with_cost"] == 0
            and cm["rows"] == 0
            and ev.get("rows_considered", 0) == 0
            and pod_b["logged_rows"] == 0
        )

        if read_errors:
            # ⛔ fail-closed: 손상/판독불가 원장은 절대 깨끗한 통과로 안 보인다.
            out["status"] = _DIAG_EVALUATOR_ERROR
            out["ok"] = False
            err_note = (
                f"EVALUATOR_ERROR: {len(read_errors)} ledger(s) unreadable "
                f"({', '.join(sorted(read_errors))}) — a garbled ledger must "
                "not read as 'nothing was spent'"
            )
            out["note"] = f"{out['note']} | {err_note}" if out["note"] else err_note
        elif nothing_considered:
            out["status"] = _DIAG_UNMEASURED
            out["ok"] = False
            um_note = (
                "UNMEASURED: no --dashboard-usd and zero rows considered across "
                "executor_log/cross_model/si_evidence/hetero_pod — this 0 is "
                "'nothing was looked at', not 'looked and found clean'"
            )
            out["note"] = f"{out['note']} | {um_note}" if out["note"] else um_note
        else:
            out["status"] = _DIAG_PASS if out["ok"] else _DIAG_FAIL

    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Cost reconcile (W3): internal jail ledgers vs the "
                    "provider dashboard number (manual input).")
    ap.add_argument("--state-dir", required=True, help="jail dir (read-only)")
    ap.add_argument("--dashboard-usd", type=float, default=None,
                    help="spend shown by the provider dashboard (manual)")
    ap.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE_USD)
    ap.add_argument("--since-midnight", action="store_true",
                    help="restrict to rows since local-calendar midnight")
    args = ap.parse_args(argv)
    since = None
    if args.since_midnight:
        import time as _time

        since = day_start_epoch(_time.time())
    out = reconcile(
        Path(args.state_dir),
        dashboard_usd=args.dashboard_usd,
        tolerance_usd=args.tolerance,
        since_ts=since,
    )
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out["ok"] else 1


if __name__ == "__main__":  # pragma: no cover — CLI shim
    raise SystemExit(main())
