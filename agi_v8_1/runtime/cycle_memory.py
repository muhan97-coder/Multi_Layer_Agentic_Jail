# __SLOT_TICK_CYCLE_MEMORY_2026_08_20__ 08-04 "루프에 기억 없음" 공백의 v1.
"""직전 사이클 관측 번들 — 이미 있는 V8 원장에서만 읽는다(원장 쓰기 0).

## 왜 (2026-08-04 실측을 그대로 잇는다)

``self_improvement_v8._build_observation_bundle`` 의 ``prev_run_context_status``
필드는 ``core/previous_run_context.load_previous_run_context`` 를 불러 채우려
하는데, 그 모듈의 계약은 v7.1 식 ``runs_root/<run_id>/artifacts/{goal_card.json,
cycle_log.jsonl,rubric_score.json}`` 구조를 기대한다 — **그 경로에 쓰는 생산자가
V8 트리에 하나도 없다**(``self_improvement_v8.py:1009-1123`` 이 이미 그렇게
자백해뒀다). 그래서 그 필드는 늘 ``"no_previous_run"``/``None`` 이다. 이 모듈은
그 죽은 생산자 계약을 되살리지 **않는다**(범위 밖 — 같은 파일 1120-1123 주석이
명시적으로 금지) — 대신 V8 이 실제로 쓰는 원장에서 직전 사이클의 관측치를
**다시 조립하는 새 생산자**다.

## 대상(lineage) — 재사용, 신설 아님

``repeated_failure_gate.target_key(row)``(``campaign_id::card_id``)를 그대로
쓴다. 이 레포에서 "직전 사이클"이 신원을 갖고 이어지는 유일한 축이
goal_campaign 카드 재급식이기 때문이다(``repeated_failure_gate`` 모듈
독스트링 "대상 정의" 절과 동일한 논거). 그 필드가 없는 행(사람 제출/
work_feeder 1회성 질의)은 lineage 가 없다 — 지어내지 않고
``status="no_lineage"`` 로 정직하게 표시한다(생략이 아니라 명명).

## 🔴 정찰 지시를 뒤집은 지점 — "맹신 금지, 검증하며 써라"

이 트랙의 정찰은 ``campaign_ledger.jsonl``(``goal_campaign.read_ledger``)의
``tick_settled`` 이벤트를 "직전 사이클" 원천으로 지목했다. **실측하면 그
이벤트는 종결(accept/escalate)일 때만 ``_append_campaign_ledger`` 로 써진다**
(``goal_campaign_feed.record_settlement`` 의 ``if terminal:`` 가지, 자기
독스트링이 명시: "retry(비종결)면 캠페인 원장에는 아무것도 안 쓴다" — retry
행을 card_id 와 함께 적으면 ``goal_campaign.done_ids`` 가 그 카드를 "끝났다"로
잘못 읽기 때문이다). 그런데 ``done_ids`` 에 들어간 card_id 는
``_next_eligible_card`` 가 **영원히 다시 안 급식한다** — 즉 어떤 행이
``campaign_ledger.jsonl`` 에 그 카드의 ``tick_settled`` 로 실렸다는 사실 자체가
"이 카드는 이제 다시 디스패치되지 않는다"를 뜻한다. 그 원장만 보면
:func:`build_bundle` 은 **실제로 재급식되는 모든 사이클**(=이 기능이 값을 내야
하는 바로 그 경우)에서 구조적으로 ``"no_previous_cycle"`` 만 낸다 — 어휘는
있지만 실런에서 값이 안 나오는 함정([[feedback_vocabulary_is_not_capability_2026_08_06]]
과 같은 모양)이다.

대신 ``campaign_registry.jsonl``(``goal_campaign_feed.registry_rows``)의
``card_settled`` 이벤트를 쓴다 — 그 모듈 자기 주석이 명시: "레지스트리에는
종결이든 아니든 **항상** 쓴다(in-flight 가드를 풀고 재시도 판정의 실패
이력을 남기려고)". retry 마다 매번 한 행씩 쌓이므로, 카드가 재급식될 때마다
**진짜** 직전 시도가 거기 있다. ``goal_campaign_feed`` 자신의 재급식-꼬리
기능(``_last_exec_arm_tail``, GRADE_FEEDBACK 축)도 정확히 이 파일을 읽는다 —
이 모듈은 그 소스를 재사용할 뿐 새 파일을 만들지 않는다.

비용(``usd_spent``)은 레지스트리에 없는 필드라(그 행은 judge_verdict/
redispatch_action/cycle_id/ts 와 선택적 ``exec_arm_tail`` 만 나른다),
``runtime/campaign_spend_join.episode_cost_by_cycle``(read-only, 게이트 없음,
``episode.jsonl`` 의 ``COST``/``episode_total`` 행을 cycle_id 로 색인)를 그
prev_cycle_id 로 조회한다 — 이 조인은 종결/재시도 구분 없이 **모든**
cycle_id 에 열려 있다(그 모듈 자기 주석: "``card_settled`` 행 단위로만 조인"
이라는 ``goal_campaign_feed`` 자신의 좁은 소비는 이 원시 함수의 한계가
아니라 그쪽 소비 로직의 범위일 뿐 — 여기서는 원시 함수를 직접 부른다).

## 읽기 전용

이 모듈은 어떤 파일에도 쓰지 않는다(``atomic_append_jsonl`` 호출 0건) —
``goal_campaign_feed.registry_rows``/``episode_log.read_rows``/
``campaign_spend_join.episode_cost_by_cycle`` 셋 다 ungated 읽기 함수다.
반복-실패 서명 카운트(``repeated_failure_signature_count``)는 **재읽기하지
않는다** — 호출부(``tick_runner``)가 dispatch 직전 이미 계산해둔
``repeated_failure_gate.should_skip()`` 의 두 번째 반환값(``tail_signatures``
를 담은 관측 dict)을 인자로 받는다(같은 파일을 두 번 열지 않는다).

## 모름 ≠ 0

값이 없는 축은 생략하지 않고 ``None``(+ 상위 ``status``)으로 명시하고,
:func:`format_for_injection` 은 그 축을 문자열 ``"not_measured"`` 로 적는다.
``objective_summary``/``judge_verdict``/``usd_spent``/``exec_arm_summary`` 는
서로 독립적으로 있거나 없을 수 있다 — 원장 자체가 그렇게 부분적으로 채워지는
계약이기 때문이다(위 "정찰을 뒤집은 지점" 절 참조).

## 계약 충돌 회피

``self_improvement_v8._build_observation_bundle`` 의 최상위 dict 키
(``prev_run_context_status`` 등)는 ``test_live_declared_sweep_2026_08_10.py``
가 존재/부재를 계약으로 이미 고정하고 있다. 이 모듈의 번들은 **그 dict 에
절대 합류하지 않는다** — ``tick_runner`` 가 dispatch 직전 objective 앞에
붙이는 별도 텍스트 블록일 뿐이다(:func:`format_for_injection`).

## Default-OFF

``AGI_V8_TICK_CYCLE_MEMORY_ENABLED`` strict ``"true"``/``"1"`` (tier: T9). OFF
면 :func:`enabled` 가 ``False`` — 호출부(``tick_runner``)는 그 경우 이 모듈의
어떤 함수도 부르지 않는다(번들 조립 자체를 건너뛴다, byte-identical). 이 모듈
자체는 게이트를 강제하지 않는다(``build_bundle``/``format_for_injection`` 은
게이트와 무관하게 순수 함수다) — 강제는 호출부의 책임이다(형제 모듈
``repeated_failure_gate`` 와 달리, 여기는 파일을 안 쓰므로 "게이트 OFF 인데
호출됐다"의 위험이 부수효과가 아니라 순전히 낭비다).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

from agi_v8_1.policy.fail_fast import swallowed as _swallowed
from agi_v8_1.runtime import repeated_failure_gate

__all__ = [
    "ENV_ENABLED", "SCHEMA_VERSION", "MAX_INJECT_BYTES",
    "enabled", "build_bundle", "format_for_injection",
]

ENV_ENABLED = "AGI_V8_TICK_CYCLE_MEMORY_ENABLED"
SCHEMA_VERSION = "cycle_memory_bundle_v1"
#: 주입 텍스트 상한(바이트) — 형제 상수 ``goal_campaign_feed.GRADE_TAIL_MAX_BYTES``
#: (400)보다 넉넉하다. 이 번들은 축이 5개(objective/verdict/cost/exec_arm/
#: repeated-failure)라 grade_tail(카드 자기 채점 하나만)보다 태생적으로 길다.
MAX_INJECT_BYTES = 900
#: objective 요약 자체의 상한(문자) — 이걸 안 두면 objective_summary 하나가
#: 위 바이트 상한을 통째로 먹어 뒤따르는 verdict/cost/exec_arm/repeated-count
#: 축이 잘려나간다(트레일링 컷은 마지막 방어선이지 1차 방어선이 아니다).
_OBJECTIVE_SUMMARY_MAX_CHARS = 240

_HEADER = "[cycle memory v1 — previous cycle from ledgers]"


def enabled() -> bool:
    """Default-OFF cycle-memory injection gate (strict ``"true"``/``"1"``)."""
    return os.environ.get(ENV_ENABLED, "") in ("true", "1")  # tier: T9


def _rfg_signature_count(rfg_obs: "Mapping[str, Any] | None") -> "int | None":
    """*rfg_obs* 는 ``repeated_failure_gate.should_skip()`` 의 두 번째 반환값
    그대로(재읽기 금지). 없으면(게이트 OFF 이거나 goal_campaign 대상이 아니면)
    ``None`` — 0 으로 접지 않는다.
    """
    if not isinstance(rfg_obs, Mapping):
        return None
    tail = rfg_obs.get("tail_signatures")
    if not isinstance(tail, list):
        return None
    return sum(1 for s in tail if s is not None)


def _prior_settlement(
    state_dir: "str | Path", *, campaign_id: str, card_id: str,
    exclude_cycle_id: str,
) -> "tuple[dict[str, Any] | None, str]":
    """이 (campaign_id, card_id) 의 직전(현재 사이클 제외) ``card_settled`` 행.

    ``campaign_registry.jsonl`` 을 읽는다(``campaign_ledger.jsonl`` 이 아니다
    — 모듈 독스트링 "정찰을 뒤집은 지점" 참조: 그 파일은 retry 를 안 쓰므로
    재급식되는 실제 사이클을 못 찾는다). 반환: ``(행 또는 None, status)``.
    ``status`` 는 ``"no_previous_cycle"``/``"loaded"``/``"error:<TypeName>"``
    중 하나.
    """
    try:
        from agi_v8_1.runtime import campaign_registry as _gcf

        rows = _gcf.registry_rows(state_dir)
    except (OSError, ValueError) as exc:
        _swallowed(exc, site="runtime.cycle_memory._prior_settlement:read",
                   category="config")
        return None, f"error:{type(exc).__name__}"
    candidates = [
        r for r in rows
        if isinstance(r, Mapping)
        and r.get("event") == "card_settled"
        and str(r.get("campaign_id")) == campaign_id
        and str(r.get("card_id")) == card_id
        and str(r.get("cycle_id")) != str(exclude_cycle_id)
    ]
    if not candidates:
        return None, "no_previous_cycle"
    best = max(candidates, key=lambda r: float(r.get("ts") or 0.0))
    return best, "loaded"


def _usd_spent_for_cycle(
    state_dir: "str | Path", prev_cycle_id: "str | None",
) -> "float | None":
    """``campaign_spend_join.episode_cost_by_cycle`` 로 그 사이클의 실지출을
    조회한다(read-only, 게이트 없음, retry/terminal 무관 — 모듈 독스트링 참조).
    조인이 안 되면(그 cycle_id 가 ``episode.jsonl`` 에 없음) ``None`` — 0 으로
    접지 않는다.
    """
    if not prev_cycle_id:
        return None
    try:
        from agi_v8_1.runtime.campaign_spend_join import episode_cost_by_cycle

        return episode_cost_by_cycle(state_dir).get(str(prev_cycle_id))
    except Exception as exc:  # noqa: BLE001 — 관측 실패가 번들 조립을 못 막는다
        _swallowed(exc, site="runtime.cycle_memory._usd_spent_for_cycle",
                   category="telemetry")
        return None


def _objective_summary(
    state_dir: "str | Path", prev_cycle_id: "str | None",
) -> "str | None":
    """그 사이클의 PLAN 행 ``summary``(=objective 원문, ``episode_log.emit_plan``
    이 dispatch 전에 이미 심어둔 값 — ``runtime/cli.py`` A③, ``run_orchestrator_
    cycle`` 을 타는 모든 tick 사이클이 이걸 낸다). ``episode_log`` 자체 게이트가
    그 사이클 당시 꺼져 있었으면 PLAN 행이 아예 없어 ``None``.
    """
    if not prev_cycle_id:
        return None
    try:
        from agi_v8_1.runtime import episode_log as _el

        rows = _el.read_rows(state_dir)
    except Exception as exc:  # noqa: BLE001 — 관측 실패가 번들 조립을 못 막는다
        _swallowed(exc, site="runtime.cycle_memory._objective_summary:read",
                   category="telemetry")
        return None
    for r in reversed(rows):
        if (isinstance(r, Mapping) and r.get("event") == "PLAN"
                and str(r.get("cycle_id")) == str(prev_cycle_id)):
            summary = r.get("summary")
            return str(summary)[:_OBJECTIVE_SUMMARY_MAX_CHARS] if summary else None
    return None


def build_bundle(
    state_dir: "str | Path", row: "Mapping[str, Any]", *,
    current_cycle_id: str, rfg_obs: "Mapping[str, Any] | None" = None,
) -> "dict[str, Any]":
    """직전 사이클 관측 번들. 순수 읽기 — 어떤 파일에도 안 쓴다.

    ⚠️ 이 함수 자체는 :func:`enabled` 를 안 본다 — 호출부가 게이트를 이미
    확인했다고 가정한다(게이트 OFF 일 때 이 함수를 아예 안 부르는 쪽이 "번들
    조립 자체를 건너뛴다"는 상위 계약을 지킨다).
    """
    lineage_key = repeated_failure_gate.target_key(row)
    bundle: "dict[str, Any]" = {
        "schema_version": SCHEMA_VERSION,
        "lineage_key": lineage_key,
        "status": "no_lineage",
        "prev_cycle_id": None,
        "objective_summary": None,
        "judge_verdict": None,
        "redispatch_action": None,
        "usd_spent": None,
        "exec_arm_summary": None,
        "repeated_failure_signature_count": _rfg_signature_count(rfg_obs),
    }
    if lineage_key is None:
        return bundle

    settled, status = _prior_settlement(
        state_dir, campaign_id=str(row.get("campaign_id")),
        card_id=str(row.get("card_id")), exclude_cycle_id=current_cycle_id)
    bundle["status"] = status
    if settled is None:
        return bundle

    prev_cycle_id = settled.get("cycle_id")
    bundle["prev_cycle_id"] = str(prev_cycle_id) if prev_cycle_id else None
    bundle["judge_verdict"] = settled.get("judge_verdict")
    bundle["redispatch_action"] = settled.get("redispatch_action")
    exec_tail = settled.get("exec_arm_tail")
    bundle["exec_arm_summary"] = dict(exec_tail) if isinstance(exec_tail, Mapping) and exec_tail else None
    bundle["usd_spent"] = _usd_spent_for_cycle(state_dir, bundle["prev_cycle_id"])
    bundle["objective_summary"] = _objective_summary(state_dir, bundle["prev_cycle_id"])
    return bundle


def format_for_injection(
    bundle: "Mapping[str, Any] | None", *, max_bytes: int = MAX_INJECT_BYTES,
) -> str:
    """번들 → objective 앞에 붙일 한 텍스트 블록.

    값이 없는 축은 ``"not_measured"`` 로 명시한다(생략이 아니다 — 모름을 0/빈
    문자열로 접지 않는다는 이 레포의 불변식). 바이트 상한을 넘으면
    ``goal_campaign_feed._format_grade_tail`` 과 같은 방식(유효 UTF-8 경계에서만
    잘라낸다, ``errors="ignore"`` 가 "디코드 가능한 최장 접두사" 와 동치)으로
    줄인다.
    """
    if not bundle:
        return ""

    def _v(x: "Any") -> "Any":
        return "not_measured" if x is None else x

    parts = [_HEADER, f"lineage_status={bundle.get('status')}"]
    if bundle.get("lineage_key"):
        parts.append(f"lineage_key={bundle['lineage_key']}")
    if bundle.get("prev_cycle_id"):
        parts.append(f"prev_cycle_id={bundle['prev_cycle_id']}")
    parts.append(f"objective_summary={_v(bundle.get('objective_summary'))}")
    parts.append(f"judge_verdict={_v(bundle.get('judge_verdict'))}")
    parts.append(f"redispatch_action={_v(bundle.get('redispatch_action'))}")
    parts.append(f"usd_spent={_v(bundle.get('usd_spent'))}")
    exec_summary = bundle.get("exec_arm_summary")
    if exec_summary:
        bits = ",".join(f"{k}={v}" for k, v in exec_summary.items())
        parts.append(f"exec_arm=[{bits}]")
    else:
        parts.append("exec_arm=not_measured")
    parts.append("repeated_failure_signatures="
                 f"{_v(bundle.get('repeated_failure_signature_count'))}")
    text = " ".join(parts)
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")
