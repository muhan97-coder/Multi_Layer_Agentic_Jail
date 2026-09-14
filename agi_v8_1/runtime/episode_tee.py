# __SLOT_P0_LOG_UNIFICATION_VERIFY_TEE_2026_08_10__ VERIFY 사영(tee) 진입점.
"""검증-형 사실을 계약 원장(``episode.jsonl``)의 VERIFY 행으로 사영한다.

## 왜 (2026-08-10 라이브 실측)

계약 6종(PLAN/DISPATCH/VERIFY/COST/HUMAN/HALT) 중 라이브 episode.jsonl 에
실리는 것은 PLAN 26 · DISPATCH 96 · COST 419 뿐이었다 — **VERIFY 는 0행**이다.
생산자(`episode_log.emit_verify`)와 호출자(`si_lanes/verify_gate_log.py`)는
있는데 그 경로가 tick 사이클에서 안 돈다(verify_gate 로그 파일 자체가 젤에
없다). 판정은 실제로 매 사이클 일어난다 — swarm 교차모델 합의(`cycle_log` 의
``status: split``), apply 세션 판정(`executor_log` 의 ``verdict: ok``) — 그런데
전부 **각자의 원장에만** 남아서, 판독기(`tools/tick_flight.py`)가 여섯 원장을
손으로 꿰고 "판정 ❌ 없음" 을 찍는다.

이 모듈은 08-07 의 COST tee(`si_spend_ledger._tee_episode_cost`)와 같은 원칙의
VERIFY 판이다:

- **이중 기록이 아니라 사영**: 원본 원장(executor_log / cycle_log)은 그대로 두고,
  같은 지역변수에서 계약 형식 한 벌을 더 낸다.
- **episode_log.enabled() 게이트 뒤**: OFF 면 byte-identical (아무 것도 안 쓴다).
- **실패는 명명 카운트**: 본작업(디스패치·apply)을 절대 죽이지 않는다 —
  `policy.fail_fast.swallowed` 초크포인트로 세고 넘어간다.
- **cycle_id 조인**: 명시 인자가 이기고, 없으면 `episode_ctx` 문맥 폴백
  (`emit_cost` 와 같은 규칙 — 바깥 사이클이 안쪽 신원을 훔치지 않는다).
- **젤 밖 쓰기 금지**: state_dir 를 모르면 안 쓴다(추측 금지 —
  `si_spend_ledger._episode_state_dir` 와 같은 판단).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

from agi_v8_1.policy.fail_fast import swallowed as _swallowed

__all__ = ["tee_verify"]


def _state_dir_from_env() -> Path | None:
    """이 프로세스가 핀한 젤. 모르면 ``None`` — **추측해서 쓰지 않는다.**"""
    env_dir = os.environ.get("AGI_V8_STATE_DIR") or os.environ.get("AGI_STATE_DIR")
    return Path(env_dir) if env_dir else None


def tee_verify(
    *,
    target: str,
    command: str | None,
    ran: bool,
    verdict: str | None,
    failed_ids: Iterable[str] | None = None,
    cycle_id: str | None = None,
    call_id: str | None = None,
    state_dir: Path | str | None = None,
) -> None:
    """검증 사실 하나를 VERIFY 행으로 사영한다. **절대 raise 하지 않는다.**

    ``verdict`` 어휘는 원본 원장의 것을 그대로 나른다(예: safe_auto_apply 의
    ``ok``/``partial_or_fail``, swarm 의 ``chosen_path:*``) — 여기서 번역하면
    게이트마다 진리값 어휘가 갈라지는 그 병이 재발한다. ``failed_ids`` 는
    **개수가 아니라 신원**이다(계약 규칙 — emit_verify 가 상한 21 로 자른다).
    """
    try:
        from agi_v8_1.runtime import episode_log as _el

        if not _el.enabled():
            return
        sd = Path(state_dir) if state_dir is not None else _state_dir_from_env()
        if sd is None:
            return          # 젤을 모르면 안 쓴다(젤 밖 쓰기 금지)
        if cycle_id is None or call_id is None:
            from agi_v8_1.runtime import episode_ctx as _ctx

            _c, _k = _ctx.current()
            cycle_id = cycle_id if cycle_id is not None else _c
            call_id = call_id if call_id is not None else _k
        _el.emit_verify(
            sd,
            cycle_id=cycle_id,
            target=target,
            command=command,
            ran=bool(ran),
            verdict=verdict,
            failed_ids=failed_ids,
            call_id=call_id,
        )
    except Exception as exc:  # noqa: BLE001 — 계측 실패가 본작업을 죽이면 안 된다
        _swallowed(exc, site="runtime.episode_tee.tee_verify", category="telemetry")
