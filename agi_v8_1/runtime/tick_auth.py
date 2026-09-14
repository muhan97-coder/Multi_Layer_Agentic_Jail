"""tick 쿼리 인가 — 서명·검증의 단일 정의처 (2026-08-08).

## 왜

``tick/queries.jsonl`` 전 행에 auth_token 43자가 **평문**으로 남아 있었다.
제출 스크립트는 토큰을 파일에서 읽어 셸 히스토리를 피하면서 **원장에는 그대로
적었다** — 한쪽만 막은 형태. 원장이 곧 제출 채널이라 행에 인가 증거는 실어야
하므로, 평문 대신 **행 내용에 결박된 HMAC** 을 싣는다: 원장에 비밀이 안 남고,
서명은 (id, objective, ts)에 묶여 다른 행 위조에 재사용할 수 없다. 같은 행
재제출(리플레이)은 서명과 무관하게 ``tick/consumed`` 의 id 중복제거가 막는다.

게이트: ``AGI_V8_TICK_AUTH_SIG_ENABLED`` (default-OFF).
  OFF = 종전과 byte-identical — 쓰기는 평문 ``auth_token``, 검증은 평문 대조만.
  ON  = 쓰기는 ``auth_sig``, 검증은 ``auth_sig`` **또는** legacy ``auth_token``
        둘 다 수용(기왕의 평문 행이 남아 있는 동안의 이행 경로).
  ⚠️ 제출자와 소비자(tick_runner)는 **다른 프로세스**다. 켜는 곳은 레포 ``.env``
  하나 — 소비자는 ``tick_runner.main`` 의 ``load_dotenv`` 로, 제출자는
  ``tick_submit.sh`` 가 같은 파일에서 이 키만 읽어서 본다(프로세스 env 가 이긴다).
  🔴 2026-08-08 적대검증 전까지 **제출자가 .env 를 안 읽어 게이트를 켤 방법이 실제로
  없었다** — 문서가 "양쪽이 같이 보는 곳"이라 적었는데 한쪽만 봤다.
  ⚠️ 비대칭 상태의 방향: 제출 ON·소비 OFF = 서명 행이 미인가로 **튕긴다**(그리고
  툼스톤이 찍혀 그 질의는 영구 소비된다 — 조용히 뚫리진 않지만 조용히 잃는다).
  제출 OFF·소비 ON = legacy 평문 수용 때문에 그냥 통과한다(= 평문이 계속 쌓인다).

⛔ 이 캐논이컬라이저의 사본을 만들지 말 것 — ``scripts/tick_submit.sh`` 의
   heredoc 도 이 모듈을 import 한다(사본은 조용히 드리프트한다,
   feedback_duplicate_prompt_definition_trap).

## __SLOT_R9_T1_2026_08_17__ Round 9 적대검증 — 라이브 실측 4건 수리

라이브 ``.env`` 는 ``AGI_V8_TICK_AUTH_SIG_ENABLED=true`` 인데도 평문
``auth_token`` 행이 **게이트와 무관하게 무조건 먼저** 통과하고 있었다(HMAC 을
켜도 소비자 쪽은 안 닫힌다). 그리고 서명은 ``producer``/``source`` 를 **일부러**
빼고 있어(옛 독스트링이 "결정에 안 쓰이는 필드라 안전"이라고 적었다) 유효 서명
행의 ``producer`` 만 바꿔도 서명이 안 깨지고 HUMAN 축 귀속이 뒤집혔다. 서명에
만료·audience 도 없어 과거 유효 서명 행을 **다른 젤**의 큐에 복사하면 재-dispatch
됐다(두 젤이 같은 토큰 파일을 공유하므로 ``expected`` 도 같다).

네 게이트로 닫는다(전부 :func:`auth_ok`/:func:`stamp_auth` 로 모인다):

  * ``AGI_V8_TICK_AUTH_ALLOW_LEGACY`` (default **false**) — 평문 ``auth_token``
    은 이제 ``sig_enabled()`` 가 False 일 때만 인가된다. sig 게이트가 ON 인데
    이 게이트까지 true 면 종전(패치 이전)과 **byte-identical** 하게 평문을
    무조건 인가한다 — 이게 이 트랙의 OFF-parity 스위치다.
  * ``AGI_V8_TICK_AUTH_BIND_PRODUCER`` (default **true**) — ``producer``/
    ``source`` 가 행에 **있으면** 서명 대상에 포함한다. 없으면(옛 행) 포함하지
    않는다 — 존재/부재를 canonical 하게 구분해서 옛 무필드 행의 검증을 안 깬다.
    false 로 내리면 이 두 필드는 절대 서명에 안 들어간다(패치 이전과
    byte-identical).
  * ``AGI_V8_TICK_AUTH_TTL_SEC`` (미설정 = 무제한 = 현행) — 설정하면
    ``ts`` 와 검증 시각의 age 가 이 초를 넘는 서명 행을 만료로 거부한다.
  * ``AGI_V8_TICK_AUTH_BIND_AUDIENCE`` (default **true**) — 서명 시
    ``state_dir`` 가 주어지면 그 젤의 **다이제스트**(원문 경로 아님, sha256
    앞 16자)를 ``aud`` 필드로 행에 심고 서명 대상에 포함한다. 검증 시 행에
    ``aud`` 가 있으면 **현재 젤의 다이제스트와 일치해야** 통과한다 — 다른
    젤로 복사된 행은 다이제스트가 달라 재-dispatch 가 막힌다. 옛 행(``aud``
    없음)은 이 검사를 건너뛴다(하위호환 — 회전으로 미소비 행을 죽이지 않는다).

⚠️ **이 네 게이트는 default-true 계열이라 "off" 표기가 뒤집혀 있다** — 안전
쪽이 기본이고, 옛(패치 이전) 동작을 재현하려면 값을 **명시적으로 낮춰야** 한다.
말이 안 되는 값(오타 등)은 항상 안전 쪽(켜짐)으로 접는다 — ``in ("true","1")``
truthy 판정이 아니라 ``not in ("false","0")`` falsy 판정을 쓴다.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from typing import Any, Mapping

# __SLOT_R9_T1_TTL_MALFORMED_2026_08_17__ choke point — 삼킴을 계수·귀속하고
# STRICT 에서 재던지게 하는 이 레포의 정의처. ``policy.fail_fast`` 는 stdlib 만
# 쓰는 잎 모듈이라 제출자(`scripts/tick_submit.sh`)의 최소 컨텍스트에서도 안전하다.
from agi_v8_1.policy.fail_fast import swallowed as _swallowed

ENV_SIG_ENABLED = "AGI_V8_TICK_AUTH_SIG_ENABLED"
ENV_ALLOW_LEGACY = "AGI_V8_TICK_AUTH_ALLOW_LEGACY"          # __SLOT_R9_T1_2026_08_17__
ENV_BIND_PRODUCER = "AGI_V8_TICK_AUTH_BIND_PRODUCER"        # __SLOT_R9_T1_2026_08_17__
ENV_BIND_AUDIENCE = "AGI_V8_TICK_AUTH_BIND_AUDIENCE"        # __SLOT_R9_T1_2026_08_17__
ENV_TTL_SEC = "AGI_V8_TICK_AUTH_TTL_SEC"                    # __SLOT_R9_T1_2026_08_17__
# __SLOT_R14_TICKPROFILE_2026_08_17__ 정본은 여기다(auth 프로파일 관심사) —
# ``runtime/tick_runner`` 는 이 이름을 import 해서 재수출만 한다(사본 금지,
# 두 곳에 같은 조건을 적으면 검사 안 받는 쪽이 썩는다는 이 레포 실측 결함).
ENV_SECURE_PROFILE = "AGI_V8_TICK_SECURE_PROFILE"

#: 위 세 default-true 게이트가 공유하는 falsy 집합 — 판정 불가/오타 값은
#: 전부 안전 쪽(켜짐)으로 접힌다(fail-closed, 관대한 쪽으로 안 접는다).
_FALSY = ("false", "0")

#: 시계 오차 허용폭. TTL 검사에서 아주 살짝 미래인 ``ts`` 까지 만료로 접지
#: 않기 위한 여유 — 리플레이 방어의 핵심(과거로의 무제한 유예)은 건드리지 않는다.
_CLOCK_SKEW_TOLERANCE_S = 60.0

#: 축자로 서명하는 필드. row 전체가 아닌 이유 — 소비·계측 쪽이 행에 필드를
#: 덧붙여도(예: HUMAN 축 ``producer``) 기존 서명이 깨지면 안 된다.
SIG_FIELDS = ("id", "ts")

#: __SLOT_R9_T1_2026_08_17__ 조건부 결박 필드 — **행에 실제로 있을 때만**(그리고
#: 대응 게이트가 켜져 있을 때만) canonical 에 들어간다. 존재하지 않는 옛 행은
#: 예전과 똑같이 서명·검증된다. ``aud`` 는 이 목록에 없다 — audience 는
#: :func:`canonical_msg` 가 아니라 :func:`stamp_auth`/:func:`_audience_ok` 에서
#: 별도로 다룬다(서명 대상 포함 + 검증 시 재계산 대조, 두 가지 역할이라).
OPTIONAL_SIG_FIELDS = ("producer", "source")

#: __SLOT_TICK_AUTH_BIND_CAMPAIGN_2026_08_21__ 🔴 campaign 정산 **제어 필드**.
#: 이 값들은 디스패치 뒤 ``goal_campaign_feed.record_settlement()`` 가 "어느
#: 캠페인·카드·config 에 결과를 적을지" 를 정하는 데 쓴다 — 즉 **결정 표면**인데
#: 서명 밖이었다. 큐 파일을 쓸 수 있지만 토큰은 모르는 동일-UID 공격자가
#: ``objective``/``source`` 를 그대로 둔 채 정산 대상만 바꿔도 HMAC 이 유효했다
#: (실행된 목표 ≠ 정산되는 카드).
#: 이 모듈이 2026-08-08 에 배운 것과 **같은 병**이다 — 그때는 ``query`` 만 있는
#: 행의 objective 가 미서명이었고, 그 수리의 교훈이 :func:`canonical_msg` 독스트링의
#: "인가 표면은 결정 표면과 같아야 한다" 다. campaign 축에서 재발했다.
#: ``OPTIONAL_SIG_FIELDS`` 와 같은 규약(키가 있을 때만 + 게이트가 켜졌을 때만)이라
#: 옛 행의 검증은 안 깨진다.
CAMPAIGN_SIG_FIELDS = (
    "campaign_id", "card_id", "card_config_path",
    # __SLOT_CAMPAIGN_SIG_DIGEST_FIELDS_2026_08_22__ 내용 결박 3필드 —
    # 경로 문자열만으로는 config·카드 **내용** 바꿔치기가 서명 유효 상태로
    # 통과한다. 다이제스트의 SSOT 는 runtime/goal_campaign.config_digests 가
    # 반환하는 키 그대로(사본 금지 계약 — 검토된 인계 계약). 존재 기반
    # 규약(:101-118 OPTIONAL_SIG_FIELDS 와 동일)이라 다이제스트 없는 옛 행은
    # 게이트 ON 이어도 검증 불변 — 하위호환 자동.
    "manifest_digest", "card_digest", "success_criteria_digest",
)

#: 위 필드의 결박 게이트. ⚠️ default-OFF 인 이유는 보안 판단이 아니라 **이행**
#: 이다: 이미 큐에 서명돼 앉아 있는 campaign 행은 이 필드들이 canonical 에
#: 없는 상태로 서명됐으므로, 켜는 순간 그 행들은 서명 불일치로 거부된다
#: (fail-closed 라 안전하지만 카드가 한 번 멈춘다). 큐가 빈 것을 확인하고 arm 하라.
ENV_BIND_CAMPAIGN = "AGI_V8_TICK_AUTH_BIND_CAMPAIGN_ENABLED"


def bind_campaign_enabled(env: "Mapping[str, str] | None" = None) -> bool:
    """campaign 정산 제어 필드를 서명 대상에 넣는가. **strict** ``true``/``1``.

    __SLOT_BIND_CAMPAIGN_SPELLING_SYMMETRY_2026_08_24__ 🔴 **자기정정 기록.**
    2026-08-24 에 내가 여기 ``.lower()`` 를 넣어 형제 게이트(sig_enabled ·
    allow_legacy · bind_producer_enabled · bind_audience_enabled — 넷 다
    ``.strip().lower()``)와 "대칭을 맞췄다". **그건 틀린 수리였다.**

    ⛔ 되돌린 이유 둘:
      1. ``test_entry_semantics_2026_08_21.py::test_campaign_bind_gate_is_strict``
         가 ``["True", "yes", "on", ""]`` 를 **전부 False 로 의도적으로 고정**한
         선언된 계약이다. 그리고 이 레포의 T9 게이트 표준이 strict
         ``in ("true", "1")`` 이라, ``.lower()`` 를 쓰는 형제 넷(전부 R9 시대
         2026-08-17 산)이 오히려 예외다. 다수가 아니라 **표준**이 기준이다.
      2. 내가 든 근거("=True 로 적으면 조용히 안 켜진다")는 **더 이상 참이 아니다**
         — 같은 날 착지한 안전 프로파일 축 ``campaign_bind``
         (tick_runner ``_SECURE_PROFILE_AXES``)가 그 상태를 위반으로 잡아
         ``insecure_tick_profile`` 로 **틱을 멈추고 이름을 남긴다**. 조용한
         실패가 시끄러운 실패로 바뀌었으므로 관용의 근거가 사라졌다.

    🔑 남는 교훈: 회귀를 ``*tick_auth*`` 파일명 패턴으로만 돌려서 이 계약 테스트
    (파일명이 ``test_entry_semantics_…``)를 놓쳤다. **소유 모듈이 아니라 소비
    계약으로 회귀 범위를 잡아야 한다.**
    """
    e = os.environ if env is None else env
    return str(e.get(ENV_BIND_CAMPAIGN, "")).strip() in ("true", "1")  # tier: T9

#: 소비자가 "무엇을 실행할지" 정할 때 보는 키들, **우선순위 순서 그대로**.
#: ⛔ 이 목록의 사본을 만들지 마라 — ``tick_runner._objective_of`` 가 이걸 부른다.
OBJECTIVE_KEYS = ("objective", "query", "prompt")


def sig_enabled(env: Mapping[str, str] | None = None) -> bool:
    e = os.environ if env is None else env
    return str(e.get(ENV_SIG_ENABLED, "")).strip().lower() in ("true", "1")  # tier: T2


def allow_legacy(env: Mapping[str, str] | None = None) -> bool:
    """__SLOT_R9_T1_2026_08_17__ true 면 sig 게이트 ON 이어도 평문을 여전히
    무조건 인가한다 — 패치 이전 동작의 명시적 재현(OFF-parity 스위치)."""
    e = os.environ if env is None else env
    return str(e.get(ENV_ALLOW_LEGACY, "")).strip().lower() in ("true", "1")


def bind_producer_enabled(env: Mapping[str, str] | None = None) -> bool:
    """__SLOT_R9_T1_2026_08_17__ default true. false 면 producer/source 는
    행에 뭐가 있든 서명 대상에 절대 안 들어간다(패치 이전과 byte-identical)."""
    e = os.environ if env is None else env
    return str(e.get(ENV_BIND_PRODUCER, "true")).strip().lower() not in _FALSY


def bind_audience_enabled(env: Mapping[str, str] | None = None) -> bool:
    """__SLOT_R9_T1_2026_08_17__ default true. false 면 aud 를 심지도, 대조하지도
    않는다(패치 이전과 byte-identical — cross-jail replay 방어가 빠진다)."""
    e = os.environ if env is None else env
    return str(e.get(ENV_BIND_AUDIENCE, "true")).strip().lower() not in _FALSY


def secure_profile_enabled(env: Mapping[str, str] | None = None) -> bool:
    """__SLOT_R14_TICKPROFILE_2026_08_17__ tick 소비의 구조적 전제조건 검사가
    켜져 있는지 (default **True** — 안전한 방향이 기본, 이 레포 보안 증분 관례).

    ``=false`` 로 명시적으로 내리면 이 축과 :func:`ttl_seconds` 의 미설정-TTL
    안전 기본값이 둘 다 꺼진다 — 패치 이전과 byte-identical(OFF-parity 스위치).
    정본은 여기 하나 — ``runtime/tick_runner.secure_profile_enabled`` 는 이
    함수를 그대로 재수출한 이름일 뿐이다(순환 import 회피 + 이름 하나).
    """
    e = os.environ if env is None else env
    return str(e.get(ENV_SECURE_PROFILE, "true")).strip().lower() not in _FALSY


#: __SLOT_R14_TICKPROFILE_2026_08_17__ 안전 프로파일이 **미선언** TTL 에 공급하는
#: 유한 천장(초) = 7일. 근거(2026-08-17 라이브 tick 큐 실측):
#:   - pending 0건, 최신 행 나이 53.4h, ``queries.jsonl`` 44행 / ``consumed.jsonl``
#:     62행 — 7일이면 지금 살아 있는 행을 단 하나도 안 버린다.
#:   - 이 TTL 이 막는 위협은 **오래전에 캡처한 서명 행의 재생**이다. 7일이면 그건
#:     막히고, 큐가 며칠 유휴 상태여도(46일 무사고 이력이 보여주듯 흔한 형상)
#:     정상 행이 깨지지 않는다.
#: 🔑 이건 **천장**이지 **권장값**이 아니다 — 운영자는 ``AGI_V8_TICK_AUTH_TTL_SEC``
#: 를 명시적으로 더 짧게 선언하는 것을 권장한다(예: 몇 시간). 이 상수는 "선언을
#: 잊었다고 루프가 멈추면 안 된다"는 가용성 하한선일 뿐, 목표값이 아니다.
DEFAULT_SECURE_TTL_SEC = 604800.0  # 7 * 24 * 3600


def ttl_seconds(env: Mapping[str, str] | None = None) -> float | None:
    """__SLOT_R9_T1_2026_08_17__ 설정값이 있으면 그 초를 만료 상한으로 쓴다.

    __SLOT_R14_TICKPROFILE_2026_08_17__ 🔴 **미선언은 더 이상 무조건 무제한이
    아니다.** 안전 프로파일(:func:`secure_profile_enabled`, default ON)이 켜져
    있으면 미선언 TTL 은 :data:`DEFAULT_SECURE_TTL_SEC`(7일)로 접힌다 — 이 값
    하나가 :func:`_fresh`(실제 강제)와 ``tick_runner._secure_profile_violations``
    (판별기) 양쪽이 보는 정본이라, "판별기는 통과하는데 강제는 없다"는 어긋남이
    구조적으로 불가능하다. 프로파일이 꺼져 있으면(``=false``) 미선언은 여전히
    ``None`` = 무제한(패치 이전과 byte-identical, OFF-parity).
    말이 안 되는 값(0/음수/파싱불가)은 프로파일과 무관하게 여전히 ``None`` +
    ``swallowed`` — 이건 "운영자가 값을 적었는데 틀렸다"는 별개의, 더 강한
    위험신호라 미선언과 같은 관용을 안 받는다.
    """
    e = os.environ if env is None else env
    raw = e.get(ENV_TTL_SEC)
    if raw is None or not str(raw).strip():
        return DEFAULT_SECURE_TTL_SEC if secure_profile_enabled(e) else None
    try:
        v = float(raw)
    except (TypeError, ValueError) as exc:
        # __SLOT_R9_T1_TTL_MALFORMED_2026_08_17__ 🔴 **오타를 "TTL 없음"으로 접지 않는다.**
        # 미선언(`raw is None`)은 위에서 이미 걸러졌다 — 여기 온 것은 운영자가
        # **값을 적었는데 숫자가 아닌** 경우다. 그 둘은 다른 사실이다: 전자는 의도된
        # 무제한이고 후자는 **의도된 보호가 조용히 사라진** 것이다. 조용히 None 을
        # 돌려주면 freshness 검사가 오타 하나로 꺼지고 아무도 모른다 —
        # 이 레포가 반복해 다친 "malformed 를 관대한 쪽으로 접기" 그 자체다.
        # choke point 로 보내 **계수·귀속**되게 하고, STRICT 에서는 크게 죽게 한다
        # (보안 노브가 malformed 인 채로 도는 것보다 멈추는 게 맞다).
        _swallowed(exc, site="runtime.tick_auth.ttl_seconds:malformed",
                   category="verify")
        return None
    return v if v > 0 else None


def resolve_objective(row: Mapping[str, Any]) -> str | None:
    """소비자가 **실제로 실행할** 목표 문자열 (없으면 None).

    서명과 디스패치가 같은 함수를 봐야 한다 — 아래 :func:`canonical_msg` 참조.
    """
    for k in OBJECTIVE_KEYS:
        v = row.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def canonical_msg(row: Mapping[str, Any], env: Mapping[str, str] | None = None) -> bytes:
    """서명 대상 = (id, ts) 축자 + **해석된** objective (+ 조건부 결박 필드).

    🔴 2026-08-08 적대검증이 잡은 것: 처음엔 ``objective`` 키를 **축자**로 서명했는데,
    소비자(``_objective_of``)는 ``objective → query → prompt`` 폴백으로 읽는다.
    그래서 ``query`` 만 가진 행(공식 지원 경로)은 ``objective: null`` 이 서명되고
    **query 를 통째로 갈아끼워도 같은 서명이 유효**했다 — 실행될 문장이 미서명.
    🔑 **인가 표면은 결정 표면과 같아야 한다.** 그래서 여기서 해석해 서명한다:
    행에 무슨 키를 더 붙이든, 소비자가 고를 문자열이 바뀌면 서명이 깨진다.

    __SLOT_R9_T1_2026_08_17__ ``producer``/``source`` 는 :data:`OPTIONAL_SIG_FIELDS`
    로 **행에 키가 있을 때만**(``k in row``, 값이 아니라 존재로 판정 — 빈 문자열도
    "있음"이다) 그리고 :func:`bind_producer_enabled` 가 켜져 있을 때만 포함한다.
    이 이중 조건이 하위호환의 전부다: 키가 없는 옛 행은 게이트가 켜져 있어도
    이 함수의 결과가 패치 이전과 **글자 그대로 같다** — 검증이 안 깨진다. 반대로
    키가 있는 행에서 그 값을 바꾸면(위조든 재부착이든) subset 이 달라져 서명이
    깨진다 — 그게 이 트랙이 막으려는 것이다.
    """
    subset: dict[str, Any] = {k: row.get(k) for k in SIG_FIELDS}
    subset["objective"] = resolve_objective(row)
    if bind_producer_enabled(env):
        for k in OPTIONAL_SIG_FIELDS:
            if k in row:
                subset[k] = row[k]
    if bind_campaign_enabled(env):
        for k in CAMPAIGN_SIG_FIELDS:
            if k in row:
                subset[k] = row[k]
    if bind_audience_enabled(env) and "aud" in row:
        subset["aud"] = row["aud"]
    return json.dumps(subset, ensure_ascii=False, sort_keys=True).encode("utf-8")


def sign_row(row: Mapping[str, Any], token: str,
             env: Mapping[str, str] | None = None) -> str:
    return hmac.new(token.encode("utf-8"), canonical_msg(row, env=env),
                    hashlib.sha256).hexdigest()


def token_fingerprint(token: str) -> str:
    """감사용 비가역 지문 — 스크럽이 평문 자리에 남기는 값(회전 대조용)."""
    return "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def audience_digest(state_dir: "str | Path") -> str:
    """__SLOT_R9_T1_2026_08_17__ 젤의 안정적 다이제스트 — **원문 절대경로를 절대
    원장에 싣지 않는다**(Round 8 공개경계 결함 재발 방지). sha256 앞 16자."""
    raw = str(Path(state_dir).resolve())
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _fresh(row: Mapping[str, Any], env: Mapping[str, str] | None, now: float) -> bool:
    """__SLOT_R9_T1_2026_08_17__ TTL 미설정 = 항상 통과(현행 무제한 그대로).
    설정 시 ``ts`` 가 숫자가 아니면 판정불가 = 거부(fail-closed) — malformed 를
    legacy 관용으로 접지 않는다."""
    ttl = ttl_seconds(env)
    if ttl is None:
        return True
    ts = row.get("ts")
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return False
    age = now - float(ts)
    return -_CLOCK_SKEW_TOLERANCE_S <= age <= ttl


def _audience_ok(row: Mapping[str, Any], env: Mapping[str, str] | None,
                  state_dir: "str | Path | None") -> bool:
    """__SLOT_R9_T1_2026_08_17__ 행에 ``aud`` 가 없으면(옛 행) 통과 — 하위호환.
    있으면 **현재 검증자의 젤**과 다이제스트가 일치해야 한다(cross-jail replay 방어).
    검증자가 자기 state_dir 을 안 넘기면(호출자 하위호환) 대조를 건너뛴다 — 실제
    소비 경로(``tick_runner``)는 항상 넘긴다."""
    if not bind_audience_enabled(env):
        return True
    aud = row.get("aud")
    if aud is None:
        return True
    if not isinstance(aud, str) or not aud:
        return False
    if state_dir is None:
        return True
    return hmac.compare_digest(aud, audience_digest(state_dir))


def auth_ok(row: Mapping[str, Any], expected: str,
            env: Mapping[str, str] | None = None, *,
            state_dir: "str | Path | None" = None,
            now: float | None = None) -> bool:
    """행 인가 판정.

    __SLOT_R9_T1_2026_08_17__ 🔴 P0 수리: legacy 평문 대조는 더 이상 게이트와
    무관하지 않다 — ``sig_enabled()`` 가 False 일 때만 무조건 인가된다. sig 게이트가
    ON 이면 :func:`allow_legacy` 가 명시적으로 true 여야만(=패치 이전 재현) 평문이
    통과한다. 그 외에는 서명 검증(있으면 TTL·audience 까지)만 본다.
    """
    sig_on = sig_enabled(env)
    tok = row.get("auth_token")
    legacy_match = isinstance(tok, str) and bool(tok) and hmac.compare_digest(tok, expected)
    if legacy_match and (not sig_on or allow_legacy(env)):
        return True
    if not sig_on:
        return False
    sig = row.get("auth_sig")
    if not isinstance(sig, str) or not sig:
        return False
    if not hmac.compare_digest(sig, sign_row(row, expected, env=env)):
        return False
    _now = time.time() if now is None else float(now)
    if not _fresh(row, env, _now):
        return False
    if not _audience_ok(row, env, state_dir):
        return False
    return True


def stamp_auth(row: dict[str, Any], token: str,
               env: Mapping[str, str] | None = None, *,
               state_dir: "str | Path | None" = None) -> dict[str, Any]:
    """생산자용 — 게이트에 따라 auth_sig 또는 legacy auth_token 을 심는다.

    ⚠️ __SLOT_R9_T1_2026_08_17__ 이 문장은 RED 수리(2026-08-08) 전의 계약이었다:
    *"SIG_FIELDS 만 읽으므로 뒤에 무슨 필드를 더해도 안전"*. 지금은 **아니다.**
    서명은 `resolve_objective` 결과를 덮으므로, 뒤에 붙는 필드가 **더 높은
    우선순위의 objective 키**(`objective` > `query` > `prompt`)면 서명이
    **깨진다** — 실행될 문자열이 바뀌었으니 맞는 동작이다.
    ⚠️ **`producer`/`source` 도 더 이상 안전하지 않다** — :data:`OPTIONAL_SIG_FIELDS`
    로 결박된다(:func:`bind_producer_enabled` 켜짐 기본). ``stamp_auth`` 호출
    **전에** 이 필드들을 행에 심어야 서명에 포함된다 — 호출 **후**에 붙이면
    canonical 이 달라져 서명이 깨진다(이게 이 트랙의 요점이다: producer 를
    사후 변조하면 서명이 죽어야 한다). 진짜 안전한 건 서명 대상이 아예 아닌
    `candidate_source` 같은 순수 계측 필드뿐이다.
    ``state_dir`` 이 주어지고 :func:`bind_audience_enabled` 가 켜져 있으면
    (sig 게이트가 ON 일 때만) ``aud`` 다이제스트를 심고 서명에 포함한다.
    """
    if sig_enabled(env):
        if bind_audience_enabled(env) and state_dir is not None:
            row["aud"] = audience_digest(state_dir)
        row["auth_sig"] = sign_row(row, token, env=env)
    else:
        row["auth_token"] = token
    return row
