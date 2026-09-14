# __SLOT_CARD_SCOPED_TARGETS_2026_08_22__ 카드가 **자기 표적**을 말하게 한다.
"""goal card 가 선언한 편집 표적(``workspace.target_files``) — 해석 + 표적 린트.

## 왜 생겼나

검증 표적(``AGI_V8_SI_OBJECTIVE_TARGETS``)은 지금까지 **프로세스 env 하나**로만
정해졌다. 한 캠페인이 카드 여러 장을 돌려도 모든 에피소드가 같은 전역 표적을
보므로, 표적을 비워두면 ``verify_gate._map_changes`` 가 모든 코드 패치를
"unknown path" 로 떨어뜨리고 ``verify_changes`` 는
``reason="unverifiable_changes_dropped"`` 를 낸다 — 즉 **아무것도 재보지 않은
RED**. 카드가 자기 표적을 말할 수 있으면 그 RED 가 "실제로 무엇을 검증하려
했는가"로 바뀐다.

⚠️ 이 트랙이 고치는 것은 거기까지다. ``si_lanes/verify_gate.py`` 는 mapped 가
있어도 ``pytest_verdict_requires_trusted_review`` 로 뒤집으므로, 표적을 준다고
패치가 사다리로 넘어가지는 **않는다**. 성과를 "카드가 패치를 착지시킨다"로
적으면 팬텀 게이트 오독(2026-08-16 R16)을 반복하는 것이다.

## 왜 린트가 같이 있나 (⛔ 분리 금지)

카드가 표적을 말할 수 있게 되는 순간, 카드는 **자기 시험지**를 표적으로 말할
수도 있게 된다. ``tests/`` · ``si_lanes/verify_gate.py`` · ``runtime/goal_grader.py``
를 편집 범위로 열어주면 후보는 코드를 고치는 대신 **재는 도구**를 고쳐 초록을
만든다 — ``card_lint`` R4 가 gc-100 실사격으로 확인한 그 모양(래칫 상수 한 줄
36→999 → PASS 위조)과 같은 계열이다. 그래서 표적 해석기와 거부 목록은 한
모듈에 산다: 공급 경로만 있고 관문이 없는 중간 상태가 존재할 수 없게.

거부는 **조용한 필터가 아니다**. 금지 경로를 목록에서 빼고 나머지로 진행하면
카드는 "표적을 선언했다"고 적힌 채 다른 범위로 돌아가고, 원장에는 그 사실이
남지 않는다. 그래서 부적격 선언이 하나라도 있으면 ``files`` 는 **빈 리스트**가
되고 ``problems`` 가 사유를 들고 나간다(fail-closed). 소비자(린트/캠페인)는 그
problems 를 각자의 형태로 거부에 쓴다 — 판정을 두 곳에 복사하지 않는다
(``workspace_snapshot.normalize_protected_rel`` 과 같은 규약).

## 계약

- 선언 **미기재**(``workspace.target_files`` 없음) = 종전과 동일한 빈손이다.
  ⛔ "표적이 없으니 전부 허용"이 아니다 — 그건 관문 무력화다.
- 형태 결함/금지 경로/basename 충돌/개수 초과 = **REJECT**(problems 비지 않음).
- 이 모듈은 파일도 안 읽고 env 도 안 바꾼다. 게이트 판정(:func:`gate_enabled`)만
  ``os.environ`` 을 본다.
"""
from __future__ import annotations

import os
import posixpath
from typing import Any, Mapping

# ⛔ 사본 금지: 쓰기 사다리가 이미 보호하는 파일 목록을 **임포트해서** 쓴다.
# 두 곳에 적으면 검사 안 받는 쪽이 썩는다(2026-08-07 자칭 SSOT 드리프트).
# ``_ADDITIONAL_PROTECTED_FILES`` 는 밑줄 이름이지만 같은 축의 정본이고
# (R5-A 감사가 추가한 실측 목록), 여기서 다시 적는 것보다 임포트가 안전하다.
from agi_v8_1.enforcement.safe_auto_apply import (
    PROTECTED_DIRS as _PROTECTED_DIRS,
    PROTECTED_FILES as _PROTECTED_FILES,
    _ADDITIONAL_PROTECTED_FILES as _ADDITIONAL_PROTECTED_FILES,
)
# 경로 정규화도 한 곳에만 산다 — 하네스(원복기)·card_lint 와 같은 판정을 쓴다.
from agi_v8_1.runtime.workspace_snapshot import (
    normalize_protected_rel as _norm_rel,
)

#: T9 — 카드 선언 표적이 실제 배선(에피소드 env / tick objective)에 흐르게 하는
#: 게이트. strict ``"true"``/``"1"``. OFF(기본) = 전역 env 만 읽던 종전 동작과
#: byte-identical(공급 경로가 아무 키도 만들지 않는다).
#: ⚠️ 표적 **린트**는 이 게이트에 매달지 않는다 — 아래 :func:`lint_problems`
#: 주석 참조.
ENV_ENABLED = "AGI_V8_CARD_SCOPED_TARGETS_ENABLED"

#: 하류 소비자가 읽는 env 이름(``self_improvement_v8._SI_OBJECTIVE_TARGETS_ENV``
#: 의 리터럴 사본 — ``tick_runner`` 가 같은 이유로 같은 사본을 들고 있고,
#: 테스트가 세 리터럴의 동일성을 pin 한다).
ENV_TARGETS = "AGI_V8_SI_OBJECTIVE_TARGETS"

#: 카드 안에서 표적이 사는 자리(정본). 원장/문제 문자열에 이 이름으로 적힌다.
CARD_FIELD = "workspace.target_files"

#: 한 카드가 선언할 수 있는 표적 수. ``si_lanes/exec_arm.MAX_TARGETS`` 가 이
#: 값을 재수출한다(= 한 개념에 한 숫자). ``_si_objective_targets`` 의 env 상한
#: 12 보다 작으므로 카드 경로가 그 상한에 걸리는 일은 없다.
MAX_TARGETS = 8

_PKG_PREFIX = "agi_v8_1/"

#: 이 트랙이 추가로 막아야 하는 것. ``PROTECTED_FILES`` 합집합에는 채점기·린트·
#: 테스트가 **빠져 있다**(그건 쓰기 사다리 가드지 표적 선언 검사가 아니다).
_EXTRA_FORBIDDEN_FILES: frozenset[str] = frozenset({
    "si_lanes/verify_gate.py",       # 검증 관문 자체
    "si_lanes/verify_isolation.py",  # 그 관문의 격리 실행기
    "runtime/goal_grader.py",        # 카드 채점기
    "runtime/card_lint.py",          # 카드 계약 린트
    "runtime/card_targets.py",       # 이 파일 = 표적 린트
    "policy/fail_fast.py",           # 삼킴 초크포인트(래칫의 계량기)
    # 수집을 죽이면 테스트를 지운 것과 같다(card_lint R4 독스트링의 변형 계열).
    "pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini",
})

#: 접두사 매칭(디렉터리 통째). ``PROTECTED_DIRS`` 는 ``runs/``·``state/`` 등.
_EXTRA_FORBIDDEN_PREFIXES: tuple[str, ...] = ("tests/", "goal_public_checks/",)

#: 어느 디렉터리에 있든 이름만으로 금지 — conftest 하나로 수집 규칙이 바뀐다.
_FORBIDDEN_BASENAMES: frozenset[str] = frozenset({"conftest.py"})


def _canon(rel: str) -> str:
    """``agi_v8_1/`` 접두사 유무 두 철자를 한 철자로.

    ``PROTECTED_FILES`` 항목은 접두사가 있고 ``_ADDITIONAL_PROTECTED_FILES`` 는
    없다(safe_auto_apply 가 자인하는 네임스페이스 갈림). 정규화하지 않으면
    린트가 절반만 잡고 초록을 판다.

    ⚠️ ``lstrip("./")`` 를 쓰지 않는다 — 그건 문자 집합 strip 이라 ``.env`` 를
    ``env`` 로, ``.gitignore`` 를 ``gitignore`` 로 만들어 거부 목록의 점 파일
    항목을 통째로 증발시킨다(``PROTECTED_FILES`` 에 둘 다 있다).
    """
    p = rel.strip()
    while p.startswith("./"):
        p = p[2:]
    p = posixpath.normpath(p) if p else "."
    while p.startswith(_PKG_PREFIX):
        p = p[len(_PKG_PREFIX):]
    return p


#: 파일 단위 거부 목록(정규화된 한 철자). 임포트 원천 ⊕ 이 트랙의 추가분.
FORBIDDEN_FILES: frozenset[str] = frozenset(
    {_canon(p) for p in _PROTECTED_FILES}
    | {_canon(p) for p in _ADDITIONAL_PROTECTED_FILES}
    | {_canon(p) for p in _EXTRA_FORBIDDEN_FILES}
)
#: 디렉터리 단위 거부 목록(뒤에 ``/`` 가 붙은 정규 접두사).
FORBIDDEN_PREFIXES: tuple[str, ...] = tuple(sorted(
    {_canon(d) + "/" for d in _PROTECTED_DIRS}
    | {_canon(d) + "/" for d in _EXTRA_FORBIDDEN_PREFIXES}
))


def gate_enabled() -> bool:
    """default-OFF, strict. OFF 면 카드 선언이 어떤 배선에도 흐르지 않는다."""
    return os.environ.get(ENV_ENABLED, "") in ("true", "1")  # tier: T9


def forbidden_reason(rel: str) -> str | None:
    """이 경로가 왜 표적이 될 수 없는가 — 아니면 ``None``.

    사유 문자열을 돌려주는 이유: 거부를 원장에 적을 때 "금지됨"만 남으면
    운영자가 카드를 어떻게 고쳐야 하는지 알 수 없다.
    """
    p = _canon(rel)
    if p in FORBIDDEN_FILES:
        return f"보호 경로(쓰기 사다리/관문 정본): {p}"
    for pre in FORBIDDEN_PREFIXES:
        if p == pre.rstrip("/") or p.startswith(pre):
            return f"보호 디렉터리 {pre!r} 아래: {p}"
    if posixpath.basename(p) in _FORBIDDEN_BASENAMES:
        return f"수집 규칙 파일(어느 디렉터리에 있든 금지): {p}"
    return None


def _entry_problems(field: str, entries: Any) -> tuple[list[str], list[str]]:
    """선언 하나(리스트)를 검사 → ``(정규화된 경로들, 문제들)``.

    문제가 하나라도 있으면 호출부는 경로들을 **쓰지 않는다**(fail-closed) —
    부분 통과는 "선언한 것과 다른 범위로 돌았다"를 조용히 만든다.
    """
    problems: list[str] = []
    if isinstance(entries, (str, bytes)) or not isinstance(entries, (list, tuple)):
        return [], [f"{field} 는 상대경로 문자열 리스트여야 함: {entries!r}"]
    if not entries:
        # ⛔ 빈 리스트를 "표적 없음"으로 접지 않는다 — 필드를 뺀 것(미선언)과
        # 빈 리스트를 적은 것(선언했는데 아무것도 안 적음)은 다른 사실이다.
        return [], [f"{field} 가 빈 리스트 — 필드를 빼거나 경로를 적어라"]
    if len(entries) > MAX_TARGETS:
        problems.append(
            f"{field} 표적 {len(entries)}개 > 상한 {MAX_TARGETS} — 조용히 자르지 "
            f"않고 거부한다(잘린 표적은 원장에서 선언한 것처럼 보인다)")
    files: list[str] = []
    for raw in entries:
        norm = _norm_rel(raw)
        if norm is None:
            problems.append(
                f"{field} 부적격 경로(절대경로/'..'/'~'/역슬래시/빈 성분 금지): {raw!r}")
            continue
        reason = forbidden_reason(norm)
        if reason is not None:
            problems.append(
                f"{field} 가 자기 채점기·관문·테스트를 표적으로 선언했다 — {reason}")
            continue
        files.append(_canon(norm))
    # ``verify_gate._map_changes`` 는 **basename** 으로 매칭한다. 같은 이름이
    # 둘이면 그 이름의 패치는 전부 ambiguous → DROPPED 가 되어, 표적을 제대로
    # 주고도 mapped=0 이 된다. 조용한 전멸 대신 이름 붙여 거부한다.
    seen: dict[str, str] = {}
    for f in files:
        base = posixpath.basename(f)
        if base in seen:
            problems.append(
                f"{field} basename 충돌 {base!r}: {seen[base]!r} 와 {f!r} — "
                f"verify_gate 는 basename 으로 매칭하므로 둘 다 DROPPED 된다")
        else:
            seen[base] = f
    return files, problems


def declared_targets(card: Mapping[str, Any]) -> dict[str, Any]:
    """카드의 ``workspace.target_files`` 해석 결과.

    반환 ``{"declared": raw|None, "files": [...], "problems": [...],
    "source": "card_workspace"|"none"}``.

    - 미선언 → ``source="none"``, files/problems 모두 빈 리스트(종전과 동일).
    - 결함 → ``files=[]`` + problems 비지 않음(**부분 통과 없음**).
    """
    ws = card.get("workspace")
    raw = ws.get("target_files") if isinstance(ws, Mapping) else None
    if raw is None:
        return {"declared": None, "files": [], "problems": [], "source": "none"}
    files, problems = _entry_problems(CARD_FIELD, raw)
    return {
        "declared": raw,
        "files": [] if problems else files,
        "problems": problems,
        "source": "card_workspace",
    }


def lint_problems(card: Mapping[str, Any]) -> list[str]:
    """표적 린트(R7) — ``card_lint`` 가 그대로 problems 에 이어붙일 문자열들.

    ⚠️ 이 검사는 :data:`ENV_ENABLED` 게이트에 **매달지 않는다**. 게이트는 카드
    선언이 배선으로 흐르는지를 정할 뿐인데, 게이트가 린트까지 껐다면 부적격
    선언이 카드 안에서 잠들어 있다가 누가 게이트를 켜는 날 한꺼번에 살아난다.
    관문은 공급 경로보다 **먼저** 서 있어야 한다. 종전 카드(이 필드가 없던
    모든 카드)에 대해서는 빈 리스트라 preflight 출력이 byte-identical 이다.

    ``exec_arm.target_files`` 도 같이 본다 — 표적을 말하는 자리가 둘인데 한쪽만
    검사하면 다른 쪽이 우회로가 된다.
    """
    cid = str(card.get("id") or "?")
    out = [f"R7 {cid}: {p}" for p in declared_targets(card)["problems"]]
    ea = card.get("exec_arm")
    raw = ea.get("target_files") if isinstance(ea, Mapping) else None
    if raw is not None:
        _files, problems = _entry_problems("exec_arm.target_files", raw)
        out.extend(f"R7 {cid}: {p}" for p in problems)
    return out


__all__ = [
    "ENV_ENABLED", "ENV_TARGETS", "CARD_FIELD", "MAX_TARGETS",
    "FORBIDDEN_FILES", "FORBIDDEN_PREFIXES",
    "gate_enabled", "forbidden_reason", "declared_targets", "lint_problems",
]
