# __SLOT_GATE_INVARIANTS_2026_08_17__ combination safety net for armed gates.
"""Gate-COMBINATION safety invariants — the thing a single-gate check can't see.

## 왜 (2026-08-17 감사)

위험 capability 는 서로 **독립된** default-OFF env 게이트로 켜진다. 하나씩 보면
전부 정상인데, 조합하면 안전 전제가 깨지는 형상이 생긴다. 실측:

  * 2026-08-17 라이브 ``.env`` 에서 ``AGI_V8_SI_COMMAND_EXEC_ARMED=true`` 인데
    ``AGI_V8_SI_VERIFY_GATE_ENABLED`` 는 꺼져 있었다(도하가 발견해 내림) — 명령
    실행은 무장됐는데 그 산출물을 검증할 게이트가 없는 조합.

기존 게이트 census(``tools/gates_map.py``/``tools/gate_risk.py``)는 게이트
**하나하나**의 반경을 잰다. 이 모듈은 그 위에 한 켜를 더한다 — 게이트
**조합**이 서로의 안전 전제를 깨는지. 대상은 진짜로 위험한 4개 축뿐이다(명령
실행, 실 파일 쓰기, 라이브 트리 승격, 원격 틱 인가) — census 의 545개 전부가
아니라, 이 표에 없는 게이트는 이 모듈이 아무 말도 하지 않는다(월권 금지).

## 설계 — 표 + 평가기, 분기 아님

각 규칙은 순수 데이터다: ``{armed_gate, requires, reason, severity, evidence}``.
``evaluate()`` 는 이 표를 순회하며 **하나의** 판정 함수만 쓴다 — 게이트마다
if/elif 로 갈라 쓰지 않는다. 새 위험 조합을 추가하는 것은 ``RULES`` 에 행 하나
추가하는 것뿐, 코드를 더 쓰는 게 아니다.

## 진짜 게이트 이름 (코드에서 실측, 추측 아님)

  * ``AGI_V8_SI_COMMAND_EXEC_ARMED``      — enforcement/command_executor.py:58
    (``command_exec_armed()`` :187-189, 실 ``subprocess.run`` 을 여는 게이트)
  * ``AGI_V8_SI_PY_ARTIFACT_RUN_ENABLED`` — enforcement/command_executor.py:61
    (``py_artifact_run_enabled()`` :433-435, 젤 파이썬을 호스트에서 실행)
  * ``AGI_V8_SI_VERIFY_GATE_ENABLED``     — self_improvement_v8.py:941,944-946
    (``_si_verify_gate_enabled()``, 패치를 실 스위트에 돌려보는 검증 게이트)
  * ``AGI_V8_SAFE_AUTO_APPLY_WRITE_ENABLED`` — enforcement/safe_auto_apply.py:702-703
    (``SafeAutoApply.write_enabled()``, 실제로 디스크에 쓰는 2-env 게이트의 절반)
  * ``AGI_V8_SAFE_AUTO_APPLY_PATH_CONTAINMENT`` — enforcement/safe_auto_apply.py:136,139-142
    (``_path_containment_enabled()``, ``..`` 트래버설 등 경로-봉쇄)
  * ``AGI_V8_SI_PROMOTE_ENABLED``         — si_lanes/promote.py:61,65-67
    (``_promote_enabled()``, 검증된 패치를 **라이브 소스 트리**에 쓰는 유일한 경로)
  * ``AGI_V8_TICK_ENABLED``               — runtime/tick_runner.py:79,95-96
    (``tick_enabled()``, ``tick/queries.jsonl`` 행을 실제로 디스패치)
  * ``AGI_V8_TICK_AUTH_SIG_ENABLED``      — runtime/tick_auth.py:37,48-50
    (``sig_enabled()``, OFF 면 원장의 평문 ``auth_token`` 대조만으로 인가)

## 어느 것이 "구조적으로 이미 안전"이고 어느 것이 진짜 조합 위험인가

``AGI_V8_SI_PY_ARTIFACT_RUN_ENABLED`` 는 이미 코드 안에서
``AGI_V8_SI_COMMAND_EXEC_ARMED`` 를 요구한다(command_executor.py:622-624,
``execute_artifact_run`` 이 두 게이트를 둘 다 체크) — 이건 이 모듈이 아니라 그
함수 자체가 지키는 불변식이라 이 표에 다시 넣지 않는다. 이 표는 코드가 **아직
강제하지 않는** 조합만 다룬다.

``si_lanes/promote.py`` 는 ``verify_changes`` 를 env 게이트와 무관하게
**무조건 직접 호출**한다(:411-414) — 그래서 "승격이 검증을 건너뛴다"는 조합은
애초에 env 로 만들 수 없다(항상 실행됨). 대신 진짜로 열려 있는 문은 승격의
실제 쓰기 경로가 ``SafeAutoApply(repo_root=source_root)`` 를 거친다는 것이다
(promote.py:27-29) — 그 경로의 path-containment 가 꺼져 있으면 **검증을 통과한
패치조차** 봉쇄되지 않은 경로에 쓰일 수 있다. 그래서 promote 규칙의 requires 는
verify gate 가 아니라 path containment 다(추측이 아니라 실제 쓰기 경로를 따라간
결론).

## 이 모듈은 default-OFF (마스터 게이트 뒤)

``AGI_V8_GATE_INVARIANTS_ENFORCED`` 가 꺼져 있으면 ``check_or_raise()`` 는
아무 것도 하지 않고 빈 리스트를 돌려준다 — 호출부는 byte-identical. 이 모듈은
**새 기능**이므로(감사가 방금 요구한 것) 스스로도 같은 규율을 진다.

⚠️ 그런데 이 게이트는 다른 default-OFF 게이트들과 성격이 다르다 — **켜는 쪽이
안전한 방향**이다. 다른 게이트들은 꺼짐=검증된 과거 동작 보존이지만, 이 게이트는
꺼짐=지금 실제로 있었던 위험 조합(COMMAND_EXEC_ARMED+VERIFY_GATE OFF)을 다시
놓칠 수 있다는 뜻이다. ``.env.example`` 에도 그렇게 적는다.

## __SLOT_R13_INVAR_2026_08_17__ 이 표가 보장하지 않는 것 (assurance_scope)

R13 §2.4 감사가 잡은 것: 이 파일의 조합 규칙이 "verify gate 가 켜져 있다" 를
"실행될 산출물이 검증됐다" 로 오독하게 만들 수 있다는 점이다. 실제로는 아니다
— ``self_improvement_v8.py:1998`` (``__SLOT_WORK_PRODUCT_OWN_SESSION_2026_08_02__``)
가 밝히듯, work-product/answer-product 산출물은 inc5 verify gate **뒤에서**
별도 세션으로 만들어져 그 검증을 아예 거치지 않는다 — 이건 2026-08-02 사고
교훈에 따른 **의도적** 설계지, 이 표가 고칠 버그가 아니다.

그래서 ``GateInvariantRule`` 에 ``assurance_scope`` 필드를 뒀다 — 그 규칙이
통과(무위반)했을 때 **무엇을 보장하지 않는지** 를 기계가 읽을 수 있는
튜플로 적는다. 비워두면 ``_DEFAULT_ASSURANCE_SCOPE`` (아래) 로 떨어진다:
이 표 전체가 "게이트 설정값의 조합 일관성 검사"이지 "산출물이 실행 시점에
검증됐다는 런타임 증명"이 아니라는 공통 한계. ``AGI_V8_SI_VERIFY_GATE_ENABLED``
를 ``requires`` 로 두는 두 규칙(command_exec/py_artifact_run)은 그 공통
한계에 더해 work-product 우회를 **명시적으로** 인용하는 전용 문구를 얹는다
— 이게 실제로 오독을 낳은 그 조합이기 때문이다. 소비자는
``rule.assurance_scope or _DEFAULT_ASSURANCE_SCOPE`` (또는 편의 함수
``scope_for(rule_id)``) 로 프로그램적으로 얻을 수 있다.

⚠️ 이 SLOT 은 문서·계측 정직성만 고친다 — 판정 로직(``_gate_on``/``evaluate``/
``check_or_raise``)은 한 글자도 안 바꿨다. 새 필드는 기존 필드 뒤에 기본값
``()`` 로 추가돼서 위치/키워드 어느 쪽으로 만든 기존 ``GateInvariantRule(...)``
호출도 그대로 동작한다.

## 위반 시 — 경고가 아니라 프로세스 시작 실패

``check_or_raise()`` 는 위반을 찾으면 ``GateInvariantError`` 를 **던진다**.
호출부(``runtime/tick_runner.main`` · ``runtime/cli.run_orchestrator_cycle``)는
이걸 삼키지 않는다 — fail-closed. 보고 메시지에는 게이트 **이름**과 무엇이
왜 위험한지만 담고, 시크릿/값은 절대 포함하지 않는다(게이트는 진리값이지 값이
아니므로 애초에 실을 것도 없다).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Mapping

logger = logging.getLogger(__name__)

__all__ = [
    "GateInvariantRule",
    "GateInvariantViolation",
    "GateInvariantError",
    "RULES",
    "enforcement_enabled",
    "evaluate",
    "check_or_raise",
    "scope_for",
]

# Master switch for THIS module's enforcement. Default-OFF; ON is the safe
# direction (see module docstring). Reading it never imports the modules the
# rules below reference, so an OFF process pays zero cost beyond one os.environ
# lookup at each of the two call sites.
_ENFORCE_ENV = "AGI_V8_GATE_INVARIANTS_ENFORCED"  # tier: T9


# __SLOT_R13_INVAR_2026_08_17__ 이 규칙 하나가 통과(무위반)했을 때 공통으로
# 깔리는 한계 — "게이트 설정 조합이 일관됐다"는 것과 "그 설정이 지키는 실제
# 실행 경로가 안전했다는 런타임 증명"은 다른 문장이다. 개별 규칙이 더 구체적인
# ``assurance_scope`` 를 안 채우면 이 기본값으로 떨어진다(빈 튜플/None 이
# 아니라 항상 최소 이 한 줄은 소비자에게 간다).
_DEFAULT_ASSURANCE_SCOPE: tuple[str, ...] = (
    "이 표는 게이트 *설정값* 의 조합 일관성만 검사한다 — requires 가 전부 켜져"
    " 있어도, 그 경로로 실제로 실행/기록되는 개별 산출물이 검증됐다는 런타임"
    " 증명은 아니다.",
)


@dataclass(frozen=True)
class GateInvariantRule:
    """One row: if ``armed_gate`` is on, every gate in ``requires`` must be on too.

    ``assurance_scope`` — __SLOT_R13_INVAR_2026_08_17__ — 이 규칙이 무위반
    판정을 내렸을 때 **무엇을 보장하지 않는지** 를 기계가 읽을 수 있게 적은
    문구들. 비우면(``()``) ``_DEFAULT_ASSURANCE_SCOPE`` 로 대체된다 — 필드
    자체는 항상 존재하고 항상 최소 1개 이상의 한계 문구를 낸다는 게 계약이다.
    값이 아니라 **판정에 영향 없는 주석용 데이터**라, ``evaluate()``/
    ``check_or_raise()`` 로직은 이 필드를 절대 읽지 않는다.
    """

    rule_id: str
    armed_gate: str
    requires: tuple[str, ...]
    reason: str  # 한국어 한 줄 — 왜 이 조합이 위험한지
    severity: str  # "critical" | "high"
    evidence: tuple[str, ...]  # file:line citations, for human audit only
    assurance_scope: tuple[str, ...] = ()  # 무엇을 보장 "안" 하는지; 비면 기본값

    def scope(self) -> tuple[str, ...]:
        """``assurance_scope`` 또는(비었으면) 모듈 기본 한계 문구."""
        return self.assurance_scope or _DEFAULT_ASSURANCE_SCOPE


# --- gate truthiness, per gate's OWN documented convention -------------------
#
# ⛔ NOT a single blanket ``in ("true","1")`` — the gates below don't all agree
# (project convention: "게이트 strict 체크... 모듈마다 관례 다를 수 있음").
# Real predicates observed at the cited sites:
#   command_exec_armed / py_artifact_run_enabled  → .strip().lower() in ("true","1")
#   _si_verify_gate_enabled / _promote_enabled     → raw `in ("true","1")` (no strip/lower)
#   _path_containment_enabled / tick_auth.sig_enabled → .strip().lower() in ("true","1")
#   SafeAutoApply.write_enabled                    → .strip().lower() == "true" ONLY (no "1")
#   tick_runner._truthy                            → .strip().lower() in ("1","true","yes","on")
#
# This checker applies ``.strip().lower()`` uniformly against each gate's
# accepted-value SET below rather than re-deriving each gate's exact
# strip/lower behaviour. That is deliberately the CONSERVATIVE direction: it
# can only ever consider a gate "on" in cases the real gate would too (strict
# raw-compare gates are a subset of strip/lower-tolerant ones for the values
# "true"/"1"), so this checker never under-reports a live danger — the one
# gap possible is a false-positive block on whitespace/case the real gate
# would reject anyway, which fails closed, not open.
_TRUTHY_SETS: dict[str, frozenset[str]] = {
    "AGI_V8_SI_COMMAND_EXEC_ARMED": frozenset({"true", "1"}),
    "AGI_V8_SI_PY_ARTIFACT_RUN_ENABLED": frozenset({"true", "1"}),
    "AGI_V8_SI_VERIFY_GATE_ENABLED": frozenset({"true", "1"}),
    "AGI_V8_SAFE_AUTO_APPLY_WRITE_ENABLED": frozenset({"true"}),
    "AGI_V8_SAFE_AUTO_APPLY_PATH_CONTAINMENT": frozenset({"true", "1"}),
    "AGI_V8_SI_PROMOTE_ENABLED": frozenset({"true", "1"}),
    "AGI_V8_TICK_ENABLED": frozenset({"1", "true", "yes", "on"}),
    "AGI_V8_TICK_AUTH_SIG_ENABLED": frozenset({"true", "1"}),
}
_DEFAULT_TRUTHY = frozenset({"true", "1", "yes", "on"})


def _gate_on(name: str, env: Mapping[str, str]) -> bool:
    accepted = _TRUTHY_SETS.get(name, _DEFAULT_TRUTHY)
    return str(env.get(name, "")).strip().lower() in accepted


# --- the rule table -----------------------------------------------------------
#
# Only the combinations the 2026-08-17 audit flagged (+ the same-category
# python-artifact seam it missed). Each ``requires`` cites where the danger
# actually lives — see the module docstring for why promote's requirement is
# path-containment rather than verify-gate.
RULES: tuple[GateInvariantRule, ...] = (
    GateInvariantRule(
        rule_id="command_exec_requires_verify_gate",
        armed_gate="AGI_V8_SI_COMMAND_EXEC_ARMED",
        requires=("AGI_V8_SI_VERIFY_GATE_ENABLED",),
        reason=(
            "명령 실행이 무장되면 SI 루프가 임의 셸 명령을 실제로 실행할 수 있는데"
            "(enforcement/command_executor.py:392 subprocess.run), 패치 검증 게이트가"
            " 꺼져 있으면 그 결과로 나온 변경을 사람이 보기 전에 걸러줄 장치가 없다."
        ),
        severity="critical",
        evidence=(
            "enforcement/command_executor.py:58 _ARMED_GATE",
            "enforcement/command_executor.py:187-189,392 command_exec_armed() 게이트",
            "self_improvement_v8.py:941,944-946 _SI_VERIFY_GATE_ENV/_si_verify_gate_enabled()",
        ),
        # __SLOT_R13_INVAR_2026_08_17__ 이게 실제로 오독을 낳은 조합이라 전용
        # 문구를 얹는다 — 다른 규칙은 _DEFAULT_ASSURANCE_SCOPE 로 충분하다.
        assurance_scope=(
            "이 규칙이 무위반이어도(VERIFY_GATE_ENABLED=on) 그건 self-edit"
            " 경로가 검증을 거친다는 것뿐이다 — work-product/answer-product"
            " 산출물은 verify gate 뒤 별도 세션이라 이 규칙과 무관하게 항상"
            " 검증을 안 거친다(의도적 설계, self_improvement_v8.py:1998"
            " __SLOT_WORK_PRODUCT_OWN_SESSION_2026_08_02__).",
            "그래서 'VERIFY_GATE_ENABLED 가 켜져 있다' 를 '이 사이클이 실행할"
            " 모든 산출물이 검증됐다' 로 읽으면 오독이다 — 이 규칙은 두 env"
            " 게이트가 서로 어긋나지 않는다는 설정 일관성만 본다.",
        ),
    ),
    GateInvariantRule(
        rule_id="py_artifact_run_requires_verify_gate",
        armed_gate="AGI_V8_SI_PY_ARTIFACT_RUN_ENABLED",
        requires=("AGI_V8_SI_VERIFY_GATE_ENABLED",),
        reason=(
            "감사가 놓친 같은 범주 — 젤 파이썬 아티팩트를 호스트에서 직접 실행하는"
            " 것도 임의 코드 실행이다(enforcement/command_executor.py:622-624). 검증"
            " 게이트 없이 실행되면 그 산출물을 아무도 걸러내지 않는다."
        ),
        severity="critical",
        evidence=(
            "enforcement/command_executor.py:61 _PY_ARTIFACT_GATE",
            "enforcement/command_executor.py:433-435,622-624 py_artifact_run_enabled() 게이트",
            "self_improvement_v8.py:941,944-946 verify gate 정의",
        ),
        # __SLOT_R13_INVAR_2026_08_17__ 같은 조합 오독, 같은 근거.
        assurance_scope=(
            "위 command_exec 규칙과 동일한 한계 — VERIFY_GATE_ENABLED=on 은"
            " self-edit 경로만 덮는다. plan_artifact_run 이 실행하는 파일이"
            " work-product 세션에서 온 것이면 그 세션 자체가 verify gate 뒤"
            " 별도 경로라(self_improvement_v8.py:1998"
            " __SLOT_WORK_PRODUCT_OWN_SESSION_2026_08_02__) 이 규칙 통과가"
            " 그 파일의 검증을 보증하지 않는다.",
        ),
    ),
    GateInvariantRule(
        rule_id="safe_auto_apply_write_requires_path_containment",
        armed_gate="AGI_V8_SAFE_AUTO_APPLY_WRITE_ENABLED",
        requires=("AGI_V8_SAFE_AUTO_APPLY_PATH_CONTAINMENT",),
        reason=(
            "실 파일 쓰기가 무장됐는데 경로 봉쇄가 꺼져 있으면 '..' 트래버설 등으로"
            " jail 밖 경로에 쓰는 취약면이 열린다 — 봉쇄 검사 자체가 꺼진 상태에서는"
            " 스킵된다."
        ),
        severity="critical",
        evidence=(
            "enforcement/safe_auto_apply.py:136 _PATH_CONTAINMENT_ENV",
            "enforcement/safe_auto_apply.py:702-703 SafeAutoApply.write_enabled()",
            "enforcement/safe_auto_apply.py:139-142,1166,1251,1283"
            " _path_containment_enabled() 뒤에서만 경로 봉쇄 검사가 적용됨",
        ),
    ),
    GateInvariantRule(
        rule_id="promote_requires_path_containment",
        armed_gate="AGI_V8_SI_PROMOTE_ENABLED",
        requires=("AGI_V8_SAFE_AUTO_APPLY_PATH_CONTAINMENT",),
        reason=(
            "승격은 verify_changes 를 env 게이트와 무관하게 무조건 직접 호출하므로"
            "(si_lanes/promote.py:411-414) 검증 자체는 항상 실행된다. 그러나 실제"
            " 라이브 트리 쓰기는 SafeAutoApply(repo_root=source_root) 를 거치므로"
            "(si_lanes/promote.py:27-29) 그 경로의 경로-봉쇄가 꺼진 채면 검증을 통과한"
            " 패치라도 봉쇄되지 않은 경로에 쓰일 위험이 남는다 — 이 시스템에서"
            " '라이브 소스 트리에 실제로 쓰는' 유일한 문이라 특히 무겁다."
        ),
        severity="critical",
        evidence=(
            "si_lanes/promote.py:61,65-67 _PROMOTE_ENV/_promote_enabled()",
            "si_lanes/promote.py:23-24 SafeAutoApply 2-env 쓰기 게이트 = 전제조건 5",
            "si_lanes/promote.py:27-29 write 경로가 SafeAutoApply(repo_root=source_root) 사용",
            "enforcement/safe_auto_apply.py:136,139-142 path containment 정의",
        ),
    ),
    GateInvariantRule(
        rule_id="tick_enabled_requires_auth_sig",
        armed_gate="AGI_V8_TICK_ENABLED",
        requires=("AGI_V8_TICK_AUTH_SIG_ENABLED",),
        reason=(
            "틱 루프가 켜지면 tick/queries.jsonl 에 떨어진 행이 그대로 디스패치된다"
            "(runtime/tick_runner.py). 서명 게이트가 꺼져 있으면 원장의 평문"
            " auth_token 문자열 대조만으로 인가되므로(runtime/tick_auth.py 문서화된"
            " 위험), 그 파일에 쓸 수 있는 경로가 곧 objective 주입 표면이 된다."
        ),
        severity="high",
        evidence=(
            "runtime/tick_runner.py:79 _TICK_ENABLED / :95-96 tick_enabled()",
            "runtime/tick_auth.py:12-23 게이트 OFF = legacy 평문 auth_token 대조만",
            "runtime/tick_auth.py:37,48-50 ENV_SIG_ENABLED/sig_enabled()",
        ),
    ),
)


@dataclass(frozen=True)
class GateInvariantViolation:
    rule_id: str
    armed_gate: str
    missing_requires: tuple[str, ...]
    reason: str
    severity: str

    def describe(self) -> str:
        """사람이 읽는 한국어 한 줄 — 게이트 이름만, 값/시크릿 없음."""
        missing = ", ".join(self.missing_requires)
        return (
            f"[{self.severity}] {self.armed_gate} 가 켜졌는데 {missing} 가 꺼져"
            f" 있다 — {self.reason}"
        )

    def __str__(self) -> str:  # pragma: no cover - display only
        return self.describe()


class GateInvariantError(RuntimeError):
    """부팅 시 안전하지 않은 게이트 조합이 발견되면 발생한다.

    경고가 아니라 **프로세스 시작 실패**다 — 호출부는 이걸 삼키지 않는다.
    """


def enforcement_enabled(env: "Mapping[str, str] | None" = None) -> bool:
    """Default-OFF master switch for this module's boot-time enforcement."""
    source = os.environ if env is None else env
    return _gate_on(_ENFORCE_ENV, source)


def scope_for(rule_id: str) -> tuple[str, ...]:
    """__SLOT_R13_INVAR_2026_08_17__ 편의 함수 — ``rule_id`` 로 해당 규칙의
    ``assurance_scope`` (없으면 ``_DEFAULT_ASSURANCE_SCOPE``) 를 바로 얻는다.
    소비자가 ``RULES`` 를 직접 순회하지 않아도 되게 한다. 알 수 없는
    ``rule_id`` 는 빈 튜플을 준다(모르는 규칙에 대해 거짓 안심을 만들지
    않도록 — 기본값조차 붙이지 않는다).
    """
    for rule in RULES:
        if rule.rule_id == rule_id:
            return rule.scope()
    return ()


def evaluate(env: "Mapping[str, str] | None" = None) -> list[GateInvariantViolation]:
    """Every violated rule against *env* (default: the real process env).

    Pure — never raises, never mutates env, never touches disk. Always runs
    the full rule table regardless of :func:`enforcement_enabled`, so callers
    that want a read-only report (audits, tests) don't need the master gate on.
    """
    source = os.environ if env is None else env
    violations: list[GateInvariantViolation] = []
    for rule in RULES:
        if not _gate_on(rule.armed_gate, source):
            continue
        missing = tuple(g for g in rule.requires if not _gate_on(g, source))
        if missing:
            violations.append(
                GateInvariantViolation(
                    rule_id=rule.rule_id,
                    armed_gate=rule.armed_gate,
                    missing_requires=missing,
                    reason=rule.reason,
                    severity=rule.severity,
                )
            )
    return violations


def check_or_raise(env: "Mapping[str, str] | None" = None) -> list[GateInvariantViolation]:
    """Boot entry point. Master gate OFF → no-op, returns ``[]`` (byte-identical
    to not calling this at all). Master gate ON → evaluate the full table and
    raise :class:`GateInvariantError` fail-closed on the first violated set.
    """
    if not enforcement_enabled(env):
        return []
    violations = evaluate(env)
    if not violations:
        # __SLOT_R13_INVAR_CONSUMER_2026_08_17__ 🔑 **소비자를 붙인다.**
        # 적대검증이 정확히 잡은 것: `assurance_scope` 를 필드로 만들어도
        # `scope()` 호출자가 자기 테스트 밖에 **0건**이면 그건 이 레포가 반복해
        # 다친 *"어휘는 생겼는데 소비가 없다"* 다 — 운영자가 보는 신호가 패치
        # 전후 동일하면 거짓 안심은 그대로 남는다. 그래서 무위반 통과 시점에
        # **실제로 무장된 규칙의 한계 문구만** 말한다.
        # ⚠️ 무장된 규칙에 대해서만 말하므로 평시 부팅에는 한 줄도 안 찍힌다
        #    (부팅 소음이 되면 다음 사람이 이 로그를 끄고, 그럼 다시 침묵이다).
        # ⚠️ 이 블록은 마스터 게이트(기본 OFF) ON 경로 안에만 있어 기본 동작은
        #    패치 이전과 byte-identical 이고, 반환값도 건드리지 않는다.
        source = os.environ if env is None else env
        armed = [r for r in RULES if _gate_on(r.armed_gate, source)]
        for rule in armed:
            for caveat in rule.scope():
                logger.warning(
                    "[gate-invariants] %s 통과 — 그러나 이것이 보장하지 "
                    "않는 것: %s", rule.rule_id, caveat,
                )
    if violations:
        lines = "\n".join(f"  - {v.describe()}" for v in violations)
        raise GateInvariantError(
            f"안전하지 않은 게이트 조합 {len(violations)}건 발견 — 시작을 거부한다"
            f"({_ENFORCE_ENV}=true):\n{lines}"
        )
    return violations
