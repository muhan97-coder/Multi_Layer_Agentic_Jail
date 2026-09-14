# __SLOT_EPISODE_LOG_STAGE1_2026_08_04__ 계약 6종 emitter (1단계).
"""``episode.jsonl`` — 공개 계약의 과정축 로그를 우리 러너에서 낸다.

⛔ **설계 문서가 아니다.** 계약은 공개 레포 ``AGI_BENCHMARK_public/README.md``
§"Episode log contract (process axis — v1 draft)" 에 이미 명세돼 있다. 이 모듈은
그 계약을 **구현**할 뿐이고, 행 모양·필드명·HALT 어휘는 계약이 정한 그대로다.
2026-08-03 에 계약의 존재를 모르고 5종을 재설계한 전례가 있다 — 기존 6종의 열등한
재발명이었다. 계획서: ``docs/EPISODE_LOG_PLAN_2026_08_04.md``.

왜 이 파일이 필요한가 (계획서 §왜 지금):
  로그가 7군데로 흩어져 있고, 사이클 간에는 아무것도 안 넘어간다. ``episode.jsonl``
  은 **카드 하나당 한 파일**이고 사이클은 그 안에서 도니까, 이 파일 자체가 사이클
  경계를 넘는 통로다. 1단계(이 파일)는 쓰기만 한다 — **3단계(읽기 측)가 없으면
  write-only 채널이 하나 느는 것뿐이고**, 그건 오늘 여섯 번 본 실패 양식이다.

계약(변경 금지):
  PLAN     plan_id · parent · summary · candidates_considered
  DISPATCH plan_id · worker · n_parallel · task
  VERIFY   target · command · ran · verdict · failed_ids   ← 개수가 아니라 **신원**
  COST     usd · provider · model · purpose                ← 실패한 콜도 행을 받는다
  HUMAN    note                                            ← 자율성에서 차감
  HALT     reason                                          ← 닫힌 어휘 8종

행 모양은 **평면**이다(계약 예시 그대로). 코덱스식 2층 봉투는 채택하지 않았다 —
계획서 부록 A ① 참조. 계약이 말하지 않은 필드(``cycle_id``·``schema_version``·
``call_id``)만 **추가**한다: 추가 필드는 계약을 깨지 않고, 스코어러는 아는 필드만 읽는다.

Contract(이 모듈 자신의):
  - Default-OFF (``AGI_V8_EPISODE_LOG_ENABLED``, strict "true"/"1").
    OFF → ``emit_*`` 는 파일시스템을 건드리지 않고 돌아온다(byte-identical 젤).
  - 젤(``state_dir/runtime_logs/``) 에만 쓴다. ``atomic_append_jsonl`` 경유.
  - **스키마 위반은 raise, IO 실패는 swallow.** 둘은 다른 종류의 사고다 —
    전자는 호출자 버그라 테스트에서 즉시 터져야 하고(잘못된 행을 쓰면 원장이
    오염된다), 후자는 런타임 조건이라 사이클을 죽이면 안 된다.
  - ``read_rows`` 는 ungated. 없는 로그는 ``[]``.
"""
from __future__ import annotations

import datetime as _dt
import getpass
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from agi_v8_1.policy.fail_fast import swallowed as _swallowed
from agi_v8_1.state.store import atomic_append_jsonl

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "episode_log_v1"
_ENV = "AGI_V8_EPISODE_LOG_ENABLED"
_LOG_REL = ("runtime_logs", "episode.jsonl")

# __SLOT_R9_T4_2026_08_17__ (round 9 track T4, security/observability fix)
# HUMAN 행 스키마에 신원 필드가 하나도 없었다(``_base_row`` 는
# schema_version/ts/event/cycle_id 뿐) — "누가 승인했나"가 원장에서 통째로
# 안 잡혔다. 기본 ON — 이건 새 판정을 더하는 게 아니라 **이미 일어난 사건의
# 관측을 늘리는 것**이라 안전 불변식(default-OFF 판정 게이트) 대상이 아니고,
# HALT 어휘·계약 필드처럼 계약을 어기지도 않는다(추가 필드는 계약 밖). 그래도
# byte-identical OFF 를 요구하는 하우스룰을 그대로 지킨다: OFF 면 이 필드
# 자체가 행에서 아예 빠진다.
_HUMAN_ACTOR_ENV = "AGI_V8_HUMAN_EVENT_ACTOR"

#: 계약 6종. 이 튜플이 곧 허용 목록이다.
EVENTS = ("PLAN", "DISPATCH", "VERIFY", "COST", "HUMAN", "HALT")

#: HALT 어휘 — 공개 ``tools/validate_leaderboard.py:BINDING_LIMITS`` 와 **같은 8낱말**.
#: 2026-08-04 (공개 5b50345) 이전에는 README 의 HALT 목록과 리더보드의 bound_by 가
#: 서로 다른 이름을 썼다(``goal_green`` vs ``green``). 이제 하나다 — 그래서
#: ``bound_by`` 는 HALT 행을 **세기만 하면** 나온다.
HALT_REASONS = (
    "green",        # 카드의 성공 조건 도달
    "budget",       # 배정 예산 소진 — 의도된 구속
    "wall_clock",   # 타임아웃
    "cycles",       # 턴/반복 상한
    "red_streak",   # 연속 실패
    "aborted",      # 루프가 터졌거나, **러너가 시작/계속을 거절**했다
    "refused",      # 에이전트가 스스로 멈췄다: 목표가 불가능하다고 판단
    "unknown",      # 정직하게 미귀속 — 이웃 칸에 접어 넣지 말 것
)

#: 최대 보존 개수 — 신원 목록이 프롬프트/원장을 삼키지 않게. 21 은 verify_gate_log
#: 의 ``failed_tests`` 상한과 같은 값(같은 이유, 같은 데이터).
_MAX_FAILED_IDS = 21
_MAX_STR = 2000


class EpisodeContractError(ValueError):
    """행이 계약을 어겼다 — **원장에 쓰지 않는다.**

    호출자 버그다(리터럴 이벤트명·리터럴 HALT 라벨을 우리 코드가 넘긴다). 조용히
    잘못된 행을 쓰면 스코어러가 그걸 사실로 읽고, 그게 이 로그가 막으려는 바로 그
    실패다. 삼키지 않고 터뜨리는 쪽이 싸다.
    """


def enabled(env: Mapping[str, str] | None = None) -> bool:
    """Default-OFF 게이트 (strict ``"true"``/``"1"`` — sibling SI 게이트와 동일).

    ``env`` 를 주면 그쪽에서 읽는다 — ``runtime.cli.role_enabled(role, env)`` 와 같은
    레포 관례.

    🔴 이 인자가 왜 필요한가 (2026-08-05 memB 실런에서 실증): `goal_campaign` **부모**
    프로세스는 config 의 ``env_overlay`` 를 안 받는다(자식에게만 씌운다). 그래서
    부모에서 고아 HALT 를 대신 찍으려 할 때 `os.environ` 을 보면 **항상 꺼져 보이고
    조용히 아무 일도 안 한다.** 게이트는 그것이 통제하는 프로세스의 env 에서 읽어야
    한다 — 그 프로세스가 호출자와 다르면 명시적으로 넘겨야 한다.
    """
    src = os.environ if env is None else env
    return str(src.get(_ENV, "")) in ("true", "1")  # tier: T2


def log_path(state_dir: Path | str) -> Path:
    return Path(state_dir).joinpath(*_LOG_REL)


# ─────────────────────────── HALT 어휘 매핑 ───────────────────────────
# Public stop vocabulary belongs to this already-delivered contract module,
# not the optional T9 orchestration producer. first_run re-exports the same map.
BINDING_BY_HALT = {
    "verify_green": "green",
    "max_cycles": "cycles",
    "consecutive_red": "red_streak",
    "budget_exhausted": "budget",
    "human_halt": "aborted",
    "idempotent_retry": "red_streak",
    "evidence_quota_exhausted": "budget",
}


def producer_binding_by_halt():
    """Check loaded producer drift without importing or discovering T9 code."""
    producer = sys.modules.get("agi_v8_1.runtime.first_run")
    if producer is None:
        return BINDING_BY_HALT
    return vars(producer)["_BINDING_BY_HALT"]


#
# 🔑 매핑을 **생산자에서 도출한다.** 2026-08-04 P0-5 에서 정확히 이걸 안 해서
# 자율성 지표 2개가 6주간 거짓이었다(``red_streak`` 라는 binding_limit **값**을
# ``halt_reason`` 과 비교했는데, halt_reason 이 갖는 건 그 dict 의 **키**들이었다).
# 아래 ``_assert_producer_contract`` 가 생산자의 키 집합을 확인하므로, 생산자가
# 어휘를 바꾸면 조용히 오분류되는 대신 **여기서 터진다.**

_HALT_BY_INTERNAL: Mapping[str, str] = {
    "verify_green": "green",
    "budget_exhausted": "budget",
    "max_cycles": "cycles",
    "consecutive_red": "red_streak",
    # __SLOT_HUMAN_HALT_VOCAB_2026_08_06__ 사람이 멈췄다 → ``aborted``
    # ("runner declined to continue"). ⛔ 계약의 ``refused`` 로 보내면 안 된다 —
    # 그건 agent-bound 라 자율성 지분이 부푼다. 근거는 ``first_run._BINDING_BY_HALT``
    # 의 같은 SLOT 에 있고, 두 맵은 **반드시 같은 키 집합**이어야 한다(아래 계약).
    "human_halt": "aborted",
    # __SLOT_ANTI_IDEMPOTENT_RETRY_2026_08_08__ 흔적 0 인 재시도 → ``red_streak``.
    # 근거는 ``first_run._BINDING_BY_HALT`` 의 같은 SLOT 에 있고, 두 맵은 **반드시
    # 같은 키 집합**이어야 한다(위 계약 검사가 양방향으로 문다).
    "idempotent_retry": "red_streak",
    # __SLOT_EVIDENCE_QUOTA_HALT_2026_09_11__ 증거 쿼터 소진(외부 상한) → ``budget``.
    # 근거는 ``first_run._BINDING_BY_HALT`` 의 같은 SLOT 에 있고, 두 맵은 **반드시
    # 같은 키 집합**이어야 한다(위 계약 검사가 양방향으로 문다).
    "evidence_quota_exhausted": "budget",
}

#: ``first_run`` 이 f-string 으로 만드는 열린 접두사들 — 값이 무한하다.
_HALT_PREFIXES: Mapping[str, str] = {
    "cycle_abort:": "aborted",    # 루프가 터졌다
    "cycle_blocked:": "aborted",  # 러너가 계속을 거절했다
}


def _assert_producer_contract() -> None:
    """두 맵의 키 집합이 **같은지** 확인한다. ⚠️ 양방향이다.

    🔴 **2026-08-06 정정: 이 검사는 단방향이었고, 위험한 방향으로는 한 번도
    발동하지 않았다.** ``set(_BINDING_BY_HALT) - set(_HALT_BY_INTERNAL)`` 만
    봤으므로:

      - ``_HALT_BY_INTERNAL`` 만 고치면 → 조용히 통과. 그런데 ``_binding_limit``
        은 그 사유를 모르므로 리더보드 쪽이 낡은 채로 남는다.
      - **둘 다 안 고치면** → 당연히 통과. "조용한 뭉개짐이 구조적으로 불가능"
        이라던 주장이 정반대였다.

    반대 방향(``_BINDING_BY_HALT`` 만 고침)은 더 나쁘다 — ``emit_halt`` 마다
    ``EpisodeContractError`` 가 나고, 호출부의 ``except Exception → telemetry``
    가 그걸 삼켜 **무관한 에피소드의 HALT 행까지 통째로 사라진다**(적대검증이
    실행으로 재현: ``rows: 0, bound_by: {}``). 그래서 아래 호출부(:_tee_episode_log)
    는 이 예외를 telemetry 가 아니라 **contract** 로 분류하고 re-raise 한다.
    """
    diff = set(producer_binding_by_halt()) ^ set(_HALT_BY_INTERNAL)
    if diff:
        raise EpisodeContractError(
            f"두 halt 어휘 맵이 갈렸다: {sorted(diff)} — 한쪽만 고치면 리더보드의 "
            "bound_by 와 계약 원장이 서로 다른 진실을 말한다. 둘을 같이 고쳐라."
        )


def halt_label(halt_reason: str | None) -> str:
    """내부 ``halt_reason`` → 계약의 닫힌 8낱말 중 하나.

    ⚠️ **우리 ``_binding_limit`` 의 ``"refused"`` 를 계약의 ``"refused"`` 로 보내면
    안 된다.** 우리 것은 ``promote_gate_armed`` / ``v8_disabled`` /
    ``budget_refused`` — **운영자 게이트가 막은 것**이지 에이전트의 판단이 아니다.
    계약의 ``refused`` 는 agent-bound 로 계수되므로(공개 ``_AGENT_BOUND``), 그대로
    매핑하면 우리 자율성 지분이 부풀어 오른다. ⇒ ``aborted`` 로 보낸다. 공개 README
    가 ``aborted`` 를 "crashed, **or the runner declined to start/continue**" 로
    명시해둔 이유가 이것이다.

    ⇒ 그래서 계약의 ``refused`` 는 **지금 우리에게 도달 불가능한 라벨**이다.
    "불가능한 목표라고 정직하게 판단하고 멈춘다"를 생산할 경로가 아직 없다.
    """
    if halt_reason is None or not str(halt_reason).strip():
        return "unknown"
    reason = str(halt_reason).strip()
    if reason in _HALT_BY_INTERNAL:
        return _HALT_BY_INTERNAL[reason]
    for prefix, label in _HALT_PREFIXES.items():
        if reason.startswith(prefix):
            return label
    # promote_gate_armed / v8_disabled / budget_refused … — 러너가 거절했다.
    return "aborted"


# ─────────────────────────── 행 만들기 ───────────────────────────


def _clip(value: Any, limit: int = _MAX_STR) -> str | None:
    if value is None:
        return None
    return str(value)[:limit]


def _int_or_none(value: Any) -> int | None:
    """모름은 ``None``. **절대 0 이 아니다** — 계약 규칙.

    0 은 "쟀는데 0" 이고 None 은 "안 쟀다" 다. 이 둘을 섞으면 스코어러가 싼 턴과
    모르는 턴을 구분할 수 없다.
    """
    if value is None or isinstance(value, bool):
        return None if value is None else int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _base_row(event: str, cycle_id: str | None, ts: float | None) -> dict[str, Any]:
    if event not in EVENTS:
        raise EpisodeContractError(
            f"{event!r} 은 계약 6종이 아니다: {list(EVENTS)}")
    return {
        "schema_version": SCHEMA_VERSION,
        "ts": float(ts) if ts is not None else time.time(),
        "event": event,
        # 계약이 말하지 않은 추가 필드. VERIFY·COST·HALT 에 사이클 식별자가 없으면
        # "직전 사이클 이벤트만 읽기"가 불가능하다 — 3단계가 이것 없이는 못 선다.
        # 레포 관례를 따른다(``si_verify_gate_log_v1`` 이 167/167 로 쓰고 있다).
        "cycle_id": _clip(cycle_id, 200),
    }


class EpisodeAuditError(RuntimeError):
    """**결정에 귀속되는 행**을 못 썼다 — 조용히 넘어가면 안 되는 종류의 실패.

    ``EpisodeContractError``(스키마 위반 = 호출자 버그)와 다르다. 이건 IO 실패인데,
    **그 행이 없으면 그 결정이 일어나지 않은 것과 같아지는** 경우다.
    """


#: 감사 등급 행이 못 써진 기록. ⚠️ **원장에 못 적는다** — 원장이 안 써져서 생긴
#: 사건을 원장에 적을 수는 없다. 프로세스 안에 남겨서 호출자가 **다른 sink**
#: (run 리포트·반환 payload)로 실어내게 한다.
_AUDIT_FAILURES: list[dict[str, Any]] = []


def audit_failures() -> tuple[dict[str, Any], ...]:
    """이 프로세스에서 못 쓴 감사 등급 행들. 비었으면 원장이 그 축에서 완전하다."""
    return tuple(_AUDIT_FAILURES)


def reset_audit_failures() -> None:
    """테스트/장기 프로세스용. ⛔ 프로덕션 경로에서 부르지 말 것 — 사실을 지운다."""
    _AUDIT_FAILURES.clear()


def _write(state_dir: Path | str, row: Mapping[str, Any], *,
           audit: bool = False) -> None:
    """행을 젤에 append. **스키마는 이미 검증된 뒤에만** 여기 온다.

    🔑 **치명도는 전역 모드가 아니라 호출지점의 성질이다** (2026-08-06, KiroCrew
    분석). 예전엔 6종 **전부** IO 실패를 telemetry 로 삼켰다. 핫패스에는 옳지만,
    그러면 "원장이 불완전하다"가 **어디에도 안 남고**, fail-closed 분기를 코드에
    써놔도 **도달 불가능한 죽은 분기**가 된다.

    ⛔ 그렇다고 전면 fail-closed 로 가지 않는다 — 사이클이 텔레메트리 때문에 죽는다.
    **아주 좁은 클래스만** 감사 등급으로 올린다: 그 행이 없으면 그 결정이 원장에서
    사라지는 것(HALT — 없으면 그 에피소드가 ``bound_by`` 계수에서 통째로 증발한다.
    2026-08-04 에 실제로 겪었고 7c43fbc 로 고친 그 결함).
    """
    try:
        atomic_append_jsonl(log_path(state_dir), row)
    except Exception as exc:  # noqa: BLE001 — 로깅이 사이클을 죽이면 안 된다
        if audit:
            _AUDIT_FAILURES.append({
                "event": str(row.get("event") or ""),
                "cycle_id": row.get("cycle_id"),
                "error": f"{type(exc).__name__}: {exc}"[:300],
            })
            # 세고 나서 올린다 — 호출자가 **거부할 수 있으면 거부**하라고.
            # 우리 현재 HALT 호출자는 둘 다 이미 종료 중이라 거부할 게 없고,
            # 대신 `audit_failures()` 를 다른 sink 로 실어낸다. 그 한계를 여기 적어둔다.
            raise EpisodeAuditError(
                f"감사 등급 행({row.get('event')})을 못 썼다: {exc}") from exc
        _swallowed(exc, site="runtime.episode_log._write:append", category="telemetry")


# ─────────────────────────── 6종 emitter ───────────────────────────


def build_cycle_context(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """이 사이클이 **어떤 설정으로 돌았는가**. D0 — 비교를 정직하게 만드는 계측.

    🔴 없어서 실제로 못 낸 결론(2026-07-31): *"flash+high 가 동급이고 빠르다"* 를
    **확정 금지**로 남겨야 했다. 원장에 강도가 없어서 "적게 생각했다"가 knob 이 꺼진
    건지 모델이 덜 생각한 건지 구분되지 않았기 때문이다. 워커 비교는 설정이 같아야
    성립하는데, 설정을 안 적으면 **같았는지조차 사후에 알 수 없다.**

    ⚠️ ``reasoning_effort_*`` 가 ``None`` 이면 **"요청 안 함"(모델 기본값)** 이지
    "최저"가 아니다. 이 둘을 섞으면 기본값 워커와 thinking 켠 워커를 같은 칸에 놓게
    된다(`enforcement/si_spend_ledger.py` 가 콜 행에서 이미 같은 구분을 한다).

    ⚠️ ``tz_offset_seconds`` 를 남기는 이유: 이 레포에서 문자열 시각으로 집계했다가
    TZ 때문에 결론이 77%→7% 로 뒤집힌 적이 있다. 행의 ``ts`` 는 epoch 이라 안전하지만
    **어느 지역에서 돌았는지**는 그 자체가 재현 정보다.

    ⛔ 값을 지어내지 않는다. 안 건 knob 은 ``None`` 으로 남는다.
    """
    src = os.environ if env is None else env

    def _s(name: str) -> str | None:
        return (str(src.get(name, "")).strip() or None)

    now = _dt.datetime.now().astimezone()
    offset = now.utcoffset()
    return {
        "reasoning_effort_deepseek": _s("AGI_V8_DEEPSEEK_REASONING_EFFORT"),
        "reasoning_effort_openai": _s("AGI_V8_OPENAI_REASONING_EFFORT"),
        # ⚠️ 핀이 있다고 그 모델이 **불렸다**는 뜻은 아니다 — 실제 호출 여부는
        # `DISPATCH.worker_source` 가 말한다. 여기 있는 건 "무엇을 쓰라고 했나"다.
        "worker_model_pinned": _s("DEEPSEEK_WORKER_MODEL"),
        "providers_enabled":
            str(src.get("AGI_V8_PROVIDERS_ENABLED", "")).strip().lower() == "true",
        "tz_name": now.tzname(),
        "tz_offset_seconds": int(offset.total_seconds()) if offset else 0,
    }


def emit_plan(
    state_dir: Path | str,
    *,
    cycle_id: str | None,
    plan_id: str,
    summary: str,
    parent: str | None = None,
    candidates_considered: int | None = None,
    memory: Mapping[str, Any] | None = None,
    context: Mapping[str, Any] | None = None,
    ts: float | None = None,
) -> None:
    """계획 하나. ``candidates_considered`` 는 **모르면 None**(0 아님).

    ``memory`` 는 계약 밖 추가 필드로, **이 계획을 무슨 기억이 정보했는가**를
    남긴다(`prev_cycle_events_read` · `episode_log_injected` · `failure_binding`).

    🔴 여기 있어야 하는 이유(A③): 같은 값이 `first_run_report.jsonl` 의 `per_cycle`
    에도 있는데, **벽시계 타임아웃이면 그 리포트가 아예 안 써진다.** 그러면 "기억이
    안 돌았다"와 "기억이 돌았는지 모른다"가 원장에서 같아진다 — HALT 가 타임아웃에
    사라지던 것(7c43fbc)과 **정확히 같은 뿌리**다.

    ⚠️ 그런데 HALT 와 달리 **부모가 대신 찍을 수 없다.** 부모(goal_campaign)는
    자식이 무엇을 읽었는지 모른다 — 그건 자식 내부 상태다. 그래서 해법이 다르다:
    부모 경로가 아니라 **읽은 직후·실행 전에** 원장에 박는다. append-only 라
    나중에 죽어도 이미 쓰인 줄은 안 지워진다.
    """
    if not enabled():
        return
    row = _base_row("PLAN", cycle_id, ts)
    row.update({
        "plan_id": _clip(plan_id, 200),
        "parent": _clip(parent, 200),
        "summary": _clip(summary),
        "candidates_considered": _int_or_none(candidates_considered),
        "memory": dict(memory) if memory is not None else None,
        # __SLOT_CYCLE_CONTEXT_2026_08_06__ D0 — 어떤 설정으로 돌았나.
        # ⛔ 7번째 이벤트 종류를 만들지 않는다. 6종은 닫힌 어휘고, 새 종류를 더하면
        # 스코어러가 우리 원장만 못 읽는다 — **자리가 있으면 채운다**(A① 과 같은 판단).
        "context": dict(context) if context is not None else None,
    })
    _write(state_dir, row)


def emit_dispatch(
    state_dir: Path | str,
    *,
    cycle_id: str | None,
    plan_id: str,
    task: str,
    role: str | None = None,
    worker: str | None = None,
    worker_source: str | None = None,
    n_parallel: int | None = None,
    call_id: str | None = None,
    end_ts: float | None = None,
    ts: float | None = None,
) -> None:
    """일감 하나를 워커에게.

    ``task`` 는 **무엇을 시켰는지**다. ⛔ 여기에 에이전트 **역할명**을 넣지 말 것 —
    2026-08-05 실런에서 `task=str(agent_kind)` 였고, 그래서 다음 사이클에 주입된
    문장이 통째로 ``attempted: critic`` 이었다. 형식적으로는 채널이 열렸는데
    **신호가 0**이었다. 링이 `decision=consensus` 만 나르던 것과 같은 병이다.
    누가 했는지는 아래 ``role`` 로 따로 남긴다 — 둘 다 사실이고, 겹치지 않는다.

    ``role`` 은 계약 밖 추가 필드(planner/architect/executor/critic/continuation).
    ⚠️ 역할 하나당 하위작업을 **지어내지 않는다.** 현재 파이프라인은 역할별 하위작업을
    기록하지 않으므로 전 역할이 같은 ``task`` 를 실어 나른다. 그게 사실이다 —
    "무엇을 시도했는가" 를 의미 수준으로 가르는 것은 Failure Ledger 의 몫이지
    이 필드를 그럴듯하게 채워서 될 일이 아니다.

    ``worker`` 는 **모델명**이다(계약 필드). 못 채우면 ``None`` — 계약은 지키지만
    리더보드 ``models.worker`` 를 못 낸다는 사실이 원장에 정직하게 남는다.

    ``worker_source`` 는 계약 밖 추가 필드로, **그 이름을 무엇을 근거로 적었는지**를
    남긴다(``env:DEEPSEEK_WORKER_MODEL`` / ``pipeline:no_provider_call`` 등).
    값만 있고 유도가 없으면 나중에 이 줄이 측정인지 추측인지 구분할 수 없다 —
    2026-08-04 에 정확히 그 구분이 없어서 원장의 `decision=consensus` 4/4 가
    제조된 값인 줄 6주간 몰랐다.

    ``call_id`` 는 계약 밖 추가 필드다. VERIFY/COST 가 같은 값을 실으면
    ``count(DISPATCH) == count(짝지어진 결과)`` 로 **삼킨 디스패치가 계수로 잡힌다**
    (코덱스 실측: custom_tool_call 208 / output 208, 고아 0).
    """
    if not enabled():
        return
    row = _base_row("DISPATCH", cycle_id, ts)
    row.update({
        "plan_id": _clip(plan_id, 200),
        "worker": _clip(worker, 200),
        "worker_source": _clip(worker_source, 200),
        "n_parallel": _int_or_none(n_parallel),
        "task": _clip(task),
        "role": _clip(role, 200),
        "call_id": _clip(call_id, 200),
        # __SLOT_CALL_INTERVAL_2026_08_07__ 계약 필드. 오늘은 **항상 ``None`` 이다.**
        #
        # ⛔ 이 행은 일을 시키기 **전에** 써야 한다(A3 교훈: 죽어도 "시작은 했다"가
        #    남아야 한다). 그러니 여기서 종료 시각을 알 방법이 없다 — ``None`` 은
        #    **모름이지 0 이 아니고**, 그 모름이 원장에 서 있는 게 맞다.
        # ⚠️ 짝짓기로도 못 채운다: 실측(2026-08-07) ``role``(4종) ↔ COST
        #    ``purpose``(3종) 교집합 **0**. 그 역할들은 스텁이라 과금 콜을 안 낸다.
        #    4역할이 실제로 provider 를 부르기 시작하면 그때 COST 행이 생기고
        #    ``call_id`` 로 짝지어진다 — 그런데 **그때는 COST 의 구간만으로 충분하다.**
        "end_ts": None if end_ts is None else float(end_ts),
    })
    _write(state_dir, row)


def emit_verify(
    state_dir: Path | str,
    *,
    cycle_id: str | None,
    target: str,
    command: str | None,
    ran: bool,
    verdict: str | None,
    failed_ids: Iterable[str] | None = None,
    call_id: str | None = None,
    ts: float | None = None,
) -> None:
    """검증 한 번. **실패는 개수가 아니라 신원으로** 남는다.

    개수는 실패가 회전해도 그대로다 — "8 failed" 가 이틀 연속이어도 같은 8건인지
    다른 8건인지 알 수 없다. 계약이 ``failed_ids`` 를 요구하는 이유.
    """
    if not enabled():
        return
    ids = [str(x)[:_MAX_STR] for x in (failed_ids or [])][:_MAX_FAILED_IDS]
    row = _base_row("VERIFY", cycle_id, ts)
    row.update({
        "target": _clip(target),
        "command": _clip(command),
        "ran": bool(ran),
        "verdict": _clip(verdict, 200),
        "failed_ids": ids,
        "call_id": _clip(call_id, 200),
    })
    _write(state_dir, row)


def emit_cost(
    state_dir: Path | str,
    *,
    cycle_id: str | None,
    usd: float,
    purpose: str,
    provider: str | None = None,
    model: str | None = None,
    input_per_token_usd: float | None = None,
    output_per_token_usd: float | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cache_hit: bool | None = None,
    usd_cumulative: float | None = None,
    call_id: str | None = None,
    scope: str | None = None,
    wall_ms: int | None = None,
    usage_measured: bool | None = None,
    reasoning_tokens: int | None = None,
    reasoning_effort: str | None = None,
    finish_reason: str | None = None,
    request_observation: Mapping[str, Any] | None = None,
    start_ts: float | None = None,
    end_ts: float | None = None,
    ts: float | None = None,
) -> None:
    """과금 콜 하나. **실패한 콜도 행을 받는다** — ``usd=0`` 은 진짜 무료일 때만.

    ``usd_cumulative`` 는 계약 밖 추가 필드다(코덱스 ``token_count`` 가
    ``total_token_usage`` 와 ``last_token_usage`` 를 같이 싣는 것과 같은 이유):
    누적을 합산으로 재도출하면 **한 줄만 빠져도 조용히 틀린다.** 호출자가 원장
    (``episode_ledger``)에서 받아 넘긴다. 모르면 ``None`` — 0 이 아니다.

    🔴 ``scope`` 는 **이중계상을 막는 필수 구분자**다 (D0②). 이 원장에는 두 종류의
    COST 행이 있다:

    ``"call"``           콜 하나 (``si_spend_ledger.record_llm_call`` tee)
    ``"episode_total"``  에피소드 합계 한 줄 (``first_run._tee_episode_log``)

    ⛔ **둘을 그냥 더하면 두 배가 된다.** 소비자는 반드시 ``scope`` 로 거르거나
    :func:`cost_rollup` 을 써야 한다. ``None`` 은 이 구분이 생기기 전(2026-08-06
    이전)에 쓰인 행 — 레거시고, 그것도 **모름이지 0 이 아니다.**

    ``provider``·``model``·``input_per_token_usd``·
    ``output_per_token_usd``는 같은 콜에 적용된 가격 출처다. 모델만 맞게
    적고 단가를 다른 표에서 가져와도 조용히 통과하는 2026-08-28
    Track2 과대계상 재발을 막는다. 모르면 ``None``이지 0이 아니다.

    ``wall_ms``·``usage_measured`` 도 계약 밖 추가 필드다. 전자는 콜 지연(D0 계측
    구멍 ③), 후자는 **이 금액이 실측인지 모름인지** — ``usd=0.0`` 하나로는
    "진짜 공짜"와 "토큰 증거가 없어 0 으로 적음"이 구분되지 않는다.

    __SLOT_D0_REASONING_ON_COST_2026_08_07__ ``reasoning_tokens`` ·
    ``reasoning_effort`` · ``finish_reason`` — D0 계측 구멍의 나머지.

    ⚠️ **셋 다 생산자는 이미 있었다.** 2026-08-07 실측: ``reasoning_tokens`` 는
    ``executor_log.jsonl`` 에 109/311 행이 값을 갖고 있었고(내 투두는 *"어느 원장에도
    없음"* 이라 적어뒀다 — 틀렸다), ``finish_reason`` 은 ``provider.last_usage`` 에
    있는데 ``out_summary`` 로 안 옮겨졌으며, ``reasoning_effort`` 는 openai 경로만
    낸다. ⇒ 구멍은 *"생산이 없다"* 가 아니라 **"계약 원장까지 안 온다"** 였다.

    - ``reasoning_effort`` 가 ``None`` = *"요청 안 함"* 이지 **"최저"가 아니다.**
      knob 이 없는 provider 는 값이 안 실리고, 그건 모름이다.
    - ``finish_reason`` 이 없으면 잘린 응답과 정상 종료가 구분되지 않는다 —
      추론 모델은 ``reasoning_tokens`` 가 같은 출력 예산에서 나가므로 조용히 잘린다
      (2026-08-07 pod B 가 정확히 그렇게 통째로 사라졌다).

    __SLOT_EPISODE_CTX_FALLBACK_2026_08_07__ ``cycle_id``·``call_id`` 를 호출자가
    안 주면 :mod:`~agi_v8_1.runtime.episode_ctx` 의 문맥에서 **폴백으로만** 읽는다.
    ⛔ 명시 인자가 항상 이긴다 — 안 그러면 바깥 사이클이 안쪽 하위콜의 신원을 훔친다.
    실측 근거: 이 배선 전에는 COST 93행 **전부** ``cycle_id=None`` 이라 어느
    사이클도 자기 비용을 몰랐고, ``unpaired_call_ids()`` 가 64 를 반환했다.

    __SLOT_CALL_INTERVAL_2026_08_07__ ``start_ts``/``end_ts`` — 이 콜의 **구간**.

    🔑 **짝짓기로 도출하지 않기로 했다**(2026-08-07 결정). 이유 셋:

    1. 우리 원장에선 애초에 짝짓기가 필요 없다 — ``wall_ms`` 가 93/93 이라 행
       하나가 이미 자기 구간을 안다. 정작 짝지어야 할 DISPATCH 쪽은 ``wall_ms``
       가 **0/64** 고, ``role``(4종) ↔ ``purpose``(3종) 교집합이 **0** 이라
       **짝지을 상대가 구조적으로 없다**(그 역할들은 스텁이라 콜을 안 낸다).
    2. 🔴 **공개 계약의 COST 행에는 ``wall_ms`` 가 없다** — 우리 확장 필드다.
       외부 제출자는 duration 을 어디에도 신고하지 않으므로 계약의 동시성
       검산식(``s.ts ≤ row.ts < s.end_ts``)이 **원리적으로 설 수 없다.**
    3. 🔑 **파생은 소비자마다 다시 구현된다.** ``ts - wall_ms/1000`` 을 각자
       계산하면 누군가는 단위를 틀린다(ms↔s). 계약 필드가 사는 것은 도출
       *가능성* 이 아니라 **오해 불가능성**이다.

    ⛔ ``ts`` 의 뜻은 **안 바꾼다** — 그건 *"이 행이 기록된 시각"* 이고 나머지 5개
    event 가 같은 뜻으로 쓴다. 오늘은 write ≈ 콜 종료라 ``end_ts`` 와 값이 같지만,
    진짜 비동기가 되어 둘이 갈라지는 날 **그 사실이 원장에 드러난다.**

    ⚠️ ``wall_ms`` 는 ``perf_counter``(monotonic) 차이고 ``ts`` 는 wall clock 이다.
    역산한 ``start_ts`` 는 NTP 조정·서스펜드에서 밀릴 수 있다 — **근사이지 측정이
    아니다.** 호출자가 진짜 시작 시각을 알면 그걸 넘겨라(명시가 이긴다).
    """
    if not enabled():
        return
    if cycle_id is None or call_id is None:
        from agi_v8_1.runtime import episode_ctx as _ctx
        _c, _k = _ctx.current()
        cycle_id = cycle_id if cycle_id is not None else _c
        call_id = call_id if call_id is not None else _k
    row = _base_row("COST", cycle_id, ts)
    row.update({
        "usd": float(usd),
        "provider": _clip(provider, 200),
        "model": _clip(model, 200),
        "input_per_token_usd": (
            None if input_per_token_usd is None else float(input_per_token_usd)
        ),
        "output_per_token_usd": (
            None if output_per_token_usd is None else float(output_per_token_usd)
        ),
        "input_tokens": _int_or_none(input_tokens),
        "output_tokens": _int_or_none(output_tokens),
        "cache_hit": None if cache_hit is None else bool(cache_hit),
        "purpose": _clip(purpose),
        "usd_cumulative": (None if usd_cumulative is None
                           else float(usd_cumulative)),
        "call_id": _clip(call_id, 200),
        "scope": _clip(scope, 32),
        "wall_ms": _int_or_none(wall_ms),
        "usage_measured": (None if usage_measured is None else bool(usage_measured)),
        "reasoning_tokens": _int_or_none(reasoning_tokens),
        # ⛔ None = "요청 안 함". 문자열로 접어 "default" 라 적지 않는다.
        "reasoning_effort": _clip(reasoning_effort, 32),
        "finish_reason": _clip(finish_reason, 64),
    })
    # Additive request-only metadata. This writer does not mask arbitrary
    # mappings, so the closed sanitizer is mandatory even for direct callers.
    if request_observation is not None:
        from agi_v8_1.runtime.llm_call_observation import sanitize_observation

        row["request_observation"] = sanitize_observation(request_observation)
    # ``end_ts`` 미지정 = 오늘의 참: write 시각이 콜 종료다. 명시가 이긴다.
    _end = float(end_ts) if end_ts is not None else float(row["ts"])
    _start = start_ts
    if _start is None and wall_ms is not None:
        _w = _int_or_none(wall_ms)
        # ⛔ 음수/비정상 wall_ms 로 미래에서 과거로 흐르는 구간을 만들지 않는다.
        _start = _end - (_w / 1000.0) if (_w is not None and _w >= 0) else None
    row["start_ts"] = None if _start is None else float(_start)
    row["end_ts"] = _end
    _write(state_dir, row)


COST_SCOPE_CALL = "call"
COST_SCOPE_EPISODE = "episode_total"


def cost_rollup(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """콜 합계 vs 에피소드 합계를 **대조**한다 — 콜 단위 계측의 진짜 값어치.

    합계를 두 경로로 얻으면 서로를 검산할 수 있다. 어긋나면 둘 중 하나가 틀린
    것이고, **어긋난다는 사실 자체가 발견**이다(이 레포는 "돈은 썼는데 기록이
    없다"를 반복해서 겪었다).

    ⚠️ ``delta`` 가 ``None`` 인 경우와 ``0.0`` 인 경우는 다르다. 전자는 한쪽 축이
    아예 없어서 **대조를 못 한 것**이고, 후자는 대조해서 일치한 것이다.

    ⛔ 두 축을 더하지 않는다. 더하면 그게 이중계상이다.
    """
    calls = [r for r in rows
             if r.get("event") == "COST" and r.get("scope") == COST_SCOPE_CALL]
    episodes = [r for r in rows
                if r.get("event") == "COST" and r.get("scope") == COST_SCOPE_EPISODE]
    legacy = [r for r in rows
              if r.get("event") == "COST" and r.get("scope") is None]

    call_sum = sum(float(r.get("usd") or 0.0) for r in calls) if calls else None
    episode_sum = (sum(float(r.get("usd") or 0.0) for r in episodes)
                   if episodes else None)
    return {
        "call_sum_usd": call_sum,
        "episode_sum_usd": episode_sum,
        "delta_usd": (None if call_sum is None or episode_sum is None
                      else round(episode_sum - call_sum, 10)),
        "calls_counted": len(calls),
        # 🔑 **세 칸이다.** 이 셋을 두 칸으로 접으면 이 레포가 반복해서 겪은 그
        # 실수가 된다: `False`(쟀는데 실측이 아님)와 `None`(호출자가 주장조차 안 함)은
        # 다른 사실이다. 둘 다 `call_sum` 을 하한으로 만들지만 **고치는 방법이 다르다** —
        # 전자는 provider 가 토큰을 안 줬고, 후자는 우리 호출부가 계측을 안 넘겼다.
        "calls_measured": sum(1 for r in calls if r.get("usage_measured") is True),
        "calls_unmeasured": sum(1 for r in calls
                                if r.get("usage_measured") is False),
        "calls_measurement_unknown": sum(1 for r in calls
                                         if r.get("usage_measured") is None),
        "episode_rows": len(episodes),
        "legacy_rows_without_scope": len(legacy),
    }


def _human_actor_enabled(env: Mapping[str, str] | None = None) -> bool:
    """행위자 컨텍스트 첨부 게이트 — 기본 ON(``emit_halt``·형제 게이트와 같은
    strict 어휘: ``os.environ.get(NAME, "").strip().lower() in ("true", "1")``
    이 이 파일의 ``enabled()`` 관례지만, 이건 **판정** 게이트가 아니라 필드
    첨부 스위치라 값 자체는 그 관례와 무관하다 — OFF 계약만 지키면 된다).
    """
    src = os.environ if env is None else env
    return str(src.get(_HUMAN_ACTOR_ENV, "true")).strip().lower() not in ("false", "0")


def _os_actor_context() -> dict[str, Any]:
    """OS 프로세스가 관측한 행위자 컨텍스트 — **인증이 아니라 감사 기록이다.**

    ⛔ 이걸 신원 증명이라고 부르지 마라. ``os_user``/``uid`` 는 **같은 uid 로
    도는 코드가 그대로 위조할 수 있다** — 이 프로세스가 ``os.getuid()`` 를
    읽는다는 것 자체가, 공격자 코드가 같은 프로세스/uid 안에서 돌면 똑같이
    읽고 똑같이 적을 수 있다는 뜻이다. OS 신원 증명은 sudo 감사 로그나 SSH
    인증서 같은 **다른 계층**의 몫이고, 여기서 하는 일은 "무엇이 관측됐는가"를
    적는 것뿐이다 — 오늘 감사 6라운드가 반복해 잡은 병이 정확히 이 혼동이었다
    ("검사를 추가했다"를 "공격이 막혔다"로 적는 것).
    """
    try:
        os_user = getpass.getuser()
    except Exception:  # noqa: BLE001 — 관측 실패는 None, 사이클을 안 죽인다
        os_user = None
    try:
        uid = os.getuid() if hasattr(os, "getuid") else None
    except Exception:  # noqa: BLE001
        uid = None
    try:
        interactive = bool(sys.stdin.isatty())
    except Exception:  # noqa: BLE001 — stdin 이 닫혀있거나 없는 논-tty 실행도 있다
        interactive = False
    return {
        "os_user": os_user,
        "uid": uid,
        "interactive": interactive,
        # ⛔ 정직한 한계 표지 — 소비자가 이 필드를 "검증된 신원"으로 오독하지
        # 않게 값 자체에 박아둔다.
        "auth_level": "os_process_unverified",
    }


def emit_human(
    state_dir: Path | str,
    *,
    cycle_id: str | None,
    note: str,
    actor: Mapping[str, Any] | None = None,
    ts: float | None = None,
) -> None:
    """사람이 개입했다. **자율성에서 차감된다 — 0 이 만점.**

    🔴 2026-08-06 확인 당시엔 이 emitter 에 프로덕션 호출자가 **없었다**. 이제
    ``si_lanes.promote.promote_patches`` 가 성공한 승격에서 부른다.

    __SLOT_R9_T4_2026_08_17__ ``actor`` (게이트 ``AGI_V8_HUMAN_EVENT_ACTOR``,
    기본 ON) — 이 행에 신원 필드가 하나도 없었다. **누가**(``os_user``/``uid``) ·
    **어디서**(``interactive``, TTY 여부) 는 여기서 자동으로 채운다. **무엇을**
    (승격 대상 다이제스트 등)은 호출자가 아는 것이라 ``actor`` 로 넘기면
    자동값 위에 병합된다(``dict.update`` — 호출자 값이 이긴다).
    ⛔ 이건 인증이 아니다 — ``_os_actor_context`` 의 docstring 을 봐라.
    OFF 면 ``actor`` 키 자체가 행에서 빠진다(패치 이전과 byte-identical).
    """
    if not enabled():
        return
    row = _base_row("HUMAN", cycle_id, ts)
    row["note"] = _clip(note)
    if _human_actor_enabled():
        ctx = _os_actor_context()
        if actor:
            ctx.update(dict(actor))
        row["actor"] = ctx
    _write(state_dir, row)


def emit_halt(
    state_dir: Path | str,
    *,
    cycle_id: str | None,
    reason: str,
    detail: str | None = None,
    ts: float | None = None,
    env: Mapping[str, str] | None = None,
) -> None:
    """에피소드가 끝났다. ``reason`` 은 닫힌 8낱말 중 하나여야 한다.

    내부 ``halt_reason`` 을 그대로 넘기지 말고 ``halt_label()`` 을 통과시킨다.
    ``detail`` 에 원래 값을 남겨 서술을 잃지 않는다(집계는 라벨로, 서술은 detail 로).

    🔑 **이 행만 감사 등급이다** (F1-b). 6종 중 유일하게, 없으면 그 에피소드가
    ``bound_by`` 계수에서 **통째로 증발한다** — ``unknown`` 으로도 안 잡히고 아예 없는
    게 된다(공개 계약이 명시적으로 금지한 상태). 2026-08-04 memA 런에서 실제로 겪었고
    7c43fbc 로 고쳤는데, 그때 고친 건 "부모가 대신 찍는 경로"였고 **쓰기 자체가
    실패하는 경우**는 여전히 조용했다. 이제 안 조용하다.

    IO 실패 시 :class:`EpisodeAuditError` 를 올리고 :func:`audit_failures` 에 남긴다.
    ⚠️ 우리 두 호출자(first_run·goal_campaign)는 **이미 종료 중이라 거부할 게 없다** —
    그래서 실질 효과는 "거부"가 아니라 **원장 불완전이 다른 sink 로 드러남**이다.
    그 한계를 여기 적어둔다. 거부가 의미 있는 호출자가 생기면 그때 거부하면 된다.
    """
    # ⚠️ 부모 프로세스가 대신 찍는 경로가 있어 게이트 원천을 주입받는다 —
    # `enabled()` 독스트링의 2026-08-05 실측 참조.
    if not enabled(env):
        return
    if reason not in HALT_REASONS:
        raise EpisodeContractError(
            f"{reason!r} 은 닫힌 HALT 어휘가 아니다: {list(HALT_REASONS)} — "
            "내부 halt_reason 을 넘겼다면 halt_label() 을 먼저 통과시켜라")
    _assert_producer_contract()
    row = _base_row("HALT", cycle_id, ts)
    row.update({"reason": reason, "detail": _clip(detail)})
    _write(state_dir, row, audit=True)


# ─────────────────────────── 읽기 (ungated) ───────────────────────────


def read_rows(state_dir: Path | str) -> list[dict[str, Any]]:
    """기록된 행 전부. 줄 단위로 관대하게 파싱. Ungated.

    깨진 줄 하나가 나머지 로그를 지우면 안 된다(통짜 ``read_jsonl`` 이면 그렇게
    된다). 깨진 줄은 **세어서 경고**하고 건너뛴다 — verify_gate_log 와 달리 합성
    행으로 바꾸지 않는다: 거기선 합성 RED 가 fail-closed 였지만, 여기서 event 를
    모르는 합성 행은 어느 축에도 못 들어가고 계수만 흐린다.
    """
    path = log_path(state_dir)
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        _swallowed(exc, site="runtime.episode_log.read_rows:read", category="telemetry")
        return []
    rows: list[dict[str, Any]] = []
    bad = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if not isinstance(obj, dict):
                raise ValueError("non-object line")
            rows.append(obj)
        except ValueError:
            bad += 1
    if bad:
        logger.warning("episode_log: %d malformed line(s) in %s skipped", bad, path)
    return rows


def bound_by(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """HALT 행을 세어 리더보드의 ``limits.bound_by`` 를 만든다.

    통일된 어휘의 배당금: **재도출이 아니라 계수다.** 어휘가 두 벌이던 시절에는
    이 함수를 쓸 수 없었다(``goal_green`` 을 ``green`` 으로 옮기는 번역이 필요했고,
    번역이 있으면 둘이 어긋날 수 있다).
    """
    counts: dict[str, int] = {}
    for r in rows:
        if r.get("event") != "HALT":
            continue
        label = str(r.get("reason") or "unknown")
        if label not in HALT_REASONS:
            label = "unknown"
        counts[label] = counts.get(label, 0) + 1
    return counts


#: 3단계 요약이 프롬프트에 넣을 수 있는 최대 글자수. 사이클 수만큼 선형으로 늘면
#: 예산을 먹고 그건 워커 비교를 다시 오염시킨다 — 계획서 §무엇이 사이클을 넘어야 하나.
SUMMARY_MAX_CHARS = 1200
_SUMMARY_MAX_FAILED = 5
_SUMMARY_TASK_CHARS = 300


def summarize_for_next_cycle(
    rows: Sequence[Mapping[str, Any]],
    *,
    exclude_cycle_id: str | None = None,
) -> dict[str, Any]:
    """직전 사이클에서 **다음 사이클이 알아야 할 최소 집합**을 뽑는다.

    실측 근거(2026-08-04): 어젯밤 링에 넘어간 것은
    ``{cycle, decision:"consensus", status:"accepted"}`` 뿐이었고 — 게다가 그
    consensus 는 제조된 값이었다 — 사이클 번호를 빼면 정보가 0이었다. 반면 luna 가
    **발명한** ``run_result.json`` 에는 ``"could not locate chest"`` 같은 **실패
    사유**가 있었고, 그래서 luna 만 이어붙였다. 능력 차이가 아니라 채널 차이였다.

    ⇒ 넘기는 것: 직전 HALT 사유 · VERIFY 실패 **신원** · DISPATCH 가 시도한 것 ·
    누적 COST(예산 인식). ⛔ 전체 로그를 붓지 않는다.

    ``exclude_cycle_id`` 는 **현재** 사이클을 제외한다 — 자기가 방금 쓴 줄을 자기
    기억으로 되먹이면 사이클 간 전달이 아니라 자기참조가 된다.

    🔴 **블록은 사이클 하나에만 스코프된다** (2026-08-06 적대검증이 잡음). 이전에는
    `prev_cycle_id`/`prev_task` 는 마지막 행 기준인데 `prev_roles` 만 **에피소드 전
    이력의 합집합**이었다. 그래서 직전 사이클이 planner 에서 죽었어도 프롬프트엔
    ``agents_ran: planner, architect, executor, critic`` 이 실렸다 — A① 이 고쳤다고
    선언한 그 혼동을, 이번엔 침묵이 아니라 **거짓 문장 주입**으로 재생산한 것이다.

    🔑 그래서 역할 분담을 명시한다: **이 요약 = 직전 사이클 하나. 이력 = Failure
    Ledger.** 둘을 한 함수가 섞으면 블록 안에서 어느 줄이 어느 사이클 얘긴지 알 수
    없다.
    """
    prior = [r for r in rows
             if not exclude_cycle_id or r.get("cycle_id") != exclude_cycle_id]

    # 직전 사이클 = ``prior`` 의 **마지막 행이 속한** 사이클. ⛔ 이벤트 종류별
    # 마지막 행에서 각각 뽑으면 안 된다 — HALT 는 c2, VERIFY 는 c1 처럼 서로 다른
    # 사이클에서 뽑혀 한 블록 안에서 사실이 섞인다.
    #
    # __SLOT_PREV_CYCLE_NEEDS_WORK_NOT_JUST_SPEND_2026_08_07__ 🔴 *"행이 있다"* 는
    # *"사이클이 돌았다"* 가 아니다. **적대검증이 실측으로 잡은 회귀**:
    # 2026-08-07 에 리뷰 콜에 ``cycle_scope`` 를 걸자, **리뷰가 거절해서 디스패치가
    # 아예 안 된 틱**의 COST 행 하나가 ``prev_cycle_id`` 를 차지했다. 그 사이클엔
    # 일한 흔적이 없으므로 다음 사이클이 받는 기억 블록이 **통째로 빈다** — 진짜
    # 직전 사이클의 기억을 유령이 가린다. (그 전에는 리뷰 행의 ``cycle_id`` 가
    # ``None`` 이라 **우연히** 건너뛰어지고 있었다.)
    #
    # ⚠️ 첫 처방은 *"PLAN 이 있어야 사이클"* 이었는데 **너무 좁았다** — 회귀 9건이
    #    잡았다. ``first_run`` 경로는 PLAN 없이 DISPATCH/VERIFY/COST 만 내고,
    #    그것도 진짜 사이클이다(PLAN 을 실행 전에 두는 A3 은 ``cli`` 경로에만 있다).
    #
    # ⇒ 유령의 특징은 *PLAN 부재* 가 아니라 **COST 밖에 없다**는 것이다:
    #    돈은 썼는데 일한 흔적이 하나도 없다. 그래서 술어는
    #    **"COST 아닌 이벤트가 하나라도 있는 사이클"** 이다.
    # ⛔ 폴백으로 "없으면 아무 행이나"를 두지 않는다 — 그러면 유령이 그대로 돌아온다.
    _worked = {str(r.get("cycle_id")) for r in prior
               if r.get("cycle_id") and r.get("event") != "COST"}
    prev_cycle_id: str | None = None
    for r in reversed(prior):
        cid = r.get("cycle_id")
        if cid and str(cid) in _worked:
            prev_cycle_id = str(cid)
            break
    scoped = ([r for r in prior if str(r.get("cycle_id") or "") == prev_cycle_id]
              if prev_cycle_id is not None else [])

    by_event: dict[str, list[Mapping[str, Any]]] = {}
    for r in scoped:
        by_event.setdefault(str(r.get("event") or ""), []).append(r)

    def _last(event: str) -> Mapping[str, Any] | None:
        seq = by_event.get(event) or []
        return seq[-1] if seq else None

    halt = _last("HALT")
    verify = None
    for r in reversed(by_event.get("VERIFY") or []):
        if r.get("failed_ids"):        # 신원이 실린 마지막 것 — 빈 것보다 정보가 많다
            verify = r
            break
    verify = verify or _last("VERIFY")
    dispatch = _last("DISPATCH")

    # 역할은 **순서를 지키며 중복 제거**한다. 마지막 DISPATCH 한 줄만 보면
    # `prev_task` 가 마지막 역할(critic/continuation)의 것으로 고정돼, 파이프라인이
    # 어디까지 갔는지가 사라진다 — planner 에서 죽은 사이클과 critic 까지 간 사이클이
    # 원장에서 같아 보였다. ⛔ set() 금지: 순서가 곧 진행 단계다.
    # ⛔ 그리고 ``scoped`` 만 본다 — 전 이력을 보면 위 함정으로 되돌아간다.
    prev_roles: list[str] | None = None
    if prev_cycle_id is not None:
        prev_roles = []
        for r in by_event.get("DISPATCH") or []:
            role = r.get("role")
            if role and str(role) not in prev_roles:
                prev_roles.append(str(role))

    # 직전 사이클이 **계획만 남기고 사라졌나.** A③ 이후 실행 전에 PLAN 이 찍히므로,
    # PLAN 은 있는데 HALT 가 없으면 "시작했고 아무 기록 없이 끝났다"는 뜻이다.
    # ⚠️ 이걸 안 말하면 그 사이클은 블록에서 **내용 0인 헤더**로만 나타난다.
    plan_only = bool(by_event.get("PLAN")) and not halt and not dispatch

    # 누적은 **합산으로 재도출하지 않는다**(한 줄만 빠져도 조용히 틀린다).
    # 마지막 행이 실어온 누적치를 쓰고, 없으면 None — 0 이 아니다.
    usd_cum = None
    for r in reversed(by_event.get("COST") or []):
        if r.get("usd_cumulative") is not None:
            usd_cum = float(r["usd_cumulative"])
            break

    return {
        # ⚠️ 이름 그대로 **직전 사이클의** 행 수다. 예전엔 `len(prior)`(전 이력)라
        # 사이클이 갈수록 단조 증가했고, 그 값이 "기억 증거"로 원장에 박혔다.
        "prev_cycle_events_read": len(scoped),
        "prev_cycle_id": prev_cycle_id,
        "prev_plan_only": plan_only,
        "prev_halt_reason": (halt or {}).get("reason"),
        "prev_halt_detail": (halt or {}).get("detail"),
        "prev_failed_ids": list((verify or {}).get("failed_ids") or [])[
            :_SUMMARY_MAX_FAILED],
        "prev_verify_ran": (verify or {}).get("ran"),
        "prev_task": (dispatch or {}).get("task"),
        "prev_roles": prev_roles,
        "usd_cumulative": usd_cum,
    }


def summary_as_prompt_block(summary: Mapping[str, Any]) -> str:
    """요약을 프롬프트에 붙일 한 덩어리 텍스트로. **길이는 코드로 고정**한다.

    ⚠️ 규칙을 코드로 고정하고 그 규칙을 테스트한다 — 자유 형식이면 사이클마다
    길이가 달라져 예산이 흔들리고, 그러면 워커 비교가 또 오염된다.
    빈 요약(직전 사이클 없음)은 **빈 문자열**을 낸다: 없는 기억을 있는 척하지 않는다.

    🔴 **헤더만 있는 블록을 내지 않는다** (2026-08-06 적대검증). A③ 이 실행 전에 PLAN
    을 찍기 시작하면서, 실행 중 죽은 사이클도 원장에 행이 1개 생겼다. 그러면
    ``prev_cycle_events_read`` 가 참이 되어 ``"[previous cycle]"`` 16자 — **사실이
    0개인 헤더**가 목표문 앞에 붙고, 원장엔 ``episode_log_injected: true`` 가 찍혔다.
    A③ 이전에는 정직하게 아무것도 안 붙었다. 즉 그 증분이 이 한 축을 **더 나쁘게**
    만들었다.

    ⇒ 고치는 방향은 "헤더를 지운다"가 아니다. 죽었다는 **사실 자체가 기억**이므로
    ``prev_plan_only`` 를 문장으로 말하고, 그래도 말할 게 없으면 빈 문자열을 낸다.
    """
    if not summary.get("prev_cycle_events_read"):
        return ""
    lines = ["[previous cycle]"]
    if summary.get("prev_cycle_id"):
        lines.append(f"cycle: {summary['prev_cycle_id']}")
    if summary.get("prev_halt_reason"):
        detail = summary.get("prev_halt_detail")
        lines.append(f"stopped_because: {summary['prev_halt_reason']}"
                     + (f" ({detail})" if detail else ""))
    elif summary.get("prev_plan_only"):
        # 계획만 남고 사라졌다 — 타임아웃·크래시·게이트 차단 중 하나다. 어느 것인지는
        # 원장에 없으므로 **고르지 않는다.** 확실한 것만 말한다.
        lines.append("stopped_because: unrecorded "
                     "(the cycle started and left no dispatch or halt)")
    if summary.get("prev_task"):
        # ⚠️ 목표문은 원장에 최대 2000자로 들어간다(fat write). 그걸 그대로 붙이면
        # 1200자 예산을 혼자 먹고 **아래의 stopped_because/failed 를 밀어낸다** —
        # 신호가 제일 센 줄이 제일 먼저 잘리는 셈이다. 그래서 여기서만 따로 조인다.
        lines.append(f"attempted: {str(summary['prev_task'])[:_SUMMARY_TASK_CHARS]}")
    roles = summary.get("prev_roles") or []
    if roles:
        # 어디까지 갔는지. planner 에서 죽은 사이클과 critic 까지 간 사이클은 다르다.
        lines.append("agents_ran: " + ", ".join(str(r) for r in roles))
    failed = summary.get("prev_failed_ids") or []
    if failed:
        lines.append("failed: " + ", ".join(str(f) for f in failed))
    elif summary.get("prev_verify_ran") is False:
        lines.append("verification did not run")
    if summary.get("usd_cumulative") is not None:
        lines.append(f"spent_so_far_usd: {summary['usd_cumulative']}")
    # ⛔ **헤더 + cycle 줄만 남았으면 아무것도 안 낸다.** 그 둘은 "직전 사이클이
    # 있었다"는 말일 뿐 기억이 아니다. 길이(대리지표)로 "기억이 실렸다"를 주장하면
    # `episode_log_injected: true` 가 거짓이 되고, A/B 두 팔이 같은 값을 낸다.
    _facts = [ln for ln in lines[1:] if not ln.startswith("cycle: ")]
    if not _facts:
        return ""
    return "\n".join(lines)[:SUMMARY_MAX_CHARS]


def unpaired_call_ids(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    """짝을 못 찾은 DISPATCH 의 ``call_id`` — **삼켜진 디스패치**.

    코덱스 제품 로그에서 가져온 장치(실측 208/208, 고아 0). 우리는 삼킴을 grep 으로
    세고 있는데, 짝이 있으면 계수로 잡힌다.
    """
    dispatched: list[str] = []
    answered: set[str] = set()
    for r in rows:
        cid = r.get("call_id")
        if not cid:
            continue
        if r.get("event") == "DISPATCH":
            dispatched.append(str(cid))
        elif r.get("event") in ("VERIFY", "COST"):
            answered.add(str(cid))
    return [c for c in dispatched if c not in answered]


__all__ = [
    "SCHEMA_VERSION", "EVENTS", "HALT_REASONS", "EpisodeContractError",
    "enabled", "log_path", "halt_label",
    "emit_plan", "emit_dispatch", "emit_verify", "emit_cost", "emit_human",
    "emit_halt", "read_rows", "bound_by", "unpaired_call_ids",
]
