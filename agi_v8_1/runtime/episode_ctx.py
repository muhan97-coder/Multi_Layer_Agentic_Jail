# __SLOT_EPISODE_CTX_2026_08_07__ COST 행이 자기가 어느 사이클 것인지 알게 한다.
"""사이클 컨텍스트 — ``cycle_id`` / ``call_id`` 를 콜 스택 대신 **문맥**으로 나른다.

## 🔴 왜 (2026-08-07 원장 축자 실측)

`state/si_jail/runtime_logs/episode.jsonl` 을 세어 보면 ::

    DISPATCH  64   4역할 × 16사이클 · worker=deepseek-v4-flash · call_id 64/64
    COST      93   실제 LLM 콜        · cycle_id **93/93 이 None** · call_id **0/93**
    두 event 의 cycle_id 겹침 = 0
    unpaired_call_ids() = 64        ← 짝짓기가 통째로 죽어 있다

`emit_dispatch` 독스트링은 *"VERIFY/COST 가 같은 값을 실으면 삼킨 디스패치가
계수로 잡힌다"* 고 적어뒀는데, 그건 **코덱스 원장의 실측**이었고 우리 쪽 생산자는
그 값을 한 번도 안 실었다. 검사기(:func:`~agi_v8_1.runtime.episode_log.unpaired_call_ids`)
는 64 를 반환하는데 **읽는 코드가 없다.**

`si_spend_ledger._tee_episode_cost` 가 그 이유를 이미 자인해 뒀다 ::

    # ⚠️ 호출자가 사이클을 안 넘기면 ``None`` 이다 — 모름이지 0 이 아니다.
    # 지금 세 호출 경로 다 사이클을 모른다(각자 자기 단계만 안다).

⇒ **어느 사이클도 자기 비용을 모른다.** `usd_cumulative` 는 전역 누적이라
"이 사이클이 얼마 썼나"는 원장에서 아예 나오지 않는다.

## ⛔ 인자로 뚫지 않는다

세 호출 경로(`self_improvement_v8` · `tick_runner` · `first_run_drills`)와 그 사이
provider 계층을 전부 고쳐 `cycle_id` 를 손으로 나르면, **다음에 생기는 네 번째
경로가 조용히 `None` 을 낸다** — 지금과 같은 상태로 돌아간다. 문맥은 문맥으로 나른다.

## 계약

- **default-OFF** (:data:`ENV`). off 면 :func:`current` 가 항상 `(None, None)` 이라
  기존 행과 byte-identical 이다.
- ⛔ **호출자가 명시로 준 값이 항상 이긴다.** 문맥은 *폴백*이지 덮어쓰기가 아니다 —
  안 그러면 바깥 사이클이 안쪽 하위콜의 신원을 훔친다.
- 재진입 가능(`ContextVar` + 토큰 복원). 스레드·async 태스크마다 독립이라
  병렬 디스패치에서 서로의 사이클을 섞지 않는다 — 계약 v3 의 동시성 검산이
  정확히 이걸 요구한다.
"""
from __future__ import annotations

import contextlib
import os
from contextvars import ContextVar
from typing import Iterator

__all__ = ["ENV", "enabled", "current", "cycle_scope", "call_scope"]

ENV = "AGI_V8_EPISODE_CTX_ENABLED"

_CYCLE: ContextVar[str | None] = ContextVar("agi_v8_episode_cycle", default=None)
_CALL: ContextVar[str | None] = ContextVar("agi_v8_episode_call", default=None)


def enabled() -> bool:
    """strict — 다른 게이트와 같은 관례(`"true"`/`"1"`)."""
    return os.environ.get(ENV, "").strip().lower() in ("true", "1")  # tier: T2


def current() -> tuple[str | None, str | None]:
    """``(cycle_id, call_id)``. 게이트가 꺼져 있으면 항상 ``(None, None)``.

    ⛔ 빈 문자열을 값으로 취급하지 않는다 — ``""`` 는 *"안 넣었다"* 이지
    *"이름이 빈 사이클"* 이 아니다.
    """
    if not enabled():
        return (None, None)
    c, k = _CYCLE.get(), _CALL.get()
    return (c or None, k or None)


@contextlib.contextmanager
def cycle_scope(cycle_id: str | None) -> Iterator[None]:
    """이 블록 안에서 난 콜은 ``cycle_id`` 를 문맥으로 갖는다.

    게이트가 꺼져 있어도 **설정은 한다** — 켜는 순간 값이 있어야지, 켠 다음
    사이클이 새로 시작될 때까지 기다리게 만들면 그게 우리가 계속 잡아온
    *"armed 인데 값이 0"* 이다. 읽기(:func:`current`)만 게이트를 본다.
    """
    tok = _CYCLE.set(str(cycle_id) if cycle_id else None)
    try:
        yield
    finally:
        _CYCLE.reset(tok)


@contextlib.contextmanager
def call_scope(call_id: str | None) -> Iterator[None]:
    """이 블록 안의 콜 하나에 ``call_id`` 를 붙인다(DISPATCH 와 짝지을 값)."""
    tok = _CALL.set(str(call_id) if call_id else None)
    try:
        yield
    finally:
        _CALL.reset(tok)
