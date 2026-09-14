# __SLOT_SPEND_RESERVATION_2026_08_19__ Sol Pro P1-d — 단일 지출 예약 원장.
"""지출 전에 예약하고, 지출 후 정산/해제하는 단일 원장.

## 왜 (갭의 정확한 위치)

``runtime.daily_cost_cap`` 은 ``executor_log.jsonl``(+opt-in cross-model 원장)
만 합산한다 — **이미 끝난** 지출만 본다. 그런데 한 틱의 dispatch 는 그 자체로
수십 초~수 분이 걸리는 창이고, 그 창 **안에서** reviewer(cross_model_verify)·
hetero dispatch·독립재현이 돈을 쓴다. 그 지출이 executor_log 에 착지하는 시점은
dispatch 가 끝난 **뒤**다 — 즉 "지금 이 순간 얼마가 나가는 중인가"를 캡이 전혀
모른다. 두 틱이 겹칠 수 없다는 전제(``cycle_spend.py`` 의 단일비행 불변식)는
캡의 판정 시점에는 아무 도움이 안 된다: 문제는 동시성이 아니라 **지연**이다 —
dispatch 가 시작된 순간과 그 실지출이 원장에 보이는 순간 사이의 창에서 캡이
"아직 안 썼다"로 읽는다.

이 모듈은 그 창을 메운다. ``reserve()`` 가 예상액을 원장에 적어 그 창 안에서도
"이만큼은 나갈 예정이다"가 보이게 하고, ``settle()`` 이 실액으로 정산한다.
소비자(``daily_cost_cap.check()``)는 "이미 정산된 것 + 아직 열린 예약"을 판정에
쓸 수 있다 — **판정 축**이 이제 실지출뿐 아니라 진행 중인 지출도 본다.

## 생애주기

```
reserve(subject, expected_usd)  → 원장에 status="open" 행 append, 반환값은
                                   그 예약의 in-memory 핸들(dict)
      │
      ├─ settle(handle)   → 실액을 측정해(가능하면) status="settled" 행 append
      │
      └─ release(handle)  → 아무것도 안 나갔다(실행 자체가 취소/스킵) —
                             status="released", actual_usd=0.0 행 append
```

원장은 **append-only**(``state.store.atomic_append_jsonl``, 다른 모든 지출
원장과 같은 I/O). 갱신이 아니라 새 행 추가로 상태를 바꾼다 — 소비자는 같은
``reservation_id`` 의 행 중 **가장 나중 것**을 그 예약의 현재 상태로 읽는다
(``episode_ledger``/``tick_lease`` 류가 이미 쓰는 append-only 관례, 새 파일
포맷 아님).

## 크래시 시 stale 예약 — TTL 은 "청소"가 아니라 "읽기 시점 필터"

프로세스가 dispatch 도중 죽으면(OOM/kill -9/전원) ``settle()``/``release()``
가 영영 안 불릴 수 있다 — 그 예약은 원장에 ``status="open"`` 인 채로 **영원히
남는다**(append-only 라 지울 수도 없다). 이걸 그대로 두면 죽은 프로세스의
예약이 캡을 **영구히** 무겁게 만든다.

``tick_lease.py`` 의 관례를 그대로 차용한다 — TTL 은 "정리 프로세스"가 아니라
**읽는 쪽의 판단 기준**이다. ``open_reservations()`` 가 지금 시각 기준으로
``reserved_ts + ttl_seconds() < now`` 인 open 행을 만나면 그 예약을 "지금은
열려있다고 못 믿는다"로 치고 합계에서 뺀다. 원장 파일 자체는 손대지 않는다 —
프로세스가 실은 살아있고 그냥 느린 것이었다면(TTL 보다 오래 걸리는 dispatch),
다음 정상 종료 시 ``settle()``이 여전히 같은 ``reservation_id``로 정산 행을
붙일 수 있고, 그 시점부터 그 예약은 다시 "닫힌 것"으로 읽힌다 — append-only
원장이 사후에 스스로 바로잡는다.

기본 TTL 은 1800s(``tick_lease._DEFAULT_TTL_SEC`` 와 같은 값) — 틱 자체의
데드라인(``tick_deadline`` 기본 900s)보다 넉넉해서, 정상 종료라면 절대 만료
전에 settle 이 먼저 온다.

## 게이트 문자열 비교 관례 — 이 파일은 **strict** 를 쓴다

이 저장소의 게이트 파싱은 두 갈래다: ``si_spend_ledger.enabled()`` 류는 strict
``"true"``/``"1"`` 만 인정하고, 이 파일의 소비처(``daily_cost_cap.py``)에 이미
있는 ``cross_model_included()``/``strict_unmeasured()`` 는 느슨하게
``"true"/"1"/"yes"/"on"`` 을 다 받는다. 이 파일은 **새 모듈**이라 자기 자신의
기존 sibling 이 없으므로, 지시서에 명시된 프로젝트 기본(strict "true"/"1")을
따른다 — ``AGI_V8_SPEND_RESERVATION_ENABLED=true`` 또는 ``=1`` 만 ON, 그
밖의 모든 값(``yes``/``on``/``TRUE`` 포함)은 OFF. 다음 세션이 이 파일을 보고
"``daily_cost_cap`` 의 다른 게이트처럼 느슨하겠지"라고 넘겨짚지 않도록 여기
명시해 둔다.

## 이중계상 — 정산 후엔 없음, 진행 중엔 있을 수 있음(방향은 항상 안전)

``daily_cost_cap.check()`` 는 ``spent_usd``(executor_log, +opt-in cross-model)
는 그대로 두고 **``allowed``/``reason`` 만** ``spent_usd + open_reservations``
로 재판정한다(그 모듈의 docstring 참조) — ``settle()`` 로 **닫힌** 예약은
``open_reservations()`` 합계에서 빠지므로(status != "open"), 정산 **완료 후**
에는 executor_log 에 실제로 착지한 돈과 "그 예약의 expected_usd"가 같은 창에서
동시에 캡의 분자에 잡히는 일은 없다 — 이 방향(정산 후)은 구조적으로 보장된다.

⚠️ **2026-08-19 적대검증 수리 — 정정**: 예전 이 절은 "두 축이 같은 달러를
동시에 세는 창이 없다"고 단정했는데 **틀렸다**. 예약이 아직 **열려 있는
동안**, 그 예약이 감싸는 dispatch 자신의 서브콜(reviewer/hetero/재현)이
``settle()`` 이 불리기 **전에** 자기 지출을 ``executor_log`` 에 이미 착지시킬
수 있다(각 서브콜은 끝나는 대로 실시간으로 원장에 적힌다 — 그게 이 예약이
닫히는 것과는 별개 사건이다). 그 창에서는:

- ``spent_usd`` 가 이미 그 서브콜의 실착지분을 포함하고,
- ``open_reservations_usd`` 는 **그 예약의 전체 ``expected_usd``**(감액 없이)를
  여전히 포함한다 — 이 모듈은 "예약 중 얼마가 이미 서브콜로 착지했는지"를
  개별 서브콜 단위로 추적하지 않는다(한 예약 = 한 dispatch 전체를 감싸는
  현재 설계, 설계결정 §7 — 서브콜 단위 세분화는 T2/exec_arm 몫).

즉 **같은 달러가 실제로 두 축에 동시에 잡힌다** — ``spent_plus_reserved_usd``
가 그 사이클의 진짜 최대 노출(≈예산)보다 과대해진다. 방향은 항상 **안전
측**이다(과소평가가 아니라 과대평가 — 캡을 더 일찍 잠글 뿐, 뚫리게 하지
않는다) — 여러 예약이 동시에 열려 있는 경우까지 정확히 넷팅하려면 서브콜을
예약에 개별 귀속시켜야 하는데, 지금의 "한 예약 = 한 dispatch" 단일-비행
설계(``cycle_spend.py`` 단일비행 불변식과 같은 전제)에서 그 정밀도를 억지로
넣으면 오히려 "이미 이 예약과 무관한 지출까지 이 예약에서 깎아 실제보다
헐겁게 보이는" 새로운(그리고 더 나쁜, under-count 방향) 실패 모양을 만들
위험이 있다 — 그래서 이번 수리는 정확한 넷팅을 넣지 않고 **이 사실을
정확히 문서화하고 테스트로 고정**하는 쪽을 택했다
(``test_cap_gate_on_own_open_reservation_mid_flight_sub_spend_double_counts_but_stays_safe_direction``).
정산이 끝나면(``settle()``) 위 문단대로 스스로 바로잡는다 — append-only 원장이
사후에 정정하는 것과 같은 패턴이다.

## default-OFF

``AGI_V8_SPEND_RESERVATION_ENABLED``(tier T9). OFF 면 ``reserve()``/
``settle()``/``release()`` 전부 파일을 안 건드리고 ``None`` 을 반환한다 —
``record_llm_call`` 관례(호출부는 무조건 호출, 게이트가 내부에서 no-op 처리).
"""
from __future__ import annotations

import os
import time
import uuid
from pathlib import Path
from typing import Any

from agi_v8_1.state.store import atomic_append_jsonl, read_jsonl

# __SLOT_FAIL_FAST_2026_07_25__ Swallowed failures route through one choke
# point: counted + named always, re-raised under AGI_V8_STRICT_FAIL_FAST.
from agi_v8_1.policy.fail_fast import (
    safe_exception_type_name as _safe_exception_type_name,
    swallowed as _swallowed,
)

_ENV_ENABLED = "AGI_V8_SPEND_RESERVATION_ENABLED"
_ENV_TTL = "AGI_V8_SPEND_RESERVATION_TTL_SEC"
#: tick_lease.py 의 진단 TTL 관례를 그대로 차용(같은 크래시-복구 문제 모양).
#: 틱 자체 데드라인(tick_deadline 기본 900s)보다 넉넉해서, 정상 종료 dispatch
#: 라면 절대 만료 전에 settle() 이 먼저 온다.
_DEFAULT_TTL_SEC = 1800.0

_LEDGER_REL = ("runtime_logs", "spend_reservation.jsonl")

STATUS_OPEN = "open"
STATUS_SETTLED = "settled"
STATUS_RELEASED = "released"

_SCHEMA_VERSION = "agi_v8_spend_reservation_v1"


def enabled() -> bool:
    """Default-OFF gate — strict ``"true"``/``"1"`` only.

    ⚠️ 이 파일의 관례는 **strict** 다(``si_spend_ledger.enabled()`` 와 같은
    모양) — ``daily_cost_cap.py`` 안에서 이 함수를 부르는 자리 옆의
    ``cross_model_included()``/``strict_unmeasured()`` 는 느슨한
    ``"1"/"true"/"yes"/"on"`` 을 받지만, **이 함수 자체는 그 관례를 안
    따른다**(신규 모듈이라 자기 sibling이 없어 지시서의 프로젝트 기본을
    따름). 모듈 docstring "게이트 문자열 비교 관례" 절 참조.
    """
    return os.environ.get(_ENV_ENABLED, "") in ("true", "1")  # tier: T9


def ttl_seconds() -> float:
    """열린 예약을 "아직 살아있다"고 믿을 최대 시간(초). 잘못된 값 → 기본."""
    raw = os.environ.get(_ENV_TTL)
    if raw is None:
        return _DEFAULT_TTL_SEC
    try:
        val = float(raw)
    except (TypeError, ValueError) as exc:
        _swallowed(exc, site="runtime.spend_reservation.ttl_seconds", category="config")
        return _DEFAULT_TTL_SEC
    return val if val > 0.0 else _DEFAULT_TTL_SEC


def _ledger_path(state_dir: "Path | str") -> Path:
    return Path(state_dir).joinpath(*_LEDGER_REL)


def _now(now_ts: "float | None") -> float:
    return float(now_ts) if now_ts is not None else time.time()


def _safe_episode_spend(state_dir: "Path | str") -> "float | None":
    """``episode_ledger.episode_spend()`` 의 안전한 래퍼.

    ⚠️ 원함수는 **strict** 다(``ValueError`` on garbled ledger, 그 모듈
    자신의 계약) — 여기서 그걸 그대로 부르면 손상된 지출 원장 하나가
    ``tick_once`` 전체를 죽일 수 있다. swallowed 로 감싸 실패를 ``None``
    (모른다)으로 승격한다. 호출부(:func:`settle`)가 "모른다"를 0 으로
    접지 않고 예상액으로 대체하는 것은 여기가 아니라 그쪽의 책임이다.
    """
    try:
        from agi_v8_1.runtime.episode_ledger import episode_spend as _ep_spend

        return _ep_spend(state_dir)
    except Exception as exc:  # noqa: BLE001 — a garbled ledger must never crash the cycle
        _swallowed(exc, site="runtime.spend_reservation._safe_episode_spend",
                   category="config")
        return None


def _append(state_dir: "Path | str", row: dict[str, Any]) -> bool:
    try:
        atomic_append_jsonl(_ledger_path(state_dir), row)
    except Exception as exc:  # noqa: BLE001 — a broken ledger must never break dispatch
        _swallowed(exc, site="runtime.spend_reservation._append", category="persist")
        return False
    return True


def reserve(
    state_dir: "Path | str", *, subject: str, expected_usd: float,
    cycle_id: "str | None" = None, now_ts: "float | None" = None,
) -> "dict[str, Any] | None":
    """예상 지출을 원장에 연다. 게이트 OFF 면 아무것도 안 쓰고 ``None``.

    반환값은 이 예약의 **in-memory 핸들**(``settle``/``release`` 에 그대로
    넘긴다) — 디스크에서 다시 읽지 않는다. ``spend_before_usd`` 는 이 시점의
    실지출 스냅샷(측정 실패 시 ``None``, :func:`settle` 이 델타 계산에 쓴다).
    """
    if not enabled():
        return None
    now = _now(now_ts)
    handle = {
        "reservation_id": uuid.uuid4().hex,
        "subject": str(subject),
        "cycle_id": cycle_id,
        "expected_usd": round(max(0.0, float(expected_usd or 0.0)), 8),
        "reserved_ts": now,
        "spend_before_usd": _safe_episode_spend(state_dir),
    }
    row = {
        "schema_version": _SCHEMA_VERSION,
        "reservation_id": handle["reservation_id"],
        "subject": handle["subject"],
        "cycle_id": handle["cycle_id"],
        "expected_usd": handle["expected_usd"],
        "status": STATUS_OPEN,
        "reserved_ts": now,
        "settled_ts": None,
        "actual_usd": None,
    }
    # __SLOT_RESERVE_DURABLE_OR_REFUSE_2026_08_20__ 🔴 Sol Pro R15 P0-A.
    #
    # 예약의 **존재 이유**는 실지출이 원장에 착지하기 전에 예상 지출을 다른
    # spender(reviewer·cross-model·exec_arm·수동 실행·별개 controller)에게
    # 보이게 하는 것이다. 그 durable write 가 실패한 순간(읽기전용 경로 ·
    # disk full · I/O 오류) in-memory 핸들만 돌려주면, 캡을 지켜야 할 바로 그
    # 장애 상황에서 **fail-open** 이 된다 — 디스크에 OPEN 행이 없으니 다른
    # 프로세스는 이 지출을 영영 못 보고, 호출자는 예약이 선 줄 알고 돈을 쓴다.
    # ⇒ 실패는 ``None`` 이 아니라 **명명된 거부**로 돌려준다(``None`` 은 이미
    # "게이트 OFF" 의미로 쓰이므로 재사용하면 두 사실이 한 값으로 접힌다 —
    # 모름을 0 으로 접지 않는다는 이 레포 규율 그대로).
    if not _append(state_dir, row):
        return {"unavailable": True, "reason": "reservation_ledger_write_failed",
                "subject": handle["subject"], "cycle_id": handle["cycle_id"]}
    return handle


def is_unavailable(reservation: "dict[str, Any] | None") -> bool:
    """``reserve()`` 가 durable write 실패로 낸 거부 핸들인가.

    ``None``(게이트 OFF)은 **거부가 아니다** — 종전과 동일한 no-op 경로다.
    호출자는 이 술어가 True 일 때만 유료 작업을 포기하면 된다.
    """
    return bool(isinstance(reservation, dict) and reservation.get("unavailable"))


def settle(
    state_dir: "Path | str", reservation: "dict[str, Any] | None", *,
    actual_usd: "float | None" = None, now_ts: "float | None" = None,
) -> "dict[str, Any] | None":
    """예약을 실액으로 정산한다. ``reservation`` 이 ``None``(OFF 또는 reserve
    실패)이면 no-op — 콜사이트는 무조건 호출해도 된다.

    실액은 기본적으로 :func:`reserve` 가 찍은 ``spend_before_usd`` 와 지금의
    ``episode_ledger.episode_spend()`` 의 **델타**로 잰다(``_real_dispatch``
    의 episode_total 계측과 같은 패턴). *actual_usd* 를 명시하면 그 값을
    그대로 쓴다(테스트/대체 계측 경로용).

    ⚠️ 델타를 못 재면(둘 중 하나라도 ``None`` — 원장 판독 실패) **예상액을
    실액으로 간주한다** — 모름을 0 으로 접으면 과소평가가 되어 캡이 무력화
    되는 바로 그 실패 모양이다.
    """
    # ``None``(게이트 OFF)과 거부 핸들(durable write 실패) 둘 다 no-op —
    # 후자는 애초에 OPEN 행이 디스크에 없으므로 정산할 대상 자체가 없다
    # (__SLOT_RESERVE_DURABLE_OR_REFUSE_2026_08_20__).
    if reservation is None or is_unavailable(reservation):
        return None
    now = _now(now_ts)
    measured = True
    if actual_usd is not None:
        amount = max(0.0, float(actual_usd))
    else:
        before = reservation.get("spend_before_usd")
        after = _safe_episode_spend(state_dir)
        if before is None or after is None:
            amount = reservation["expected_usd"]
            measured = False
        else:
            amount = max(0.0, round(after - before, 8))
    row = {
        "schema_version": _SCHEMA_VERSION,
        "reservation_id": reservation["reservation_id"],
        "subject": reservation["subject"],
        "cycle_id": reservation.get("cycle_id"),
        "expected_usd": reservation["expected_usd"],
        "status": STATUS_SETTLED,
        "reserved_ts": reservation["reserved_ts"],
        "settled_ts": now,
        "actual_usd": round(amount, 8),
        "actual_measured": measured,
    }
    _append(state_dir, row)
    return row


def release(
    state_dir: "Path | str", reservation: "dict[str, Any] | None", *,
    reason: "str | None" = None, now_ts: "float | None" = None,
) -> "dict[str, Any] | None":
    """예약을 취소한다 — dispatch 자체가 실행되지 않아 실지출이 없는 경우.

    ``reservation`` 이 ``None``(게이트 OFF) 이거나 거부 핸들(durable write
    실패)이면 no-op. ``actual_usd`` 는 항상 0.0(정의상 "안 나갔다" — settle 의
    "모른다"와 다른, **알고 있는** 0).
    """
    if reservation is None or is_unavailable(reservation):
        return None
    now = _now(now_ts)
    row = {
        "schema_version": _SCHEMA_VERSION,
        "reservation_id": reservation["reservation_id"],
        "subject": reservation["subject"],
        "cycle_id": reservation.get("cycle_id"),
        "expected_usd": reservation["expected_usd"],
        "status": STATUS_RELEASED,
        "reserved_ts": reservation["reserved_ts"],
        "settled_ts": now,
        "actual_usd": 0.0,
        "actual_measured": True,
        "release_reason": str(reason) if reason else None,
    }
    _append(state_dir, row)
    return row


def open_reservations(
    state_dir: "Path | str", *, now_ts: "float | None" = None,
) -> dict[str, Any]:
    """지금 열려 있는(미정산·미해제·TTL 안 지남) 예약들의 요약.

    반환: ``{"open_usd": float, "open_count": int, "expired_count": int,
    "read_failed": str | None}``. ``read_failed`` 는 원장이 있는데 못 읽었을
    때만 채워진다(``daily_cost_cap._read_ledger_rows`` 와 같은 계약 —
    ``None`` = 정상 판독, 파일 없음/빈 원장 포함).

    같은 ``reservation_id`` 의 행 중 **가장 나중 것**이 그 예약의 현재
    상태다(append-only 마지막-승 — 파일 순서가 시간 순서라는 전제는
    ``atomic_append_jsonl`` 의 순차 append 계약이 보장한다).
    """
    now = _now(now_ts)
    try:
        rows = read_jsonl(_ledger_path(state_dir))
    except Exception as exc:  # noqa: BLE001 — a garbled ledger must never crash the cap
        _swallowed(exc, site="runtime.spend_reservation.open_reservations:read",
                   category="config")
        return {"open_usd": 0.0, "open_count": 0, "expired_count": 0,
                "read_failed": _safe_exception_type_name(exc)}

    latest: dict[str, dict[str, Any]] = {}
    for r in rows:
        rid = r.get("reservation_id")
        if isinstance(rid, str) and rid:
            latest[rid] = r  # last wins — append-only, sequential

    ttl = ttl_seconds()
    total = 0.0
    open_count = 0
    expired_count = 0
    for r in latest.values():
        if r.get("status") != STATUS_OPEN:
            continue
        ts = r.get("reserved_ts")
        if not isinstance(ts, (int, float)) or isinstance(ts, bool):
            continue  # 시각을 모르면 "열려 있다"고 못 믿는다(합계에서 뺀다)
        if now - float(ts) > ttl:
            expired_count += 1  # 읽기 시점 필터 — 원장 파일은 안 건드린다
            continue
        v = r.get("expected_usd")
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            total += float(v)
            open_count += 1
    return {"open_usd": round(total, 8), "open_count": open_count,
            "expired_count": expired_count, "read_failed": None}


def open_reservations_usd(state_dir: "Path | str", *, now_ts: "float | None" = None) -> float:
    """:func:`open_reservations` 의 얇은 편의 래퍼 — 합계만."""
    return open_reservations(state_dir, now_ts=now_ts)["open_usd"]


__all__ = [
    "STATUS_OPEN",
    "STATUS_SETTLED",
    "STATUS_RELEASED",
    "enabled",
    "ttl_seconds",
    "reserve",
    "settle",
    "release",
    "open_reservations",
    "open_reservations_usd",
]
