# __SLOT_TICK_RUNNER_2026_06_20__ multi-turn steering — slice 3 (advisory soak).
"""Query-driven tick: map ONE queued query to ONE orchestrator cycle.

This is the steering half of the multi-turn design (the briefing half shipped in
``observability/briefing_builder.py``). A cron line fires ``scripts/tick.sh``
every ~10 min; that wrapper holds a ``flock`` so only one tick runs at a time
and invokes this module. Each tick:

  1. is a NO-OP unless the master gate ``AGI_V8_TICK_ENABLED`` is "true"
     (kill-switch = flip the gate false; cron one-shots leave no warm process);
  2. reads an append-only query inbox under ``<jail>/tick/queries.jsonl`` and a
     tombstone log ``<jail>/tick/consumed.jsonl`` — idle inbox ⇒ silent skip
     (no heartbeat, no autonomous churn);
  3. authenticates each query against a secret token kept OUTSIDE the repo
     (``AGI_V8_TICK_AUTH_TOKEN[_FILE]``) — a query is an *armed objective*, so an
     unauthenticated one is tombstoned and skipped, never run;
  4. refuses to start once the **per-day** cumulative cost cap is reached
     (``runtime/daily_cost_cap``) and bounds the cycle it does start to the
     smaller of the per-cycle cap and the remaining daily headroom;
  5. optionally runs an **Agent-on-Agent review** (``runtime/tick_review``) —
     an INDEPENDENT model vets the objective's coherence/scope/safety before
     dispatch; default-OFF (``AGI_V8_TICK_REVIEW_ENABLED``), fail-closed
     (reviewer unavailable or malformed output ⇒ reject, never skip-review).
     The review call itself spends real money, so step 4's cap verdict is
     re-read once the review approves (``AGI_V8_TICK_CAP_REREAD_AFTER_REVIEW``,
     default ON, __SLOT_CAP_REREAD_AFTER_REVIEW_2026_08_19__) before this
     cycle's budget is computed;
  6. dispatches exactly ONE cycle into an explicit ``--state-dir`` JAIL — there
     is no tmpdir default on this path — with ``--with-agents`` OFF during the
     advisory soak (flip ``AGI_V8_TICK_WITH_AGENTS`` to arm, slice 4).

Every consequential outcome (dispatched / rejected / cap-blocked / misconfig) is
appended to ``<jail>/tick/tick_log.jsonl``; pure idle ticks write nothing.

The orchestrator call is injected (``dispatch_fn``) so the gating/auth/cap logic
is unit-testable without importing the orchestrator or spending a cent — and so
the kill-switch ("gate off ⇒ dispatch_fn is never called") is provable.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Mapping
from agi_v8_1.capabilities import PayloadPort, resolve_payload
import sys as _payload_sys

from agi_v8_1.runtime import (
    cycle_memory,
    daily_cost_cap,
    halt_sentinel,
    repeated_failure_gate,
    spend_reservation,
    tick_auth,
    tick_deadline,
    tick_lease,
)
from agi_v8_1.runtime.tick_policy_ports import goal_campaign_feed, tick_review, work_feeder
from agi_v8_1.state.store import atomic_append_jsonl, read_jsonl

# __SLOT_FAIL_FAST_2026_07_25__ Swallowed failures route through one choke
# point: counted + named always, re-raised under AGI_V8_STRICT_FAIL_FAST.
from agi_v8_1.policy.fail_fast import (
    format_exception_for_critical_record as _format_exception_for_critical_record,
    format_exception_for_log as _format_exception_for_log,
    format_text_for_sink as _format_text_for_sink,
    swallowed as _swallowed,
)
# __SLOT_GATE_INVARIANTS_2026_08_17__ combination safety net (default-OFF
# master gate; ON is the safe direction — see policy/gate_invariants.py).
from agi_v8_1.policy import gate_invariants as _gate_invariants

logger = logging.getLogger(__name__)


def _mask_published_text(text: str) -> str:
    """Always mask free text before this module publishes it to tick JSONL.

    A tick ledger is already a publication boundary, so use the always-on
    canonical sink formatter (including truncated-PEM and bare-base64 guards).
    Reviewer reasons already have a 300-character contract; normalize them to
    one line and preserve that cap after masking.
    """
    return _format_text_for_sink(text, max_chars=300, one_line=True, keep="head")

# Env gates --------------------------------------------------------------------
# __SLOT_VERIFY_GATE_PRESEAL_CAPTURE_2026_08_20__ ②강화안 — see
# _preseal_git_identity_capture_enabled() and the call site inside
# tick_once() for the full rationale. default-OFF, T9 tier; strict
# ("true"/"1" only) per this track's invariant, NOT the looser
# ``_truthy()`` four-token form other tick gates below use.
_PRESEAL_GIT_IDENT_ENABLED = "AGI_V8_VERIFY_GATE_PRESEAL_GIT_IDENT_ENABLED"
_TICK_ENABLED = "AGI_V8_TICK_ENABLED"          # master kill-switch (default OFF)
_WITH_AGENTS = "AGI_V8_TICK_WITH_AGENTS"        # arm real agents (default OFF / soak)
_REVIEW_ENABLED = "AGI_V8_TICK_REVIEW_ENABLED"  # Agent-on-Agent objective review (default OFF)
# __SLOT_CAP_REREAD_AFTER_REVIEW_2026_08_19__ 리뷰 지출 뒤 캡 재판독 (default ON,
# see cap_reread_after_review_enabled() below for the full rationale).
_CAP_REREAD_AFTER_REVIEW = "AGI_V8_TICK_CAP_REREAD_AFTER_REVIEW"
_CAP_REREAD_AFTER_REVIEW_FALSY = ("false", "0")
# __SLOT_TICK_REVIEW_TRANSACTION_2026_08_20__ Sol Pro R15 P1-D — see
# tick_review_transaction_enabled() below for the full rationale.
_REVIEW_TRANSACTION_ENABLED = "AGI_V8_TICK_REVIEW_TRANSACTION_ENABLED"
_AUTH_TOKEN = "AGI_V8_TICK_AUTH_TOKEN"          # expected token (inline)
_AUTH_TOKEN_FILE = "AGI_V8_TICK_AUTH_TOKEN_FILE"  # or a 0o600 file outside the repo
_PER_CYCLE_CAP = "AGI_V8_TICK_PER_CYCLE_USD_CAP"  # per-cycle ceiling (default $1)
# __SLOT_R13_LEDGER_2026_08_17__ 판독 실패와 "비어 있음"을 구분할지 — 새 방어라
# 기본 ON(``state/store.py::strict_perms_enabled`` 와 같은 falsy-set 관례: 판정불가/
# 오타 값은 켜짐 쪽으로 접힌다). OFF = 패치 이전과 byte-identical(관대한 삼킴).
_LEDGER_FAILCLOSED = "AGI_V8_TICK_LEDGER_FAILCLOSED"
_LEDGER_FAILCLOSED_FALSY = ("false", "0")
# __SLOT_R14_TICKPROFILE_2026_08_17__ 🔴 R14 감사 2순위 — "env 관례"가 아니라
# tick 소비 진입점 자신의 구조적 전제조건으로 승격. 이 게이트가 지키는 위험
# 조합은 ``AGI_V8_TICK_ENABLED=true`` + ``AGI_V8_TICK_AUTH_SIG_ENABLED`` 미설정
# + ``AGI_V8_GATE_INVARIANTS_ENFORCED`` 미설정 — 마스터 안전조합 검사(비활성)에
# 기대지 않고, 여기(소비 진입점)가 자기 자신을 검사한다. default **ON**(다른
# default-OFF 증분과 반대 방향 — 이 레포의 보안 증분 관례). ``=false`` 로
# 내리면 이 SLOT 전체가 비활성화되고 패치 이전과 byte-identical(OFF-parity).
#
# 🔑 정본은 ``tick_auth.ENV_SECURE_PROFILE``/``tick_auth.secure_profile_enabled``
# 다(auth 프로파일 관심사라 R14 2차 라운드에서 옮겼다 — 그래야 ``tick_auth.
# ttl_seconds()`` 의 미선언-TTL 안전 기본값도 **같은 게이트**를 본다. 여기 두 번
# 적으면 판별기와 강제가 다른 사실을 보는 사고가 재발한다). 이 이름은 재수출일
# 뿐이라 이 모듈은 사본을 안 든다 — ``_SECURE_PROFILE`` 상수도 여기 없다.

# __SLOT_TICK_OBJECTIVE_TARGETS_PARSE_2026_08_20__ objective 본문의
# ``target_files: a,b`` 패턴 → inc3 소비부(``self_improvement_v8.
# _si_objective_targets``, ``AGI_V8_SI_OBJECTIVE_TARGETS`` 를 무변경으로 읽는
# 그 함수)가 볼 값을 **env 로** 보충한다. env 가 이미 설정돼 있으면 손대지
# 않는다(env 우선, 본문은 보충일 뿐) — 08-19 실측(산문 강등, target_files_for
# 가 아무 컨텍스트도 못 받아 새-파일 제안으로 새는 형상)을 이 소비 경로에서
# 뒤집는 게 이 게이트의 요점이다. 새 채널을 만들지 않는다 — 기존 env 이름
# 그대로 채운다.
_OBJECTIVE_TARGETS_PARSE_ENABLED = "AGI_V8_TICK_OBJECTIVE_TARGETS_PARSE_ENABLED"
# ⚠️ 이 이름은 ``self_improvement_v8._SI_OBJECTIVE_TARGETS_ENV`` 의 리터럴
# 사본이다 — 그 모듈을 임포트해서 상수를 재수출하면 순환 import 위험이 생기고
# (``self_improvement_v8`` 는 무거운 provider/orchestrator 임포트 그래프의
# 루트다), 문자열 리터럴 자체는 두 파일 다 바뀔 일이 없는 env 계약 이름이라
# 사본으로 둬도 "자칭 SSOT 드리프트"(2026-08-07 실측)에 해당하지 않는다 —
# ``tests/v8_1/test_cycle_memory_2026_08_20.py`` 가 두 리터럴이 같은 문자열임을
# pin 으로 고정한다.
_SI_OBJECTIVE_TARGETS_ENV = "AGI_V8_SI_OBJECTIVE_TARGETS"
_TARGET_FILES_LINE = re.compile(r"target_files:\s*([^\r\n]+)")


def objective_targets_parse_enabled() -> bool:
    """Default-OFF target_files: 파싱 게이트(이 파일 sibling 관례: ``_truthy``)."""
    return _truthy(_OBJECTIVE_TARGETS_PARSE_ENABLED)  # tier: T9


def _parse_objective_target_files(objective: str) -> list[str]:
    """objective 원문의 ``target_files: a,b`` 한 줄 → relpath 리스트.

    ``self_improvement_v8._si_objective_targets()`` 와 같은 정규화(strip +
    빈 항목 제거 + 12개 상한)라, 여기서 만든 값은 운영자가 env 로 직접 채운
    값과 하류에서 구분되지 않는다.
    """
    m = _TARGET_FILES_LINE.search(objective)
    if not m:
        return []
    return [t.strip() for t in m.group(1).split(",") if t.strip()][:12]


def _maybe_set_scoped_objective_targets(objective: str) -> "tuple[bool, str | None]":
    """게이트 ON + 패턴 있음 + env 미설정일 때만 env 를 **이 프로세스에** 채운다.

    반환 ``(touched, prior)`` — *touched* 가 참일 때만 호출부가
    :func:`_restore_scoped_objective_targets` 로 되돌려야 한다.

    ⚠️ **왜 scoped 인가**: ``os.environ`` 은 프로세스 전역이다. dispatch 호출을
    감싸는 구간에서만 세팅→복원하지 않으면, 이 패턴이 없는 **다음** 사이클
    (같은 프로세스 안의 반복 호출 — 테스트 루프 포함)에도 값이 새어 남아
    "패턴 없는 사이클에도 targets 가 남아있다"는 오탐을 만든다(정찰 위험 노트).
    """
    if not objective_targets_parse_enabled():
        return False, None
    prior = os.environ.get(_SI_OBJECTIVE_TARGETS_ENV)
    if prior:
        return False, None  # env 우선 — 이미 채워져 있으면 안 건드린다
    targets = _parse_objective_target_files(objective)
    if not targets:
        return False, None
    os.environ[_SI_OBJECTIVE_TARGETS_ENV] = ",".join(targets)
    return True, prior  # prior 는 여기서 None 또는 "" 뿐이다(위에서 이미 걸렀다)


def _restore_scoped_objective_targets(touched: bool, prior: "str | None") -> None:
    if not touched:
        return
    if prior is None:
        os.environ.pop(_SI_OBJECTIVE_TARGETS_ENV, None)
    else:
        os.environ[_SI_OBJECTIVE_TARGETS_ENV] = prior


_INBOX_REL = ("tick", "queries.jsonl")
_CONSUMED_REL = ("tick", "consumed.jsonl")
_TICKLOG_REL = ("tick", "tick_log.jsonl")


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def tick_enabled() -> bool:
    return _truthy(_TICK_ENABLED)


def _preseal_git_identity_capture_enabled() -> bool:
    """②강화안 pre-seal git-identity capture gate — strict ``"true"``/``"1"``
    only (this track's invariant), unlike the looser ``_truthy()`` most other
    tick gates in this module use."""
    return os.environ.get(_PRESEAL_GIT_IDENT_ENABLED, "") in ("true", "1")  # tier: T9


def with_agents_armed() -> bool:
    return _truthy(_WITH_AGENTS)


def review_enabled() -> bool:
    """Default-OFF Agent-on-Agent review gate (see tick_review.py)."""
    return _truthy(_REVIEW_ENABLED)


def cap_reread_after_review_enabled() -> bool:
    """리뷰가 실제로 지출을 남긴 뒤 :func:`daily_cost_cap.check` 를 다시 볼지.

    __SLOT_CAP_REREAD_AFTER_REVIEW_2026_08_19__ 원래 흐름은 (4) 캡 판정 →
    (4.5) 리뷰 → budget 계산이 그 **(4) 의 verdict** 를 그대로 썼다. 리뷰가
    독립 모델 호출이라 ``episode_ctx`` 스코프를 통해 같은
    ``executor_log.jsonl`` 에 실지출을 남기는데(``_build_review_fn`` 의
    ``_record_spend``), 그 지출이 budget 계산에 전혀 반영되지 않았다 — 리뷰
    비용이 캡을 채워도 이번 사이클 budget 은 리뷰 이전 headroom 그대로 잡힌다
    (과소계상 방향). K1b(``__SLOT_HUMAN_HALT_TICK_2026_08_06__``)의 "돈이
    움직인 뒤 판정을 다시 본다" 와 같은 모양이라 기본 **ON** — 리뷰 게이트
    (:func:`review_enabled`) 자체가 default-OFF 이므로 이 재판독은 리뷰가
    실제로 켜진 날에만 동작한다. ``=false`` 는 패치 이전과 byte-identical
    (리뷰 뒤에도 (4) 의 첫 verdict 그대로 budget 을 잡는다).
    """
    v = os.environ.get(_CAP_REREAD_AFTER_REVIEW, "true").strip().lower()
    return v not in _CAP_REREAD_AFTER_REVIEW_FALSY


def tick_review_transaction_enabled() -> bool:
    """Default-OFF — fold the review call into the SAME safety machinery
    ``__SLOT_SPEND_RESERVATION_2026_08_19__``/``__SLOT_TICK_DEADLINE_2026_08_18__``
    already give ``dispatch()``.

    __SLOT_TICK_REVIEW_TRANSACTION_2026_08_20__ Sol Pro R15 P1-D. Measured gap:
    (4.5)'s reviewer call is a REAL provider network call (mirrors the SI
    proposer's ``propose_fn`` seam — see ``tick_review.py``'s module
    docstring) that runs directly in this process, BEFORE ``reserve()`` opens
    and OUTSIDE ``tick_deadline``'s wall-clock boundary. Two independent
    exposures follow from that:

    1. **No deadline.** A hung reviewer transport blocks this synchronous call
       forever — exactly the failure ``tick_deadline.py`` exists to close for
       ``dispatch()``, just left open one call earlier. It still holds
       ``tick.sh``'s flock the whole time.
    2. **No transaction.** The reviewer's spend lands in ``executor_log.jsonl``
       (via ``episode_ctx``) before any reservation exists for it, so a crash
       between the reviewer call and ``reserve()`` leaves that spend invisible
       to every OTHER spender's cap check for the whole window (the exact gap
       ``spend_reservation.py`` closed for dispatch is still open here).

    ON: opens its OWN ``spend_reservation`` (subject ``"tick_review"``,
    settled in a ``finally`` covering every exit including ``HumanHalt``) and,
    when ``tick_deadline.enabled()`` is ALSO on, runs the review call through
    the same ``tick_deadline.run_with_deadline`` fork boundary ``dispatch()``
    uses — a timeout or an unavailable deadline resolves to a REJECTED review
    (fail-closed, same posture ``tick_review.review_objective`` already
    documents for every other reviewer error shape), never a silent approval.
    ``reserve()`` returning the durable-write-refusal shape aborts the tick
    the same way it already does for dispatch (``is_unavailable``) rather than
    reviewing on an un-recorded reservation.

    Strict ``"true"``/``"1"`` (this SLOT's own convention, matching
    ``spend_reservation.enabled()`` — the gate this function wires into).
    tier: T9. OFF (default): this whole path is skipped and (4.5) runs
    exactly as before — byte-identical.
    """
    return os.environ.get(_REVIEW_TRANSACTION_ENABLED, "") in ("true", "1")


def ledger_failclosed_enabled() -> bool:
    """판독 실패와 "비어 있음"을 구분할지 (default True).

    ⚠️ ON 이 안전한 방향이다 — OFF 로 낮추면 :func:`_read_inbox`/:func:`_consumed_ids`
    의 관대한 예외 삼킴(``except Exception: return []/set()``)이 어떤 후단 검사도
    없이 그대로 진행한다. 즉 손상된 ``consumed.jsonl`` 이 "아직 소비 안 됨"으로
    읽혀 과거 objective 가 재디스패치되고 provider 비용이 재발생하는 사슬이
    OFF 에서는 패치 이전과 byte-identical 로 남는다.
    """
    v = os.environ.get(_LEDGER_FAILCLOSED, "true").strip().lower()
    return v not in _LEDGER_FAILCLOSED_FALSY


# __SLOT_R14_TICKPROFILE_2026_08_17__ 재수출 — 정본은 ``tick_auth`` (위 참고).
# ``tick_runner.secure_profile_enabled()`` 를 부르던 기존 호출자/테스트 계약을
# 안 깨려고 이름만 여기 유지한다. 새 로직을 추가하지 마라 — 고칠 곳은 하나다.
secure_profile_enabled = tick_auth.secure_profile_enabled


def _per_cycle_cap() -> float:
    try:
        return max(0.0, float(os.environ.get(_PER_CYCLE_CAP, "1.0")))
    except (TypeError, ValueError) as _ff_exc:
        _swallowed(_ff_exc, site="runtime.tick_runner._per_cycle_cap:84", category="config")
        return 1.0


def _expected_token() -> str | None:
    """Secret that authorizes a query. File takes precedence over inline env.

    Returns None when no token is configured — which makes the runner fail
    CLOSED (it will not run any query) rather than open.
    """
    fpath = os.environ.get(_AUTH_TOKEN_FILE, "").strip()  # tier: T9
    if fpath:
        try:
            tok = Path(fpath).read_text(encoding="utf-8").strip()
            return tok or None
        except OSError as _ff_exc:
            _swallowed(_ff_exc, site="runtime.tick_runner._expected_token:99", category="config")
            return None
    tok = os.environ.get(_AUTH_TOKEN, "").strip()  # tier: T9
    return tok or None


def _query_id(row: dict[str, Any]) -> str:
    """Stable idempotency key for a query row.

    Uses caller-supplied ``id`` when present; otherwise a content hash so the
    same row can still be tombstoned exactly once.
    """
    from agi_v8_1.runtime.public_entry import query_id
    return query_id(row)


def _objective_of(row: dict[str, Any]) -> str | None:
    # __SLOT_TICK_AUTH_SIG_2026_08_08__ 이 규칙의 정의처는 tick_auth 하나다 —
    # 서명이 **실행될 문자열**을 결박하려면 서명자와 소비자가 같은 함수를 봐야 한다.
    # 사본을 되살리면 인가 표면과 결정 표면이 다시 갈라진다(적대검증이 잡은 RED).
    return tick_auth.resolve_objective(row)


def _auth_ok(row: dict[str, Any], expected: str, *,
             state_dir: Path | None = None, now: float | None = None) -> bool:
    # __SLOT_TICK_AUTH_SIG_2026_08_08__ 판정의 정의처는 tick_auth 하나다.
    # legacy 평문 대조 + (게이트 ON 시) 행 결박 HMAC — 평문이 원장에 남는 문제의
    # 소비측 절반. 생산측 절반은 tick_submit.sh / work_feeder 의 stamp_auth.
    # __SLOT_R9_T1_2026_08_17__ state_dir/now 를 넘겨야 TTL·cross-jail audience
    # 검사가 실제로 발화한다 — 안 넘기면 tick_auth 가 조용히 두 검사를 건너뛴다
    # (호출자 하위호환을 위한 설계지, 이 소비 경로에서 생략할 이유는 없다).
    return tick_auth.auth_ok(row, expected, state_dir=state_dir, now=now)


def _read_inbox(state_dir: Path) -> list[dict[str, Any]]:
    try:
        return read_jsonl(state_dir.joinpath(*_INBOX_REL))
    except Exception as _ff_exc:  # noqa: BLE001 — a garbled inbox must never crash the tick
        _swallowed(_ff_exc, site="runtime.tick_runner._read_inbox:136", category="config")
        return []


def _consumed_ids(state_dir: Path) -> set[str]:
    try:
        rows = read_jsonl(state_dir.joinpath(*_CONSUMED_REL))
    except Exception as _ff_exc:  # noqa: BLE001
        _swallowed(_ff_exc, site="runtime.tick_runner._consumed_ids:143", category="config")
        return set()
    return {str(r.get("id")) for r in rows if r.get("id") is not None}


# __SLOT_TICK_TOCTOU_LEDGER_SNAPSHOT_2026_08_18__ ─────────────────────────────
# 🔴 R3 잔여 ① — 실측 확인된 TOCTOU. ``atomic_append_jsonl`` 은 **쓰기만**
# flock 으로 직렬화한다(``state/store.py``) — 읽기는 전혀 안 잠근다. 그런데
# (1a)/(1b) 판독 실패 검사와 (2) ``pending_queries()`` 의 소비 읽기는 지금까지
# **서로 다른 두 번**(각자 독립적인 ``open()``)의 unlocked read 였다. 그 사이
# 창에서 ``consumed.jsonl`` 이 훼손/교체되면 (1a)가 통과시킨 원장을 (2)가 다시
# 읽어 실패 시 조용히 빈 값으로 접는다(``_consumed_ids``) — 이미 소비된 질의가
# 재디스패치(재과금)된다. 결정론적 주입으로 직접 재현했다
# (``tests/security/test_adv_r3_toctou_ledger_2026_08_18.py::test_a2_*`` — 수리
# 전 코드에 대고 돌리면 ``dispatched=True`` 로 재과금이 실제로 일어난다).
#
# 수리: **단일 판독 재사용.** 상류 두 축(``inbox_queue``/``bought_ledger``)을
# 이 틱 안에서 **정확히 한 번**만 열고(:func:`_ledger_snapshot`), (1a)/(1b)/(2)
# 세 검사가 전부 그 결과 하나를 나눠 본다 — 그 사이에 디스크가 바뀌어도 이미
# 메모리에 있는 값은 안 바뀐다(스냅샷 완료 **이후**의 변경은 다음 틱의 몫,
# 정상적인 콜드 재시작과 같은 사실이라 안전한 방향이다).
#
# ⚠️ **필터링 없는 원본을 공유한다** — ``work_feeder._read_rows`` 는 객체 아닌
#    줄을 조용히 걸러낸 ``good`` 리스트를 돌려주는데, 그 필터링된 값을 그대로
#    (2)에 넘기면 "게이트와 무관하게 여전히 터진다"던 기존 계약(HEAD 크래시
#    보존 — ``test_progress_stall_2026_08_09.py::test_r10_*`` 독스트링 참조)이
#    조용히 사라진다(터지는 대신 나쁜 행을 몰래 버림). 그래서 스냅샷은
#    ``read_jsonl`` 원본(비필터링)만 들고, "good/note" 파생은 순수 함수
#    (:func:`_axis_good_and_note`)로 **디스크 접근 없이** 그 원본에서 계산한다
#    — (1a)/(1b) 는 그 파생을 보고, (2)는 원본을 그대로 본다(종전과 동일한
#    crash-on-non-object 계약 유지).
#: __SLOT_TICK_TORN_READ_GUARD_2026_08_18__ 몇 번까지 재시도할지 — 진짜 경합은
#: 짧은 창이라 이 정도면 충분하고도 남는다. 정상 경로(파일이 판독 전후로
#: 안 바뀜)는 언제나 1회차에 통과하므로, 이 예산은 **지속적** 경합에서만
#: 소모된다(그 경우는 애초에 fail-closed 로 접는 게 옳다 — 아래 참조).
_TORN_READ_MAX_ATTEMPTS = 20
#: 판독 **직후** 짧게 기다렸다 다시 stat 해서 "판독 값이 여전히 최신인가"를
#: 확인한다. 이 파일시스템에서 실측한 mtime 해상도(``~4.3ms`` 버킷 — 그
#: 미만 간격의 재기록은 같은 mtime 으로 뭉친다)보다 넉넉히 크게 잡아, 서로
#: 다른 재기록이 우연히 "안 바뀜"으로 뭉치는 걸 피한다.
_TORN_READ_SETTLE_DELAY_SEC = 0.02


def _stat_signature(path: Path) -> "tuple[bool, int, int] | None":
    """(존재여부, 크기, mtime_ns) — 판독 전/후 비교용. ``stat`` 자체 실패는 None."""
    try:
        st = path.stat()
    except FileNotFoundError:
        return (False, -1, -1)
    except OSError as _ff_exc:
        _swallowed(_ff_exc, site="runtime.tick_runner._stat_signature", category="persist")
        return None
    return (True, st.st_size, st.st_mtime_ns)


def _read_axis_raw(path: Path) -> "list[Any] | None":
    """단일 축 원시 판독(필터링 없음, ``read_jsonl`` 그대로) — 실패시 None.

    # __SLOT_TICK_TORN_READ_GUARD_2026_08_18__ 🔴 R3 잔여 ① 후속 수리 — 위
    # ``_ledger_snapshot`` 은 "같은 파일을 두 번 다른 시점에 여는" 창은
    # 닫았지만, **그 한 번의 판독 자체**가 다른 writer 의 비원자적 재기록
    # (예: ``Path.write_text`` 의 open-truncate-then-write)과 겹치는 창은
    # 그대로였다. ``read_jsonl`` 은 빈/토막난 내용을 예외 없이 그대로
    # 돌려준다(빈 JSONL 은 유효한 빈 리스트다) — 그 경합이 "판독 실패"로도
    # "이상한 줄"로도 안 잡히고 "아무것도 없음"으로 조용히 접혀, 이미 소비된
    # 질의가 재디스패치(재과금)됐다. 실측: 해머 스레드(비원자적 반복 재기록)
    # 아래 3/3 회 재과금, 해머 없는 베이스라인은 0/N.
    #
    # 🔴 **판독 전/후 대조만으로는 안 막혔다** — 첫 시도는 실측으로도 여전히
    # 3/3 회 재과금이었다. ``Path.write_text`` 는 ``open(mode='w')`` 시점에
    # **즉시** 파일을 0바이트로 자르고, 실제 내용은 ``close()`` 가 버퍼를
    # 플러시할 때야 비로소 디스크에 나타난다 — 그래서 "잘려서 비어 있는" 구간이
    # 한 loop 당 대부분(실측 ~77%)을 차지하고, 우리 판독(전/후 stat 을 낀 단일
    # ``read_jsonl``) 은 그 안에 통째로 들어가기 쉬워 "안 바뀜"으로 (틀리게)
    # 통과한다 — 그 좁은 창 자체가 writer 의 corrupt 사이클보다 짧아서다.
    #
    # 진짜 수리: 판독 **전/후** 대조(위와 동일, 자기 자신과의 경합을 잡는다)에
    # **더해**, 판독 *직후* ``_TORN_READ_SETTLE_DELAY_SEC`` 만큼 실제로 기다렸다
    # 다시 stat 해서 "우리가 읽은 값이 그 이후로도 안 바뀐 채 남아 있는가"를
    # 확인한다(:func:`_stat_signature`). 지속적으로 재기록하는 writer 는 이
    # 정착 창 안에서도 계속 파일을 건드리므로 대조가 계속 어긋나 재시도가
    # 이어진다 — writer 가 실제로 멈춰야만(또는 우리 예산을 다 써야만) 끝난다.
    # 정상 경로(경합 없음)는 언제나 1회차에 두 대조 모두 통과 — ``read_jsonl``
    # 은 **정확히 한 번**만 불린다(``os.stat`` 세 번 + 짧은 sleep 하나가
    # 전부인 추가 비용, cron 10분 주기에 무의미). 재시도를 다 써도 안 서면
    # 판독 실패(``None``)로 접는다 — 기존 계약과 같은 신호라 호출자
    # (``_failed_ledgers``)가 그 틱을 fail-closed 로 거부한다. "조용한 성공적
    # 빈 읽기"로는 절대 안 접힌다 — 지속 경합 중엔 매 틱 거부가 맞는 방향이다
    # (다음 틱이 다시 시도한다, 재과금 위험은 여전히 0).
    """
    for _attempt in range(_TORN_READ_MAX_ATTEMPTS):
        sig_before = _stat_signature(path)
        try:
            rows = read_jsonl(path)
        except Exception as _ff_exc:  # noqa: BLE001 — a garbled ledger must never crash the tick
            _swallowed(_ff_exc, site="runtime.tick_runner._read_axis_raw", category="persist")
            return None
        sig_after = _stat_signature(path)
        if sig_before is None or sig_after is None:
            # stat 자체가 실패(권한 등) — 이미 _swallowed 됐다. 판독 실패로 접는다.
            return None
        if sig_before != sig_after:
            continue  # 판독 도중 파일이 바뀌었다 — 자기 자신과 이미 안 맞음.
        if not sig_after[0]:
            # __SLOT_TICK_TORN_READ_GUARD_2026_08_18__ 파일이 아예 없다(콜드
            # 스타트 — 46일간 4,446번 나온 가장 흔한 분기). 있지도 않은 파일을
            # 누가 "재기록" 할 순 없으니 정착 대조 자체가 무의미하다 — 지연
            # 없이 즉시 확정한다(``[]``, 기존 부재=정상 계약 그대로).
            return rows
        time.sleep(_TORN_READ_SETTLE_DELAY_SEC)
        sig_settled = _stat_signature(path)
        if sig_settled is None:
            return None
        if sig_settled == sig_after:
            return rows  # 판독 전/후, 그리고 판독 이후 정착 창까지 전부 안 바뀜.
        # 판독 직후 다시 바뀌었다 — 방금 읽은 값이 이미 낡았을 수 있다, 재시도.
    _swallowed(
        RuntimeError(f"torn read: {path} never settled across "
                     f"{_TORN_READ_MAX_ATTEMPTS} attempts"),
        site="runtime.tick_runner._read_axis_raw:torn_read_exhausted",
        category="persist",
    )
    return None


def _ledger_snapshot(state_dir: Path) -> "dict[str, list[Any] | None]":
    """상류 두 축을 **각각 정확히 한 번** 읽는다 — (1a)/(1b)/(2) 가 나눠 쓴다."""
    return {axis: _read_axis_raw(state_dir.joinpath(*rel))
            for axis, rel in _UPSTREAM_RELS.items()}


def _axis_good_and_note(raw: "list[Any] | None") -> "tuple[list[dict] | None, str | None]":
    """``work_feeder._read_rows`` 와 동일한 파생(필터링+결손사유) — 순수 함수,
    디스크 접근 없음. 이미 읽은 원본(:func:`_ledger_snapshot`)에서 계산하므로
    이 함수를 몇 번 불러도 새 창이 안 생긴다.

    ⚠️ 두 구현이 갈라지면 안 된다 — ``work_feeder._read_rows`` 의 파생 규칙이
    바뀌면 이것도 같이 봐야 한다.
    """
    if raw is None:
        return None, "read_failed"
    good = [r for r in raw if isinstance(r, dict)]
    if len(good) == len(raw):
        return good, None
    return good, f"non_object_rows={len(raw) - len(good)}"


def pending_queries(
    state_dir: Path | str,
    *,
    _snapshot: "Mapping[str, list[Any] | None] | None" = None,
) -> list[dict[str, Any]]:
    """대기 중(제출됐고 아직 소비 안 된) 질의 행들 — **공개 조회면.**

    # __SLOT_TICK_TRAY_PENDING_2026_08_09__ 이 정의(인박스 − 툼스톤, ``_query_id``
    # 기준 id 집합 차)는 종전엔 :func:`tick_once` 의 (2) 단계 안에만 살았고 private
    # 이었다. 그래서 트레이(PowerShell)가 같은 뺄셈을 **사본으로** 들고 있었는데,
    # 그 사본은 이미 정본과 갈라져 있었다 — id 없는 행을 정본은 내용 해시
    # (``auto_…``)로 각각 세고, 사본은 전부 빈 문자열 한 키로 접었다. 한 값을 두
    # 곳에 적으면 검사 안 받는 쪽이 썩는다. ⇒ 정본을 공개하고 사본을 지웠다
    # (소비자: ``scripts/tick_tray_status.sh`` 의 ``pending`` 필드).
    #
    # ⛔ 읽기 전용이다 — 툼스톤도 원장도 안 쓴다. :func:`tick_once` 가 같은 함수를
    # 쓰므로(아래 (2) 단계) 트레이가 보는 수와 틱이 집는 수는 정의상 같다.
    # ⚠️ 관대하지 않다: ``_consumed_ids`` 는 원장 판독 실패는 접지만 *객체 아닌
    # JSONL 행*에서는 종전 그대로 터진다(HEAD 동작 — 여기서 바꾸지 않는다).
    # 관측 소비자는 그 예외를 "모름"으로 표시해야지 0 으로 접으면 안 된다.
    #
    # __SLOT_TICK_TOCTOU_LEDGER_SNAPSHOT_2026_08_18__ *_snapshot* 은 ``tick_once``
    # 전용 내부 배선이다(공개 계약 아님, 트레이/외부 호출자는 안 넘긴다 — 안 넘기면
    # 이 함수는 종전 그대로 자기 판독을 한다, byte-identical). 넘기면 그 스냅샷의
    # **원본**(비필터링)만 쓴다 — 필터링된 값을 쓰면 위 모듈-레벨 주석이 설명하는
    # crash-on-non-object 계약이 깨진다.
    """
    sd = Path(state_dir)
    if _snapshot is not None:
        raw_inbox = _snapshot.get("inbox_queue")
        inbox: list[Any] = [] if raw_inbox is None else raw_inbox
        raw_consumed = _snapshot.get("bought_ledger")
        consumed = (set() if raw_consumed is None
                    else {str(r.get("id")) for r in raw_consumed if r.get("id") is not None})
    else:
        inbox = _read_inbox(sd)
        consumed = _consumed_ids(sd)
    return [r for r in inbox if _query_id(r) not in consumed]


# __SLOT_STALL_VERDICT_2026_08_09__ 🔴 4라운드: **상류의 crash 두 칸.**
#
# ``tick_once`` 는 인박스와 툼스톤을 급식보다 먼저 읽는데, 위 두 함수는 ``read_jsonl``
# 의 raise 만 잡는다. *유효 JSON 인데 객체가 아닌 줄*(``"x"``/``null``/``[1,2]``)은
# 통과하고 소비부(``_query_id`` · ``r.get("id")``)가 ``AttributeError`` 로 터진다.
# ``tick_once`` 층 굶기기 행렬 실측: 두 칸 CRASH.
#
# ⛔ **깨진 줄을 버리는 것은 여전히 안 한다.** 툼스톤 한 줄을 버리면 이미 소비된
#    질의가 다시 디스패치된다(실지출 재과금) — 3라운드가 crash 를 남긴 이유가 그것이고
#    그 판단은 유효하다. 바뀌는 것은 **crash 냐 행이냐**뿐이다: 게이트 ON 이면
#    디스패치 없이 **이름 붙은 행 하나**를 남기고 돌아온다. 버리는 것도, 사는 것도 없다.
# ⚠️ 잃는 것도 적는다: 프로세스 종료코드가 0 이 되어 **systemd 가 실패로 안 센다.**
#    그래서 ``_swallowed`` 도 같이 부른다(WARNING + census, STRICT 면 그대로 raise).
#: 이 사유의 행. ⛔ ``self_feed_*`` 와 합치지 않는다 — 그건 급식 판정이고 이건
#: **급식에 도달하기 전 원장 고장**이다.
REASON_LEDGER_NON_OBJECT = "ledger_non_object_row"
# __SLOT_R13_LEDGER_2026_08_17__ 🔴 R13 사슬 B — 위 두 REASON 은 "유효 JSON 인데
# 모양이 이상함"만 잡았다. 이 REASON 은 그 위 계층: **판독/파싱 자체가 실패**한
# 경우(``work_feeder._read_rows`` 가 ``rows=None`` 을 돌려주는 경우) — 파일이
# 손상됐거나(JSONDecodeError) 못 읽는(OSError) 경우다. 파일이 그냥 **없는** 것과는
# 다른 사실이다(``_read_rows`` 는 부재를 ``([], None)`` 으로, 즉 정상 콜드스타트로
# 돌려준다 — 이 REASON 은 절대 안 뜬다).
REASON_LEDGER_READ_FAILED = "ledger_read_failed"
# __SLOT_R14_TICKPROFILE_2026_08_17__ 🔴 R14 감사 2순위 — tick 이 켜졌는데
# 안전 프로파일(서명·legacy 차단·TTL·audience·producer 결박)의 다섯 축 중
# 하나라도 깨지면 이 이유로 그 틱 하나만 거부한다(프로세스는 안 죽는다).
REASON_INSECURE_TICK_PROFILE = "insecure_tick_profile"
#: 상류가 **읽는** 젤 원장들(축 이름 → 경로). ⛔ 손으로 두 줄 적지 않는다 —
#: 위 ``_INBOX_REL``/``_CONSUMED_REL`` 상수를 그대로 참조한다.
_UPSTREAM_RELS: "dict[str, tuple[str, ...]]" = {
    "inbox_queue": _INBOX_REL,
    "bought_ledger": _CONSUMED_REL,
}


def _non_object_ledgers(
    state_dir: Path, *, _snapshot: "Mapping[str, list[Any] | None] | None" = None,
) -> "list[str]":
    """상류가 곧 읽을 원장 중 **객체 아닌 줄**이 있는 것들의 축 이름(정렬).

    ⛔ 여기서 파일을 고치지 않는다 — 사실만 센다. 판독 자체가 실패한 원장
    (``rows is None``)은 여기 안 든다: 그건 :func:`_failed_ledgers` 의 몫이다
    (아래) — 이 함수는 "읽혔지만 모양이 틀린 줄"만, 그 함수는 "아예 못 읽음"만 잡는다.

    __SLOT_TICK_TOCTOU_LEDGER_SNAPSHOT_2026_08_18__ *_snapshot* 이 주어지면(항상
    ``tick_once`` 가 넘긴다) 새로 안 읽는다 — 이미 읽은 원본에서 파생만 한다.
    단독 호출(스냅샷 없이)은 종전처럼 자기 판독을 한다(공개 표면 아님이지만
    byte-identical 유지).
    """
    out: list[str] = []
    if _snapshot is not None:
        for axis in _UPSTREAM_RELS:
            rows, note = _axis_good_and_note(_snapshot.get(axis))
            if rows is not None and note is not None:
                out.append(axis)
        return sorted(out)
    for axis, rel in _UPSTREAM_RELS.items():
        rows, note = work_feeder._read_rows(state_dir.joinpath(*rel))
        if rows is not None and note is not None:
            out.append(axis)
    return sorted(out)


def _failed_ledgers(
    state_dir: Path, *, _snapshot: "Mapping[str, list[Any] | None] | None" = None,
) -> "list[str]":
    """상류가 곧 읽을 원장 중 **판독/파싱 자체가 실패**한 것들의 축 이름(정렬).

    __SLOT_R13_LEDGER_2026_08_17__ ``_read_inbox``/``_consumed_ids`` 가 지금 접는
    바로 그 사실(``rows is None``)을 **미리** 드러낸다 — 이걸 ``tick_once`` 가
    ``pending_queries()`` 호출 **전**에 검사하면, 손상된 ``consumed.jsonl`` 이
    "소비한 게 없다"로 둔갑해 과거 objective 를 재디스패치하는 사슬이 끊긴다
    (디스패치 자체가 이 틱에서 일어나지 않으므로).

    ⛔ 파일 **없음**은 여기 안 든다 — ``_read_rows`` 가 부재를 ``([], None)`` 으로
    돌려주므로(정상 콜드스타트), 이 함수는 파일이 **있는데** 깨진 경우만 잡는다.

    ⚠️ **두 축은 비대칭이다** — 손으로 대칭을 맞추지 않는다:

    - ``inbox_queue`` (``queries.jsonl``) 판독 실패는 **항상** 잡는다(작업지시서
      규율: "``_read_inbox`` 도 같은 규율로 — 판독 실패 시 후보 없음이 아니라 거부").
      인박스를 못 읽으면 뭐가 대기 중인지 원천적으로 모른다.
    - ``bought_ledger`` (``consumed.jsonl``) 판독 실패는 인박스에 **실제로 원행이
      있을 때만** 잡는다. 인박스가 정상적으로 비어 있으면(``[]``) 소비 원장이
      깨져도 재디스패치될 과거 행 자체가 없어 재과금 위험이 0 이다 — 그 경우는
      이미 출하된 self-feed/stall-verdict 경로(``progress_stall.REASON_BOUGHT_
      UNREADABLE``, ``work_feeder.feed_one``)가 "급식은 막되 틱은 안 죽인다"를
      맡는다. 여기서 그 계약을 덮어쓰면 이미 테스트된 동작이 깨진다(실측 회귀:
      ``test_progress_stall_2026_08_09.py::test_b0b_a_garbled_ledger_does_not_
      kill_the_tick`` · ``test_r9_starving_the_bought_arm_is_unknown_not_a_
      cold_start``) — 그 축은 그 축의 방어를 쓰고, 이 축(R13 사슬 B)은 "인박스에
      실제로 재과금될 과거 질의가 있는" 좁은 위험창만 막는다.

    __SLOT_TICK_TOCTOU_LEDGER_SNAPSHOT_2026_08_18__ *_snapshot* 이 주어지면(항상
    ``tick_once`` 가 넘긴다) 새로 안 읽는다 — 바로 이 재판독이 R3 가 실측으로 잡은
    TOCTOU 창의 절반이었다(나머지 절반은 ``pending_queries`` 의 재판독).
    """
    if _snapshot is not None:
        inbox_rows, _ = _axis_good_and_note(_snapshot.get("inbox_queue"))
        bought_rows, _ = _axis_good_and_note(_snapshot.get("bought_ledger"))
    else:
        inbox_rows, _ = work_feeder._read_rows(state_dir.joinpath(*_INBOX_REL))
        bought_rows, _ = work_feeder._read_rows(state_dir.joinpath(*_CONSUMED_REL))
    out: list[str] = []
    if inbox_rows is None:
        out.append("inbox_queue")
    if bought_rows is None and inbox_rows:  # 빈/부재 인박스면 재과금 위험이 없다
        out.append("bought_ledger")
    return sorted(out)


def _ledger_fault_edge(state_dir: Path, axes: "list[str]", *,
                       reason: str = REASON_LEDGER_NON_OBJECT) -> bool:
    """이 원장 고장이 **처음 관측된 것**인가. 같은 고장의 반복이면 False.

    ⚠️ 10분 cron × 고장 유지 = 하루 144행이면 원장이 사람의 결정이 아니라 **cron
    주기를 재게 된다**(사람 정지 행에 대해 이 파일이 이미 내린 판단과 같다).
    ⛔ 그 억제를 쿨다운 앵커에는 절대 못 쓴다(1라운드 RED) — 여기 행은 앵커가
    아니다. 이 경로는 스캔을 **지불하지 않으므로** 억제해도 태울 CPU 가 없다.

    ⛔ tick 원장 자체를 못 읽으면 **안 쓴다**(``True`` 를 못 낸다): 대조할 수 없는데
    쓰면 하루 144행이 된다. 그 경우 사실은 census/WARNING 채널에만 남는다.

    *reason* 은 어느 고장 클래스의 전이인지 구분한다(``REASON_LEDGER_NON_OBJECT``
    기본값 유지 — R13 이 새로 부르는 자리는 ``REASON_LEDGER_READ_FAILED`` 를 넘긴다).
    """
    rows, _ = work_feeder._read_rows(state_dir.joinpath(*_TICKLOG_REL))
    if rows is None:
        return False
    if not rows:
        return True                          # 첫 관측
    last = rows[-1]
    if last.get("reason") != reason:
        return True                          # 사이에 다른 사건이 있었다 = 새 전이
    # ⛔ ``or []`` 로 접지 않는다 — 목록이 없거나 모양이 다르면 **같은 고장이라고
    #    말할 수 없다** ⇒ 기록한다(보수적 방향: 잃는 것은 원장 한 줄뿐이다).
    prev = last.get("ledgers")
    return not (isinstance(prev, list) and prev == axes)


# __SLOT_R14_TICKPROFILE_2026_08_17__ ──────────────────────────────────────────
# R14 감사 2순위: "AGI_V8_TICK_ENABLED=true + AGI_V8_TICK_AUTH_SIG_ENABLED
# 미설정 + AGI_V8_GATE_INVARIANTS_ENFORCED 미설정" 이 가능했다 — 마스터 안전조합
# 검사(policy/gate_invariants.py)가 이 위험을 **알고 있지만** 자기 게이트가
# default-OFF 라 강제하지 않는다. 그 마스터 게이트를 켜라고 요구하는 대신, tick
# 소비 진입점(``tick_once``) 자신이 자기 전제조건을 매 틱 검사한다 — 환경 관례가
# 아니라 구조적 전제조건으로 승격.
#
# 다섯 축, 전부 ``tick_auth`` 의 canonical 판별기를 그대로 부른다(사본 없음 —
# 두 곳에 같은 조건을 적으면 검사 안 받는 쪽이 썩는다, 이 레포 실측 결함):
#   * sig            — tick_auth.sig_enabled() 가 True
#   * legacy_allowed — tick_auth.allow_legacy() 가 False
#   * ttl            — tick_auth.ttl_seconds() 가 None 아니고 > 0 (유한)
#   * audience_bind  — tick_auth.bind_audience_enabled() 가 True
#   * producer_bind  — tick_auth.bind_producer_enabled() 가 True
#
# ``sig`` 항목은 ``policy/gate_invariants.py`` 에 이미 있는
# ``tick_enabled_requires_auth_sig`` 규칙을 그대로 빌린다 — 같은 조건을 여기
# 두 번째로 안 적는다. ``gate_invariants.evaluate()`` 는 **마스터 게이트
# (``AGI_V8_GATE_INVARIANTS_ENFORCED``) 와 무관하게 항상 전체 표를 평가한다**
# (모듈 자체 계약 — "callers that want a read-only report... don't need the
# master gate on") — 그래서 여기서 그 함수를 불러도 요구사항 3(마스터 게이트에
# 의존하지 말 것)이 깨지지 않는다. 우회하는 것은 오직 ``check_or_raise()`` 가
# 앞단에서 거는 ``enforcement_enabled()`` 체크뿐이고, 우리는 그 함수를 아예
# 안 부른다.
# __SLOT_SECURE_PROFILE_CAMPAIGN_BIND_2026_08_24__ 여섯 번째 축 — campaign_bind.
#
# R19/R20 적대검증이 지목한 자리: tick 3축(TICK_ENABLED + AUTH_SIG + TICK_FEED)이
# 전부 live true 인데 ``bind_campaign_enabled`` 만 미선언이면, 서명이 objective 는
# 덮고 **정산 대상**(campaign_id/card_id/card_config_path + 다이제스트 3종)은 안
# 덮는다 ⇒ 토큰 없는 동일-UID 큐 쓰기자가 실행 목표는 그대로 둔 채 어느 카드로
# 정산할지만 바꿔치기해도 HMAC 이 유효하다. 이 모듈이 이미 적어둔 원칙
# ("인가 표면은 결정 표면과 같아야 한다", tick_auth.canonical_msg 독스트링)의
# campaign 축 판이고, 나머지 다섯 축과 **같은 등급**이라 같은 표에 들어가야 한다.
#
# ⚠️ 착지 순서가 있다(라이브 프로브로 검거): 이 축을 .env arm **없이** 넣으면
#    ``_secure_profile_violations()`` 가 ['campaign_bind'] 를 내고 :1294 가 모든
#    틱을 스킵한다(insecure_tick_profile) — 즉 tick 레인이 통째로 멈춘다.
#    그래서 arm(2026-08-24) 뒤에 이 축을 넣었고, 착지 시점 라이브 판정은 [] 다.
_SECURE_PROFILE_AXES = ("sig", "legacy_allowed", "ttl", "audience_bind",
                        "producer_bind", "campaign_bind")


def _secure_profile_violations(env: "Mapping[str, str] | None" = None) -> "list[str]":
    """__SLOT_R14_TICKPROFILE_2026_08_17__ 깨진 전제조건 이름들(정렬됨).

    빈 리스트 = 다섯 축 전부 안전. 순수 함수 — env 를 읽기만 하고, 절대 쓰지
    않고, 디스크를 안 건드린다(``gate_invariants.evaluate()`` 와 같은 계약).

    __SLOT_R14_TICKPROFILE_2026_08_17__ 🔴 2차 라운드: ``ttl`` 축은
    ``tick_auth.ttl_seconds()`` 를 **그대로** 본다 — 그 함수가 이제 미선언 TTL 을
    (프로파일 ON 이면) ``DEFAULT_SECURE_TTL_SEC`` 로 스스로 채우므로, 여기서
    "미선언은 OK" 를 따로 적지 않는다. 여기서 따로 적었다면 판별기(여기)는
    통과하는데 실제 강제(``tick_auth._fresh``)는 TTL 을 하나도 안 본다는
    어긋남이 생겼을 것 — 그 함정을 라이브 `.env` 재실측이 착지 직전에 잡았다.
    """
    e = os.environ if env is None else env
    out: list[str] = []
    if any(v.rule_id == "tick_enabled_requires_auth_sig"
           for v in _gate_invariants.evaluate(e)):
        out.append("sig")
    if tick_auth.allow_legacy(e):
        out.append("legacy_allowed")
    _ttl = tick_auth.ttl_seconds(e)
    if _ttl is None or _ttl <= 0:
        out.append("ttl")
    if not tick_auth.bind_audience_enabled(e):
        out.append("audience_bind")
    if not tick_auth.bind_producer_enabled(e):
        out.append("producer_bind")
    # __SLOT_SECURE_PROFILE_CAMPAIGN_BIND_2026_08_24__ 🔴 이 축만 조건부다.
    #
    # 위 두 축(audience/producer)은 **default-true** 라 미선언이 곧 안전이다.
    # campaign 결박은 **default-OFF** 이고 그건 보안 판단이 아니라 **이행** 때문이다
    # (이미 서명돼 큐에 앉은 campaign 행이 켜는 순간 거부된다 — tick_auth
    # ENV_BIND_CAMPAIGN 주석). 그래서 무조건 재면 미선언 = 위반 = **틱 정지**가 되고,
    # 그건 이 트랙이 명시적으로 설계 목표로 삼은 것("운영자가 SIG 하나만 켜고
    # 나머지 게이트의 존재를 몰라도 되게" — test_live_shaped_config_does_not_
    # stop_the_loop 독스트링)을 정면으로 깬다. **그 테스트가 내 초판 설계를 잡았다.**
    #
    # 그래서 **보호 대상 표면이 실제로 살아 있을 때만** 잰다: 캠페인 급식이 꺼져
    # 있으면 정산 결정 표면 자체가 없고, 없는 표면에 인가를 요구하는 건 과잉이다
    # (이 모듈의 원칙은 "인가 표면 = 결정 표면"이지 "인가 표면 ⊇ 모든 게이트"가 아니다).
    # 급식이 켜져 있으면(라이브 형상) 종전대로 결박을 요구한다.
    if _campaign_feed_on(e) and not tick_auth.bind_campaign_enabled(e):
        out.append("campaign_bind")
    return sorted(out)


def _campaign_feed_on(e: "Mapping[str, str]") -> bool:
    """캠페인 급식이 켜져 있는가 — 게이트 이름의 **정본을 import** 한다.
    ⛔ 문자열 사본 금지(두 곳에 적으면 검사 안 받는 쪽이 썩는다, 이 레포 실측)."""
    try:
        from agi_v8_1.runtime.campaign_registry import ENV_ENABLED as _FEED_ENV
    except Exception as _ff_exc:  # noqa: BLE001 — 판별 불가는 축을 재지 않는다
        _swallowed(_ff_exc, site="runtime.tick_runner._campaign_feed_on:import",
                   category="config")
        return False
    return str(e.get(_FEED_ENV, "")).strip() in ("true", "1")


def _secure_profile_edge(state_dir: Path, violated: "list[str]") -> bool:
    """이 불안전 조합이 **처음 관측된 것**인가. 같은 조합의 반복이면 False.

    ``_ledger_fault_edge`` 와 같은 원칙(10분 cron × 고장 유지가 하루 144행으로
    원장을 cron 주기계로 만드는 것을 막는다) — 필드 이름만 ``ledgers`` 대신
    ``violated`` 를 본다(이 축은 원장이 아니라 env 조합이 사실이라).
    """
    rows, _ = work_feeder._read_rows(state_dir.joinpath(*_TICKLOG_REL))
    if rows is None:
        return False
    if not rows:
        return True                          # 첫 관측
    last = rows[-1]
    if last.get("reason") != REASON_INSECURE_TICK_PROFILE:
        return True                          # 사이에 다른 사건이 있었다 = 새 전이
    prev = last.get("violated")
    return not (isinstance(prev, list) and prev == violated)


def _tombstone(state_dir: Path, qid: str, reason: str, *, now: float) -> None:
    row: dict[str, Any] = {"id": qid, "reason": reason, "ts": now}
    # __SLOT_LEDGER_JOIN_2026_08_08__ 소비는 **사이클 안**에서 일어난다(제출과 달리).
    # ⛔ 문맥에 기대지 않고 `_cycle_id_for(now, qid)` 로 **직접 계산**한다 — 툼스톤은
    #    사이클 진입 전에도 찍힐 수 있고(스킵/거절 경로), 그 경우 문맥은 비어 있다.
    #    같은 (now, qid) 라 값은 사이클과 축자 일치한다.
    try:
        from agi_v8_1.runtime.ledger_join import join_keys

        row.update(join_keys(cycle_id=_cycle_id_for(now, qid)))
    except Exception as _join_exc:  # noqa: BLE001
        _swallowed(_join_exc, site="runtime.tick_runner._tombstone:join_keys",
                   category="telemetry")
    atomic_append_jsonl(state_dir.joinpath(*_CONSUMED_REL), row)


def _log_tick(state_dir: Path, record: dict[str, Any]) -> None:
    atomic_append_jsonl(state_dir.joinpath(*_TICKLOG_REL), record)


# __SLOT_HUMAN_HALT_TICK_2026_08_06__ ─────────────────────────────────────────


def _log_tick_safe(state_dir: Path, record: dict[str, Any]) -> dict[str, Any]:
    """정지 기록이 실패해도 **정지 자체는 성립한다**.

    ⚠️ 디스크가 차거나 젤이 RO 면 ``_log_tick`` 이 던진다. 그때 예외가 위로
    올라가면 ``tick.sh`` 가 비정상 종료로 보이고, 더 나쁘게는 호출부가 그걸
    잡아 *"에이전트가 터졌다"* 로 기록한다. 정지는 기록보다 우선한다.

    ⛔ 그렇다고 조용히 넘어가지도 않는다 — 삼킴은 이름을 붙여 세고, 반환 레코드에
    ``log_write_error`` 를 실어 **기록이 빠졌다는 사실 자체에 이름을 붙인다.**
    """
    try:
        _log_tick(state_dir, record)
    except Exception as exc:  # noqa: BLE001 — 기록 실패가 정지를 막으면 안 된다
        _swallowed(exc, site="runtime.tick_runner._log_tick_safe", category="persist")
        record = dict(record, log_write_error=_format_exception_for_log(exc))
    return record


_HALT_ACK_REL = ("tick", "halt_ack.json")


def _halt_edge(state_dir: Path, rec: dict[str, Any]) -> bool:
    """이 정지가 **처음 관측된 것**인가. 같은 정지의 반복이면 False.

    ⚠️ 이 파일은 **중복 억제용**이지 확인 신호(ack)가 아니다. 젤 안에 있으므로
    루프가 지울 수 있고, 지워지면 결과는 "행이 하나 더 생김"뿐이라 안전한
    방향이다. 사람에게 *"정말 멈췄다"* 를 보증하는 신호는 레포 밖 root 원장
    (``halt_sentinel.ACK_LEDGER``)의 몫이고 여기서 흉내내지 않는다.
    """
    key = [rec.get("sentinel"), rec.get("stop_ts"), rec.get("reason"), rec.get("error")]
    path = state_dir.joinpath(*_HALT_ACK_REL)
    try:
        if path.exists() and json.loads(path.read_text(encoding="utf-8")).get("key") == key:
            return False
    except (OSError, ValueError, TypeError) as exc:
        # 못 읽으면 **기록하는 쪽**으로 넘어진다 — 중복 한 행이 누락 한 행보다 낫다.
        _swallowed(exc, site="runtime.tick_runner._halt_edge", category="persist")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"key": key, "observed_ts": rec.get("ts")}),
                        encoding="utf-8")
    except OSError as exc:
        _swallowed(exc, site="runtime.tick_runner._halt_edge:write", category="persist")
    return True


def _human_halt_row(st: "halt_sentinel.HaltState", now: float, *,
                    pending: "int | None" = None) -> dict[str, Any]:
    """사람 정지의 틱 로그 행. **게이트 없이** 항상 쓴다.

    ⚠️ ``sentinel_state`` 는 못 읽었을 때 ``None`` 이다 — ``False`` 로 접으면
    "사람이 안 눌렀다"와 "눌렀는지 모른다"가 같은 칸에 들어간다.
    """
    return {
        "dispatched": False,
        # 못 읽어서 멈춘 것과 파일을 보고 멈춘 것은 **다른 사실**이다.
        "reason": "human_halt" if st.readable else "human_halt_unknown",
        "sentinel": st.sentinel,
        "sentinel_tier": st.tier,
        "sentinel_state": True if st.readable else None,
        "sentinel_kind": st.entry_kind,
        "stop_ts": (st.mtime_ns / 1e9) if st.mtime_ns is not None else None,
        "tier_is_guaranteed": st.tier_is_guaranteed,
        "error": st.error,
        "pending": pending,
        "ts": now,
    }


def _cycle_id_for(now: float, qid: str) -> str:
    """틱 하나의 신원. ⛔ 질의 id 를 자르지 않는다.

    __SLOT_CYCLE_ID_NO_TRUNCATION_2026_08_07__ 🔴 **적대검증이 실측으로 잡았다.**

    종전 ``f"tick_{int(now)}_{qid[:8]}"`` 는 ``qid = "q" + epoch초(10자리)`` 를
    ``"q" + 7자리`` 로 잘랐다 ⇒ **같은 100초 안에 들어온 질의는 같은 이름을 받는다.**
    라이브 큐 실측(2026-08-07): 질의 25개 중 고유 접두 20개, **충돌 5종** ::

        q1786066533 · q1786066616  →  둘 다 "q1786066"   (사용자 질의 2건)
        q1786053837 · q1786053854  →  둘 다 "q1786053"   (큐 제출 2건)

    ``int(now)`` 가 붙어 있어 전체 충돌은 드물지만, 그건 **우연한 방어**다 —
    같은 초에 두 질의가 소비되면 사이클 신원이 그대로 겹치고, 비용·기억·
    ``call_id`` 짝짓기가 전부 남의 것과 섞인다. 자를 이유가 애초에 없었다.
    """
    return f"tick_{int(now)}_{qid}"


# __SLOT_HUMAN_AXIS_2026_08_08__ ─────────────────────────────────────────────
# HUMAN=0 은 "사람이 안 도왔다"가 아니라 **"안 쟀다"** 였다 —
# ``episode_log.emit_human`` 은 2026-08-06 부터 프로덕션 호출자 0 이었고, 그 0 을
# 리더보드가 만점 자율성으로 읽는다. 여기가 **첫 호출자**다: 사람-제출 질의를
# 소비해 사이클을 여는 순간이 "사람이 루프를 조향했다"가 성립하는 자리다.
# default-OFF (``AGI_V8_HUMAN_AXIS_ENABLED``). OFF = 행도 호출도 없음(byte-identical).
_ENV_HUMAN_AXIS = "AGI_V8_HUMAN_AXIS_ENABLED"


def _human_axis_enabled() -> bool:
    return _truthy(_ENV_HUMAN_AXIS)


def _producer_of(row: dict[str, Any]) -> str:
    """질의 행의 생산자. 우선순위: 명시 ``producer`` > ``source`` > id 접두 > human.

    ⛔ 기본값이 "human" 인 이유: 사람 채널(tray/tick_submit)은 HUMAN_AXIS 가
    꺼져 있으면 ``producer`` 키를 안 남기므로, 그 행을 human 으로 접는 것이 맞다.
    모르는 행을 human 으로 접는 방향은 자율성 점수에 **불리**한 쪽 — 부풀림의
    반대 방향으로만 틀린다.

    ⚠️ 2026-08-21 정정: 여기 있던 "오늘 큐의 쓰기자는 둘뿐" 이라는 근거는
    **낡았다**. ``goal_campaign_feed`` 가 세 번째 쓰기자이고, 그 행은
    ``source="goal_campaign"`` 이라 위 두 분기를 전부 빗나가 **human 으로
    접혔다**(campaign 목표가 사람 개입으로 계수됨). 수리는 이 폴백을 바꾸는
    게 아니라 **그 생산자가 자기 이름을 심게** 한 것이다
    (``__SLOT_PRODUCER_REQUIRED_2026_08_21__``) — 폴백은 사람 채널에 대해
    여전히 옳고, 생산자가 스스로를 밝히는 쪽이 서명에도 포함되기 때문이다.
    """
    p = row.get("producer")
    if isinstance(p, str) and p:
        return p
    if row.get("source") == "work_feeder":
        return "work_feeder"
    if str(row.get("id") or "").startswith(work_feeder.ID_PREFIX):
        return "work_feeder"
    return "human"


def _emit_human_for_query(sd: Path, row: dict[str, Any], qid: str,
                          cycle_id: str, now: float) -> None:
    """사람-제출 질의의 소비에 HUMAN 행을 적는다 (큐-소비 쪽 첫 emit_human 호출자).

    ``emit_human`` 자체도 episode 게이트를 본다 — 이중 게이트는 의도다(축 게이트가
    켜져도 episode 원장이 꺼져 있으면 안 쓴다, fail 방향 = 기록 안 하는 쪽).
    """
    if not _human_axis_enabled():
        return
    if _producer_of(row) != "human":
        return
    try:
        from agi_v8_1.runtime import episode_log as _el

        _el.emit_human(sd, cycle_id=cycle_id,
                       note=f"human-submitted query {qid} dispatched", ts=now)
    except Exception as exc:  # noqa: BLE001 — 계측이 디스패치를 죽이면 안 된다
        _swallowed(exc, site="runtime.tick_runner._emit_human_for_query",
                   category="telemetry")


def _real_dispatch(state_dir: Path, objective: str, cycle_id: str, *,
                   budget_usd: float, now_ts: float) -> dict[str, Any]:
    """Public admission delegates autonomy to the optional T9 implementation."""
    return resolve_payload(PayloadPort(9, "agi_v8_1.runtime.autonomous_tick_payload", "dispatch"))(
        _payload_sys.modules[__name__], state_dir, objective, cycle_id,
        budget_usd=budget_usd, now_ts=now_ts)

def _build_review_fn(state_dir: Path | None = None) -> "tick_review.ReviewFn | None":
    """Real reviewer construction belongs to T9; safety stays in this caller."""
    from agi_v8_1.providers.base import providers_enabled
    if not providers_enabled():
        logger.info("tick_review: providers disabled -> no real review_fn")
        return None
    return resolve_payload(PayloadPort(9, "agi_v8_1.runtime.autonomous_tick_payload", "build_review_fn"))(
        _payload_sys.modules[__name__], state_dir)

DispatchFn = Callable[..., dict[str, Any]]


def _daily_cap_block_rec(verdict: dict[str, Any], qid: str, now: float) -> dict[str, Any]:
    """공용 원장 행 — daily_cost_cap 이 이 사이클을 막을 때 두 호출부(최초 판정
    (4), :func:`cap_reread_after_review_enabled` 재판독)가 같은 모양으로 적는다.
    __SLOT_TICK_SURFACES_THE_CAP_VERDICT_2026_08_03__ 의 필드 계약을 그대로 지킨다.
    """
    return {"dispatched": False, "reason": verdict["reason"], "id": qid,
            "ts": now, "spent_usd": verdict["spent_usd"],
            "cap_usd": verdict["cap_usd"],
            "unmeasured_rows": verdict["unmeasured_rows"],
            "spend_is_complete": verdict["spend_is_complete"],
            "rows_considered": verdict["rows_considered"]}


def tick_once(state_dir: Path | str, *, dispatch_fn: DispatchFn | None = None,
              now_ts: float | None = None) -> dict[str, Any]:
    """Run a single tick. Returns a JSON-able outcome dict.

    *state_dir* is REQUIRED and is the jail every read/write/dispatch is confined
    to — there is intentionally no tmpdir default here. *dispatch_fn* defaults to
    the real orchestrator call; tests inject a fake.
    """
    sd = Path(state_dir)
    now = float(now_ts) if now_ts is not None else time.time()
    dispatch = dispatch_fn or _real_dispatch

    # (0) __SLOT_HUMAN_HALT_TICK_2026_08_06__ 사람이 누른 정지. **env 게이트보다
    # 앞이다** — 사람의 결정이 설정보다 우선하고, 정지 사실은 반드시 기록된다.
    #
    # ⛔ 이 검사는 정지를 **보장하지 않는다**. 보장은 레포 밖 root 강제자의 몫이고
    #    (``runtime/halt_sentinel`` 독스트링 참조), 여기가 하는 일은 그 정지가
    #    원장에서 ``unknown`` 으로 뭉개지지 않게 하는 것이다.
    #
    # 여기서 멈추면 안전한 이유: 인박스를 아직 안 읽었고, tombstone 을 안 찍었고,
    # ``dispatch_fn`` 은 구조적으로 호출 불가다(모듈 독스트링의 "gate off ⇒
    # dispatch_fn is never called" 속성이 **강해지기만** 한다). 질의는 pending 으로
    # 남으므로 소실도 재과금도 없다.
    #
    # __SLOT_TICK_LEASE_2026_08_18__ 🔴 R3 잔여 ② 수리(2026-08-18 재수리) — lease
    # 획득은 **(0)/(1) 두 검사 다음**에만 온다. 처음 배선은 이 검사들보다 앞에
    # lease 를 잡아 ``tick_lease.enabled()`` 만 켜고 ``AGI_V8_TICK_ENABLED`` 는
    # 그대로 둔(=게이트 OFF) 조합에서 ``tick/lease.json(.lock)`` 을 실제로 만들어
    # 버렸다 — ``test_main_gate_off_prints_and_writes_nothing`` 이 못 박은
    # "게이트 OFF ⇒ 아무것도 안 쓴다" 계약(바로 아래 ② 주석)을 정면으로 어겼다.
    # 이제는 halt/gate 둘 다 통과한 뒤에만 lease 를 만들므로, 두 검사 중 하나라도
    # 함수를 조기 반환시키면 lease 관련 파일은 디스크에 전혀 안 생긴다.
    _halt = halt_sentinel.state()
    if _halt.halted:
        rec = _human_halt_row(_halt, now)
        # ⚠️ **전이에서만 기록한다.** 10분 cron × 정지 유지 = 하루 144행이면
        # 원장이 사람의 결정이 아니라 cron 주기를 재게 된다. 반환값은 항상
        # 완전하고(호출자가 판단에 쓴다), 억제되는 것은 *기록*뿐이다.
        if _halt_edge(sd, rec):
            rec = _log_tick_safe(sd, rec)
        return rec

    # (1) master kill-switch — silent no-op when disarmed.
    #
    # ⚠️ **여기는 일부러 무기록으로 둔다**(2026-08-06 검토). 초안은 "6종 종료 사유
    # 중 운영자 결정만 침묵한다"며 기록을 붙이려 했는데, 세 가지가 걸린다:
    #   ① 이 행은 **매 틱** 나온다 — 10분 cron 이면 하루 144행. 그러면 원장이
    #      사람의 결정이 아니라 **cron 주기를 재게 된다**(적대검증이 정지 행에
    #      대해 경고한 것과 같은 병).
    #   ② 무장 해제된 기계에서 젤 디렉터리를 만들어버린다. 기존 테스트
    #      ``test_main_gate_off_prints_and_writes_nothing`` 이 그걸 계약으로 박아뒀다.
    #   ③ "사람 정지가 unknown 으로 뭉개진다"는 문제는 **sentinel 경로(0)** 가
    #      푼다. 그건 사람이 실제로 파일을 만들었을 때만 발화한다.
    # ⇒ gate_off 기록은 **전이 감지가 붙는 날** 같이 온다(§ 백로그).
    if not tick_enabled():
        return {"dispatched": False, "reason": "gate_off", "now_ts": now}

    if os.environ.get("AGI_V8_PUBLIC_TIER_GATE_ENABLED", "false").strip().lower() == "true":
        from agi_v8_1.policy.tier_gate import require_tier
        require_tier(9)

    # __SLOT_VERIFY_GATE_PRESEAL_CAPTURE_2026_08_20__ ②강화안 — 게이트 ON일
    # 때만, 이 프로세스가 아직 host user namespace 안에 있는(=
    # ``tick_deadline``/``tick_descendant_seal`` 이 아직 한 번도 unshare 하지
    # 않은) 지금 이 자리에서 신원 캡처를 남긴다. 반드시 **여기**여야 하는
    # 이유(A6): 이 프로세스가 seal 을 처음 넘는 자리는 dispatch(§(4) 아래)가
    # 아니라 review 트랜잭션이고, exec_arm 정산 단계도 dispatch 이후 별도로
    # unshare 한다 — 그 어느 쪽보다 늦게 캡처하면 첫 봉인 자식은 이미 캡처
    # 없이 돈 뒤다. ``capture_host_git_identity()`` 자체가 ns 별
    # idempotent(no-op 재호출 안전)이므로 이 자리에 정확히 1번 두면 된다.
    # OFF(default)면 이 블록 전체가 스킵되고 ``si_lanes.verify_gate`` 는
    # 아예 import 되지 않는다 — 08-20 착지분과 byte-identical.
    if _preseal_git_identity_capture_enabled():
        try:
            _capture_host_git_identity = resolve_payload(PayloadPort(
                5, "agi_v8_1.si_lanes.verify_gate", "capture_host_git_identity",
            ))
            _capture_host_git_identity()
        except Exception as _capture_exc:  # noqa: BLE001 — capture must never
            # crash a tick; the consumer (verify_gate._snapshot_git) fails
            # closed on its own whenever no usable capture is present, so a
            # capture failure here degrades to "no capture", not a crash.
            _swallowed(
                _capture_exc,
                site="runtime.tick_runner.tick_once:preseal_git_identity_capture",
                category="verify",
            )

    # __SLOT_TICK_LEASE_2026_08_18__ R3 잔여 ② — 이 틱의 소유권을 임대한다.
    # default-OFF(``AGI_V8_TICK_LEASE_ENABLED``) — OFF 면 이 블록 전체가 스킵되고
    # 아래는 패치 이전과 byte-identical. ON 이면 ``scripts/tick.sh`` 의 flock 이
    # 못 덮는 자리(직접/수동/미래 non-cron 호출자)까지 단일 흐름을 보장한다.
    # 다른 소유자가 살아있는 kernel flock 을 쥐고 있으면 **아무 것도 읽지 않고**
    # 즉시 반환한다(인박스/툼스톤 검사 전부 전이다 — 재과금 위험 0). 소유 프로세스가
    # 죽으면 커널이 fd/lock 을 즉시 회수하므로 다음 acquire 가 자동 인수한다. JSON
    # TTL 은 진단값일 뿐, 살아 있는 소유자를 밀어내는 권한이 아니다.
    #
    # ⚠️ 위 (0)/(1) 을 이미 통과했으므로 — halt 도 안 걸렸고 게이트도 켜져 있다 —
    # 여기서부터의 lease 파일 쓰기는 "무장된 기계가 실제로 일할 채비를 하는"
    # 시점에만 일어난다. gate-off/halt 조기 반환 경로는 이 지점에 절대 안 닿는다.
    # Deadline 은 Linux fork/subreaper transaction이라 main-thread,
    # single-threaded/no-preexisting-child 컨텍스트에서만 안전하게 제공된다.
    # lease/원장/인박스에 손대기 전에 typed fail-closed 로 거부한다.
    if tick_deadline.enabled():
        try:
            tick_deadline.assert_supported_context()
        except tick_deadline.DeadlineUnavailable as _deadline_unavailable:
            _swallowed(
                _deadline_unavailable,
                site="runtime.tick_runner.tick_once:deadline_unavailable",
                category="config",
            )
            return {
                "dispatched": False,
                "reason": "deadline_unavailable",
                "now_ts": now,
                "error": _format_exception_for_log(_deadline_unavailable),
            }

    _lease_ctx = None
    if tick_lease.enabled():
        # ``now`` is an event timestamp and can be injected by tests/callers.
        # Lease authority must use the real acquisition clock/kernel fd instead.
        _lease_ctx = tick_lease.acquire(sd)
        try:
            _lease_ctx.__enter__()
        except tick_lease.LeaseBusy as _busy:
            return {"dispatched": False, "reason": "lease_busy", "now_ts": now,
                    "held_by": _busy.holder.get("owner"),
                    "lease_expires_ts": _busy.holder.get("expires_ts")}
        except tick_lease.LeaseUnavailable as _lease_unavailable:
            _swallowed(
                _lease_unavailable,
                site="runtime.tick_runner.tick_once:lease_unavailable",
                category="persist",
            )
            return {
                "dispatched": False,
                "reason": "lease_unavailable",
                "now_ts": now,
                "error": _format_exception_for_log(_lease_unavailable),
            }
    try:
        # (1z) __SLOT_R14_TICKPROFILE_2026_08_17__ 🔴 R14 감사 2순위 — tick 이
        # enabled 인데 안전 프로파일의 다섯 축(서명·legacy 차단·TTL·audience/producer
        # 결박) 중 하나라도 깨지면 여기서 거부한다. **프로세스는 안 죽는다** — 명명된
        # 이유 행만 남기고 이 틱만 스킵한다(디스패치도, 인박스/툼스톤 읽기도 아직
        # 전이다 — 재과금 위험 0). ``AGI_V8_GATE_INVARIANTS_ENFORCED`` 를 안 본다
        # (요구사항: 그 마스터 게이트가 꺼져 있어도 이 전제조건은 강제돼야 한다).
        # OFF(``AGI_V8_TICK_SECURE_PROFILE=false``)면 이 블록 전체가 스킵되고 아래는
        # 패치 이전과 byte-identical(OFF-parity).
        if secure_profile_enabled():
            _violated = _secure_profile_violations()
            if _violated:
                _swallowed(
                    RuntimeError(f"insecure tick profile — violated: {_violated}"),
                    site="runtime.tick_runner.tick_once:secure_profile",
                    category="config",
                )
                _rec = {"dispatched": False, "reason": REASON_INSECURE_TICK_PROFILE,
                        "ts": now, "violated": _violated}
                return (_log_tick_safe(sd, _rec)
                        if _secure_profile_edge(sd, _violated) else _rec)

        # (1a) __SLOT_R13_LEDGER_2026_08_17__ 🔴 R13 사슬 B — 상류가 읽을 원장(인박스·
        # 소비 툼스톤) 중 **판독/파싱 자체가 실패**한 것이 있나 (1b) 보다 먼저 본다:
        # (1b) 는 "읽혔지만 모양이 이상함"만 잡고 "아예 못 읽음"은 명시적으로 범위 밖에
        # 뒀다(위 ``_non_object_ledgers`` 독스트링). 그 남은 구멍이 바로 확정사실의
        # 사슬이다 — ``consumed.jsonl`` 이 손상되면 ``_consumed_ids`` 가 관대하게
        # 빈 집합을 돌려주고, "소비 원장을 못 읽는다"가 "소비한 게 없다"로 둔갑해
        # ``queries.jsonl`` 의 과거 행이 재디스패치되고(provider 비용 재발생) 반복된다.
        #
        # ⛔ **파일 없음은 여기 안 걸린다** — ``_read_rows`` 는 부재를 ``([], None)`` 으로
        #    돌려주므로(정상 콜드스타트, 지금과 동일하게 아래 (2) 로 진행), 이 블록은
        #    파일이 **있는데** 못 읽는 경우만 잡는다.
        # 🔴 **가용성**: 디스패치도 tombstone 삭제/추가(``_tombstone`` 은 여기서 안 부른다)
        #    도 없다 — 재과금 위험 0. 거부는 **이 틱 한 번**뿐이다: 다음 틱이 같은 검사를
        #    다시 돌리고, 손상된 파일이 수리/교체/삭제되면 그 다음 틱부터 자동으로 다시
        #    열린다(코드 재배포도, 게이트 조작도 필요 없다 — 운영자가 손상된 축 파일만
        #    고치면 된다). 손상이 지속되면 그 사실은 반환값에서 매번 보이지만(호출자는
        #    항상 ``dispatched: False`` 를 받는다), tick_log 행 자체는 **전이에서만**
        #    쓴다(``_ledger_fault_edge``) — 10분 cron × 고장 유지가 하루 144행으로 원장을
        #    cron 주기계로 만드는 것을 막는다(위 (1b)/사람 정지 블록과 같은 원칙).
        # OFF (``AGI_V8_TICK_LEDGER_FAILCLOSED=false``) 면 이 블록은 아예 안 돌고 아래
        # ``_read_inbox``/``_consumed_ids`` 의 관대한 삼킴이 패치 이전과 byte-identical
        # 로 남는다.
        #
        # __SLOT_TICK_TOCTOU_LEDGER_SNAPSHOT_2026_08_18__ 🔴 R3 잔여 ① — 상류 두 축을
        # 이 틱 안에서 **정확히 한 번**만 읽는다. (1a)/(1b) 검증과 (2) 소비 읽기가 서로
        # 다른 시점에 다시 열리면, 검증이 통과시킨 **직후** 파일이 훼손/교체돼도 (2)가
        # 그걸 못 본다(unlocked read — ``state/store.py`` 는 append 만 flock 으로
        # 직렬화한다) — 그 창에서 이미 소비된 질의가 재디스패치(재과금)된다. 결정론적
        # 주입으로 실측 확인됨(``tests/security/test_adv_r3_toctou_ledger_2026_08_18.py``).
        # 두 검사 게이트 중 하나라도 실제로 원장을 볼 때만(=검증이 있을 때만 창이
        # 존재할 수 있다) 스냅샷을 만든다 — 둘 다 꺼져 있으면(비기본 조합) 이 블록도
        # 안 돌고 (2)가 예전처럼 자기 판독을 한다(추가 I/O 없음, byte-identical).
        _ledger_check_reads = ledger_failclosed_enabled() or work_feeder.stall_enabled()
        _ledger_snap = _ledger_snapshot(sd) if _ledger_check_reads else None
        if ledger_failclosed_enabled():
            _failed = _failed_ledgers(sd, _snapshot=_ledger_snap)
            if _failed:
                _swallowed(RuntimeError(f"원장 판독/파싱 실패(파일 없음이 아님): {_failed}"),
                           site="runtime.tick_runner.tick_once:ledger_read_failed",
                           category="persist")
                _rec = {"dispatched": False, "reason": REASON_LEDGER_READ_FAILED,
                        "ts": now, "ledgers": _failed}
                return (_log_tick_safe(sd, _rec)
                        if _ledger_fault_edge(sd, _failed, reason=REASON_LEDGER_READ_FAILED)
                        else _rec)

        # (1b) __SLOT_STALL_VERDICT_2026_08_09__ 상류가 읽을 원장에 **객체 아닌 줄**이
        # 있나. 있으면 게이트 ON 에서는 crash 대신 행 하나를 남기고 돌아온다 —
        # 디스패치도, 툼스톤 삭제도 없다(재과금 위험은 그대로 0). 근거는
        # ``_non_object_ledgers`` 위 주석. ⛔ 게이트 OFF 는 HEAD 그대로 아래에서 터진다.
        if work_feeder.stall_enabled():
            _bad = _non_object_ledgers(sd, _snapshot=_ledger_snap)
            if _bad:
                _swallowed(TypeError(f"객체가 아닌 JSONL 행: {_bad}"),
                           site="runtime.tick_runner.tick_once:non_object_row",
                           category="persist")
                _rec = {"dispatched": False, "reason": REASON_LEDGER_NON_OBJECT,
                        "ts": now, "ledgers": _bad}
                return _log_tick_safe(sd, _rec) if _ledger_fault_edge(sd, _bad) else _rec

        # (2) inbox / tombstones. 대기 집합의 정의는 :func:`pending_queries` 하나다 —
        # 트레이가 보는 수와 여기서 집는 수가 같은 함수에서 나온다.
        # __SLOT_TICK_TOCTOU_LEDGER_SNAPSHOT_2026_08_18__ 위 (1a)/(1b) 와 같은 스냅샷을
        # 재사용한다(``_ledger_snap`` — None 이면 이 호출은 종전처럼 자기 판독을 한다).
        pending = pending_queries(sd, _snapshot=_ledger_snap)
        if not pending:
            return resolve_payload(PayloadPort(
                9, "agi_v8_1.runtime.autonomous_tick_payload", "feed_idle"))(
                    _payload_sys.modules[__name__], sd, now)

        # (3) auth must be configured, else fail closed (run nothing).
        expected = _expected_token()
        if expected is None:
            rec = {"dispatched": False, "reason": "auth_misconfig", "ts": now,
                   "pending": len(pending)}
            _log_tick(sd, rec)
            return rec

        # Walk pending in arrival order: tombstone malformed/unauthorized (cheap,
        # $0) and dispatch the FIRST valid one. At most one cycle per tick.
        for row in pending:
            qid = _query_id(row)
            objective = _objective_of(row)
            if objective is None:
                _tombstone(sd, qid, "rejected_malformed", now=now)
                _log_tick(sd, {"dispatched": False, "reason": "rejected_malformed",
                               "id": qid, "ts": now})
                continue
            if not _auth_ok(row, expected, state_dir=sd, now=now):
                _tombstone(sd, qid, "rejected_auth", now=now)
                _log_tick(sd, {"dispatched": False, "reason": "rejected_auth",
                               "id": qid, "ts": now})
                continue

            # (4) per-day cost floor — leave the query UNCONSUMED so it runs after
            # the daily reset rather than being lost.
            verdict = daily_cost_cap.check(sd, now_ts=now)
            if not verdict["allowed"]:
                # __SLOT_TICK_SURFACES_THE_CAP_VERDICT_2026_08_03__ ``reason`` 이
                # 하드코딩이라 STRICT 미측정 차단도 "daily_cap_reached" 로 남았다 —
                # ``spent_usd: 0.0``, ``cap_usd: 10.0`` 과 함께. 운영자가 보기에
                # "0 을 썼는데 10 달러 캡에 도달했다" 는 말이 안 되고, 진짜 이유
                # (미측정 지출)는 어디에도 없었다. 2026-08-02 가 만든 신호
                # (``unmeasured_rows``/``spend_is_complete``)의 소비자가 0개였던 것도
                # 여기다 — 캡 안에서 결함을 고치고 한 층 위에서 같은 결함을 재생산했다.
                rec = _daily_cap_block_rec(verdict, qid, now)
                _log_tick(sd, rec)
                return rec

            # (4.5) Agent-on-Agent review — an INDEPENDENT model vets the objective
            # before it is allowed to dispatch. Auth already proved the SENDER is
            # trusted; this checks the CONTENT is coherent, scoped, and not
            # requesting something destructive/policy-weakening. Default-OFF; when
            # the gate is off this block never runs — tick_once stays byte-
            # identical to the pre-review behavior.
            # __SLOT_TICK_REVIEW_2026_07_13__
            if review_enabled():
                review_fn = _build_review_fn(sd)
                if review_fn is None:
                    rverdict = {"approved": False, "risk": "high",
                               "reason": "reviewer_unavailable"}
                else:
                    # __SLOT_EPISODE_CTX_REVIEW_2026_08_07__ 리뷰가 쓴 돈도 이 틱 것이다.
                    # ⚠️ `cycle_id` 는 아직 없다(예산 계산 뒤에 생긴다) — 지어내지 않고
                    # **리뷰 단계라고 정직하게** 적는다. 디스패치 행과 짝이 안 지어지는
                    # 것이 사실이고, 그 사실이 원장에 보여야 한다.
                    from agi_v8_1.runtime import episode_ctx as _episode_ctx
                    _cid = _cycle_id_for(now, qid)

                    def _run_review() -> dict[str, Any]:
                        with _episode_ctx.cycle_scope(_cid), \
                                _episode_ctx.call_scope(f"{_cid}:review"):
                            return tick_review.review_objective(
                                objective, review_fn=review_fn)

                    # __SLOT_TICK_REVIEW_TRANSACTION_2026_08_20__ P1-D — see
                    # tick_review_transaction_enabled() for the full
                    # rationale. Gate OFF: falls straight to the direct call
                    # below, byte-identical to pre-patch.
                    if tick_review_transaction_enabled():
                        # __SLOT_RESERVE_FRESH_TS_2026_08_22__ 백로그 #11(d).
                        # 예약·정산 행의 타임스탬프에 tick 시작 `now` 를 쓰면
                        # review 가 길어질수록 reserved_ts 가 실제보다 늙어
                        # TTL(기본 1800s) 을 조기 만료시킨다 — open_usd 합계에서
                        # 빠져 나가 캡 여유를 과대평가하는 과소계상. 이제
                        # now_ts 를 안 넘겨 spend_reservation._now() 가 **호출
                        # 시각**을 쓰게 한다(settle 도 동일 — 실제 정산 시각).
                        # 일일 캡 창 계산(daily_cost_cap.check 의 now_ts)은 여전히
                        # tick 시작 now 를 쓴다 — 그 축은 날짜 귀속이지 나이가
                        # 아니므로 바꾸면 하루 경계 귀속이 흔들린다.
                        _review_reservation = spend_reservation.reserve(
                            sd, subject="tick_review",
                            expected_usd=max(0.0, min(_per_cycle_cap(),
                                                      verdict["remaining_usd"])),
                            cycle_id=_cid)
                        if spend_reservation.is_unavailable(_review_reservation):
                            rec = {"dispatched": False, "id": qid, "ts": now,
                                   "cycle_id": _cid,
                                   "reason": "spend_reservation_unavailable",
                                   "stage": "review"}
                            _log_tick(sd, rec)
                            return rec
                        try:
                            try:
                                if tick_deadline.enabled():
                                    rverdict = tick_deadline.run_with_deadline(
                                        _run_review)
                                else:
                                    rverdict = _run_review()
                            except halt_sentinel.HumanHalt as hh:
                                return _log_tick_safe(
                                    sd, dict(_human_halt_row(hh.state, now),
                                            id=qid, cycle_id=_cid,
                                            halted_mid_dispatch=True,
                                            stage="review"))
                            except tick_deadline.DeadlineUnavailable as due:
                                _swallowed(
                                    due,
                                    site="runtime.tick_runner.tick_once:"
                                         "review_deadline_unavailable",
                                    category="config")
                                rverdict = {"approved": False, "risk": "high",
                                           "reason": "review_deadline_unavailable"}
                            except tick_deadline.DeadlineExceeded as de:
                                _swallowed(
                                    de,
                                    site="runtime.tick_runner.tick_once:"
                                         "review_deadline",
                                    category="config")
                                rverdict = {"approved": False, "risk": "high",
                                           "reason": "review_deadline_exceeded"}
                            except tick_deadline.DispatchChildError as dce:
                                # review_objective() already fail-closes every
                                # provider-side error inside the child; a
                                # DispatchChildError here means the IPC/child
                                # envelope itself broke, not the reviewer's
                                # own logic. Same posture: never a silent pass.
                                _swallowed(
                                    dce,
                                    site="runtime.tick_runner.tick_once:"
                                         "review_child_error",
                                    category="config")
                                rverdict = {"approved": False, "risk": "high",
                                           "reason": "review_child_error"}
                        finally:
                            spend_reservation.settle(
                                sd, _review_reservation)  # __SLOT_RESERVE_FRESH_TS_2026_08_22__ 실제 정산 시각
                    else:
                        rverdict = _run_review()
                if not rverdict["approved"]:
                    _tombstone(sd, qid, "rejected_review", now=now)
                    _log_tick(sd, {"dispatched": False, "reason": "rejected_review",
                                   "id": qid, "ts": now, "risk": rverdict["risk"],
                                   "review_reason": _mask_published_text(
                                       rverdict["reason"]
                                   )})
                    continue

                # __SLOT_CAP_REREAD_AFTER_REVIEW_2026_08_19__ 리뷰(``review_fn``)가
                # 방금 실지출을 남겼다(episode_ctx 스코프 → executor_log.jsonl) —
                # 아래 budget 이 그 지출을 보게 (4) 의 verdict 를 다시 읽는다.
                # default ON, see cap_reread_after_review_enabled(). OFF 면 이
                # 블록은 안 돌고 ``verdict`` 는 (4) 의 값 그대로(byte-identical).
                if cap_reread_after_review_enabled():
                    verdict = daily_cost_cap.check(sd, now_ts=now)
                    if not verdict["allowed"]:
                        rec = _daily_cap_block_rec(verdict, qid, now)
                        _log_tick(sd, rec)
                        return rec

            budget = max(0.0, min(_per_cycle_cap(), verdict["remaining_usd"]))
            # __SLOT_BUDGET_BASIS_IS_STATED_2026_08_03__ ``remaining_usd`` 는 분자가
            # 불완전한 날에는 사실이 아니라 **상한**이다(모듈 docstring 이 그렇게
            # 적어놨다). 그런데 유일한 소비자인 여기가 그것을 그대로 사이클 예산으로
            # 쓴다 — 즉 실지출이 기록보다 클 가능성이 높은 바로 그 날에 예산이 과대
            # 배정된다. 숫자를 지어내 깎지는 않는다(모르는 값을 아는 값으로 바꾸는
            # 것은 이 전체 작업이 막으려는 실패다). 대신 **어떤 근거로 잡은
            # 예산인지**를 틱 로그에 적어 운영자가 구분할 수 있게 한다.
            budget_basis = ("headroom_fact" if verdict["spend_is_complete"]
                            else "headroom_ceiling_numerator_incomplete")
            cycle_id = _cycle_id_for(now, qid)
            # __SLOT_HUMAN_HALT_TICK_2026_08_06__ K1b — **재과금 차단.**
            #
            # 툼스톤이 dispatch **뒤**에만 있었다. 그래서 비상 정지(또는 크래시)가
            # 디스패치 도중에 들어오면 이 질의는 소비 표시가 안 된 채로 남고, 다음
            # 틱이 **같은 목표에 또 돈을 쓴다**. 사람이 멈추라고 누른 직후에 과금이
            # 한 번 더 일어나는 것이 이 설계에서 가장 배신감 있는 실패다.
            # ⇒ 먼저 ``in_flight`` 를 찍는다. ``_consumed_ids`` 는 id 집합만 만들므로
            #   이 한 행으로 재집기가 막히고, 정상 종료 시 ``dispatched`` 행이 뒤에
            #   붙어 최종 사유가 된다(소비자들은 dict 마지막-승 또는 any() 라 무영향).
            # __SLOT_REPEATED_FAILURE_GATE_2026_08_19__ 🔑 Prime autonomous.ts:284 계보 —
            # "직전과 같은 실패면 반복하지 않는다" 신호(v0, 관측 번들: 스킵+로그).
            # ⚠️ 이름은 ``anti_idempotent`` 가 아니라 ``repeated_failure_gate`` 다 —
            # ``runtime/anti_idempotent.py`` 는 이미 다른(2026-08-08, first_run
            # 워크스페이스-다이제스트) 모듈이 쓰고 있다(모듈 독스트링 참조).
            # default-OFF(``AGI_V8_TICK_REPEATED_FAILURE_GATE_ENABLED``) — OFF 면
            # :func:`repeated_failure_gate.should_skip` 이 항상 ``(False, None)`` 이고
            # 아무 파일도 안 읽는다. 판정은 dispatch **직전**에 내려야 한다 — 그래야
            # 실제 provider 호출을 아낀다(이게 이 게이트의 요점).
            _rfg_skip, _rfg_obs = repeated_failure_gate.should_skip(sd, row)
            # __SLOT_TICK_CYCLE_MEMORY_2026_08_20__ 08-04 "루프에 기억 없음"
            # 공백의 v1 — 직전 사이클 관측(runtime/cycle_memory.py, 읽기전용)을
            # objective 앞에 얹는다.
            #
            # ⚠️ **역할 경계**(중복 주입 금지): GRADE_FEEDBACK
            # (``goal_campaign_feed.py:701-705``, ``ENV_GRADE_FEEDBACK_ENABLED``)
            # 는 ``feed_one()`` 안에서 **생산자측(업스트림)** 으로 카드 자기
            # 채점 꼬리를 ``row["objective"]`` 뒤에 이미 구워 넣는다(그 카드
            # 하나의 exec_arm 관측만 본다). 여기 cycle_memory 는 dispatch
            # **직전** **소비자측(여기)** 에서 V8 원장 전체(campaign_registry/
            # episode/repeated_failure_gate)를 다시 읽어 **별도 블록**을 얹는다
            # — 헤더 문자열이 달라 절대 안 섞인다(켜짐조합 테스트가 두 블록의
            # 공존+텍스트 비중복을 확인한다).
            #
            # default-OFF(``AGI_V8_TICK_CYCLE_MEMORY_ENABLED``, cycle_memory.
            # enabled()) — OFF 면 아래는 안 돌고(번들 조립 자체를 건너뛴다)
            # ``objective`` 는 종전과 byte-identical.
            #
            # __SLOT_TICK_OBJECTIVE_TARGETS_PARSE_2026_08_20__ ⚠️ **먼저 스냅샷.**
            # 아래에서 ``objective`` 를 cm_text 로 prepend 하기 **전**의 원문을
            # 따로 쥐어둔다 — target_files 파싱(``_maybe_set_scoped_objective_
            # targets``, 아래 (5) 직전)이 이 스냅샷을 본다. cm_text 는 단일 줄
            # (개행 없음, ``format_for_injection`` 이 " ".join)이고 그 축 중
            # ``objective_summary`` 는 **직전 사이클 objective 원문**(240자
            # 까지)을 그대로 담는다 — 그 직전 objective 가 이 기능이 전제하는
            # 바로 그 패턴(``target_files: a,b``)을 담고 있었다면, prepend 뒤의
            # ``objective`` 전체에 정규식 ``.search()``(첫 매치만)를 돌리면
            # cm_text 안에 실린 **지난** target_files 가 먼저 걸려 뒤따르는
            # judge_verdict/usd_spent/... 축까지 콤마-분해되어 이번 사이클의
            # 진짜 target_files 대신 오염된 값이 파싱된다(실측 확인됨). 두
            # 게이트 다 이 트랙 산출물이라 함께 켜는 조합이 자연스러운 다음
            # 단계인데, 소비 시점을 cm_text 합류 **이전**으로 고정해두면 그
            # 조합 자체가 애초에 이 상호작용 표면을 안 만든다.
            _objective_for_targets_parse = objective
            if cycle_memory.enabled():
                try:
                    _cm_bundle = cycle_memory.build_bundle(
                        sd, row, current_cycle_id=cycle_id, rfg_obs=_rfg_obs)
                    _cm_text = cycle_memory.format_for_injection(_cm_bundle)
                except Exception as exc:  # noqa: BLE001 — 기억 조립 실패가 디스패치를 못 막는다
                    _swallowed(exc, site="runtime.tick_runner.tick_once:cycle_memory",
                               category="telemetry")
                    _cm_text = ""
                if _cm_text:
                    objective = f"{_cm_text}\n\n{objective}"
            _tombstone(sd, qid, "in_flight", now=now)
            # __SLOT_HUMAN_AXIS_2026_08_08__ 사람이 시킨 사이클임을 dispatch **전에**
            # 적는다 — 도중 크래시가 나도 "누가 시켰나"는 이미 참이다.
            _emit_human_for_query(sd, row, qid, cycle_id, now)
            # (5) dispatch exactly one cycle into the jail.
            #
            # __SLOT_TICK_DEADLINE_2026_08_18__ R3 잔여 ③ — ``dispatch`` 자체는
            # in-process 호출이고 벽시계 상한이 전무했다(``run_orchestrator_cycle``
            # 이 provider 네트워크에 물리면 이 프로세스가 ``tick.sh`` 의 flock 을
            # 무기한 쥔다). default-OFF(``AGI_V8_TICK_DEADLINE_ENABLED``) — OFF 면
            # 아래는 동기 직접 호출, 패치 이전과 byte-identical. ON 이면 별도
            # POSIX main-thread timer가 **같은 dispatch 스택**을 중단하고 정상적인
            # 예외 unwind/finally 정리를 끝낸 뒤 돌아온다. 배경 worker가 없으므로
            # ``tick_once`` 반환/lease release 뒤에 옛 dispatch가 살아남지 않는다.
            # 이미 위에서 ``_tombstone(..., "in_flight", ...)`` 을 찍어뒀으므로
            # provider가 timeout 직전 요청을 수신한 경우에도 같은 질의를 재구매하지
            # 않는다(K1b 재과금 방어가 이 경로 위에도 그대로 얹힌다).
            if _rfg_skip:
                # __SLOT_REPEATED_FAILURE_GATE_2026_08_19__ 실제 ``dispatch()``
                # 호출을 건너뛴다 — 이게 이 게이트가 아끼는 유일한 것이다. ⛔ 그
                # 아래 goal_campaign 정산(``record_settlement``)/tombstone/tick_log
                # 는 **평범한 실패 경로와 완전히 같은 모양**으로 그대로 흘려보낸다:
                # 정산을 생략하면 goal_campaign_feed 의 in-flight 가드(``card_fed``
                # 뒤 ``card_settled`` 짝)가 절대 안 풀려 그 캠페인이 영원히
                # 멈춘다(고아 카드) — "복잡한 정책 금지"가 "캠페인을 고아로 만들어도
                # 된다"는 뜻은 아니다. judge_verdict/redispatch_action 판정 로직은
                # 이 증분에서 손대지 않는다 — 그 판정은 여기서 만든 ``dres``/``err``
                # 를 평범한 실패로 받아 스스로 계산할 뿐이다.
                dres = {"ok": False, "repeated_failure_gate_skip": True}
                err = ("repeated_failure_gate: repeated failure signature "
                       f"({_rfg_obs.get('repeated_signature') if _rfg_obs else None}) "
                       "— dispatch skipped")
            else:
                # __SLOT_SPEND_RESERVATION_2026_08_19__ P1-d — dispatch 창
                # 안에서 나가는 돈(reviewer/hetero/재현 포함, 전부 이 한 번의
                # ``dispatch()`` 호출 안에서 트리거된다)을 캡의 판정 축에
                # 실시간으로 보이게 한다. ⛔ ``_rfg_skip`` 분기(위)는 이
                # ``else`` 에 아예 안 들어오므로 그쪽에서는 reserve 자체가
                # 안 불린다 — 실제 dispatch 를 시도하지 않는 스킵에 예약을
                # 열면 절대 settle 안 되는 유령 예약이 쌓인다. 게이트 OFF 면
                # ``reserve()`` 가 즉시 ``None`` 을 반환하고 아래 전부
                # no-op(``settle(None)`` 도 no-op) — byte-identical.
                # __SLOT_TICK_OBJECTIVE_TARGETS_PARSE_2026_08_20__ 이 실제
                # ``dispatch()`` 호출 **직전에만** env 를 채운다 — 아래 finally
                # 가 (settle 과 같은 자리에서) 반드시 복원한다. ``_rfg_skip``
                # 분기(위)는 이 ``else`` 에 안 들어오므로 스킵된 재급식에는
                # env 자체가 안 건드려진다(실제 provider 호출이 없으니 스코프
                # 할 대상도 없다). ⚠️ ``objective``(cm_text 가 이미 prepend
                # 됐을 수 있는 최종본)가 아니라 위에서 찍어둔
                # ``_objective_for_targets_parse``(cm_text 합류 이전 원문)를
                # 넘긴다 — cycle_memory 축 안의 지난 target_files 문자열이
                # 이번 사이클 파싱을 오염시키지 않도록.
                _targets_touched, _targets_prior = _maybe_set_scoped_objective_targets(
                    _objective_for_targets_parse)
                # __SLOT_RESERVE_FRESH_TS_2026_08_22__ dispatch reserve 도
                # 실제 예약 시각을 쓴다 — 위 review reserve 주석 참조.
                _reservation = spend_reservation.reserve(
                    sd, subject="tick_dispatch", expected_usd=budget,
                    cycle_id=cycle_id)
                # __SLOT_RESERVE_DURABLE_OR_REFUSE_2026_08_20__ 🔴 Sol Pro R15
                # P0-A. 예약 원장에 OPEN 행이 **디스크에 안 남았으면** 이
                # 지출은 다른 spender(reviewer·cross-model·exec_arm·수동 실행)
                # 에게 보이지 않는다 — 그 상태로 돈을 쓰면 예약이 지키려던
                # 바로 그 순간에 캡이 뚫린다. ⇒ fail-closed: 유료 dispatch 를
                # 포기하고 사유를 남긴다. (게이트 OFF 면 ``reserve()`` 가
                # ``None`` 을 주고 이 분기는 절대 참이 안 된다 — OFF 파리티.)
                if spend_reservation.is_unavailable(_reservation):
                    # __SLOT_TARGETS_RESTORE_ON_RESERVE_FAIL_R16_2026_08_20__
                    # R16 §13: 이 early return 은 아래 try/finally **밖**이라,
                    # 복원을 여기서 명시하지 않으면 위에서 세팅한 프로세스
                    # 전역 env(AGI_V8_SI_OBJECTIVE_TARGETS)가 남아 장수
                    # 프로세스의 다음 사이클로 샌다(target parse 게이트 ON +
                    # 예약 durable write 실패의 좁은 조합에서 실측 재현됨).
                    _restore_scoped_objective_targets(_targets_touched, _targets_prior)
                    rec = {"dispatched": False, "id": qid, "ts": now,
                           "cycle_id": cycle_id,
                           "reason": "spend_reservation_unavailable"}
                    _log_tick(sd, rec)
                    return rec
                try:
                    try:
                        if tick_deadline.enabled():
                            dres = tick_deadline.run_with_deadline(
                                dispatch, sd, objective, cycle_id,
                                budget_usd=budget, now_ts=now)
                        else:
                            dres = dispatch(sd, objective, cycle_id, budget_usd=budget, now_ts=now)
                        err = None
                    except halt_sentinel.HumanHalt as hh:
                        # ⛔ ``except Exception`` **앞**이어야 한다. 뒤에 두면 사람의 정지가
                        # 삼켜져 ``error: "HumanHalt: …"`` 로 남고, 그건 원장에서
                        # "에이전트가 터졌다"와 구분되지 않는다.
                        return _log_tick_safe(sd, dict(_human_halt_row(hh.state, now),
                                                       id=qid, cycle_id=cycle_id,
                                                       halted_mid_dispatch=True))
                    except tick_deadline.DeadlineUnavailable as due:
                        # Context was checked before inbox access, but pipe/fork/prctl
                        # setup can still fail. Never dispatch unbounded.
                        _swallowed(due, site="runtime.tick_runner.tick_once:deadline_unavailable_race",
                                   category="config")
                        dres = {"ok": False, "deadline_unavailable": True}
                        err = _format_exception_for_log(due)
                    except tick_deadline.DeadlineExceeded as de:
                        # ⛔ ``except Exception`` **앞**이어야 한다 — 아래 catch-all 도
                        # 기능적으로는 같은 모양(dres/err)을 만들지만, 여기서 따로 잡아
                        # "격리 dispatch 종료" 사실(``deadline_exceeded``)을 원장에
                        # 명시한다 — 운영자가 에이전트 오류와 시간상한을 구분해야 한다.
                        # 이 시점에는 child tree가 kill/reap되어 배경 작업이 없다.
                        _swallowed(de, site="runtime.tick_runner.tick_once:deadline",
                                   category="config")
                        dres = {"ok": False, "deadline_exceeded": True,
                               "budget_seconds": de.budget_seconds}
                        err = _format_exception_for_log(de)
                    except Exception as exc:  # noqa: BLE001 — never let a bad cycle wedge the queue
                        _swallowed(exc, site="runtime.tick_runner.tick_once:312", category="config")
                        dres, err = {"ok": False}, _format_exception_for_log(exc)
                finally:
                    # __SLOT_SPEND_RESERVATION_2026_08_19__ 이 바깥 finally 가
                    # HumanHalt 의 except-내부 조기 ``return`` 을 포함해 이
                    # 블록을 빠져나가는 **모든** 경로를 덮는다 — 안쪽
                    # try/except 만으로는 그 return 경로에서 settle 이 샌다
                    # (예약이 영원히 open 으로 남아 TTL 까지 캡을 무겁게
                    # 만든다). ``settle(sd, None, ...)`` 은 no-op 이므로
                    # 게이트 OFF/reserve 실패 시에도 안전하다.
                    # __SLOT_TICK_OBJECTIVE_TARGETS_PARSE_2026_08_20__ env 는
                    # 프로세스 전역이다 — 이 finally 가 위에서 튀는 모든
                    # 경로(HumanHalt 조기 return 포함)를 덮으므로 여기서 복원
                    # 해야 다음 사이클(같은 프로세스 반복 호출 포함)로 안 샌다.
                    _restore_scoped_objective_targets(_targets_touched, _targets_prior)
                    spend_reservation.settle(sd, _reservation)  # __SLOT_RESERVE_FRESH_TS_2026_08_22__
            # __SLOT_GOAL_CAMPAIGN_TICK_FEED_2026_08_16__ 역사슬 최소 배선: 이 질의가
            # goal_campaign 급식이었으면 디스패치 결과(실행 실패 신호)를 그 캠페인의
            # 원장에 retry/escalate 로 접는다. 계측 실패가 디스패치 결과를 절대
            # 못 바꾼다(순수 부수효과, swallowed 로 격리).
            if row.get("source") == "goal_campaign":
                try:
                    goal_campaign_feed.record_settlement(
                        sd, row=row, cycle_id=cycle_id, dispatch_result=dres,
                        error=err, now_ts=now)
                except Exception as exc:  # noqa: BLE001
                    _swallowed(exc, site="runtime.tick_runner.tick_once:gc_settle",
                               category="persist")
            # __SLOT_REPEATED_FAILURE_GATE_2026_08_19__ 이번 결과의 실패 서명을
            # history 에 남긴다 — 다음 재급식이 위 skip 검사를 걸 수 있게.
            # ``record_outcome`` 은 게이트 OFF/target 없음이면 자체적으로 즉시
            # 반환하고 아무 파일도 안 건드린다(module contract). 순수 관측
            # 부수효과라 여기서 나는 예외가 디스패치 결과(``rec``)를 절대 못
            # 바꾼다 — swallowed 로 격리.
            try:
                repeated_failure_gate.record_outcome(
                    sd, row, dispatch_result=dres, error=err,
                    cycle_id=cycle_id, now_ts=now)
            except Exception as exc:  # noqa: BLE001
                _swallowed(exc,
                           site="runtime.tick_runner.tick_once:repeated_failure_gate_record",
                           category="persist")
            _tombstone(sd, qid, "dispatched", now=now)
            rec = {"dispatched": True, "reason": "dispatched", "id": qid,
                   "cycle_id": cycle_id, "objective": objective[:200],
                   "budget_usd": round(budget, 6), "budget_basis": budget_basis,
                   "unmeasured_rows": verdict["unmeasured_rows"],
                   "with_agents": with_agents_armed(),
                   "ts": now, "error": err, "dispatch_result": dres}
            # __SLOT_REPEATED_FAILURE_GATE_2026_08_19__ 관측행: 게이트가 실제로
            # 판정을 냈을 때만(``_rfg_obs is not None`` — 곧 게이트 ON) tick_log
            # 행에 tail_signatures/threshold 를 얹는다. 게이트 OFF 면 ``_rfg_obs``
            # 는 항상 ``None`` 이라 ``rec`` 는 이 필드가 생기기 전과 byte-identical.
            if _rfg_obs is not None:
                rec["repeated_failure_gate"] = _rfg_obs
            _log_tick(sd, rec)
            return rec

        return {"dispatched": False, "reason": "no_valid_pending", "now_ts": now}
    finally:
        if _lease_ctx is not None:
            _lease_ctx.__exit__(None, None, None)


# __SLOT_TICK_NARRATIVE_LOG_2026_08_15__ v3/v5 밀도의 사람용 서사 복원.
# 실측(08-15): SI 경로 모듈들은 지금도 logger.info/warning 으로 v5 문체의
# 서사를 말한다(self_improvement_v8 만 40곳) — 그런데 틱 경로에 로깅 핸들러가
# 0개라 전부 버려졌다("INFO lines are discarded wholesale",
# si_lanes/verify_gate_log.py 독스트링의 자백). 기계 축(episode 6종 계약,
# 공개 README 소유)은 그대로 두고, 사람 축만 핸들러 하나로 되찾는다.
# Default-OFF · OFF=byte-identical(핸들러 미부착 = 종전 그대로 폐기).
_ENV_NARRATIVE = "AGI_V8_TICK_NARRATIVE_LOG_ENABLED"
_NARRATIVE_REL = ("runtime_logs", "narrative.log")
_NARRATIVE_MARK = "_agi_v81_tick_narrative"


def _attach_narrative_handler(state_dir: Path) -> None:
    """루트 로거에 젤 내 회전 파일 핸들러를 붙인다 (게이트 ON 일 때만, 1회).

    루트에 붙여야 si_lanes/core/providers 모듈 로거의 전파를 전부 받는다.
    실패는 틱을 절대 못 죽인다(계측이 본체를 죽이면 배보다 배꼽이다)."""
    if not _truthy(_ENV_NARRATIVE):  # tier: T2
        return
    try:
        import logging
        from logging.handlers import RotatingFileHandler

        root = logging.getLogger()
        if any(getattr(h, _NARRATIVE_MARK, False) for h in root.handlers):
            return  # 중복 부착 방지(같은 프로세스 재호출)
        path = Path(state_dir).joinpath(*_NARRATIVE_REL)
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
        ))
        handler.setLevel(logging.INFO)
        setattr(handler, _NARRATIVE_MARK, True)
        root.addHandler(handler)
        if root.level > logging.INFO or root.level == logging.NOTSET:
            root.setLevel(logging.INFO)
        logging.getLogger(__name__).info(
            "narrative log attached: %s (v5-density human stream)", path
        )
    except Exception as exc:  # noqa: BLE001 — 계측 실패가 틱을 못 죽인다
        _swallowed(exc, site="runtime.tick_runner._attach_narrative_handler",
                   category="telemetry")


def _repo_env_path() -> Path:
    """Repo-root ``.env`` — derived from this file's own location.

    __SLOT_TICK_DOTENV_REPO_ROOT_2026_08_20__ 이전엔 리터럴
    기존 배치의 환경 파일 경로를 하드코딩했다 — linked worktree 안에서 이
    모듈이 돌면 자기 트리가 아니라 **라이브 트리의 .env** 를 읽어(격리 위반),
    라이브 파일이 지워지거나 이사하면 worktree 실행까지 조용히 깨진다.
    ``runtime/first_run_drills.py:37`` 의 ``_REPO_ROOT = Path(__file__).
    resolve().parent.parent`` 관용구를 그대로 재사용한다 — ``tick_runner.py``
    도 ``runtime/`` 바로 아래라 부모의 부모가 레포 루트다.

    ⚠️ **라이브 10분 크론이 쓰는 파일**이다 — 이 계산이 실제 라이브 배치 경로
    (설치된 패키지 루트의 환경 파일)와 바이트 동일함을 행동보존테스트가 고정한다
    (``tests/v8_1/test_cycle_memory_2026_08_20.py``). 의심스러우면 실행 전에
    라이브에서 실측할 것 — worktree 격리 원칙상 이 파일 자체는 실측 없이는
    안 건드리는 게 맞는 판단이었다는 게 이 주석의 자기 점검이다.

    각주 — 같은 하드코딩이 ``runtime/first_run.py:600``·
    ``runtime/goal_campaign.py:2675`` 에도 있다(이번 트랙 파일 배타표 밖이라
    안 건드림 — ``goal_campaign.py`` 는 이 트랙에서 읽기전용 지정).
    """
    return Path(__file__).resolve().parent.parent / ".env"


def main(argv: list[str] | None = None) -> int:
    import argparse

    # Best-effort: load the repo .env so gates/tokens are present under cron too.
    # (load_dotenv does NOT override env already set by the caller's shell.)
    # AGI_V8_TICK_SKIP_DOTENV lets tests exercise main() without mutating the
    # suite's environment from the real .env.
    if not _truthy("AGI_V8_TICK_SKIP_DOTENV"):
        try:
            from dotenv import load_dotenv  # type: ignore
            load_dotenv(str(_repo_env_path()))
        except Exception as _ff_exc:  # noqa: BLE001
            _swallowed(_ff_exc, site="runtime.tick_runner.main:336", category="config")
            pass

    ap = argparse.ArgumentParser(description="One query-driven SI tick (gated).")
    ap.add_argument("--state-dir", required=True,
                    help="Fixed jail state_dir (NO tmpdir default on this path).")
    args = ap.parse_args(argv)
    # __SLOT_GATE_INVARIANTS_2026_08_17__ fail-closed boot check: refuses to
    # start (raises) if an armed gate's required companion gate is off. No-op
    # unless AGI_V8_GATE_INVARIANTS_ENFORCED is on (default-OFF, byte-identical
    # when off — see policy/gate_invariants.py for why ON is the safe default
    # to move toward).
    _gate_invariants.check_or_raise()
    _attach_narrative_handler(Path(args.state_dir))
    out = tick_once(Path(args.state_dir))
    print(json.dumps(out, ensure_ascii=False, sort_keys=True))
    return 0


__all__ = ["tick_once", "tick_enabled", "with_agents_armed", "pending_queries",
           "ledger_failclosed_enabled", "secure_profile_enabled", "main"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
