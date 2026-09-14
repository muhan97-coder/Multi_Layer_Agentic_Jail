# __SLOT_REPEATED_FAILURE_GATE_2026_08_19__ Prime autonomous.ts:284 계보 — "직전과
# 같은 실패면 반복하지 않는다" 신호를 이 코드베이스에 처음 배선한다.
"""연속 동일 실패 서명 관측 게이트(v0) — ``tick_runner`` 재급식 후보 축.

⚠️ **이름 충돌 회피(2026-08-19)**: ``runtime/anti_idempotent.py`` 는 이미
2026-08-08 부터 존재하는 **완전히 다른** 모듈이다(`first_run` 루프의
워크스페이스-다이제스트 무변화 재시도 감지, ``AGI_V8_ANTI_IDEMPOTENT_RETRY_ENABLED``).
이 모듈이 다루는 것은 그것과 무관한 **tick 재급식 축의 실패 서명 반복**이라
``anti_idempotent`` 라는 이름을 재사용하면 그 891줄짜리 기존 모듈을 덮어쓰게
된다(실제로 이 증분 작업 중 한 번 그 사고를 냈다 — HEAD 로 복구했다). 그래서
파일명은 ``repeated_failure_gate``로, 게이트 env 도 ``ANTI_IDEMPOTENT`` 어간을
피해 ``AGI_V8_TICK_REPEATED_FAILURE_GATE_ENABLED``로 짓는다 — 두 게이트가
운영자 눈에 뒤섞이지 않게. (기각한 이름을 여기 풀 스펠로 적으면 게이트
스캐너가 팬텀 게이트로 등재한다 — 그래서 어간만 적는다.)

## 갭

``goal_campaign_feed.record_settlement`` (``runtime/goal_campaign_feed.py``)의
카드 재시도 판정(``judge_verdict`` retry/escalate)은 연속 실패 **횟수**만 센다
(``_consecutive_card_failures``) — 실패 **내용**은 전혀 안 본다. 두 번 다른
이유로 실패한 카드와 두 번 같은 이유로 실패한 카드가 판정에서 구분되지 않는다.
Prime 계보가 요구하는 "직전과 동일한 실패 서명이면 반복 안 함" 신호는 이
레포 어디에도 계산되지 않았다.

``runtime/failure_ledger.py`` 에 조건 지문(fingerprint)+binding 계산이 이미
있지만, 그건 ``episode.jsonl`` VERIFY/HALT → propose 프롬프트 주입 축 전용
(``runtime/cli.py``)이라 이 tick 재급식 축과 완전히 분리돼 있다. 이 모듈이
그 계보를 tick 축에 처음 심는다.

## v0 범위 — 관측 번들(스킵+로그), 강제 정책 아님

이 모듈은 **판정 로직(judge_verdict/redispatch_action)을 절대 바꾸지 않는다.**
``goal_campaign_feed.record_settlement`` 은 그대로 두고, 이 모듈은 ``tick_runner``
가 dispatch **직전**에 "이 대상이 최근 :func:`threshold` 회 연속 같은 실패
서명이었나"만 보고 스킵 여부를 정한다. 스킵되면 실제 ``dispatch()`` 호출(=
provider 비용)만 건너뛴다 — ``tick_runner`` 는 그 자리에 합성 실패 결과를
채워 넣고 **평범한 실패 경로와 완전히 같은 모양**으로 정산/tombstone/로그를
흘려보낸다(정산을 생략하면 goal_campaign 의 in-flight 가드가 안 풀려 캠페인이
고아로 남는다 — 그 이유는 ``tick_runner.py`` 의 이음새 주석 참조).

## 대상(target) 정의

재시도가 구조적으로 일어나는 유일한 축은 goal_campaign 카드 재급식이다
(``campaign_id``+``card_id`` 조합 — ``goal_campaign_feed.feed_one`` 이 매
재급식마다 새 ``qid`` 를 발급하므로 qid 로는 반복을 못 잡는다, 오직
campaign_id/card_id 조합만 카드 신원을 지속시킨다). 그 두 필드가 행에
없으면(사람 제출/work_feeder 1회성 질의 — 소비되면 같은 qid 가 다시 안 온다)
대상이 정의되지 않으므로 :func:`should_skip` 은 항상 통과시킨다.

## 서명(signature)

``dispatch_result`` 의 결정론적 필드(``ok``/``deadline_exceeded``/
``deadline_unavailable``)와 예외 **타입명만**(``"TypeName: message"`` 형식의
``:`` 이전 부분, ``policy.fail_fast.format_exception_for_sink`` 의 출력 형식)을
sha256 해시한다. 전체 예외 메시지는 서명에 안 넣는다 — 메시지 안에 메모리
주소·타임스탬프 같은 비결정 요소가 섞이면 같은 논리적 실패가 매 회 다른
서명을 받을 위험이 있다(정찰 위험 노트). 타입명만 쓰면 거칠지만(coarse)
결정론적이다 — v0 이 관측용으로 받아들이는 트레이드오프다.

## 저장

``<state_dir>/repeated_failure_gate/history.jsonl`` 에 정산마다 한 행 append.
target 이 없는 호출(비-goal_campaign 행)은 기록하지 않는다 — 파일 자체가
안 생긴다.

## Default-OFF

``AGI_V8_TICK_REPEATED_FAILURE_GATE_ENABLED`` strict ``"true"``/``"1"``
(tier: T9). OFF 이면 :func:`should_skip` 은 항상 ``(False, None)`` 이고
:func:`record_outcome` 은 즉시 반환해 아무 파일도 안 건드린다 — ``tick_runner``
는 이 모듈을 게이트 뒤에서만 부르므로, OFF 인 젤은 이 모듈이 존재하기 전과
byte-identical.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

from agi_v8_1.policy.fail_fast import swallowed as _swallowed
from agi_v8_1.state.store import atomic_append_jsonl, read_jsonl

__all__ = [
    "ENV_ENABLED", "ENV_THRESHOLD", "DEFAULT_THRESHOLD", "SCHEMA_VERSION",
    "enabled", "threshold", "history_path", "target_key",
    "failure_signature", "should_skip", "record_outcome",
]

ENV_ENABLED = "AGI_V8_TICK_REPEATED_FAILURE_GATE_ENABLED"
ENV_THRESHOLD = "AGI_V8_TICK_REPEATED_FAILURE_GATE_THRESHOLD"
#: env 미설정/파싱 실패/1 미만이면 이 값으로 되돌아간다.
#
# __SLOT_REPEATED_FAILURE_GATE_THRESHOLD_2026_08_19__ 🔑 도달성 논증(왜 1인가,
# 왜 2 는 구조적으로 도달 불가능한가): ``goal_campaign_feed.FAILURE_ESCALATE_THRESHOLD``
# (``runtime/goal_campaign_feed.py``)도 2 다 — 그리고 그 판정은
# ``streak + 1 >= FAILURE_ESCALATE_THRESHOLD`` 로, **연속 2번째 실패 그 자체가
# escalate 로 카드를 terminal 로 접는다**(3번째 급식은 아예 없다). 이 게이트의
# should_skip 은 dispatch **직전** history 를 보므로, threshold=2 였을 때 필요한
# "이미 2개 쌓인 이력"은 카드가 이미 escalate 로 done_ids 에 편입된 뒤에만
# 존재한다 — 그 시점엔 재급식 자체가 없으니 should_skip 호출 기회가 없다(정찰
# brief 의 시퀀스 재구성). threshold=1 이면 dispatch 직전 history 가 1개(첫
# 실패 1건)만 있어도 스킵이 발동해, 두 번째 재급식이 "스킵된 합성 실패"로
# 소비되고 정상 실패 경로와 같은 모양으로 escalate 까지 흘러간다 — 도달 가능한
# 유일한 값이다. 이 관계는 ``FAILURE_ESCALATE_THRESHOLD`` 를 이 모듈이 import
# 하면 순환 import 가 생겨(``goal_campaign_feed`` 는 이 모듈을 모르지만 향후
# 배선 변경에 취약) 여기서 런타임으로 강제하지 않는다 — 대신
# ``tests/v8_1/test_repeated_failure_gate_2026_08_19.py`` 의 pin 테스트가
# ``DEFAULT_THRESHOLD < FAILURE_ESCALATE_THRESHOLD`` 를 직접 assert 로 고정한다
# (레포 관례: ``anti_idempotent.py:426`` — "구조로 못 박는 관계는 테스트가
# 건다"). 런타임 env 오버라이드(``ENV_THRESHOLD`` 를 2 이상으로 올리는 것)까지는
# 이 pin 이 못 막는다 — 형제 게이트(``tick_deadline.deadline_seconds``)와 동형의
# 의도적 스코프다.
DEFAULT_THRESHOLD = 1
SCHEMA_VERSION = "repeated_failure_gate_history_v1"

_HISTORY_REL = ("repeated_failure_gate", "history.jsonl")


def enabled() -> bool:
    """default-OFF, strict ``"true"``/``"1"``."""
    return os.environ.get(ENV_ENABLED, "") in ("true", "1")  # tier: T9


def threshold() -> int:
    """연속 동일 서명 문턱(카드가 안전하게 스킵되려면 몇 회가 쌓여야 하나).

    env 미설정이거나 정수로 못 읽거나 1 미만이면 :data:`DEFAULT_THRESHOLD`.
    지어낸 값으로 조용히 접지 않고 명시적 fallback 상수를 쓴다.
    """
    # 형제 관례(runtime/tick_deadline.deadline_seconds)와 동형: 미설정은 get 의
    # default 로 정상 경로를 타고, ``_swallowed`` 는 **malformed 값**에만 남는다
    # (미설정마다 swallow 원장을 적시면 "싼 턴"과 "모르는 턴"이 안 갈린다).
    raw = os.environ.get(ENV_THRESHOLD, str(DEFAULT_THRESHOLD))
    try:
        n = int(raw)
    except ValueError as exc:
        _swallowed(exc, site="runtime.repeated_failure_gate.threshold",
                   category="config")
        return DEFAULT_THRESHOLD
    return n if n >= 1 else DEFAULT_THRESHOLD


def history_path(state_dir: "str | Path") -> Path:
    return Path(state_dir).joinpath(*_HISTORY_REL)


def target_key(row: "Mapping[str, Any]") -> "str | None":
    """이 후보의 반복-대상 신원, 없으면 ``None``(=이 게이트의 대상이 아님).

    ``campaign_id``/``card_id`` 둘 다 있는 행만 대상이다 — goal_campaign 카드
    재급식이 이 레포에서 반복이 구조적으로 가능한 유일한 축이기 때문이다
    (모듈 독스트링 "대상 정의" 절 참조).
    """
    campaign_id = row.get("campaign_id")
    card_id = row.get("card_id")
    if not campaign_id or not card_id:
        return None
    return f"{campaign_id}::{card_id}"


def failure_signature(
    dispatch_result: "Mapping[str, Any] | None", error: "str | None",
) -> "str | None":
    """실패 정산이면 결정론적 서명(sha256 앞 12자), 성공이면 ``None``.

    성공 판정은 ``goal_campaign_feed.record_settlement`` 과 같은 규칙:
    ``error is None`` 이고 ``dispatch_result["ok"] is True``.
    """
    dres = dispatch_result if isinstance(dispatch_result, dict) else {}
    if error is None and dres.get("ok") is True:
        return None
    exc_type: "str | None" = None
    if isinstance(error, str) and error:
        # ``format_exception_for_sink`` 출력은 "TypeName: message" 형식이다.
        # message 절반은 버린다 — 비결정 요소(주소/타임스탬프)가 섞일 수 있다.
        exc_type = error.split(":", 1)[0].strip()[:80] or None
    payload = {
        "ok": bool(dres.get("ok")),
        "deadline_exceeded": bool(dres.get("deadline_exceeded")),
        "deadline_unavailable": bool(dres.get("deadline_unavailable")),
        "exc_type": exc_type,
    }
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def _tail_signatures(
    state_dir: "str | Path", target: str, *, limit: int,
) -> "list[str | None]":
    """이 target 의 history 행 중 꼬리에서부터 최대 *limit* 개의 signature.

    index 0 = 가장 최근. 매 호출마다 파일을 새로 읽는다(프로세스 메모리 캐시
    없음 — ``goal_campaign_feed.registry_rows`` 와 같은 원칙, 틱 재기동에도
    이력이 파일 구조로만 산다).
    """
    try:
        rows = read_jsonl(history_path(state_dir))
    except (OSError, ValueError) as exc:
        # 손상/부재 판독 실패는 fail-open 관측이다 — 정산축(record_outcome)이
        # 아니라 스킵 판단(should_skip)만 이걸 쓰므로, 여기서 막히면 "스킵 못
        # 함"으로 안전하게 넘어간다(디스패치가 막히지 않는다). ``ValueError`` 는
        # ``read_jsonl`` 이 손상된 JSONL 줄에서 던지는
        # ``json.JSONDecodeError``(``ValueError`` 서브클래스)를 잡는다.
        _swallowed(exc, site="runtime.repeated_failure_gate._tail_signatures:read",
                   category="persist")
        return []
    out: "list[str | None]" = []
    for r in reversed(rows):
        if str(r.get("target")) != target:
            continue
        out.append(r.get("signature"))
        if len(out) >= limit:
            break
    return out


def should_skip(
    state_dir: "str | Path", row: "Mapping[str, Any]",
) -> "tuple[bool, dict[str, Any] | None]":
    """이 후보를 스킵해야 하나. 게이트 OFF/대상 없음이면 항상 ``(False, None)``.

    스킵 조건: 이 target 의 마지막 :func:`threshold` 개 history 행이 전부
    존재하고(이력이 충분히 쌓였고) 전부 같은 non-None signature 다. 두 번째
    반환값은 skip 여부와 무관하게 tick_log 관측 행에 그대로 실을 수 있는
    dict(``None`` 은 오직 게이트 OFF/대상 없음일 때만).
    """
    if not enabled():
        return False, None
    target = target_key(row)
    if target is None:
        return False, None
    n = threshold()
    tail = _tail_signatures(state_dir, target, limit=n)
    observation: "dict[str, Any]" = {
        "target": target, "tail_signatures": tail, "threshold": n,
    }
    if len(tail) < n:
        return False, observation
    first = tail[0]
    if first is None or any(s != first for s in tail):
        return False, observation
    observation["repeated_signature"] = first
    return True, observation


def record_outcome(
    state_dir: "str | Path", row: "Mapping[str, Any]", *,
    dispatch_result: "Mapping[str, Any] | None", error: "str | None",
    cycle_id: str, now_ts: float,
) -> None:
    """이 정산의 실패 서명을 history 에 append.

    게이트 OFF 이거나 :func:`target_key` 가 ``None`` 이면(비-goal_campaign
    행) 즉시 반환하고 파일을 만들지 않는다. 쓰기 실패는 swallowed 로 격리한다
    — 이건 순수 관측 부수효과라 디스패치 결과를 절대 못 바꾼다.
    """
    if not enabled():
        return
    target = target_key(row)
    if target is None:
        return
    record = {
        "schema_version": SCHEMA_VERSION, "target": target,
        "signature": failure_signature(dispatch_result, error),
        "cycle_id": cycle_id, "ts": now_ts,
    }
    try:
        atomic_append_jsonl(history_path(state_dir), record)
    except OSError as exc:
        _swallowed(exc, site="runtime.repeated_failure_gate.record_outcome:append",
                   category="persist")
