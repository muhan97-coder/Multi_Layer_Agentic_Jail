#!/usr/bin/env python3
# __SLOT_LEDGER_JOIN_CHECK_2026_08_08__ 조인축 커버리지를 **재서** 말한다.
"""젤 원장 전수 조인 커버리지 — *"임의의 사이클 하나로 몇 개 원장을 볼 수 있나"*.

## 왜 도구인가

슬롯 4(로그·계측 통일)의 진행을 **주장이 아니라 수치**로 말하려고 만든다.
`DOCS/TODO.ko.md` 가 *"원장 16개"* 라 적어뒀는데 2026-08-08 실측은 **17** 이었다
(`bus/predicted_delta_grades` 누락) — 손으로 센 목록은 썩는다.

## 무엇을 세나

    ts     행 시각을 해석할 수 있는 행의 비율   (runtime.ledger_join.row_ts)
    cycle  cycle_id 를 가진 행의 비율          (runtime.ledger_join.row_cycle)

⛔ **해석 못 한 행을 0 으로 접지 않는다.** `ts=None` 인 행이 실제로 있고
(`continuation_ring`), 그건 *"1970년"* 이 아니라 *"모른다"* 다.

사용::

    python3 tools/ledger_join_check.py                    # 커버리지 표
    python3 tools/ledger_join_check.py --cycle <cycle_id> # 그 사이클을 조회
    python3 tools/ledger_join_check.py --json             # 기계 판독
    python3 tools/ledger_join_check.py --ratchet          # 🔒 baseline 대조 (2층)
    python3 tools/ledger_join_check.py --ratchet --update # baseline 동결

`--cycle` 없이 부르면 **가장 최근 사이클**을 자동으로 골라 조회한다.

## 래칫이 2층인 이유 (2026-08-08 실측)

**1층 = 정적 생산자 전수**(:data:`CYCLE_WRITERS` + :func:`probe_calls` /
:func:`probe_sets_key` / :func:`declaration_defects` / :func:`exemption_defects` /
:func:`scope_defects`,
소비자는 ``tests/v8_1/test_ledger_join_ratchet_2026_08_08.py``). 누가
``join_keys()`` 를 떨어뜨리면 **그 자리에서** 적색이다. 젤을 안 본다.

⚠️ 1층 술어는 **여기 한 곳에만** 산다. 테스트가 사본을 들고 있으면 라이브가 쓰는
것과 테스트가 재는 것이 갈라진다 — 이 레포가 이미 치른 값이다.

⛔ 1층이 잡는 범위의 정본은 :func:`live_nodes` 의 독스트링이다. 요약: **리터럴로
결정되는 죽은 가지까지**이고 상수 전파·호출그래프는 안 본다. 완전한 도달가능성은
결정 불가능하므로 마지막 답은 언제나 **행위 표면**(원장에 한 줄 실제로 써 보는
``tests/v8_1/test_ledger_join_writers_2026_08_08.py``)이다.

**2층 = 라이브 baseline**(``--ratchet``). 원장이 조인축에 *올라온 적 있나*
(:func:`ratchet_measure` 의 ``ever_joined``)를 동결한다.

🔒 **동결 경로에도 래칫이 있다**(:func:`rescope_refusals`). 읽는 쪽의 방어는 전부
``--update`` 한 번으로 지워진다 — 그래서 *분모 안 → 분모 밖* 강등은 **쓰는 쪽**이
거부한다. 처방된 통로는 :data:`RESCOPE_ALLOWLIST` 에 적히는, 재동결 뒤 소진되는
한 줄이다.

⛔ **"유일한 통로" 가 아니다**(2026-08-09 4차 적대검증이 반증했다). 거부는 *이전
동결본과의 비교*라서 **비교 대상을 없애면** 성립하지 않는다. 남아 있는 통로 둘과
그 마찰의 정확한 크기는 :data:`RESCOPE_ALLOWLIST` 의 '안 잡는 것' 에 실측과 함께
적혀 있다. 셋째였던 *손편집*(1바이트)은 이 판에서 닫았다(:func:`ratchet_update` 의
:class:`BaselineTampered` 거부).

⛔ **2층만으로는 탐지기가 아니다.** 원장은 append-only 라 오늘 생산자를 지워도
과거 행이 남아 ``ever_joined`` 는 계속 참이다. 그리고 **행 비율은 지표로 못
쓴다** — 실측: ``executor_log`` 는 전체 19%/최근20행 50%(과거가 분모를 영구히
짓누른다), ``tick_log`` 은 전체 74%/최근 60%(정상 비-사이클 행이 섞여 최근이
오히려 낮다), 마지막 1행은 정상 행 하나로 뒤집힌다. 비단조 지표를 래칫에
넣으면 래칫이 아니라 알람 소음이다. ⇒ baseline 에는 **비율을 넣지 않는다**
(meta 에 관측치로만 적고 비교 대상에서 뺀다).
"""
from __future__ import annotations

import argparse
import ast
import datetime as _dt
import json
import re
import sys
from pathlib import Path
from typing import Any, Final, Iterator, Mapping

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO.parent) not in sys.path:
    sys.path.insert(0, str(_REPO.parent))

from agi_v8_1.policy.fail_fast import (  # noqa: E402
    format_exception_for_critical_record as _format_exception_for_critical_record,
    format_text_for_critical_record as _format_text_for_critical_record,
    strict_scope as _strict_scope,
    swallowed as _swallowed,
)
from agi_v8_1.runtime.ledger_join import row_cycle, row_ts  # noqa: E402
from agi_v8_1.tools.ratchet import (  # noqa: E402
    BETTER,
    MISSING,
    NEUTRAL,
    WORSE,
    BaselineMissing,
    BaselineTampered,
    compare,
    load_baseline,
    render_diff,
    write_baseline,
)

DEFAULT_JAIL = _REPO / "state" / "si_jail"
DEFAULT_BASELINE = _REPO / "DOCS" / "ledger-join-baseline.json"

# 🔑 **baseline 부재/손상은 삼킴이 아니라 판정이다** (2026-08-09 R5, 부류 4건 전수:
# `ratchet_check`:missing/tampered · `ratchet_update`:no_prior/tampered).
#
# HEAD 까지 이 네 자리는 `policy.fail_fast.swallowed` 를 그대로 탔고, strict 모드
# (`AGI_V8_STRICT_FAIL_FAST=true`)에서는 그 초크포인트가 재-raise 하므로 "이름 붙은
# 거부"(reason + 명시 exit 코드)가 **스택트레이스**로 바뀌었다 — 프로세스 exit=1 은
# `EXIT_REGRESSION` 과 같은 값이라 소비자(CI/훅)가 "회귀를 쟀다"와 "아예 못 쟀다"를
# 구분 못 한다(strict 실측: 세 스위트 21 failed 중 17건이 이 부류).
#
# 그래서 네 자리 전부 `with _strict_scope(False): _swallowed(...)` 로 **기록은
# 유지하고 재-raise 만 명시적으로 옵트아웃**한다. strict 의 목적 — *이름 없는
# 삼킴*을 시끄럽게 만드는 것 — 은 훼손되지 않는다: 이 자리들은 이름 없이 계속
# 가는 게 아니라 판정(반환값 reason/refused_rescope + 종료코드)으로 소비자에게
# 간다. 기록(카운터 + WARNING)도 그대로 남는다. ⛔ 헬퍼로 빼지 마라 — 침묵 핸들러
# 센서스는 초크포인트 호출이 **핸들러 몸통 안**에 있어야 routed 로 세고, 헬퍼 뒤에
# 숨기면 이 핸들러들이 침묵으로 계수된다(tools/ 상한은 이 레인 소유 파일이 아니다).
#
# ⚠️ `strict_scope` 는 `os.environ` 을 잠깐 바꾼다 — 프로세스 전역. 이 도구는
# CLI/pytest 표면이라 감수한다(초크포인트에 "기록만" API 가 생기면 그쪽으로 옮기는
# 게 맞다 — `policy/fail_fast.py` 는 이 레인 소유가 아니라 이 판에서 안 건드렸다).
# 두 모드가 같은 dict 를 돌려주는 계약은 `test_strict_*` 다섯이 고정한다.

#: 이 래칫의 신원. ⛔ 다른 래칫과 절대 같으면 안 된다 — 같으면 서로의 baseline 을
#: 물려도 통과한다(해시가 schema 까지 덮는 이유).
RATCHET_SCHEMA: Final[str] = "ledger_join_v1"

#: 🔑 **사이클에 속하지 않는 원장** — `cycle_id` 가 없는 게 결함이 아니라 사실이다.
#: 이유를 여기 적어 두지 않으면 다음 사람이 "빠뜨렸네" 하고 심는다. ⛔ 심으면
#: 없는 귀속을 만들어 **안 한 일을 셈한다**. 값은 "왜 아닌가"이고 표에 그대로 뜬다.
NOT_CYCLE_SCOPED: Final[dict[str, str]] = {
    "tick/queries.jsonl":
        "제출 시점 — 이 행이 날 때 사이클은 아직 없다. 필요한 축은 "
        "cycle 이 아니라 **누가 넣었나**(사람 vs work_feeder) = HUMAN 축",
    "briefings/emissions.jsonl":
        "범위 문서 — window_start/window_end 로 **여러 사이클**을 덮는다. "
        "하나에 귀속시키면 거짓",
    "promotions.jsonl":
        "승격 구동 기록 — 한 행이 한 번의 HUMAN 구동으로 **여러 후보(여러 "
        "사이클)** 의 계획·거절·적용을 덮는다(생산자는 tools/ 의 승격 구동기 "
        "CLI — 사이클 미배선이 설계라 배선-금지 트립와이어가 그 이름의 "
        "리터럴 인용까지 막는다). 필요한 축은 cycle 이 아니라 driver=HUMAN "
        "축이고, 후보별 귀속은 행 안의 plans[] 가 든다",
    "swarm_v8/router_freeze_ledger.jsonl":
        "외부 변경 스냅샷 — added/modified/removed 파일 집계"
        "(agent_system/swarm_v8/router_freeze.py). 사이클이 아니라 **파일시스템 "
        "관측 시점**이 축이다. 사이클에 귀속시키면 거짓",
    # __SLOT_CAMPAIGN_FAMILY_FOLD_2026_08_23__ 캠페인 차원 접기 패밀리 중
    # 사이클 밖 원장들(생산자 실증: 2026-08-23 전수 조사).
    "apply_chain_two_phase/chain.jsonl":
        "행 신원축이 txn_id+seq+prev_hash(Merkle)다 — cycle_id 는 코드·행 "
        "모두 0%(젤 30행·에피소드 4행 직접 계수). 🔴 baseline 이 이걸 "
        "cycle_scoped=true 로 동결한 것은 자체 meta(rows_with_cycle_id: 0)와 "
        "모순된 동결이었다 — 이 선언과 함께 재동결로 정정된다. 생산자는 "
        "enforcement/safe_auto_apply.py:_two_phase_txn",
    "goal_campaign/*/episodes/*/runtime_logs/first_run_report.jsonl":
        "per_cycle[].cycle_id 는 SI 사이클이 아니라 first_run 루프의 자체 "
        "순번 아이디(first_run_N)다 — 톱레벨 행에는 cycle_id 없음(실측). "
        "생산자=runtime/first_run.py:_append_report",
    "runtime_logs/card_proposer.jsonl":
        "제안 시점엔 사이클이 없다(구조적 no) — 카드 초안을 만드는 CLI 런이 "
        "축이다. 생산자=runtime/card_proposer.py:main→_append_ledger",
    "goal_campaign/*/episodes/*/apply_chain_two_phase/chain.jsonl":
        "톱레벨과 같은 Merkle 축 원장(txn_id+seq+prev_hash)의 에피소드 인스턴스"
        "— 행 cycle_id 0%(4행 직접 계수). 생산자=enforcement/safe_auto_apply.py:"
        "_two_phase_txn",
    "goal_campaign/*/episodes/*/swarm_v8/router_freeze_ledger.jsonl":
        "외부 변경 스냅샷(톱레벨 항목과 동일 성격)의 에피소드 인스턴스 — 축은 "
        "파일시스템 관측 시점이지 사이클이 아니다",
    "goal_campaign/*/episodes/*/workspace/**":
        "워크스페이스 스냅샷 내부물 — episode_workspace 에 찍힌 **레포 데이터의 "
        "사본**(data/*·swarm runs·goal_cards assets 등)이지 젤 원장이 아니다. "
        "조인축 밖이며, 스냅샷 내용은 캠페인마다 통째로 바뀌므로 파일별 열거는 "
        "무의미한다(family() 가 통째로 접는다)",
}

#: 🔑 **파일 하나 = 원장 하나가 아니다.** 이 디렉터리들은 한 원장을 날짜/런으로
#: 쪼갠다 — 그래서 파일 경로를 baseline 키로 쓰면 내일 아침 키가 하나 늘어난다
#: (문서는 *"원장 17개"* 라 적어뒀고 도구는 18개를 셌다. 둘 다 맞았다).
#:
#: ⛔ **열거한다. 추론하지 않는다.** "숫자로 끝나면 분할" 같은 규칙을 쓰면 다음
#: 원장이 규칙을 어기고, 그때 두 원장이 조용히 한 패밀리로 삼켜진다. 이 레포는
#: 그 대가를 이미 두 번 치렀다(``horizon_ts`` 접미사 추론 · ``cycle`` 순번 별칭).
#: 값은 "왜 분할인가"이고 baseline 에 실려 다음 사람에게 전달된다.
PARTITIONED: Final[dict[str, str]] = {
    "si_audit":
        "하루 한 파일 — `si_audit/<YYYYMMDD>.jsonl`. 자정마다 새 파일이 나므로 "
        "파일 경로를 키로 잡으면 baseline 이 매일 '신규 무신고' 로 적색이 된다",
    "continuation_ring":
        "런마다 한 파일 — `continuation_ring/<run_id>.jsonl` "
        "(`StatusHistoryRing._sidecar_path` 가 run_id 를 파일명 stem 으로 쓴다)",
}


def family(rel: str, partitioned: Mapping[str, Any] | None = None) -> str:
    """원장 상대경로 → **패밀리**. 분할 원장만 뭉치고 나머지는 그대로.

    ⛔ 서로 다른 원장을 삼키면 안 된다: ``bus/predicted_deltas.jsonl`` 과
    ``bus/predicted_delta_grades.jsonl`` 은 같은 디렉터리지만 **다른 원장**이다.
    그래서 뭉치는 축은 "디렉터리" 가 아니라 :data:`PARTITIONED` 에 **이름이
    적힌** 디렉터리이고, 그 아래 한 겹까지만이다(더 깊으면 그대로 둔다).

    ``partitioned`` 를 주면 그 표로 정규화한다 — *"이 표를 쓰면 무엇이 삼켜지나"*
    를 물을 수 있어야 :func:`scope_defects` 가 분할 선언을 검사할 수 있다.
    ⛔ 기본값은 여전히 전역 표다(기존 호출자의 판정을 바꾸지 않는다).
    """
    table = PARTITIONED if partitioned is None else partitioned
    norm = rel.replace("\\", "/")
    # __SLOT_CAMPAIGN_FAMILY_FOLD_2026_08_23__ 캠페인 실행 차원 접기 — 캠페인
    # id(및 .halfrun_*/.round* 재시도 접미사)와 카드 id는 **런마다 새 세그먼트**라
    # 파일 경로를 키로 잡으면 캠페인 한 번 돌 때마다 baseline 이 '신규 무신고'
    # 로 적색이 된다(실측: 08-21~22 두 캠페인+probe 5회 = 미신고 ~140가족).
    # 접는 축은 캠페인/카드 **두 세그먼트뿐**이고 꼬리(원장 이름·하위 구조)는
    # 그대로 보존한다 — 서로 다른 원장을 삼키지 않는다(PARTITIONED 와 같은 계약).
    # `goal_campaign/campaign_registry.jsonl` 처럼 2세그먼트(파일이 곧바로
    # goal_campaign 아래)인 것은 매치 자체가 안 돼 그대로 둔다 — 고정 원장.
    m = re.match(r"^goal_campaign/([^/]+)/(.+)$", norm)
    if m:
        tail = m.group(2)
        em = re.match(r"^episodes/([^/]+)/(.+)$", tail)
        if em:
            ctail = em.group(2)         # 카드 세그먼트는 <card> 로 접힌다
            if ctail.startswith("workspace/") or ctail == "workspace":
                # 스냅샷 내부물은 통째로 한 패밀리 — 레포 데이터 사본이라 내용이
                # 캠페인마다 통째로 바뀌고 조인축 밖이다(NOT_CYCLE_SCOPED 의
                # workspace/** 항목 참조). 파일별 열거는 무의미.
                return "goal_campaign/*/episodes/*/workspace/**"
            shead, ssep, stail = ctail.partition("/")
            if ssep and shead in table and stail and "/" not in stail:
                ctail = f"{shead}/*.jsonl"  # 꼬리 안의 분할 원장도 기존 규칙과 합성
            return f"goal_campaign/*/episodes/*/{ctail}"
        shead, ssep, stail = tail.partition("/")
        if ssep and shead in table and stail and "/" not in stail:
            tail = f"{shead}/*.jsonl"
        return f"goal_campaign/*/{tail}"
    head, sep, tail = norm.partition("/")
    if sep and head in table and tail and "/" not in tail:
        return f"{head}/*.jsonl"
    return rel


#: 🔑 **사이클 범위 원장의 생산자 전수.** 1층 래칫(pytest)이 이 표를 코드와
#: 대조한다 — 심볼이 사라지거나 그 몸통에서 조인 기제가 빠지면 **그 자리에서**
#: 적색이다. 2층(라이브 baseline)은 append-only 라 그걸 못 잡는다.
#:
#: ``writer`` 는 *"조인키를 이 행에 찍는 심볼"* 이다. 바이트를 append 하는 심볼과
#: 다를 수 있다(예: ``episode_log._base_row`` 는 행을 만들고 append 는 그 밖에서
#: 한다) — 우리가 지키려는 건 **키가 찍히는 자리**다.
#:
#: ``mechanism``:
#:   ``join_keys``    몸통에 ``join_keys(...)`` 호출 노드가 있다(게이트 뒤)
#:   ``native``       몸통이 ``"cycle_id"`` 키를 직접 세운다(게이트 없음)
#:   ``cycle_id_for`` ``join_keys(cycle_id=_cycle_id_for(...))`` — 문맥이 아니라
#:                    **직접 계산**. 툼스톤은 사이클 진입 **전**에도 찍히므로
#:                    문맥에서 읽으면 빈손이 된다.
#:
#: ⛔ **빈칸 금지.** 모르면 ``writer=None`` + ``why`` 를 적어라. *"못 찾았다"* 와
#: *"없다"* 는 다른 사실이고, 빈칸은 둘을 구분 못 한다.
#: ``stampers`` 칸 (선택) — **우리가 지키려는 건 키가 찍히는 자리다.**
#: writer 몸통 밖(다른 모듈 래퍼·행 생산 헬퍼)에서 이 원장 행에 키를 찍는 심볼을
#: ``"파일.py:심볼"`` 로 적는다. 소비자는 둘이다:
#:
#: 1. `tools/contracts_map.py` 가 그 심볼에서 필드를 **유도**해 원장 계약에
#:    합류시킨다(§2 stamper 표). 앵커 없이는 "다른 모듈 헬퍼가 행을 변형" 유도
#:    사각이라 그 키가 계약에서 빠진다 — 2026-08-10 역대조 실측:
#:    `apply_chain.jsonl` 의 `entry_hash`/`prev_hash`/`ts` 는 writer 앵커
#:    (`run_one_si_cycle`)가 아니라 체인 래퍼(`apply_chain_full.append`)가 찍는데
#:    선언에 없어 🔴 신호였다.
#: 2. :func:`declaration_defects` 가 앵커의 **실재**(파일+심볼)를 검사한다 —
#:    낡은 앵커는 침묵으로 비는 게 아니라 적색이다.
#:
#: ⛔ writer 를 **대체하지 않는다**: writer/mechanism 은 조인키(cycle_id) 탐침의
#: 앵커로 그대로 남는다. `tick/tick_log.jsonl` 의 stamper 셋은 writer
#: (`tick_once`)가 그대로 원장에 넘기는 **행 생산 헬퍼**들이다(판정 행 · 급식 행 ·
#: 사람 정지 행) — 2026-08-10 역대조의 미선언 19키(`stall_scan`·`sentinel*` 등)가
#: 전부 이 세 자리에서 나온다.
CYCLE_WRITERS: Final[dict[str, dict[str, Any]]] = {
    "apply_chain.jsonl": {
        "writer": "self_improvement_v8.py:SelfImprovementV8.run_one_si_cycle",
        "mechanism": "native",
        "stampers": ("enforcement/apply_chain_full.py:append",),
    },
    "bus/falsifier_events.jsonl": {
        "writer": "bus/falsifier_bus.py:append_event",
        "mechanism": "join_keys",
    },
    "bus/predicted_delta_grades.jsonl": {
        "writer": "bus/consumers/predicted_delta.py:grade_due",
        "mechanism": "join_keys",
    },
    "bus/predicted_deltas.jsonl": {
        "writer": "bus/consumers/predicted_delta.py:register_prediction",
        "mechanism": "join_keys",
    },
    "continuation_ring/*.jsonl": {
        "writer": "core/continuation_ring.py:StatusHistoryRing._write_sidecar",
        "mechanism": "join_keys",
    },
    "cycle_log.jsonl": {
        "writer": "self_improvement_v8.py:SelfImprovementV8.run_one_si_cycle",
        "mechanism": "native",
    },
    "events.jsonl": {
        "writer": "core/cycle_logger.py:CycleLogger.log_event",
        "mechanism": "native",
    },
    "runtime_logs/cross_model_verify_spend.jsonl": {
        "writer": "verifier/cross_model_budget.py:record_spend",
        "mechanism": "join_keys",
    },
    "runtime_logs/episode.jsonl": {
        "writer": "runtime/episode_log.py:_base_row",
        "mechanism": "native",
    },
    # __SLOT_EXEC_SELECT_LOG_JOIN_KEYS_2026_08_16__ 🔴 이전 판까지 미선언
    # (undeclared writer) — `--ratchet`이 "추가 1: runtime_logs/exec_select_log.jsonl"
    # 로 잡아냈다(2026-08-16 track8이 신설한 원장, 08-16 조인축 판이 등재).
    # `record_event`가 이제 `join_keys()`를 찍는다(si_lanes/exec_select_log.py 참조).
    "runtime_logs/exec_select_log.jsonl": {
        "writer": "si_lanes/exec_select_log.py:record_event",
        "mechanism": "join_keys",
    },
    "runtime_logs/executor_log.jsonl": {
        "writer": "enforcement/executor_log.py:_emit",
        "mechanism": "join_keys",
    },
    "runtime_logs/si_swarm_evidence.jsonl": {
        "writer": "bridge/si_evidence.py:swarm_evidence_blocks",
        "mechanism": "native",
    },
    "si_audit/*.jsonl": {
        "writer": "enforcement/safe_auto_apply.py:_audit_event",
        "mechanism": "join_keys",
    },
    "si_dispatcher_v8_chain.jsonl": {
        "writer": "sia/self_improvement_dispatcher_v8.py:SIDispatcherV8.dispatch_cycle",
        "mechanism": "native",
    },
    "tick/consumed.jsonl": {
        "writer": "runtime/tick_runner.py:_tombstone",
        "mechanism": "cycle_id_for",
    },
    "tick/tick_log.jsonl": {
        "writer": "runtime/tick_runner.py:tick_once",
        "mechanism": "native",
        "stampers": (
            "runtime/work_feeder.py:_verdict_row",
            "runtime/work_feeder.py:_fed_row",
            "runtime/tick_runner.py:_human_halt_row",
            # __SLOT_SELF_FEED_REFEED_STAMPER_2026_08_16__ 트랙6이 신설한
            # 관측 행(REASON_REFEED_TRIGGERED) — writer 몸통 밖에서 tick_log
            # 에 행을 얹는 네 번째 헬퍼. 선언 안 하면 `contracts_map.py`의
            # 필드 유도가 이 자리를 못 보고(§2 stamper 표), `refeed_streak`/
            # `refeed_threshold`/`refeed_found_candidate`/`refeed_n_rejected`
            # 가 계약에서 조용히 빈다 — 2026-08-10 apply_chain 역대조와
            # 같은 사각.
            "runtime/work_feeder.py:_write_refeed_marker",
        ),
    },
    # __SLOT_CAMPAIGN_FAMILY_FOLD_2026_08_23__ 캠페인 차원 접기 패밀리들 —
    # 08-21~22 두 캠페인+probe 런이 만든 미신고 ~140가족(선재 부채)의 정식
    # 등재. 생산자는 전부 실코드 검증분(2026-08-23 전수 조사: 각 원장 행을
    # 직접 열어 cycle_id 유무를 세고, 심볼은 grep 으로 실재 확인했다).
    "goal_campaign/campaign_registry.jsonl": {
        "writer": "runtime/goal_campaign_feed.py:record_settlement",
        "mechanism": "native",
        "why": ("혼합 원장 — card_settled 행만 cycle_id 를 심고(open/fed 행은 "
                "날 때 사이클이 아직 미정), 그래도 조인 가능 축이 살아 있어 "
                "CYCLE_WRITERS 쪽에 둔다"),
    },
    # __SLOT_CAMPAIGN_FAMILY_FOLD_2026_08_23__ 🔴 처음에 NOT_CYCLE_SCOPED 로
    # 선언했더니 **라이브가 반증**했다(CLI 래칫): 재도전 캠페인 레저 24행 중
    # 3행이 cycle_id 보유 — 밤새 착지된 tick-feed 정산 배선(b4bfe05 계열)의
    # tick_settled 종결 행이 dispatch 사이클을 심기 시작했다. 대부분 행은 여전히
    # 카드 수명축이라 혼합이지만, 조인 가능한 축이 살아 있으면 분모 안이
    # 정직한 쪽이다("사이클 밖" 신고는 분모에서 빠져 숨기는 방향이다).
    "goal_campaign/*/campaign_ledger.jsonl": {
        "writer": "runtime/goal_campaign_feed.py:record_settlement",
        "mechanism": "native",
        "why": ("혼합 원장 — 대부분 행(campaign_start·카드 종결 등)은 cycle_id "
                "없이 카드 수명축으로 찍히지만, tick_settled 종결 행만 "
                "dispatch 사이클을 심는다(실측 3/24)"),
    },
    "goal_campaign/*/episodes/*/apply_chain.jsonl": {
        "writer": "self_improvement_v8.py:SelfImprovementV8.run_one_si_cycle",
        "mechanism": "native",
        "stampers": ("enforcement/apply_chain_full.py:append",),
    },
    "goal_campaign/*/episodes/*/bus/falsifier_events.jsonl": {
        "writer": "bus/falsifier_bus.py:append_event",
        "mechanism": "join_keys",
        "why": ("조건부 — episode-ctx 가 없으면 join_keys() 가 빈 dict 를 "
                "반환해 행에 키가 안 찍힌다. 캠페인 에피소드 실측 78행 0% 는 "
                "미배선이 아니라 **컨텍스트 부재**다(runtime/ledger_join.py 참조)"),
    },
    "goal_campaign/*/episodes/*/continuation_ring/*.jsonl": {
        "writer": "core/continuation_ring.py:StatusHistoryRing._write_sidecar",
        "mechanism": "join_keys",
        "why": ("run_id 분할(톱레벨 PARTITIONED 와 동일 근거)의 에피소드 인스턴스"
                "— 조건부로 젤 최상위 51행 중 30행만 키가 있다"),
    },
    "goal_campaign/*/episodes/*/cycle_log.jsonl": {
        "writer": "self_improvement_v8.py:SelfImprovementV8.run_one_si_cycle",
        "mechanism": "native",
    },
    "goal_campaign/*/episodes/*/events.jsonl": {
        "writer": "core/cycle_logger.py:CycleLogger.log_event",
        "mechanism": "native",
    },
    "goal_campaign/*/episodes/*/runtime_logs/episode.jsonl": {
        "writer": "runtime/episode_log.py:_base_row",
        "mechanism": "native",
    },
    "goal_campaign/*/episodes/*/runtime_logs/exec_select_log.jsonl": {
        "writer": "si_lanes/exec_select_log.py:record_event",
        "mechanism": "join_keys",
    },
    "goal_campaign/*/episodes/*/runtime_logs/si_verify_gate_log.jsonl": {
        "writer": "si_lanes/verify_gate_log.py:record",
        "mechanism": "native",
    },
    "goal_campaign/*/episodes/*/runtime_logs/executor_log.jsonl": {
        "writer": "enforcement/executor_log.py:_emit",
        "mechanism": "join_keys",
    },
    "goal_campaign/*/episodes/*/runtime_logs/si_swarm_evidence.jsonl": {
        "writer": "bridge/si_evidence.py:swarm_evidence_blocks",
        "mechanism": "native",
    },
    "goal_campaign/*/episodes/*/si_audit/*.jsonl": {
        "writer": "enforcement/safe_auto_apply.py:_audit_event",
        "mechanism": "join_keys",
    },
    "goal_campaign/*/episodes/*/si_dispatcher_v8_chain.jsonl": {
        "writer": "sia/self_improvement_dispatcher_v8.py:SIDispatcherV8.dispatch_cycle",
        "mechanism": "native",
    },
    "promotion_outlet.jsonl": {
        "writer": "tools/promotion_outlet.py:dispatched",
        # __SLOT_CAMPAIGN_FAMILY_FOLD_2026_08_23__ join_keys 가 아니라 native —
        # dispatched 는 소스 행의 cycle_id 를 **직접 세워** 복사한다(탐침 검증
        # 실측). summary 행(row_kind=summary)은 계수 스냅샷이라 키가 없다.
        "mechanism": "native",
    },
    "runtime_logs/si_verify_gate_log.jsonl": {
        "writer": "si_lanes/verify_gate_log.py:record",
        "mechanism": "native",
    },
    "runtime_logs/spend_reservation.jsonl": {
        "writer": "runtime/spend_reservation.py:reserve",
        "mechanism": "native",
        "stampers": ("runtime/spend_reservation.py:settle",
                     "runtime/spend_reservation.py:release"),
    },
}

#: ``mechanism`` 의 허용 어휘. ⛔ 오타를 중립으로 접으면 그 원장의 검사가 조용히
#: 사라진다 — 1층 래칫이 이 집합 밖의 값을 즉시 적색으로 만든다.
MECHANISMS: Final[frozenset[str]] = frozenset({"join_keys", "native", "cycle_id_for"})

#: 🔒 **면제 상한(래칫).** ``writer=None`` 은 *"생산자를 못 찾았다"* 를 정직하게
#: 적는 칸이지 검사를 끄는 스위치가 아니다 — 그런데 계량하지 않으면 그게 정확히
#: 스위치가 된다(2026-08-09 실측: 한 줄을 ``writer=None`` 으로 바꾸자 그 원장의
#: 1층 탐침이 통째로 사라졌는데 스위트는 **64→63 통과**, 실패 0. 개수가 준 것만이
#: 유일한 신호였고 아무도 개수를 안 본다).
#:
#: 값은 **오늘의 라이브 실측**이다(2026-08-09 기준 ``writer=None`` 0건). 올리려면
#: 이 줄을 고쳐야 하고 그건 리뷰를 받는 편집이다. ⚠️ 올려도 면제가 공짜는 아니다 —
#: :func:`declaration_defects` 의 ``why`` 형식검사와
#: :func:`exemption_defects` 의 증거 대조가 각 면제 건마다 따로 걸린다.
#:
#: 🔑 **상한을 올리는 것은 정당한 복구 경로다**(단, 실측과 정확히 같아야 한다 —
#: ``test_exemption_cap_matches_the_live_table``). 그래서 상한을 넘는지 재는 테스트는
#: 상수 1 을 박지 않고 ``cap+1`` 로 만든다. 도구가 처방하는 경로를 테스트가 막으면
#: 개발자는 **테스트를 고쳐서** 통과시키고, 그 순간 방어는 사라진다.
MAX_UNDECLARED_WRITERS: Final[int] = 0

#: 🔒 **더 큰 면제의 상한.** 원장을 :data:`CYCLE_WRITERS` 에서
#: :data:`NOT_CYCLE_SCOPED` 로 옮기는 것은 ``writer=None`` 보다 **크다** — 1층 탐침이
#: 사라질 뿐 아니라 그 원장이 **분모에서도 빠진다**(= 지표가 예뻐진다).
#: 2026-08-09 실측 2건으로 동결 → 같은 날 저녁 신규 원장 2건 등재로 4
#: (promotions=승격 경로 개통이 만든 원장 · router_freeze=외부 변경 스냅샷,
#: 둘 다 **강등이 아니라 신규** — 동결본에 cycle_scoped 로 실린 적 없음).
#:
#: ⚠️ 이 상한이 없을 때(2026-08-09 적대검증) `cross_model_verify_spend` 를 사이클
#: 밖으로 재분류하고 baseline JSON 한 줄을 같이 손편집하니 **적색 0** 이었다.
# __SLOT_CAMPAIGN_FAMILY_FOLD_2026_08_23__ 4 → 9: 캠페인 접기로 드러난 실측
# 기반 면제 5건 추가(first_run_report·card_proposer·에피소드 two_phase chain·
# 에피소드 router_freeze 인스턴스·workspace/** — 전 항목 생산자 실증 why 동봉).
# campaign_ledger 는 라이브 반증으로 CYCLE_WRITERS 로 이동해 제외(위 참조).
# 상한은 마찰 장치라 정확히 현재 개수에 맞춘다 — 다음 재분류는 이 숫자를
# 다시 올리며 근거를 써야 한다.
MAX_NOT_CYCLE_SCOPED: Final[int] = 10

#: 🔒 **재분류 영수증.** 동결본이 *분모 안*(``cycle_scoped=true``)이라 말하는 원장을
#: :data:`NOT_CYCLE_SCOPED` 로 옮기려면 그 패밀리 이름을 여기 적어야 하고, 그래야
#: ``--ratchet --update`` 가 그 강등을 동결한다(:func:`rescope_refusals`).
#:
#: 🔑 **왜 상한이 아니라 영수증인가**(2026-08-09 3차 적대검증). 상한과 동결증거
#: 대조는 *"재분류 + 상한 올림 + **정상 재동결**"* 3단 앞에 무력했다 — 증거 파일이
#: 새로 쓰이면 1층이 대조할 게 없고, 2층의 라이브 행 반증은 **cycle_id 행이 0개인
#: 원장**(= 정확히 숨기고 싶은 그 원장)에는 원리상 발화하지 않는다. 그래서 래칫을
#: *읽는 쪽*이 아니라 **쓰는 쪽**에 건다: 강등은 동결 경로가 거부한다.
#:
#: ⛔ **서 있는 면제가 아니다.** 재동결이 끝나면 동결본이 이미 분모 밖이라 말하므로
#: 이 줄은 **소진**되고, 남겨두면 :func:`rescope_allowlist_defects` 가 적색이다
#: (= 다음 재분류가 이 줄을 타고 조용히 지나가는 길을 막는다). 그래서 여기 상한은
#: 필요 없다 — 비어 있는 것이 정상 상태이고, 오늘도 비어 있다.
#:
#: ## ⛔ 이 방어가 **안 잡는 것** (2026-08-09 4차 실측, 둘 다 열려 있다)
#:
#: 거부는 *"이전 동결본과 비교해서"* 성립하므로, 비교 대상 자체를 없애면 통과한다.
#: 아래 둘은 ``test_the_documented_laundering_paths_are_open`` 이 **거동으로**
#: 못 박는다 — 문서만 적어두면 다음 판에서 조용히 참/거짓이 바뀐다:
#:
#: 1. **baseline 을 지우고 처음부터 재동결** — ``BaselineMissing`` ⇒ 이전 판이 없다
#:    ⇒ 강등이 아니라 최초 동결이다(실측: ``wrote=True``, 이후 ``--ratchet`` exit=0).
#: 2. **다른 경로에 동결한 뒤 덮어쓰기** — ``--baseline /tmp/x.json --update`` 는
#:    같은 젤·같은 스키마라 해시가 **정상**이고, 그 파일을 제자리에 복사하면
#:    ``payload_sha256`` 검사도 통과한다(실측: exit=0).
#:
#: 두 경로에 남는 마찰은 **커밋 diff 하나뿐**이다: `DOCS/ledger-join-baseline.json`
#: 이 통째로 다시 쓰이고 ``meta.carried_forward`` 이력이 사라진다. ⇒ 이건 기계가
#: 막는 문이 아니라 **사람이 보는 문**이고, 그렇게 적어둔다. (HEAD 는 '조용한 두
#: 편집' 이었고 지금은 '전면 재작성 + 리뷰' 다 — 올라갔지만 닫히지 않았다.)
#: 진짜로 닫으려면 동결본이 **레포 밖의 앵커**(커밋 이력·서명)를 물어야 한다.
#:
#: ### 🔒 셋째 경로는 이 판에서 닫았다 — 그리고 그게 왜 여기 적혀 있나
#:
#: 3라운드는 위 목록을 **완전하다고 읽히게** 적었는데, 4차 적대검증이 더 싼 셋째
#: 경로를 찾았다: 동결본을 **손편집**하면(``meta.at`` 에 공백 한 글자 — 래칫
#: payload 에 들어가지도 않는 필드) ``ratchet_update`` 가 ``BaselineTampered`` 를
#: ``BaselineMissing`` 과 같은 핸들러로 삼켜 ``doc=None`` 으로 내려갔고, 그러면
#: :func:`rescope_refusals` 가 **아예 호출되지 않는다**(실측: ``wrote=True``,
#: 헤드라인 13/15 → 13/14, 이후 exit=0). ⇒ 지금은 거부한다.
#: 🔑 교훈은 *"셋을 다 막았다"* 가 아니라 **열거는 완전성의 증거가 아니다** 이다 —
#: 이 목록도 오늘까지 찾은 것뿐이고, 넷째가 있으면 그건 여기 없다.
RESCOPE_ALLOWLIST: Final[frozenset[str]] = frozenset()
# __SLOT_CAMPAIGN_FAMILY_FOLD_2026_08_23__ 소진 기록: 재동결로 3건의 강등을
# 완료해 허가를 지웠다 — ①apply_chain_two_phase/chain.jsonl(톱레벨, Merkle 축
# cycle_id 0%) ②그 에피소드 인스턴스 ③에피소드 router_freeze_ledger(파일시스템
# 관측 축, 톱레벨과 동일 성격). 근거는 각 표 항목 why 참조. 미결 허가를
# 남기면 rescope_allowlist_defects 가 적색이다(소진 허가 보관 금지 계약).

#: 🔒 **분할 선언의 상한.** :data:`PARTITIONED` 에 디렉터리를 하나 더하면 그 아래
#: 원장들이 **한 패밀리로 삼켜진다** — 두 원장의 판정이 하나로 접히는 길이다.
#: 2026-08-09 실측 2건으로 동결. (삼킴 자체는 :func:`scope_defects` 가 따로 잡는다.)
MAX_PARTITIONED: Final[int] = 2

#: 🔒 **stamper 바닥(래칫).** ``stampers`` 는 2026-08-10 아침까지 **한 방향만**
#: 계약이었다: 신고하면 그 심볼이 실재해야 하지만, **신고를 지우는 것은 완전
#: 침묵**이었다. 같은 날 오후 적대검증 실측 — 15 패밀리의 ``stampers`` 를 전부
#: ``()`` 로 비우거나 키를 통째로 지워도 ``ledger_join or stamp`` 274개가 **전부
#: 초록**이었다(대조군: writer 를 없는 파일로 · 없는 심볼로 · 패밀리 삭제 ·
#: cap 99 는 전부 KILLED). ``writer=None`` 이 :data:`MAX_UNDECLARED_WRITERS` 로
#: 계량되는 것과 정확히 반대 방향의 구멍이다.
#:
#: ⇒ 패밀리별 **최소 개수**를 오늘의 실측으로 동결한다. 소비자가 둘이라 침묵이
#: 두 번 아프다: `tools/contracts_map.py` 의 필드 유도가 그 앵커를 잃으면 그 원장의
#: 계약 필드가 조용히 비고(2026-08-10 역대조의 미선언 19키가 그 자리에서 나왔다),
#: :func:`declaration_defects` 의 실재검사는 **신고된 것**만 볼 수 있다.
#:
#: ⛔ **심볼 이름을 여기 복사하지 않는다** — 같은 값을 두 곳에 적으면 검사 안 받는
#: 쪽이 썩는다(이 레포가 pricing SSOT 에서 이미 치른 값). 개수만 동결하고, 그
#: 개수가 실측과 정확히 같은지는 ``test_stamper_floor_matches_the_live_table`` 이 잰다.
#: 앵커를 **바꾸는** 것(같은 개수, 다른 심볼)은 여전히 실재검사만 받는다 —
#: 그건 이 래칫이 막는 대상이 아니다(막는 것은 **지우기**다).
#:
#: ## 🔴 개수는 **서로 다른 앵커**의 개수다 (2026-08-10 오후, R2 적대검증)
#:
#: 첫 판은 ``len(stampers)`` 였고 그건 **복제를 못 봤다**. ``(a, a, a)`` 로 적으면
#: 진짜 앵커 2개 삭제가 이 바닥을 그대로 통과했다 — 실측: 소유 스위트 235 초록 ·
#: ``-k "ledger_join or stamp"`` 277 초록 · 그런데 `tools/contracts_map.py` 산출물에서는
#: 그 두 앵커가 유도하던 계약 행이 **정말로 사라졌다**. ⇒ *"지우기를 계량한다"* 는
#: 선언이 복제 한 줄 앞에서 거짓이었다. 지금은 (a) 개수가 ``set`` 기준이고
#: (b) 복제 자체가 :func:`declaration_defects` 의 별도 적색이다(바닥이 없는
#: 패밀리에서도 — 같은 심볼을 두 번 적어봐야 유도에 아무것도 안 보탠다).
REQUIRED_STAMPERS: Final[dict[str, int]] = {
    "apply_chain.jsonl": 1,
    # __SLOT_SELF_FEED_REFEED_STAMPER_2026_08_16__ 3 → 4: `_write_refeed_marker`
    # 등재(위 CYCLE_WRITERS 참조). `test_stamper_floor_matches_the_live_table`
    # 이 이 값과 실측 개수의 정확 일치를 잰다.
    "tick/tick_log.jsonl": 4,
    # __SLOT_CAMPAIGN_FAMILY_FOLD_2026_08_23__ 캠페인 접기 패밀리 2종 — 실측과
    # 정확 일치 유지(바닥이 낮으면 그 차이가 조용한 삭제 예산이 된다).
    "goal_campaign/*/episodes/*/apply_chain.jsonl": 1,
    "runtime_logs/spend_reservation.jsonl": 2,
}

#: ``why`` 안에서 *"여기를 뒤졌다"* 로 인정하는 토큰 모양. 길이 검사만으로는
#: 아무 문장이나 통과한다(실측: 20자 넘는 한 문장으로 면제가 성립했다).
_PY_PATH_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]*\.py")


# ---------------------------------------------------------------------------
# 1층 탐침 — 도달가능성 인지 AST
# ---------------------------------------------------------------------------
#: 이 문 **뒤**의 같은 블록 문장은 실행되지 않는다. 정지문제가 아니라 산술이다.
_TERMINATORS: Final[tuple[type, ...]] = (
    ast.Return, ast.Raise, ast.Break, ast.Continue)


def static_truth(node: ast.AST) -> bool | None:
    """리터럴만으로 판정되는 진리값. 모르면 ``None`` (= 살아 있다고 본다).

    ⛔ **상수 전파를 하지 않는다.** ``_ENABLED = False`` 뒤의 ``if _ENABLED:`` 는
    여기서 ``None`` 이다 — 모듈 상태·import 순서·monkeypatch 를 따라가기 시작하면
    탐침이 인터프리터가 되고, 그때부터는 탐침의 버그가 판정을 만든다.
    """
    if isinstance(node, ast.Constant):
        return bool(node.value)
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        if any(isinstance(e, ast.Starred) for e in node.elts):
            return None                       # `[*xs]` 는 길이를 모른다
        return bool(node.elts)
    if isinstance(node, ast.Dict):
        if any(k is None for k in node.keys):
            return None                       # `{**m}` 는 길이를 모른다
        return bool(node.keys)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        inner = static_truth(node.operand)
        return None if inner is None else (not inner)
    if isinstance(node, ast.BoolOp):
        vals = [static_truth(v) for v in node.values]
        if isinstance(node.op, ast.And):
            # `x and False` 는 x 가 뭐든 falsy 다(첫 falsy 또는 마지막을 돌려주므로).
            if any(v is False for v in vals):
                return False
            return True if all(v is True for v in vals) else None
        if any(v is True for v in vals):
            return True
        return False if all(v is False for v in vals) else None
    return None


def _live_block(stmts: list[Any]) -> list[Any]:
    """한 문장 리스트에서 도달 가능한 문장만 — 종결문 **뒤**를 잘라낸다."""
    out: list[Any] = []
    for stmt in stmts:
        out.append(stmt)
        if isinstance(stmt, _TERMINATORS):
            break
    return out


def _live_comprehension(node: ast.AST) -> Iterator[ast.AST]:
    """comprehension 의 살아 있는 조각.

    평가 순서가 전부다: 첫 ``iter`` 는 **항상** 평가되고, 그것이 비어 있으면
    ``target`` · ``ifs`` · 뒤따르는 generator · 결과식(``elt`` 또는 ``key``/
    ``value``)은 **한 번도 안 돈다**. ``[join_keys() for _ in []]`` 은
    ``if False: join_keys()`` 와 정확히 같은 등급의 죽은 코드다.

    🔑 **필터도 같은 등급이다**(2026-08-09 3차 적대검증이 걸어 들어온 문):
    ``[join_keys() for i in [1] if False]`` 의 결과식은 iter 가 비어 있을 때와
    똑같이 **0회** 돈다. 첫 판은 ``ifs`` 를 그냥 통과시켜서 이 형태가 probe=1
    이었다 — 실행 오라클로 재확인했다(runtime=0). 거짓인 필터 **뒤**의 필터도
    평가되지 않는다.

    ⚠️ 뒤쪽 generator 의 ``iter`` 는 바깥이 한 바퀴라도 돌아야 평가되지만, 그걸
    모른 채 **살아 있다고** 세는 쪽이 안전하다(못 잰 것으로 적색을 만들지 않는다).
    """
    for gen in node.generators:
        yield gen.iter
        if static_truth(gen.iter) is False:
            return          # target·ifs·이후 generator·결과식 전부 안 돈다
        yield gen.target
        for cond in gen.ifs:
            yield cond      # 이 조건 **자체**는 평가된다(`[x for x in [1] if f()]`)
            if static_truth(cond) is False:
                return      # 통과하는 원소가 0개 ⇒ 이후 필터·generator·결과식 사망
    if isinstance(node, ast.DictComp):
        yield node.key
        yield node.value
    else:
        yield node.elt      # ListComp / SetComp / GeneratorExp


def _live_children(node: ast.AST) -> Iterator[ast.AST]:
    if isinstance(node, ast.IfExp):                    # `a if <cond> else b`
        truth = static_truth(node.test)
        yield node.test
        if truth is not False:
            yield node.body
        if truth is not True:
            yield node.orelse
        return
    if isinstance(node, (ast.If, ast.While)):
        truth = static_truth(node.test)
        yield node.test
        if truth is not False:
            yield from _live_block(node.body)
        # 🔑 `while False: ... else: X` 의 X 는 **실행된다** — else 는 루프가
        #    break 없이 끝나면 도는 절이고, 0회 반복도 그 경우다.
        if isinstance(node, ast.While) or truth is not True:
            yield from _live_block(node.orelse)
        return
    if isinstance(node, (ast.For, ast.AsyncFor)):
        # 🔑 `for _ in []:` 은 `if False:` 와 **같은 등급**이다 — 리터럴만으로
        #    0회 반복이 결정된다. 2026-08-09 적대검증이 정확히 이 구멍으로
        #    걸어 들어왔다(`if False:` 는 잡히는데 `for _ in []:` 는 통과).
        truth = static_truth(node.iter)
        yield node.iter
        if truth is not False:
            yield node.target
            yield from _live_block(node.body)
        # `for _ in []: ... else: X` 의 X 도 **실행된다**(0회 반복 = break 없이 종료).
        yield from _live_block(node.orelse)
        return
    if isinstance(node, (ast.ListComp, ast.SetComp,
                         ast.GeneratorExp, ast.DictComp)):
        yield from _live_comprehension(node)
        return
    if isinstance(node, ast.BoolOp):
        # 단락평가도 리터럴 산술이다: `False and f()` 의 f() 는 안 돈다.
        # and 는 첫 **거짓**에서, or 는 첫 **참**에서 멈춘다.
        stop = False if isinstance(node.op, ast.And) else True
        for value in node.values:
            yield value
            if static_truth(value) is stop:
                return
        return
    for _field, value in ast.iter_fields(node):
        if isinstance(value, list):
            items = [v for v in value if isinstance(v, ast.AST)]
            if items and all(isinstance(v, ast.stmt) for v in items):
                items = _live_block(items)
            yield from items
        elif isinstance(value, ast.AST):
            yield value


def live_nodes(node: ast.AST) -> Iterator[ast.AST]:
    """:func:`ast.walk` 의 도달가능성 인지 판 — **정적으로 죽은 가지를 뺀다**.

    ## 왜 있나 (2026-08-09 실측)

    ``ast.walk`` 는 ``if False:`` 안도 걷는다. 그래서 생산자의 진짜 호출을 지우고
    ``if False: entry.update(join_keys())`` 만 남겨도 1층 스위트가 **64개 전부
    통과**했고, 같은 순간 라이브 행 2개는 ``cycle_id`` 를 하나도 안 달았다.
    이 레포가 이미 비싸게 배운 항목의 재발이다 — *어휘가 있다 ≠ 값이 나온다*.

    ⚠️ 2026-08-09 2차 적대검증: 첫 판은 ``if False:`` 만 잡고 ``for _ in []:`` 은
    통과시켰다. 둘은 **같은 등급**(리터럴만으로 0회 실행이 결정)인데 목록에 for 문이
    아예 없었다 — 문서가 실제보다 강하게 주장하고 있었다.

    🔑 **아래 두 목록은 R5(2026-08-09)부터 양방향 기계검사된다** — 단, 산문이
    아니라 **마커**가 검사 대상이다. 각 항목의 ``[probe:<slug>]`` 마커와 테스트
    corpus 표(``_DEAD_BODIES``/``_BLIND_BODIES``,
    ``tests/v8_1/test_ledger_join_ratchet_2026_08_08.py``)의 키를
    :func:`doc_claim_defects` 가 양쪽으로 대조한다: 마커만 있고 corpus 가 없으면
    **과대주장**, corpus 가 고정하는 성질이 마커로 없으면 **과소서술**. 4차가
    "규율이지 기계가 아니다" 라 적어둔 자리다 — 이제 목록에 한 줄 늘리려면 corpus
    도 같이 늘려야 하고, 그 반대도 같다. ⛔ 산문 파싱은 여전히 안 한다(문서가
    굳는다 — 3라운드의 선택 유지): 마커 없는 문장(monkeypatch·정지문제·깊은 not)
    은 기계 범위 밖이고, 그 경계의 정본은 :func:`doc_claim_defects` 독스트링이다.

    ## 잡는 것 — 경계는 하나: **리터럴만으로 실행 여부가 결정되는 것**

    거짓 리터럴 = ``False`` · ``0`` · ``None`` · ``''`` · ``[]`` · ``()`` · ``{}``
    (그리고 ``not``/``and``/``or`` 로 이들을 조합한 것). ⛔ ``[*xs]``·``{**m}`` 은
    길이를 모르므로 리터럴이 아니다.

    * ``if <거짓 리터럴>:`` 의 body / ``if <참 리터럴>:`` 의 orelse [probe:dead-if]
    * ``while <거짓 리터럴>:`` 의 body (⚠️ ``else:`` 절은 **살아 있다**)
      [probe:dead-while]
    * ``for <t> in <거짓 리터럴>:`` 의 target·body (⚠️ ``else:`` 절은 **살아 있다** —
      0회 반복도 break 없이 끝난 것이므로 else 가 돈다). ``async for`` 도 같다.
      [probe:dead-for]
    * comprehension — ``[f() for _ in []]`` · ``{k: f() for k in ()}`` ·
      ``(f() for _ in '')``. 첫 ``iter`` 는 항상 평가되고 그 뒤가 죽는다
      (:func:`_live_comprehension`). 뒤쪽 generator 의 ``iter`` 가 거짓 리터럴이면
      결과식도 죽는다. [probe:dead-comprehension-iter]
    * comprehension 의 **필터** — ``[f() for i in [1] if False]``. 거짓 필터 뒤의
      필터·generator·결과식이 죽는다(조건식 **자체**는 평가되므로 살아 있다).
      ⚠️ 2026-08-09 3차 적대검증이 정확히 이 구멍으로 들어왔다: ``for _ in []`` 은
      잡히는데 ``if False`` 는 통과했고, 두 목록 어디에도 안 적혀 있었다.
      [probe:dead-comprehension-filter]
    * ``x if <거짓 리터럴> else y`` 의 죽은 가지 (:class:`ast.IfExp`)
      [probe:dead-ifexp]
    * 단락평가 — ``False and f()`` 의 ``f()``, ``True or f()`` 의 ``f()``
      [probe:dead-shortcircuit]
    * 같은 블록에서 ``return`` / ``raise`` / ``break`` / ``continue`` **뒤**의 문장
      [probe:dead-after-terminator]

    ## 안 잡는 것 — ⛔ 잡는다고 주장하지 않는다

    * ``FLAG = False`` 후 ``if FLAG:`` — **상수 전파 안 한다**(:func:`static_truth`)
      [probe:blind-constant-fold]
    * 아무도 안 부르는 함수 안의 호출 — 호출그래프 도달가능성은 안 본다
      [probe:blind-call-graph]
    * ``if not __debug__:`` — ``-O`` 여부에 달렸다 [probe:blind-debug-flag]
    * ``assert False`` **뒤**의 문장 — ``-O`` 에서 assert 가 증발한다
      [probe:blind-assert-off]
    * ``sys.exit()`` / ``os._exit()`` **뒤**의 문장 — 종결은 **호출**이지 문법이
      아니다. 이름으로 종결자를 알아보기 시작하면 탐침이 호출그래프를 흉내낸다.
      [probe:blind-exit-call]
    * ``match`` 의 리터럴로 결정되는 case — 패턴 의미론까지 들어가면 인터프리터다
      [probe:blind-match-literal]
    * ``if cfg.enabled:`` 처럼 런타임 값에 달린 가지 — 원리적으로 못 본다
      [probe:blind-runtime-branch]
    * monkeypatch·``sys.modules`` 수술로 런타임에 죽는 코드
    * **리터럴 비교·산술·부호** — ``if 1 == 2:`` · ``if len([]):`` · ``if -0.0:`` 는
      ``None``(모름)이다. ⚠️ 부호는 2026-08-10 적대검증이 짚은 자리다: ``-0.0`` 은
      ``UnaryOp(USub, Constant)`` 라 **리터럴 노드가 아니고**, 그래서 falsy 인데도
      live 로 센다. 이 목록이 그 사실을 안 적으면 *"리터럴만으로 결정"* 이라는
      선언이 구현보다 넓게 읽힌다(과대계상은 fail-closed 쪽이라 안전하지만,
      선언과 구현이 어긋난 것 자체는 그것대로 결함이다).
      :func:`static_truth` 는 진리값을 접을 뿐 **평가하지 않는다**. 접기 시작하면
      경계가 "리터럴" 에서 "내가 구현한 파이썬 부분집합" 으로 미끄러진다.
      (2026-08-09 3차 실측: 둘 다 probe=1 / runtime=0 인 **미탐**이다.)
      [probe:blind-literal-arithmetic]
    * **소비되지 않는 generator** — ``z = (f() for _ in [1])`` 는 아무도 안 돌리면
      ``f()`` 가 한 번도 안 돈다(실측 probe=1 / runtime=0). 소비 여부는 데이터플로
      질문이라 여기서 안 본다. ⚠️ 조인키를 이렇게 쓰는 생산자가 나타나면 1층은
      속는다 — 마지막 답은 행위 표면이다. [probe:blind-unconsumed-generator]
    * ``while True:`` (break 없는 무한 루프) **뒤**의 문장 — 실행되지 않지만 안
      잡는다(실측 probe=1). break 도달가능성 분석은 리터럴 산술이 아니다.
      [probe:blind-infinite-loop]
    * **깊게 중첩된 ``not``** — :func:`static_truth` 는 여전히 재귀 폴드라
      ``not not … not x`` 가 ~1000 겹을 넘으면 ``RecursionError`` 다(실측:
      깊이 2000 에서 ``probe_calls`` 는 죽고 ``ast.walk`` 는 4009 노드로 멀쩡하다.
      ``x and x and …`` 는 **평평한 BoolOp 한 개**라 2000 개도 안 죽는다).
      :func:`live_nodes` 자체는 이 판에서 스택 판으로 바꿔 깊이 5000 짜리 이항 연산
      체인까지 견디지만, 그 아래 폴드는 안 고쳤다 — 오늘 레포에 그런 형태가 없고
      (전수 실측 0건) 고치면 폴드가 후위 순회 재작성이 되기 때문이다.
      ⛔ 이건 "미탐" 이 아니라 **예외**다: 미탐은 조용히 통과시키지만 이건 터진다
      (= 검사 실패로 보인다). 그래서 오탐/미탐 목록과 성격이 다르다.
    * 일반적인 도달가능성은 **결정 불가능**하다(정지문제). 여기는 리터럴 산술까지다.

    ⇒ 그래서 1층은 마지막 방어선이 아니다. 실제 행에 키가 붙는지는 **행위 표면**
    (``tests/v8_1/test_ledger_join_writers_2026_08_08.py``)이 원장을 실제로 써서 잰다.

    ## ⚠️ 이 레포에 있는 **사촌 구현**(2026-08-09 3차 적대검증이 지적)

    ``_live_nodes`` 라는 이름의 도달가능성 술어가 넷 더 있다 —
    ``runtime/trap_forensics.py`` · ``goal_cards/graders/swallow_census.py`` ·
    ``goal_cards/drafts/graders/silent_except_census.py`` ·
    ``tests/v8_1/test_fail_fast_and_preflight_2026_07_25.py``. 그쪽은
    ``tests/v8_1/test_census_predicate_copies_agree_2026_08_02.py`` 가 공통 코퍼스로
    동치를 고정하는 **한 계보**이고, 여기는 그 계보가 아니다(정책이 다르다: 그쪽은
    ``For`` 의 body 를 아예 안 걷는다 — 침묵 삼킴을 세는 게 목적이라 루프 안을 볼
    이유가 없다). ⛔ **합치지 마라, 그러나 갈라지는 걸 모른 채로 두지도 마라** —
    같은 질문을 다르게 답하는 술어가 다섯 벌이면 언젠가 하나가 조용히 썩는다.
    합칠 거면 두 정책의 차이를 먼저 코퍼스로 고정해야 한다(이 판에서 안 했다).
    """
    # ⛔ **재귀로 쓰지 마라**(2026-08-09 4차 적대검증). 재귀 제너레이터판은 깊이
    #    1500 짜리 이항 연산 체인(`1+1+…`)에서 `RecursionError` 를 냈고 — 같은 입력을
    #    `ast.walk` 는 4503 노드로 멀쩡히 걷는다. 즉 HEAD 가 처리하던 입력에서
    #    탐침만 죽었다(= 새 예외). 오늘 레포에는 그런 깊이가 없지만(전수 실측
    #    36,877 심볼, RecursionError 0건) '지금 코퍼스에 없다' 는 계약이 아니다.
    #    아래는 같은 **선입 깊이우선** 순서를 스택으로 명시한 판이다 — 자식을 역순으로
    #    쌓아야 재귀판과 방문 순서가 바이트로 같다.
    stack: list[ast.AST] = [node]
    while stack:
        current = stack.pop()
        yield current
        children = list(_live_children(current))
        children.reverse()
        stack.extend(children)


def probe_calls(node: ast.AST, name: str) -> int:
    """몸통 안의 **도달 가능한** ``name(...)`` 호출 노드 수. 문자열 언급은 안 센다."""
    hits = 0
    for sub in live_nodes(node):
        if not isinstance(sub, ast.Call):
            continue
        func = sub.func
        if isinstance(func, ast.Name) and func.id == name:
            hits += 1
        elif isinstance(func, ast.Attribute) and func.attr == name:
            hits += 1
    return hits


def probe_sets_key(node: ast.AST, key: str = "cycle_id") -> int:
    """몸통이 ``key`` 를 **세우는** 자리 수 (dict 리터럴 키 / 첨자 대입), 도달 가능한 것만.

    ⛔ 읽기(``row.get("cycle_id")``)는 안 센다 — 읽는 코드가 있다고 쓰는 게 아니다.
    """
    hits = 0
    for sub in live_nodes(node):
        if isinstance(sub, ast.Dict):
            hits += sum(1 for k in sub.keys
                        if isinstance(k, ast.Constant) and k.value == key)
        elif isinstance(sub, (ast.Assign, ast.AnnAssign)):
            targets = sub.targets if isinstance(sub, ast.Assign) else [sub.target]
            for tgt in targets:
                if (isinstance(tgt, ast.Subscript)
                        and isinstance(tgt.slice, ast.Constant)
                        and tgt.slice.value == key):
                    hits += 1
    return hits


#: :func:`doc_claim_defects` 가 독스트링에서 읽는 유일한 형식. 산문은 안 읽는다 —
#: 산문을 입력으로 삼으면 문서가 굳는다(3라운드가 거절했고 R5 도 유지한다).
#: 기계와 문서의 접점은 **마커 한 조각**뿐이다.
_PROBE_CLAIM_RE: Final[re.Pattern[str]] = re.compile(r"\[probe:([a-z0-9-]+)\]")


def doc_claim_defects(doc: Any, tested: Any) -> list[str]:
    """독스트링 ↔ 탐침 테스트의 **양방향** 대조. fail-closed.

    ``doc`` 은 :func:`live_nodes` 의 독스트링(마커 ``[probe:<slug>]`` 포함),
    ``tested`` 는 *테스트가 실제로 고정하는 성질*의 슬러그 집합 — 소비자
    (``tests/v8_1/test_ledger_join_ratchet_2026_08_08.py``)가 corpus 표의 **키에서
    유도**해 넘긴다(손으로 나란히 적은 두 번째 목록이면 이 검사는 장식이다).

    잡는 것, 이름 붙여서:

    * **과대주장** — 독스트링에 ``[probe:x]`` 가 있는데 그 성질을 고정하는 corpus
      가 없다(``x ∉ tested``). 문서가 코드보다 강하다 — 다음 사람이 없는 방어를
      믿는다(2026-08-09 2차: "for 문이 목록에 아예 없었다"가 정확히 이 부류의 역상).
    * **과소서술** — 테스트가 고정하는 성질(``x ∈ tested``)이 독스트링에 마커로
      없다. 코드가 문서보다 강하다 — 문서만 읽은 사람이 그 성질을 모른 채 지우고,
      테스트가 왜 빨간지 아무도 모른다(TODO §L2 R4 잔여 2 가 적은 방향).

    ## ⛔ 안 잡는 것 — 잡는다고 주장하지 않는다

    * **마커 없는 산문 문장.** monkeypatch·정지문제·RecursionError(미탐이 아니라
      예외인 항목)처럼 ``probe==0/1`` 로 잴 수 없는 주장은 마커 없이 산문으로
      남는다 — 그 문장들의 참/거짓은 여전히 사람 몫이다.
    * **corpus 가 주장대로 거동하는지**(dead ⇒ probe 0 / blind ⇒ probe 1)는 여기가
      아니라 그 corpus 를 먹는 테스트가 잰다 — 여기는 *이름의 대응*만 본다.
    * **alive corpus**(과잉 가지치기 가드)는 대조 대상이 아니다 — 그건 "잡는다"
      주장의 반례 방지지, 문서에 실릴 새 주장이 아니다.
    """
    if not isinstance(doc, str) or not doc.strip():
        # 독스트링이 없으면 "문서와 어긋난 게 없다"가 아니라 **대조 불가**다.
        return ["독스트링이 없다/비었다 — 마커를 대조할 문서 자체가 없다(fail-closed)"]
    if not isinstance(tested, (frozenset, set)):
        return [f"tested 의 모양이 집합이 아니다 → {type(tested).__name__} — "
                f"모양이 무너진 입력 위의 초록은 초록이 아니다(fail-closed)"]
    if not tested or not all(isinstance(s, str) for s in tested):
        return ["tested 가 비었다/문자열이 아니다 — 고정하는 성질이 하나도 없다면 "
                "이 대조는 공허하다(fail-closed)"]
    claimed = set(_PROBE_CLAIM_RE.findall(doc))
    out: list[str] = []
    for slug in sorted(claimed - tested):
        out.append(
            f"과대주장: 독스트링이 [probe:{slug}] 를 주장하는데 그 성질을 고정하는 "
            f"테스트 corpus 가 없다 — 마커를 지우거나 corpus 에 {slug!r} 를 더해라. "
            f"문서가 코드보다 강하면 다음 사람이 없는 방어를 믿는다")
    for slug in sorted(tested - claimed):
        out.append(
            f"과소서술: 테스트가 {slug!r} 성질을 고정하는데 독스트링에 "
            f"[probe:{slug}] 마커가 없다 — 해당 문장에 마커를 달아라. 코드가 "
            f"문서보다 강하면 문서만 읽은 사람이 그 성질을 모른 채 지운다")
    return out


def symbol_node(rel: str, dotted: str, *, repo: Path | None = None) -> ast.AST | None:
    """``파일.py:Class.method`` 의 심볼 노드. 줄번호·순서·인접 코드는 안 본다."""
    root = _REPO if repo is None else Path(repo)
    node: ast.AST = ast.parse((root / rel).read_text(encoding="utf-8"), filename=rel)
    for part in dotted.split("."):
        found = None
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.ClassDef)) and child.name == part:
                found = child
                break
        if found is None:
            return None
        node = found
    return node


# ---------------------------------------------------------------------------
# 1층 탐침 — 신고표 위생
# ---------------------------------------------------------------------------

def declaration_defects(cycle_writers: Mapping[str, Mapping[str, Any]],
                        not_cycle_scoped: Mapping[str, Any],
                        partitioned: Mapping[str, Any],
                        *, repo: Path | None = None) -> list[str]:
    """두 표의 계약 위반 목록. **합성 표에도 같은 술어를 먹여** 비공허를 증명한다.

    (실제 표에 대해 빈 리스트가 나오는 것만으로는 술어가 살아 있다는 증거가 못 된다.)

    🔒 ``writer=None`` 은 여기서 **계량된다** — :data:`MAX_UNDECLARED_WRITERS` 를
    넘으면 적색이고, 남은 면제도 ``why`` 형식검사를 통과해야 한다.
    """
    root = _REPO if repo is None else Path(repo)
    out: list[str] = []
    overlap = set(cycle_writers) & set(not_cycle_scoped)
    if overlap:
        out.append(f"두 표가 겹친다(배타여야 한다): {sorted(overlap)}")
    undeclared: list[str] = []
    for fam, spec in cycle_writers.items():
        if family(fam) != fam:
            out.append(f"{fam}: 정규화된 패밀리가 아니다 → {family(fam)}")
        if not isinstance(spec, Mapping):
            # ⛔ malformed 는 통과가 아니라 **거부**다. 예외로 터뜨리면 진단이
            #    "탐침이 깨졌다" 로 읽히지만, 이건 신고표가 틀린 것이다.
            out.append(f"{fam}: 신고 항목이 매핑이 아니다 → {type(spec).__name__} "
                       f"— 모양이 무너진 신고는 검사 대상에서 빠지므로 거부한다")
            continue
        writer, mech = spec.get("writer"), spec.get("mechanism")
        why = spec.get("why")
        # 🔒 stamper 앵커의 실재 검사 — 선언만 있고 심볼이 없으면(옮김/개명) 그
        # 원장의 계약 필드가 침묵으로 빈다. 앵커는 fail-closed 로 여기서 적색.
        stampers = spec.get("stampers", ())
        # 🔒 **바닥 먼저.** 실재검사는 신고된 것만 볼 수 있어서, 신고를 지우면
        #    검사가 통째로 사라진다(2026-08-10 실측: 전 패밀리 stampers 를 비워도
        #    274 초록). 그래서 지우기를 개수로 계량한다 — 이 표에 없는 패밀리는
        #    영향이 없으므로 합성 표(``{"a.jsonl": ...}``)의 술어는 그대로다.
        #
        # 🔴 **개수는 서로 다른 앵커의 개수다** (2026-08-10 오후 R2 적대검증).
        #    바닥을 처음 지었을 때는 ``len(stampers)`` 였고, 그건 **복제를 못 봤다**:
        #    살아남은 앵커 하나를 3벌로 적으면(``(a, a, a)``) 진짜 앵커 2개 삭제가
        #    완전 침묵이었다 — 실측으로 소유 스위트 235 전부 초록, `ledger_join or
        #    stamp` 277 도 초록, 그리고 `contracts_map` 산출물에서는 그 두 앵커가
        #    유도하던 계약 행이 **실제로 사라졌다**(51074B vs 50867B).
        #    ⇒ 이 래칫이 닫는다고 선언한 바로 그 구멍(지우기=침묵)이 그 자리에서
        #      열려 있었다. 복제는 별도로도 적색이다(아래) — 같은 심볼을 두 번 적어
        #      봐야 유도에 아무것도 안 보태므로, 바닥이 없는 패밀리에서도 위반이다.
        if isinstance(stampers, (list, tuple)):
            seen: set[str] = set()
            dupes: list[str] = []
            for s in stampers:
                if not isinstance(s, str):
                    continue
                if s in seen and s not in dupes:
                    dupes.append(s)
                seen.add(s)
            if dupes:
                out.append(
                    f"{fam}: stamper 앵커가 중복이다 → {sorted(dupes)}. 같은 심볼을 "
                    f"두 번 적으면 contracts_map 유도는 하나도 안 늘고 **개수 바닥만** "
                    f"늘어난다 ⇒ 복제 한 줄로 진짜 앵커 삭제를 가릴 수 있다"
                    f"(2026-08-10 실측: `(a, a, a)` 로 235 전부 초록이었다)")
        distinct_stampers = {s for s in stampers if isinstance(s, str)} \
            if isinstance(stampers, (list, tuple)) else set()
        floor = REQUIRED_STAMPERS.get(fam)
        if floor is not None:
            n = len(distinct_stampers)
            if n < floor:
                out.append(
                    f"{fam}: stamper 앵커가 {n}개(서로 다른 것) < 바닥 {floor} — "
                    f"신고를 지우는 것은 "
                    f"낡은 앵커와 달리 **완전 침묵**이다(실재검사는 신고된 것만 본다). "
                    f"그 원장의 계약 필드가 contracts_map 유도에서 조용히 빠진다. "
                    f"앵커가 정말 사라졌다면 REQUIRED_STAMPERS 를 실측과 같은 값으로 "
                    f"내려라 — 그건 리뷰를 받는 한 줄이다")
        if not isinstance(stampers, (list, tuple)):
            out.append(f"{fam}: stampers 가 목록이 아니다 → "
                       f"{type(stampers).__name__}")
        else:
            for s in stampers:
                if not isinstance(s, str) or ":" not in s:
                    out.append(f"{fam}: stamper 형식이 '파일.py:심볼' 이 아니다"
                               f" → {s!r}")
                    continue
                s_rel, s_dotted = s.split(":", 1)
                if not (root / s_rel).is_file():
                    out.append(f"{fam}: stamper 파일이 없다 → {s_rel}")
                    continue
                try:
                    s_node = symbol_node(s_rel, s_dotted, repo=root)
                except (OSError, SyntaxError) as _ff_exc:
                    _swallowed(_ff_exc,
                               site="tools.ledger_join_check."
                                    "declaration_defects:stamper",
                               category="verify")
                    s_node = None
                if s_node is None:
                    out.append(f"{fam}: 낡은 stamper 앵커 — {s} 심볼이 없다"
                               f"(옮겼거나 이름이 바뀌었다)")
        if writer is None:
            undeclared.append(fam)
            out.extend(_undeclared_why_defects(fam, why, root))
            if mech is not None:
                out.append(f"{fam}: writer 가 없는데 mechanism={mech!r} 을 주장한다")
            continue
        if not isinstance(writer, str) or ":" not in writer:
            out.append(f"{fam}: writer 형식이 '파일.py:심볼' 이 아니다 → {writer!r}")
            continue
        if mech not in MECHANISMS:
            out.append(f"{fam}: mechanism={mech!r} 은 허용 어휘 "
                       f"{sorted(MECHANISMS)} 밖이다")
    if len(undeclared) > MAX_UNDECLARED_WRITERS:
        # 🔑 면제는 **개수가 동결값**이다. 신고표 한 줄로 탐침을 끄는 길을 막는 건
        #    "면제 금지"가 아니라 "면제가 눈에 보이게 세어지는 것"이다.
        out.append(
            f"writer=None 면제 {len(undeclared)}건 > 상한 {MAX_UNDECLARED_WRITERS} "
            f"— {sorted(undeclared)}. 면제된 원장은 1층 탐침이 통째로 사라진다"
            f"(그 mechanism 의 parametrize 에서 빠질 뿐 실패는 안 난다). "
            f"정말 생산자를 못 찾았다면 MAX_UNDECLARED_WRITERS 를 **실측과 같은 값으로** "
            f"올려라(여유분을 두면 test_exemption_cap_matches_the_live_table 이 적색이다) "
            f"— ⛔ 그건 조용한 면제가 아니라 리뷰를 받는 한 줄이고, 올려도 각 면제는 "
            f"why 형식검사와 exemption_defects 의 동결증거 대조를 따로 통과해야 한다")
    for fam, why in not_cycle_scoped.items():
        if family(fam) != fam:
            out.append(f"{fam}: 정규화된 패밀리가 아니다 → {family(fam)}")
        if not isinstance(why, str) or len(why) < 20:
            out.append(f"{fam}: 제외 사유가 비었다/짧다 — 빈 사유로 분모에서 빼는 건 "
                       f"지표를 예쁘게 만드는 것이다")
    if len(not_cycle_scoped) > MAX_NOT_CYCLE_SCOPED:
        # 🔑 ``writer=None`` 보다 **큰** 면제라 같은 규율을 건다. 이 줄이 없던 판에서
        #    적색인 원장을 사이클 밖으로 옮기는 것만으로 검사가 사라졌다.
        out.append(
            f"'사이클 밖' 면제 {len(not_cycle_scoped)}건 > 상한 {MAX_NOT_CYCLE_SCOPED} "
            f"— {sorted(not_cycle_scoped)}. 재분류는 writer=None 보다 **큰 면제**다: "
            f"1층 탐침이 사라지는 데 더해 그 원장이 **분모에서도 빠진다**. "
            f"정말 사이클 밖이면 MAX_NOT_CYCLE_SCOPED 를 실측과 같은 값으로 올려라 — "
            f"scope_defects 의 동결증거 대조가 각 건마다 따로 걸린다")
    for prefix, why in partitioned.items():
        if "/" in prefix:
            out.append(f"{prefix}: 분할 선언은 디렉터리 이름 한 겹이어야 한다")
        if not isinstance(why, str) or len(why) < 20:
            out.append(f"{prefix}: 분할 사유가 비었다/짧다")
    if len(partitioned) > MAX_PARTITIONED:
        out.append(
            f"분할 선언 {len(partitioned)}건 > 상한 {MAX_PARTITIONED} "
            f"— {sorted(partitioned)}. 디렉터리를 하나 더하면 그 아래 원장들이 "
            f"**한 패밀리로 삼켜져** 두 판정이 하나로 접힌다. 정말 분할 원장이면 "
            f"MAX_PARTITIONED 를 실측과 같은 값으로 올려라")
    return out


def _undeclared_why_defects(fam: str, why: Any, root: Path) -> list[str]:
    """``writer=None`` 의 ``why`` **형식검사**. 길이만 보면 아무 문장이나 통과한다.

    요구: *"어디를 뒤졌는지"* 를 적어라 — ``why`` 안에 **레포에 실재하는** ``.py``
    경로가 최소 하나 있어야 한다. 그래야 다음 사람이 같은 자리를 다시 볼 수 있고,
    *"못 찾았다"* 가 반증 가능한 문장이 된다.
    """
    if not isinstance(why, str) or len(why) < 20:
        return [f"{fam}: writer=None 인데 why 가 없다/짧다 "
                f"— '못 찾았다'와 '없다'는 다른 사실이다"]
    if not root.is_dir():
        # fail-closed: 검사를 못 하면 통과가 아니다.
        return [f"{fam}: why 형식검사를 못 했다 — repo 루트 부재({root})"]
    named = [tok for tok in _PY_PATH_RE.findall(why) if (root / tok).is_file()]
    if not named:
        return [f"{fam}: writer=None 의 why 가 **뒤진 자리**를 안 적었다 — "
                f"레포에 실재하는 .py 경로를 최소 하나 적어라"
                f"(예: 'enforcement/executor_log.py 를 봤는데 조인 기제가 없다'). "
                f"길이만 채운 사유는 반증이 불가능하고, 반증 불가능한 면제는 면제가 "
                f"아니라 구멍이다"]
    return []


def exemption_defects(cycle_writers: Mapping[str, Mapping[str, Any]],
                      frozen_payload: Mapping[str, Any]) -> list[str]:
    """``writer=None`` 면제를 **동결된 증거**와 대조한다. fail-closed.

    🔑 *"생산자를 못 찾았다"* 는 원장 자신이 반증할 수 있는 문장이다: 그 원장에
    ``cycle_id`` 를 가진 행이 **한 번이라도** 있었다면(``ever_joined``) 무언가가
    그 키를 찍고 있다는 뜻이고, 그러면 "못 찾았다"는 사실이 아니라 게으름이다.

    증거는 :data:`DEFAULT_BASELINE` 의 payload — 커밋된 파일이고 ``payload_sha256``
    으로 덮여 있어 손편집이 :class:`BaselineTampered` 로 잡힌다. ⛔ 라이브 젤을
    읽지 않는다(클린 클론에서 영구 초록이 되는 길).
    """
    out: list[str] = []
    if not isinstance(frozen_payload, Mapping) or not frozen_payload:
        # ⛔ 면제가 **0건이어도** 여기서 거부한다. 안 그러면 baseline 이 비거나
        #    못 읽히는 날 이 검사가 공허하게 초록이 된다 = 고장난 탐지기 위의 초록.
        return ["동결 payload 가 비었다/모양이 아니다 — **대조할 증거 자체가 없다**. "
                "증거 없이 통과시키지 않는다(fail-closed)"]
    for fam, spec in cycle_writers.items():
        if not isinstance(spec, Mapping):
            # 모양을 모르면 면제인지 아닌지도 모른다 ⇒ 거부(fail-closed).
            out.append(f"{fam}: 신고 항목이 매핑이 아니다 → {type(spec).__name__} "
                       f"— 면제 여부를 판정할 수 없다")
            continue
        if spec.get("writer") is not None:
            continue
        frozen = frozen_payload.get(fam)
        if not isinstance(frozen, Mapping):
            out.append(f"{fam}: writer=None 인데 동결본에 이 패밀리가 없다 — "
                       f"이 면제를 대조할 항목이 없다")
            continue
        if frozen.get("ever_joined") is True:
            out.append(
                f"{fam}: writer=None('생산자를 못 찾았다')인데 동결본은 "
                f"ever_joined=true 라 말한다 — 이 원장에는 cycle_id 를 가진 행이 "
                f"**이미 있다**. 행을 찍는 무언가가 존재하므로 그 면제는 증거에 "
                f"반증됐다. 생산자를 찾아 적어라")
    return out


def scope_defects(not_cycle_scoped: Mapping[str, Any],
                  partitioned: Mapping[str, Any],
                  frozen_payload: Mapping[str, Any]) -> list[str]:
    """**재분류·분할**을 동결된 증거와 대조한다. fail-closed.

    :func:`exemption_defects` 가 ``writer=None`` 에 대고 묻는 것과 같은 질문을,
    더 큰 두 면제에 대고 묻는다.

    1. *"이 원장은 사이클 밖이다"* 는 동결본이 반증할 수 있다 — 동결본이 그
       패밀리를 ``cycle_scoped=true`` 로 적어뒀다면, 그건 **어제까지 분모 안에
       있던 원장을 오늘 분모 밖으로 옮기는 것**이다(래칫이 썩는 가장 흔한 길).
    2. *"이 디렉터리는 한 원장의 분할이다"* 도 동결본이 반증한다 — 새 분할 선언이
       기존 패밀리를 삼키면 ``family(f) != f`` 가 되고, 그 순간 두 원장의 판정이
       하나로 접힌다.

    ⛔ 라이브 젤을 읽지 않는다. 증거는 :data:`DEFAULT_BASELINE` 의 payload 이고,
    **소비 표면이 ``payload_sha256`` 을 검증하고 읽어야** 이 대조가 의미를 갖는다
    (2026-08-09 적대검증: 생으로 읽던 동안 baseline 손편집 한 줄이 이 문을 열었다).
    """
    out: list[str] = []
    if not isinstance(frozen_payload, Mapping) or not frozen_payload:
        return ["동결 payload 가 비었다/모양이 아니다 — **대조할 증거 자체가 없다**. "
                "증거 없이 재분류를 통과시키지 않는다(fail-closed)"]
    for fam in not_cycle_scoped:
        frozen = frozen_payload.get(fam)
        if not isinstance(frozen, Mapping):
            out.append(f"{fam}: '사이클 밖' 이라 신고했는데 동결본에 이 패밀리가 없다 "
                       f"— 이 재분류를 대조할 항목이 없다. 먼저 "
                       f"`--ratchet --update` 로 동결한 뒤 재분류하라(fail-closed)")
            continue
        if frozen.get("cycle_scoped") is True:
            out.append(
                f"{fam}: '사이클 밖' 이라 신고했는데 동결본은 cycle_scoped=true 라 "
                f"말한다 — 어제까지 **분모 안**이던 원장을 오늘 분모 밖으로 옮기는 "
                f"것이다. 적색을 지우는 게 아니라 적색을 숨기는 경로이므로 거부한다")
    for fam in frozen_payload:
        if family(fam, partitioned) != fam:
            out.append(
                f"{fam}: 동결된 패밀리인데 지금 표로는 "
                f"{family(fam, partitioned)!r} 로 정규화된다 "
                f"— PARTITIONED 선언이 이미 동결된 원장을 삼켰다. 두 원장의 판정이 "
                f"하나로 접히면 한쪽 적색이 다른 쪽 초록에 가려진다")
    return out


def rescope_allowlist_defects(allowlist: Any,
                              not_cycle_scoped: Mapping[str, Any],
                              frozen_payload: Mapping[str, Any]) -> list[str]:
    """:data:`RESCOPE_ALLOWLIST` 가 **미결 거래**인지 검사한다. fail-closed.

    허가는 *"지금 이 원장을 분모 밖으로 옮기는 중"* 이라는 영수증이지 서 있는
    면제가 아니다. 그래서 세 가지를 요구한다:

    1. 그 패밀리가 **지금** :data:`NOT_CYCLE_SCOPED` 에 있어야 한다(안 옮기면서
       허가만 들고 있는 것 = 다음 사람이 쓸 수 있는 백지수표).
    2. 동결본에 그 패밀리가 있어야 한다(없으면 강등할 대상이 없다).
    3. 동결본이 아직 ``cycle_scoped=true`` 여야 한다 — 이미 분모 밖이면 그 허가는
       **소진됐다**. 소진된 줄을 남기면 다음 재분류가 그 줄을 타고 지나간다.

    ⛔ 증거가 없으면 거부다. 허가가 0건이어도 거부다 — 안 그러면 baseline 이 비는
    날 이 검사가 공허하게 초록이 된다(:func:`exemption_defects` 와 같은 규율).
    """
    out: list[str] = []
    if not isinstance(frozen_payload, Mapping) or not frozen_payload:
        return ["동결 payload 가 비었다/모양이 아니다 — **재분류 허가를 대조할 증거가 "
                "없다**. 증거 없이 통과시키지 않는다(fail-closed)"]
    if not isinstance(allowlist, (frozenset, set, tuple, list)):
        return [f"RESCOPE_ALLOWLIST 의 모양이 집합이 아니다 → "
                f"{type(allowlist).__name__} — 모양이 무너진 허가는 거부한다"]
    # ⛔ `key=repr` — 원소가 문자열이 아닌 날 `sorted` 가 TypeError 로 터지면
    #    "결함 목록을 돌려주는" 계약이 예외로 바뀐다(모양 붕괴는 defect 로 말한다).
    for fam in sorted(allowlist, key=repr):
        if fam not in not_cycle_scoped:
            out.append(f"{fam}: 재분류 허가가 있는데 NOT_CYCLE_SCOPED 에 없다 — "
                       f"허가는 **지금 옮기는** 원장에만 붙는다. 안 옮길 거면 지워라")
        frozen = frozen_payload.get(fam)
        if not isinstance(frozen, Mapping):
            out.append(f"{fam}: 재분류 허가가 있는데 동결본에 이 패밀리가 없다 — "
                       f"강등할 대상 자체가 없다(fail-closed)")
        elif frozen.get("cycle_scoped") is not True:
            out.append(
                f"{fam}: 재분류 허가가 **이미 소진됐다** — 동결본이 이미 분모 밖이라 "
                f"말한다. 이 줄을 지워라. 남겨두면 다음 재분류가 이 줄을 타고 "
                f"조용히 지나간다(허가는 영수증이지 서 있는 면제가 아니다)")
    return out


def rescope_refusals(frozen_payload: Mapping[str, Any],
                     payload: Mapping[str, Any],
                     allowlist: Any) -> list[str]:
    """**동결 경로**가 거부할 강등 목록 — *분모 안 → 분모 밖* 재동결.

    🔑 이 래칫이 *쓰는 쪽*에 있는 이유(2026-08-09 3차 적대검증 실측): 읽는 쪽의
    방어는 전부 재동결로 지워진다. `scope_defects` 는 증거 파일이 새로 쓰이면
    대조할 게 없고, 2층의 라이브 행 반증은 ``cycle_id`` 행이 0개인 원장에는
    **원리상 발화 못 한다** — 그런데 숨기려는 원장이 정확히 그 부류다.

    판정은 :func:`compare` + :func:`_classify` 로 한다(악화의 정의는 한 곳에만
    산다). 그중 **강등만** 거부한다 — ``ever_joined`` 하락은 알려진 예외
    (:mod:`tools.executor_log_quarantine`)가 있어 동결이 정당할 수 있다.

    ## ⛔ 안 잡는 것 — 크레딧을 정확히 붙인다 (2026-08-09 4차 실측)

    * **패밀리 키가 바뀌는 강등**. 비교는 ``frozen ∩ live`` 교집합에서만 하므로,
      :data:`PARTITIONED` 에 디렉터리를 하나 더해 희생양을 **새 이름의 패밀리**로
      접으면 여기서 거부는 **0건**이다(실측: ``bus`` 를 분할로 선언해 세 원장을
      ``bus/*.jsonl`` 하나로 접자 refusals=[]). ⇒ 이 함수가 막는 것은
      **키가 보존되는 강등**뿐이라고 읽어라.
      🔑 그 공격이 실제로 막히는 것은 맞지만 **여기 크레딧이 아니다**: 옛 키 셋이
      ``carried_forward`` 로 동결본에 남고(:func:`ratchet_update`), 그 상태를
      :func:`scope_defects` 가 계속 적색으로 만든다(``family(f) != f`` = 삼킴 +
      *분모 안* 강등). 방어 셋이 한 줄로 이어져 있다는 사실 자체가 취약점이다 —
      셋 중 하나만 지워도 이 조합은 조용히 끊긴다.
    * **payload 의 *키* 가 문자열이 아닌 모양 붕괴.** 값 쪽 붕괴는 defect 로 말하지만
      (fail-closed), 키 쪽은 한 층 더 깊은 :func:`compare` 의 정렬에서 ``TypeError``
      로 나간다(:func:`scope_defects` 는 ``family()`` 의 ``rel.replace`` 에서
      ``AttributeError``). ⚠️ **오늘 도달 불가**다 — JSON 객체 키는 항상 문자열이고
      :func:`ratchet_measure` 의 키도 문자열이라, 이 경로는 직접 만든 dict 로만
      닿는다. 그래서 고치지 않고 **범위 결함으로 적어둔다**: 3라운드가
      ``sorted(allowlist, key=repr)`` 한 곳만 고치고 "모양 붕괴는 예외가 아니라
      defect 로 말한다" 를 전칭으로 적었는데, 그 문장은 이 층에서 거짓이다.
    """
    if not isinstance(frozen_payload, Mapping) or not isinstance(payload, Mapping):
        return ["동결본/실측 payload 의 모양이 무너졌다 — 재동결을 거부한다"]
    common = {k: v for k, v in frozen_payload.items() if k in payload}
    allowed = allowlist if isinstance(allowlist, (frozenset, set, tuple, list)) else ()
    out: list[str] = []
    for fam in compare(common, payload, classify=_classify).worse:
        # ⛔ `diff.worse` 에는 **신규 원장**(동결본에 없음)도 들어온다 — 그건
        #    강등이 아니라 `--ratchet` 이 따로 말하는 '신고 없는 신규' 다.
        old, new = common.get(fam), payload.get(fam)
        if not (isinstance(old, Mapping) and old.get("cycle_scoped") is True):
            continue                      # 강등이 아니다(다른 종류의 악화)
        if isinstance(new, Mapping) and new.get("cycle_scoped") is True:
            continue
        if fam in allowed:
            continue                      # 영수증이 있다 — 이번 한 번 동결한다
        out.append(
            f"{fam}: 동결본은 이 원장을 **분모 안**(cycle_scoped=true)이라 말하는데 "
            f"지금 표는 분모 밖으로 옮겼다. 재동결은 그 강등을 증거에서 지우는 "
            f"편집이므로 거부한다.\n"
            f"  처방: 정말 사이클 밖이면 `RESCOPE_ALLOWLIST` 에 {fam!r} 를 적고 "
            f"(리뷰받는 한 줄이다) 다시 동결한 뒤 **그 줄을 지워라** — 소진된 허가를 "
            f"남기면 rescope_allowlist_defects 가 적색이다.\n"
            f"  ⛔ **정직한 재동결로는** 못 지운다 — 재동결 자체를 막는다. 다만 "
            f"동결본을 지우거나 딴 경로에 동결해 덮어쓰면 지워진다(RESCOPE_ALLOWLIST "
            f"의 '안 잡는 것' 참조). 그 두 경로에 남는 것은 기계가 아니라 "
            f"**커밋 diff 를 보는 사람**이다.")
    return out


# __SLOT_ITER_ROWS_SKIP_COUNT_2026_08_16__ 🔴 08-09 밤 잔여 #5 — 종전 독스트링
# "깨진 행은 세되 파싱은 포기"는 **거짓**이었다: 코드는 `continue`로 조용히
# 버릴 뿐 아무것도 세지 않았고, non-dict 행(리스트/스칼라 JSON)은 카운트는커녕
# 언급도 안 됐다. 라이브 실측(오늘 젤)은 깨진 행 0건이라 실해는 없었지만,
# 무신호 증발은 그 자체로 결함이다([[feedback_negative_evidence_policy]]).
# ⇒ 세는 쪽으로 고친다: 경로별 스킵 카운트를 모듈 전역에 기록하고
# :func:`skipped_rows_for`로 읽는다. 계약 표면이 아니다 — `_iter_rows`의
# 반환 타입(list[dict])과 정상 행 순서는 그대로이므로 기존 5개 호출부는
# 무변화. 카운터는 순수 부가 관측치라 default-OFF 게이트 불필요(버그수리).
_SKIPPED_ROWS: dict[str, int] = {}


def skipped_rows_for(path: Path) -> int:
    """마지막 :func:`_iter_rows` 호출에서 그 경로가 버린 행 수(JSON 파싱 실패 +
    non-dict 행 합계). 호출 전이면 0(=아직 안 잼, 0건 스킵과 구분 안 됨 —
    소비자는 먼저 ``_iter_rows(path)``를 부른 뒤 이 함수를 써야 한다)."""
    return _SKIPPED_ROWS.get(str(path), 0)


def _iter_rows(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    skipped = 0
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        _SKIPPED_ROWS[str(path)] = skipped
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:  # noqa: BLE001 — 깨진 행은 세고 파싱은 포기
            skipped += 1
            continue
        if isinstance(obj, dict):
            out.append(obj)
        else:
            skipped += 1  # 유효 JSON 이지만 행 형태(dict)가 아니다 — 이것도 버림
    _SKIPPED_ROWS[str(path)] = skipped
    return out


def survey(jail: Path) -> dict[str, Any]:
    """원장별 ts/cycle 커버리지. 정렬은 이름순(결정론적)."""
    ledgers: list[dict[str, Any]] = []
    for path in sorted(jail.rglob("*.jsonl")):
        rows = _iter_rows(path)
        n = len(rows)
        n_ts = sum(1 for r in rows if row_ts(r) is not None)
        n_cy = sum(1 for r in rows if row_cycle(r) is not None)
        rel = str(path.relative_to(jail))
        ledgers.append({
            "ledger": rel,
            "rows": n,
            # __SLOT_ITER_ROWS_SKIP_COUNT_2026_08_16__ 깨진/non-dict 행 수
            # (0 이면 "안 셌다"가 아니라 "세어봤더니 0건"이다).
            "skipped_rows": skipped_rows_for(path),
            "ts_resolved": n_ts,
            "cycle_resolved": n_cy,
            # 사이클에 안 속하는 원장은 **미달이 아니다**. 이유를 실어 보낸다.
            "not_cycle_scoped": NOT_CYCLE_SCOPED.get(rel),
            # ⛔ 행이 0 개면 비율은 **모름**이지 100% 가 아니다. 08-07 에 캡이
            #    "행 0 개 ⇒ 완전한 $0" 이라 말하던 것과 같은 함정.
            "ts_pct": (round(100.0 * n_ts / n, 1) if n else None),
            "cycle_pct": (round(100.0 * n_cy / n, 1) if n else None),
        })
    tot = sum(l["rows"] for l in ledgers)
    # 🔑 분모에서 **사이클에 안 속하는 원장을 뺀다.** 안 그러면 영원히 100% 가 안 되고,
    # "남은 건 못 한 일"인지 "남은 건 안 할 일"인지 지표가 말을 못 한다
    # ([[feedback_metric_denominator_2026_07_27]] — 파생비율 단정 전 분모 정의).
    scoped = [l for l in ledgers if not l["not_cycle_scoped"]]
    return {
        "jail": str(jail),
        "ledger_files": len(ledgers),
        "rows_total": tot,
        "rows_ts_resolved": sum(l["ts_resolved"] for l in ledgers),
        "rows_cycle_resolved": sum(l["cycle_resolved"] for l in ledgers),
        "ledgers_with_any_cycle": sum(1 for l in ledgers if l["cycle_resolved"]),
        # 분모 둘을 **나란히** 낸다 — 어느 쪽을 인용하는지 소비자가 고르게.
        "ledgers_cycle_scoped": len(scoped),
        "ledgers_not_cycle_scoped": len(ledgers) - len(scoped),
        "scoped_rows_total": sum(l["rows"] for l in scoped),
        "scoped_rows_cycle_resolved": sum(l["cycle_resolved"] for l in scoped),
        "ledgers": ledgers,
    }


def join_cycle(jail: Path, cycle_id: str) -> dict[str, Any]:
    """한 사이클을 원장 전체에서 끌어모은다 — 슬롯 4 가 사려는 바로 그 능력."""
    hits: list[dict[str, Any]] = []
    for path in sorted(jail.rglob("*.jsonl")):
        rows = [r for r in _iter_rows(path) if row_cycle(r) == cycle_id]
        if not rows:
            continue
        stamps = [t for t in (row_ts(r) for r in rows) if t is not None]
        hits.append({
            "ledger": str(path.relative_to(jail)),
            "rows": len(rows),
            "first_ts": min(stamps) if stamps else None,
            "last_ts": max(stamps) if stamps else None,
            "ts_unknown": len(rows) - len(stamps),
        })
    spans = [h["first_ts"] for h in hits if h["first_ts"] is not None]
    ends = [h["last_ts"] for h in hits if h["last_ts"] is not None]
    return {
        "cycle_id": cycle_id,
        "ledgers_hit": len(hits),
        "rows_total": sum(h["rows"] for h in hits),
        "span_s": (round(max(ends) - min(spans), 3) if spans and ends else None),
        "hits": hits,
    }


def latest_cycle(jail: Path) -> str | None:
    """가장 최근에 시각이 찍힌 사이클. ⛔ 시각 모르는 행으로 고르지 않는다."""
    best: tuple[float, str] | None = None
    for path in jail.rglob("*.jsonl"):
        for r in _iter_rows(path):
            cid, t = row_cycle(r), row_ts(r)
            if cid and t is not None and (best is None or t > best[0]):
                best = (t, cid)
    return best[1] if best else None


# ---------------------------------------------------------------------------
# 2층 — 라이브 baseline 래칫
# ---------------------------------------------------------------------------

#: ``--ratchet`` 종료코드. 0/1 만이 "쟀다"이고 2/3 은 **안 쟀다**이다 — 이 둘을
#: 초록으로 접으면 고장난 탐지기 위에 래칫을 세우게 된다.
EXIT_OK: Final[int] = 0
EXIT_REGRESSION: Final[int] = 1
EXIT_NO_JAIL: Final[int] = 2
EXIT_CANNOT_COMPARE: Final[int] = 3


def scope_for(jail: Path) -> str:
    """baseline 의 ``scope``. 레포 안이면 **레포 상대경로**.

    ⛔ 절대경로를 쓰면 다른 머신·클린 클론에서 scope 불일치가 나고, 그건
    *"영원히 비교 거절되는 baseline"* 이다(= skip 되는 래칫 = 래칫 아님).
    """
    # ⛔ `try: relative_to / except ValueError` 로 쓰지 않는다. 그건 침묵 핸들러
    #    래칫이 삼킴 1건으로 세는 모양이고(`tools/` 는 이미 상한 초과 적색이다),
    #    같은 질문을 예외 없이 물을 수 있는 술어가 표준에 있다.
    resolved = Path(jail).resolve()
    if resolved.is_relative_to(_REPO):
        return resolved.relative_to(_REPO).as_posix()
    return resolved.as_posix()


def ratchet_measure(jail: Path, *, since: float | None = None) -> dict[str, Any]:
    """패밀리별 동결 지표. **비율 없음** — 단조 증가하는 불리언 하나뿐.

    돌려주는 값::

        {
          "payload": {family: {...}},           # ← baseline 과 비교되는 것
          "observed": {family: {관측치...}},     # ← meta/화면용, 비교 안 함
          "since_total_loss": [family, ...],    # --since 전용
        }

    ``ever_joined`` 는 *"이 원장이 조인축에 올라온 적 있나"* 다. 원장이
    append-only 라 **단조**이므로 래칫으로 쓸 수 있다. ⚠️ 알려진 예외:
    :mod:`tools.executor_log_quarantine` 는 원장을 **재작성**한다(행 제거) —
    ``ever_joined`` 가 참→거짓으로 떨어지면 코드 회귀 전에 그 도구를 먼저 의심하라.
    """
    rows_by_family: dict[str, list[dict[str, Any]]] = {}
    files_by_family: dict[str, list[str]] = {}
    for path in sorted(jail.rglob("*.jsonl")):
        rel = path.relative_to(jail).as_posix()
        fam = family(rel)
        rows_by_family.setdefault(fam, []).extend(_iter_rows(path))
        files_by_family.setdefault(fam, []).append(rel)

    payload: dict[str, Any] = {}
    observed: dict[str, Any] = {}
    since_total_loss: list[str] = []
    for fam in sorted(rows_by_family):
        rows = rows_by_family[fam]
        why = NOT_CYCLE_SCOPED.get(fam)
        n_cycle = sum(1 for r in rows if row_cycle(r) is not None)
        if why is not None:
            # 🔑 *"못 한 일"* 과 *"안 할 일"* 의 구분은 **baseline 안에** 보존한다.
            # 분모에서만 빼고 파일에서 지우면 다음 사람이 이유를 모른 채 다시 심는다.
            payload[fam] = {"cycle_scoped": False, "why": why}
        else:
            payload[fam] = {"cycle_scoped": True, "ever_joined": n_cycle > 0}
        observed[fam] = {
            "files": len(files_by_family[fam]),
            "rows": len(rows),
            "rows_with_cycle_id": n_cycle,
            # ⛔ 비율은 **여기까지**다. baseline 에는 안 들어간다.
            "cycle_pct": (round(100.0 * n_cycle / len(rows), 1) if rows else None),
        }
        if since is None or why is not None:
            continue
        # ⛔ 시각 모르는 행을 0 으로 접지 않는다. "동결 이후"임을 **증명 못 한**
        #    행은 신규에서 뺀다 — 이 검사는 신규가 있을 때만 적색이므로 빼는 쪽이
        #    fail-closed 다(못 잰 것을 근거로 적색을 만들지 않는다).
        fresh: list[dict[str, Any]] = []
        for row in rows:
            stamp = row_ts(row)
            if stamp is not None and stamp > since:
                fresh.append(row)
        observed[fam]["rows_since"] = len(fresh)
        observed[fam]["rows_since_with_cycle_id"] = sum(
            1 for r in fresh if row_cycle(r) is not None)
        if fresh and observed[fam]["rows_since_with_cycle_id"] == 0:
            since_total_loss.append(fam)
    return {"payload": payload, "observed": observed,
            "since_total_loss": since_total_loss}


def _classify(key: str, old: Any, new: Any) -> str:
    """이 래칫의 **유일한 seam** — "악화" 의 정의는 여기에만 적는다.

    ⛔ `MISSING`(행 자체 없음)과 `None`(기록된 값)을 구분한다. 접으면 신규
    무신고 원장이 *"값이 None 인 원장"* 으로 보인다.
    """
    if old is MISSING:
        return WORSE          # 젤에 있는데 baseline 에 없다 = 신고 없는 신규 원장
    if new is MISSING:
        return WORSE          # 구조상 안 나온다(비교 범위를 관측된 것으로 좁혔다)
    if not isinstance(old, Mapping) or not isinstance(new, Mapping):
        return WORSE          # 모양이 무너졌으면 fail-closed
    old_scoped = old.get("cycle_scoped", MISSING)
    new_scoped = new.get("cycle_scoped", MISSING)
    if not isinstance(old_scoped, bool) or not isinstance(new_scoped, bool):
        # ⛔ 모양이 무너진 항목을 중립으로 접으면 그 원장의 판정이 조용히 사라진다.
        return WORSE
    if old_scoped is True and new_scoped is not True:
        # 🔑 분모에서 빼는 것으로 적색을 지우는 경로. 래칫이 썩는 가장 흔한 길이라
        #    **악화**로 못 박는다.
        return WORSE
    if old_scoped is not True and new_scoped is True:
        return NEUTRAL        # "안 할 일" → "못 한 일" 로 승격. 정직한 작업이나 재동결 필요
    if new_scoped is True:
        old_joined = old.get("ever_joined", MISSING)
        new_joined = new.get("ever_joined", MISSING)
        if old_joined is True and new_joined is not True:
            return WORSE
        if old_joined is not True and new_joined is True:
            return BETTER
        return WORSE          # 둘 다 불리언이 아니다 = 모양이 무너졌다
    return NEUTRAL            # 사이클 밖 원장의 사유 문구가 바뀐 것


_FIX_HINT = (
    "처방: 생산자에서 조인키가 빠졌다. `CYCLE_WRITERS` 의 해당 writer 를 열어 "
    "`join_keys()` / `\"cycle_id\"` 가 살아 있는지 보라.\n"
    "  ⚠️ `ever_joined` 가 참→거짓이면 **코드 회귀보다 먼저** "
    "`tools/executor_log_quarantine.py` 를 의심하라 — 그건 원장을 재작성(행 제거)하므로 "
    "이 지표의 단조성을 깨는 **알려진 예외**다."
)
_FREEZE_HINT = (
    "처방: 개선/중립이다. 값을 확인한 뒤 "
    "`python3 tools/ledger_join_check.py --ratchet --update` 로 동결하라."
)


def ratchet_check(jail: Path, baseline_path: Path, *,
                  since: float | None = None) -> dict[str, Any]:
    """baseline 대조. 순수 판정 — **쓰지 않는다**(쓰면 자기 자신을 재게 된다).

    ``exit`` 는 :data:`EXIT_OK` … :data:`EXIT_CANNOT_COMPARE`.
    """
    measured = ratchet_measure(jail, since=since)
    payload = measured["payload"]
    lines: list[str] = []

    try:
        doc = load_baseline(baseline_path, schema=RATCHET_SCHEMA,
                            scope=scope_for(jail))
    except BaselineMissing as exc:
        # ⛔ 부재는 통과가 아니다. 침묵도 아니다 — 그리고 **예외도 아니다**(R5):
        #    이름 붙은 거부(reason="baseline_missing", exit=EXIT_CANNOT_COMPARE)를
        #    strict 여부와 무관하게 돌려준다. 근거는 모듈 상단 "삼킴이 아니라 판정".
        with _strict_scope(False):
            _swallowed(exc, site="tools.ledger_join_check.ratchet_check:missing",
                       category="verify")
        return {"exit": EXIT_CANNOT_COMPARE, "reason": "baseline_missing",
                "measured": measured,
                "lines": [
                    "비교 못 했다 — baseline 부재: "
                    f"{_format_text_for_critical_record(baseline_path, max_chars=1000)}",
                    _format_exception_for_critical_record(exc, max_chars=1000),
                ]}
    except BaselineTampered as exc:
        # 손상/오물림도 같은 부류 — 판정으로 거부한다(reason=baseline_<사유>).
        with _strict_scope(False):
            _swallowed(exc, site="tools.ledger_join_check.ratchet_check:tampered",
                       category="verify")
        head = ("비교 못 했다 — 다른 래칫/다른 대상의 baseline 을 물렸다"
                if exc.is_refusal else "비교 못 했다 — baseline 이 손상됐다(손편집?)")
        refusal_reason = "baseline_unknown"
        if exc.reason == "shape":
            refusal_reason = "baseline_shape"
        elif exc.reason == "unparseable":
            refusal_reason = "baseline_unparseable"
        elif exc.reason == "hash":
            refusal_reason = "baseline_hash"
        elif exc.reason == "schema":
            refusal_reason = "baseline_schema"
        elif exc.reason == "scope":
            refusal_reason = "baseline_scope"
        return {"exit": EXIT_CANNOT_COMPARE,
                "reason": refusal_reason, "measured": measured,
                "lines": [
                    head,
                    _format_exception_for_critical_record(exc, max_chars=1000),
                ]}

    base_payload = doc["payload"]
    # 🔑 *"이 젤에서 관측 못 함"* 은 **회귀가 아니다** — 클린 클론·다른 머신에는
    #    행이 없는 게 정상이다. 비교 범위에서 빼고 **보고만** 한다.
    not_observed = sorted(k for k in base_payload if k not in payload)
    old_scoped = {k: v for k, v in base_payload.items() if k in payload}

    diff = compare(old_scoped, payload, classify=_classify)
    exit_code = EXIT_OK
    if not diff.empty:
        exit_code = EXIT_REGRESSION
        lines.append(render_diff(diff, fix_hint=_FIX_HINT, freeze_hint=_FREEZE_HINT))
    if diff.added:
        lines.append("  ↑ '추가' = 젤에 있는데 baseline 에 없는 원장 = **신고 없는 신규 원장**. "
                     "`CYCLE_WRITERS` 또는 `NOT_CYCLE_SCOPED` 에 먼저 신고하라.")
    if not_observed:
        lines.append(f"관측 못 함 {len(not_observed)} (회귀 아님 — 이 젤에 행이 없을 뿐): "
                     + ", ".join(not_observed))

    # 🔑 **재동결로 지울 수 없는 증거 — 단, 발화 조건이 있다.** 1층의 동결증거 대조
    # (:func:`scope_defects`)는 `--ratchet --update` 한 번이면 무력해진다 — 증거
    # 파일 자체가 새로 쓰이기 때문이다. 반면 *"이 원장은 사이클 밖이다"* 는
    # **라이브 행이 반증한다**: 정말 사이클 밖이면 cycle_id 를 가진 행이 0개여야
    # 한다. 2026-08-09 실측으로 이 문장이 오늘 참임을 확인했다(사이클 밖 2개 모두
    # 0/97, 0/32). 재분류 공격의 표적이던 cross_model_verify_spend 는 10/46 이다.
    #
    # ⛔ **이 검사는 cycle_id 행이 0개인 원장에 대해 구조적으로 공허하다**(2026-08-09
    # 3차 적대검증). 조건이 `rows_with_cycle_id > 0` 이므로, *아직 한 번도 조인 안
    # 된* 원장 — 즉 지금 적색이라 숨기고 싶은 바로 그 원장 — 을 사이클 밖으로
    # 재분류하면 여기서는 절대 안 걸린다(실측: `bus/falsifier_events` 0/78 ·
    # `si_audit/*` 0/2). 그 부류를 막는 것은 이 블록이 아니라 **동결 경로의 거부**
    # (:func:`rescope_refusals`)다. 한 문장으로: 여기는 *"거짓 신고"* 를 잡고,
    # 거기는 *"증거 세탁"* 을 잡는다. 어느 쪽도 다른 쪽을 대신하지 않는다.
    contradicted = sorted(
        fam for fam, value in payload.items()
        if isinstance(value, Mapping) and value.get("cycle_scoped") is False
        and measured["observed"].get(fam, {}).get("rows_with_cycle_id", 0) > 0)
    if contradicted:
        exit_code = EXIT_REGRESSION
        for fam in contradicted:
            got = measured["observed"][fam]
            lines.append(
                f"'사이클 밖' 신고가 라이브 행에 반증됐다: {fam} — "
                f"{got['rows_with_cycle_id']}/{got['rows']} 행이 cycle_id 를 갖고 "
                f"있다. 사이클 밖 원장은 **분모에서 빠지므로** 이 신고는 적색을 "
                f"지우는 게 아니라 숨긴다. NOT_CYCLE_SCOPED 에서 빼고 생산자를 "
                f"CYCLE_WRITERS 에 적어라.\n"
                f"  ⛔ 이 검사는 baseline 재동결로 못 지운다 — 동결본이 아니라 "
                f"**지금 젤의 행**을 본다.")
    if measured["since_total_loss"]:
        exit_code = EXIT_REGRESSION
        lines.append(
            "--since 완전손실 " + ", ".join(measured["since_total_loss"])
            + f" — 동결({since}) 이후 신규 행이 있는데 그중 cycle_id 를 가진 행이 **0개**다.\n"
            "  ⚠️ 이 검사는 **완전 손실만** 잡는다. 신규 행 100개 중 99개에서 cycle_id 가 "
            "빠져도 1개만 있으면 통과한다 — 부분 누락 탐지는 1층(정적 생산자 전수) 소관이다.\n"
            "  원인 후보 셋을 **이 순서로** 보라(오진 방지):\n"
            "    1. `AGI_V8_LEDGER_JOIN_KEYS_ENABLED` / `AGI_V8_EPISODE_CTX_ENABLED` 가 꺼졌나 "
            "— 게이트가 OFF 면 행이 종전대로 나는 게 **정상**이고 이건 회귀가 아니다\n"
            "    2. 생산자가 조인키를 잃었나 — 1층(pytest)이 심볼 단위로 말해준다\n"
            "    3. 이 원장이 아직 **한 번도 발동 안 했나** — 배선은 됐는데 미발동인 원장이 "
            "2026-08-08 기준 둘 있다(`bus/falsifier_events` · `si_audit/*`)"
        )
    return {"exit": exit_code, "reason": "compared", "measured": measured,
            "diff": diff, "not_observed_here": not_observed, "lines": lines}


def ratchet_update(jail: Path, baseline_path: Path, *,
                   meta_extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """baseline 을 동결한다. ⛔ ``--update`` 경로 전용.

    관측 못 한 패밀리는 **지우지 않고 그대로 옮긴다**(``carried_forward``).
    빈 젤에서 한 번 돌리면 baseline 이 통째로 비어 다음부터 영원히 초록이 되는
    함정을 구조적으로 막는다 — 잊는 쪽이 아니라 기억하는 쪽으로 실패한다.

    🔒 **동결을 거부하는 자리는 둘**이고, 둘 다 파일을 **안 쓰고**
    ``refused_rescope`` 를 채워 돌려준다 — 예외가 아니라 "안 씀" 으로 실패하는 쪽이
    fail-closed 다(호출자가 결과를 무시해도 증거는 그대로 남는다):

    1. **강등**(:func:`rescope_refusals`) — 동결본이 *분모 안* 이라 말하는 원장을
       지금 표가 분모 밖으로 옮겼다.
    2. **손상된 이전 판**(:class:`BaselineTampered`) — 이전 판을 못 읽으면 1번 검사
       자체가 사라지므로, 손상 위에 재동결하지 않는다. ⚠️ :class:`BaselineMissing`
       (=최초 동결)은 여기 안 낀다 — 이전 판이 없으면 강등도 없다.

    ⛔ **이 둘로 재동결 세탁이 닫히지 않는다.** 남은 두 통로(동결본 삭제 후 재동결 ·
    딴 경로에 동결 후 복사)는 :data:`RESCOPE_ALLOWLIST` 의 '안 잡는 것' 에 실측과
    함께 적혀 있고, 거기 남는 마찰은 기계가 아니라 **커밋 diff 를 보는 사람**이다.

    ## strict 모드에서도 "거부"는 거부다 (2026-08-09 R5 — 4차의 유산을 닫음)

    4차까지는 두 ``except`` 가 :func:`policy.fail_fast.swallowed` 를 타서
    ``AGI_V8_STRICT_FAIL_FAST=true`` 에서 원래 예외가 **재-raise** 됐다 — 판정
    (거부 dict)이 스택트레이스로 바뀌고, 프로세스 exit=1 이 :data:`EXIT_REGRESSION`
    과 같은 값이라 소비자가 "회귀"와 "사고"를 구분 못 했다(strict 실측: 세 스위트
    21 failed, 그중 이 부류 4건 — check:missing/tampered · update:no_prior/tampered
    — 이 17건의 원인). R5: 네 자리 전부 기록(초크포인트)은 유지하되 strict
    재-raise 만 명시적으로 옵트아웃했다 — baseline 부재/손상은 *삼킨 예외*가
    아니라 **이름 붙은 판정**이다(모듈 상단 "삼킴이 아니라 판정" 블록이 정본).
    두 모드가 같은 dict 를 돌려주는 계약은 ``test_strict_*`` 다섯이 고정한다.
    """
    measured = ratchet_measure(jail)
    payload = dict(measured["payload"])
    carried: list[str] = []
    try:
        doc = load_baseline(baseline_path, schema=RATCHET_SCHEMA,
                            scope=scope_for(jail))
    except BaselineMissing as exc:
        # 🔑 **최초 동결은 허용한다** — 이전 판이 없으면 강등도 없다(비교 대상 자체가
        #    없다). 이 통로가 열려 있다는 사실은 :data:`RESCOPE_ALLOWLIST` 의
        #    '안 잡는 것' 1번이고, `test_the_documented_laundering_paths_are_open`
        #    이 그 거동을 못 박는다. ⛔ 처방된 경로를 strict 가 사고로 만들면 클린
        #    클론에서 래칫을 세울 수조차 없다 — 재-raise 는 옵트아웃한다(모듈 상단).
        with _strict_scope(False):
            _swallowed(exc, site="tools.ledger_join_check.ratchet_update:no_prior",
                       category="verify")
        doc = None
    except BaselineTampered as exc:
        # ⛔ **손상된 이전 판은 최초 동결이 아니다.** 2026-08-09 4차 적대검증이 정확히
        #    여기로 들어왔다: 두 예외를 한 핸들러로 묶어 `doc=None` 으로 내려보내면
        #    아래 `rescope_refusals` 가 **아예 안 불린다** ⇒ 손편집 1바이트(래칫
        #    payload 에 들어가지도 않는 `meta.at`)로 강등이 유효 해시와 함께
        #    재동결됐다(실측: 헤드라인 13/15 → 13/14, 이후 `--ratchet` exit=0,
        #    1층 네 술어 전부 침묵). 처방된 경로(영수증 한 줄)보다 **싸고 리뷰
        #    신호가 적은** 우회였다.
        #    ⇒ 손상은 거부한다. 예외로 터뜨리지 않고 "안 씀" 으로 돌려주는 이유는
        #    `refused` 경로와 같다 — 호출자가 결과를 무시해도 증거 파일이 그대로
        #    남는 쪽이 fail-closed 다. (R5: strict 모드에서도 같은 dict 다 — 판정이
        #    재-raise 로 바뀌면 거부 채널이 모드에 따라 갈라진다. 모듈 상단 참조.)
        with _strict_scope(False):
            _swallowed(exc, site="tools.ledger_join_check.ratchet_update:tampered",
                       category="verify")
        return {
            "payload": {}, "carried_forward": [],
            "observed": measured["observed"], "wrote": False,
            "refused_rescope": [
                (f"다른 래칫/다른 대상의 baseline 을 물렸다({exc.reason})"
                 if exc.is_refusal
                 else f"동결본이 손상됐다({exc.reason}) — 손편집?")
                + f": {_format_text_for_critical_record(baseline_path, max_chars=1000)}\n"
                f"  {_format_exception_for_critical_record(exc, max_chars=1000)}\n"
                f"  ⛔ 이전 판을 못 읽으면 그 위에 재동결하지 않는다 — 못 읽는 순간 "
                f"강등(분모 안 → 분모 밖) 검사가 통째로 사라지고, 그게 증거 세탁의 "
                f"가장 싼 경로다(2026-08-09 실측: `meta.at` 에 공백 한 글자).\n"
                "  처방: 손편집이면 되돌려라(`git diff <baseline>`). "
                f"scope/schema 가 어긋난 거면 **물린 대상이 틀린 것**이니 --jail / "
                f"--baseline 을 맞춰라. 정말 동결본을 버려야 한다면 **지우고** 다시 "
                f"동결해라 — 그건 커밋 diff 에 전면 재작성으로 남아 사람이 본다."],
        }
    if doc is not None:
        refused = rescope_refusals(doc["payload"], measured["payload"],
                                   RESCOPE_ALLOWLIST)
        if refused:
            return {"payload": dict(doc["payload"]), "carried_forward": [],
                    "observed": measured["observed"], "refused_rescope": refused,
                    "wrote": False}
        for key, value in doc["payload"].items():
            if key in payload:
                continue
            # __SLOT_CAMPAIGN_FAMILY_FOLD_2026_08_23__ 접기 마이그레이션 — 옛
            # 철자의 family() 가 **이번 동결에서 실관측된** 접힌 키와 같으면
            # 그건 새 원장이 아니라 같은 패밀리의 접기 이전 표기다. 이월하면
            # 옛 철자가 미신고 적색으로 영원히 남는다(~140가족 실측) — 흡수해
            # 하나로 합친다. ⚠️ 접힌 형태가 이번에 관측되지 않았으면 종전대로
            # 이월한다(정직한 재동결 계약 불변 — 관측 없는 소멸은 여전히 금지).
            folded = family(key)
            if folded != key and folded in payload:
                continue
            payload[key] = value
            carried.append(key)
    meta = {
        "measured_by": "python3 tools/ledger_join_check.py --ratchet --update",
        "jail_scope": scope_for(jail),
        "carried_forward": sorted(carried),
        # 비율은 **여기**에만 산다 — 관측치이지 계약이 아니다(비교 대상 아님).
        "observed_at_freeze": measured["observed"],
        **dict(meta_extra or {}),
    }
    write_baseline(baseline_path, payload, meta,
                   schema=RATCHET_SCHEMA, scope=scope_for(jail))
    return {"payload": payload, "carried_forward": sorted(carried),
            "observed": measured["observed"], "refused_rescope": [],
            "wrote": True}


def _run_ratchet_cli(jail: Path, baseline_path: Path, *, update: bool,
                     since: float | None, as_json: bool) -> int:
    """``--ratchet`` 화면. ⚠️ 자동 호출자는 붙이지 않는다 — 읽기전용 CLI 다."""
    if update:
        # 출처는 **호출측 책임**이다(`write_baseline` 은 자동 주입을 안 한다).
        # ⛔ 커밋 해시는 안 적는다: 동결은 커밋 **전**에 일어나므로 여기 적히는 건
        #    부모 커밋이고, 그러면 파일이 자기가 안 잰 트리를 가리키게 된다
        #    (= 선언이 코드보다 강해지는 그 형태). 잰 날짜와 명령줄만 적는다.
        result = ratchet_update(jail, baseline_path, meta_extra={
            "at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d"),
        })
        if as_json:
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        elif result["refused_rescope"]:
            # ⛔ 동결 **거부**. 파일은 안 썼다 — 종료코드로 말한다.
            print(f"[거부] 동결 안 함: {baseline_path}")
            for line in result["refused_rescope"]:
                print(line)
        else:
            print(f"동결: {baseline_path}  패밀리 {len(result['payload'])}개")
            if result["carried_forward"]:
                print("  이월(이 젤에서 관측 못 함, 지우지 않는다): "
                      + ", ".join(result["carried_forward"]))
        return EXIT_REGRESSION if result["refused_rescope"] else EXIT_OK

    report = ratchet_check(jail, baseline_path, since=since)
    if as_json:
        printable = {k: v for k, v in report.items() if k != "diff"}
        printable["diff_keys"] = list(report["diff"].keys) if "diff" in report else []
        print(json.dumps(printable, ensure_ascii=False, indent=2,
                         sort_keys=True, default=repr))
    else:
        verdict = {EXIT_OK: "통과", EXIT_REGRESSION: "회귀",
                   EXIT_CANNOT_COMPARE: "비교 못 함"}[report["exit"]]
        print(f"[{verdict}] {baseline_path}  (scope={scope_for(jail)})")
        for line in report["lines"]:
            print(line)
        if report["exit"] == EXIT_OK:
            observed = report["measured"]["observed"]
            joined = sum(1 for k, v in report["measured"]["payload"].items()
                         if v.get("cycle_scoped") and v.get("ever_joined"))
            scoped = sum(1 for v in report["measured"]["payload"].values()
                         if v.get("cycle_scoped"))
            print(f"  조인축에 올라온 패밀리 {joined}/{scoped}"
                  f"  (사이클 밖 {len(observed) - scoped}개 제외)")
    return int(report["exit"])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--jail", default=str(DEFAULT_JAIL))
    ap.add_argument("--cycle", default=None, help="조회할 cycle_id (없으면 최근 것)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--ratchet", action="store_true",
                    help="baseline 대조 (0 통과 / 1 회귀 / 2 젤부재 / 3 비교불가)")
    ap.add_argument("--update", action="store_true",
                    help="baseline 동결. --ratchet 과 함께만 쓴다")
    ap.add_argument("--baseline", default=str(DEFAULT_BASELINE))
    ap.add_argument("--since", type=float, default=None,
                    help="이 epoch 이후 신규 행만 본다 (완전 손실만 잡는다)")
    args = ap.parse_args(argv)

    jail = Path(args.jail)
    if not jail.is_dir():
        print(f"jail 없음: {jail}", file=sys.stderr)
        return EXIT_NO_JAIL

    if args.update and not args.ratchet:
        print("--update 는 --ratchet 과 함께만 쓴다 (동결은 검사 경로가 아니다)",
              file=sys.stderr)
        return EXIT_CANNOT_COMPARE

    if args.ratchet:
        return _run_ratchet_cli(jail, Path(args.baseline),
                                update=args.update, since=args.since,
                                as_json=args.json)

    rep = survey(jail)
    cid = args.cycle or latest_cycle(jail)
    rep["join_demo"] = join_cycle(jail, cid) if cid else None

    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    print(f"젤 {rep['jail']}")
    print(f"원장 파일 {rep['ledger_files']}  ·  행 {rep['rows_total']:,}")
    print(f"{'원장':44s} {'행':>7s} {'ts':>12s} {'cycle':>13s}")
    print("-" * 80)
    for l in rep["ledgers"]:
        ts = "—" if l["ts_pct"] is None else f"{l['ts_resolved']}/{l['rows']} {l['ts_pct']:.0f}%"
        cy = "—" if l["cycle_pct"] is None else f"{l['cycle_resolved']}/{l['rows']} {l['cycle_pct']:.0f}%"
        if l["not_cycle_scoped"]:
            mark, cy = "⚪", "해당없음"
        elif l["rows"] and not l["cycle_resolved"]:
            mark = "🔴"
        else:
            mark = "  "
        print(f"{mark}{l['ledger']:42s} {l['rows']:>7,} {ts:>12s} {cy:>13s}")
    print("-" * 80)
    print(f"조인축에 올라온 원장  {rep['ledgers_with_any_cycle']}/{rep['ledgers_cycle_scoped']}"
          f"  (사이클 밖 {rep['ledgers_not_cycle_scoped']}개 제외)")
    print(f"cycle_id 가진 행     {rep['scoped_rows_cycle_resolved']:,}/{rep['scoped_rows_total']:,}"
          f"  (전체 기준 {rep['rows_cycle_resolved']:,}/{rep['rows_total']:,})")
    print(f"시각 해석된 행        {rep['rows_ts_resolved']:,}/{rep['rows_total']:,}")
    if rep["ledgers_not_cycle_scoped"]:
        print("\n⚪ 사이클에 속하지 않는 원장 — 미달이 아니라 사실이다")
        for l in rep["ledgers"]:
            if l["not_cycle_scoped"]:
                print(f"    {l['ledger']:36s} {l['not_cycle_scoped']}")

    demo = rep["join_demo"]
    if demo:
        print(f"\n한 사이클 조회: {demo['cycle_id']}")
        print(f"  원장 {demo['ledgers_hit']}개 · 행 {demo['rows_total']} · 폭 {demo['span_s']}s")
        for h in demo["hits"]:
            unk = f"  (시각모름 {h['ts_unknown']})" if h["ts_unknown"] else ""
            print(f"    {h['ledger']:42s} {h['rows']:>4} 행{unk}")
    else:
        print("\n조회할 사이클이 없다 — cycle_id 를 가진 행이 하나도 없음")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
