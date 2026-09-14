# __SLOT_LEDGER_JOIN_2026_08_08__ 원장 17개를 하나의 사이클로 꿰는 조인축.
"""원장 조인축 — ``cycle_id`` 는 **쓰면서** 심고, 시각은 **읽으면서** 정규화한다.

## 🔴 왜 (2026-08-08 젤 원장 18파일 축자 실측)

    cycle_id 를 가진 원장           6 / 17
      runtime_logs/executor_log      375행  cycle_id 0     ← 일일 캡이 읽는 원장
      runtime_logs/cross_model_…      36행  cycle_id 0     ← 08-07 에 나온 "젤 지출의 53%"
      briefings/emissions             95행  cycle_id 0
      bus/falsifier_events            78행  cycle_id 0
      ...
    시각 필드 **이름이 5종**
      ts · timestamp_unix · registered_ts · graded_ts · ts_utc(문자열)

⇒ *"이 사이클에 무슨 일이 있었나"* 를 물으면 원장 여섯을 손으로 꿰어야 한다.
`tick_flight.py` 가 존재하는 이유가 정확히 이것이고, 그건 **판독기지 통일이 아니다.**

## 🔑 두 축은 고치는 자리가 다르다

- **cycle 축 = 쓰기 쪽.** 행이 난 시점의 문맥이라 나중에 **유도할 수 없다.**
  읽는 쪽에서 시간창으로 추정하면 그게 바로 *"이 사이클이 얼마 썼나의 답이 둘"*
  (`cycle_spend` 는 시간창, `episode_log` 는 `cycle_id`)이다.
- **시각 축 = 읽기 쪽.** 값은 **이미 전부 있고 이름만 다르다.** 생산자 8곳을 고치면
  기존 소비자가 깨지고, 그러고도 다음 원장이 또 새 이름을 쓴다. ⇒ 별칭표 하나.

## ⛔ `horizon_ts` 는 행 시각이 아니다

`bus/predicted_deltas` 는 `registered_ts`(등록 시각)와 `horizon_ts`(채점 예정
시각, 실측 +24h)를 **둘 다** 갖는다. 이름에 `_ts` 가 붙었다고 행 시각이 아니다 —
접미사로 별칭을 뽑으면 미래값을 행 시각으로 읽고 **정렬이 조용히 뒤집힌다**
([[feedback_timestamp_string_vs_epoch_2026_08_01]] 이 77%→7% 로 겪은 그 형태).
⇒ 별칭은 **열거**한다. 추론하지 않는다.

## 계약

- 쓰기(:func:`join_keys`)는 **default-OFF** (:data:`ENV`). off 면 빈 dict 라
  ``entry.update(join_keys())`` 가 기존 행과 byte-identical 이다.
- 값이 없으면 **키를 안 싣는다.** ⛔ `None`/`""` 을 실어 *"모름"* 을 *"값"* 으로
  만들지 않는다.
- 읽기(:func:`row_ts`/:func:`row_cycle`)는 순수 함수이고 **게이트가 없다** —
  이미 있는 값을 해석할 뿐이라 끌 이유가 없다. 모르면 ``None`` 을 낸다.
"""
from __future__ import annotations

import datetime as _dt
import os
from typing import Any, Final, Mapping

# __SLOT_FAIL_FAST_2026_07_25__ 삼키는 지점은 이름을 붙여 센다.
# ⚠️ 이 모듈의 첫 판이 이 초크포인트를 안 거쳐 침묵 래칫을 64→66 으로 올렸다 —
# **관측을 짓는 모듈 안에 침묵을 넣은 것**이라 그 자리에서 잡았다.
from agi_v8_1.policy.fail_fast import swallowed as _swallowed

__all__ = [
    "ENV", "enabled", "join_keys",
    "TS_ALIASES", "NOT_ROW_TS", "row_ts", "row_cycle",
]

ENV: Final[str] = "AGI_V8_LEDGER_JOIN_KEYS_ENABLED"

#: 행 시각으로 인정하는 필드 — **열거만**. 실측 2026-08-08 기준 젤 원장 전수.
#: 순서가 우선순위다(앞이 이긴다).
TS_ALIASES: Final[tuple[str, ...]] = (
    "ts",              # 대다수 원장
    "timestamp_unix",  # events.jsonl
    "ts_utc",          # si_audit/<날짜>.jsonl — ⚠️ ISO **문자열**
    "registered_ts",   # bus/predicted_deltas.jsonl
    "graded_ts",       # bus/predicted_delta_grades.jsonl
    "ts_epoch",        # 다른 원장이 이 이름을 쓰면 받아준다
    "timestamp",
)

#: ⛔ ``_ts`` 로 끝나지만 **행 시각이 아닌** 것들. 별칭표에 실수로 들어오면
#: 정렬이 조용히 뒤집히므로 이름을 박아 둔다(회귀가 이 둘의 배타성을 검사).
NOT_ROW_TS: Final[tuple[str, ...]] = (
    "horizon_ts",      # 채점 **예정** 시각(실측 등록시각 +24h)
    "expires_ts",
    "deadline_ts",
    "next_ts",
)

#: ⛔ 사이클처럼 보이지만 **신원이 아닌** 것들 — 2026-08-08 에 실제로 밟을 뻔했다.
#: ``continuation_ring/si_loop_v81.jsonl`` 의 ``cycle`` 은 ``1,2,3…`` **정수 순번**
#: 이다. 이름이 비슷하다고 :func:`row_cycle` 의 별칭으로 넣었으면 **모든 런의
#: 1번 사이클이 한 덩어리로 조인**된다 — 틀린 조인은 안 되는 조인보다 나쁘다.
#: 🔑 순번(ordinal)과 신원(identity)은 다른 종류의 값이다.
NOT_CYCLE_ID: Final[tuple[str, ...]] = (
    "cycle",           # 런 내 순번(int)
    "cycle_index",
    "cycle_no",
    "iteration",
)


def enabled() -> bool:
    """strict — 다른 게이트와 같은 관례(``"true"``/``"1"``)."""
    return os.environ.get(ENV, "").strip().lower() in ("true", "1")  # tier: T2


def join_keys(*, cycle_id: str | None = None,
              call_id: str | None = None) -> dict[str, str]:
    """원장 행에 실을 조인키. **게이트 OFF 면 빈 dict.**

    문맥(:mod:`agi_v8_1.runtime.episode_ctx`)에서 폴백을 읽되 ⛔ **명시 인자가
    항상 이긴다** — 안 그러면 바깥 사이클이 안쪽 하위콜의 신원을 훔친다.

    ⚠️ 문맥 게이트(``AGI_V8_EPISODE_CTX_ENABLED``)가 꺼져 있으면 폴백은
    ``(None, None)`` 이라 이 함수도 빈 dict 를 낸다 — **켰는데 값이 안 붙으면
    문맥 게이트를 보라**는 뜻이지, 조용한 실패가 아니다.
    """
    if not enabled():
        return {}
    try:
        from agi_v8_1.runtime import episode_ctx as _ctx
        ctx_cycle, ctx_call = _ctx.current()
    except Exception as exc:  # noqa: BLE001 — 조인키는 절대 호출자를 죽이지 않는다
        _swallowed(exc, site="runtime.ledger_join.join_keys:ctx", category="telemetry")
        ctx_cycle, ctx_call = (None, None)

    out: dict[str, str] = {}
    c = cycle_id or ctx_cycle
    k = call_id or ctx_call
    if c:
        out["cycle_id"] = str(c)
    if k:
        out["call_id"] = str(k)
    return out


def _coerce_ts(value: Any) -> float | None:
    """epoch float / ISO 문자열 → epoch float. 못 읽으면 ``None``."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        f = float(value)
        # 0 은 "안 찍혔다"에 가깝고, 음수는 시각이 아니다.
        return f if f > 0 else None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return float(s) if s.replace(".", "", 1).isdigit() else \
                _dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
        except Exception as exc:  # noqa: BLE001
            # ⚠️ 파서의 실패도 **센다**. 못 읽은 시각이 조용히 ``None`` 이 되면
            # "이 원장엔 시각이 없다"와 "이 원장의 시각을 우리가 못 읽는다"가
            # 같아 보인다 — 후자는 별칭표를 고치라는 신호다.
            _swallowed(exc, site="runtime.ledger_join._coerce_ts", category="telemetry")
            return None
    return None


def row_ts(row: Mapping[str, Any]) -> float | None:
    """행 시각(epoch 초). 모르면 ``None`` — ⛔ 0 으로 접지 않는다.

    ``0.0`` 을 내면 *"1970년에 일어난 일"* 이 되어 정렬 맨 앞에 붙고, 그건
    *"시각을 모른다"* 와 완전히 다른 주장이다.
    """
    if not isinstance(row, Mapping):
        return None
    for key in TS_ALIASES:
        if key in row:
            got = _coerce_ts(row[key])
            if got is not None:
                return got
    return None


def row_cycle(row: Mapping[str, Any]) -> str | None:
    """행의 ``cycle_id``. 한 겹 안쪽(예: ``dispatch_result.cycle_id``)도 본다.

    ⚠️ 시간창으로 **추정하지 않는다.** 없으면 ``None`` 이고, 그건 그 원장이
    아직 조인축에 안 올라왔다는 사실 그대로다.

    ⛔ :data:`NOT_CYCLE_ID` 의 이름들은 **보지 않는다** — 순번은 신원이 아니다.
    ⛔ 문자열만 받는다. ``cycle_id`` 라는 이름으로 정수가 오면 그건 순번일
    가능성이 높고, 신원이라면 문자열로 오게 만드는 쪽이 맞다.
    """
    if not isinstance(row, Mapping):
        return None
    direct = row.get("cycle_id")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    for value in row.values():
        if isinstance(value, Mapping):
            nested = value.get("cycle_id")
            if isinstance(nested, str) and nested.strip():
                return nested.strip()
    return None
