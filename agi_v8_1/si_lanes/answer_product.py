# __SLOT_SI_ANSWER_PRODUCT_LANE_2026_08_06__ 목표-구동 **답변** 레인 (F3-A).
"""Answer lane — 질문형 목표에 대해 **사람이 읽을 문서**를 낸다.

## 왜 이게 따로 있나 (측정된 것)

2026-08-06 실측: 사용자의 질문 목표는 이미 **DeepSeek 에 그대로 도달하고 응답도
돌아온다**. objhash 로 축자 확인됐다 —
``sha256('중국 api들의 가성비 비교…')[:8] == 'b075b837'`` 이고 원장의
``apply_proposal_ids`` 가 ``['f1i3_b075b837_0']`` 이다. 그러니 결함은 "아무도 목표를
안 본다"가 **아니었다**.

진짜 결함은 **돌아온 게 답이 아니라는 것**이다. 그 목표가 태워진 레인은
code-change proposer 이고, 그 레인의 승자를 고르는 저울이 이렇게 생겼다::

    si_lanes/breadth_select.py:226-256  _score
        ast.parse 되는 파이썬   +1000 (+10/def)
        파싱 안 되는 산문        -500

🔑 그래서 **어떤 종류가 이겼는지 사이클마다 셀 수 있다**(한글 마크다운은
``ast.parse`` 에서 반드시 ``SyntaxError`` 를 낸다). 실측::

    '중국 API 가성비 비교'   SyntaxError 0/16  → 후보 전원 **파이썬**
    '밥이 없으면 빵…반증하라' SyntaxError 16/16 → 후보 전원 산문

⇒ 그 레인의 산출물을 착지시키면 사용자는 **가성비 비교표가 아니라 ``.py`` 모듈**을
읽게 된다. *"원장엔 있는데 사람은 못 본다"* 보다 한 단계 나쁜
**"사람이 엉뚱한 걸 본다"** 이다. 그래서 처방은 *"버려지는 답을 착지시키기"* 가
아니라 **"질문형 목표를 code-change proposer 레인에 태우지 않기"** 다.

## F3(work_product) 과 뭐가 같고 뭐가 다른가

같다 — 안전 불변식 전부:

  1. **모델은 path/action 을 못 정한다.** 문서 본문만 데이터로 받고, 경로
     (``workspace/answers/<objhash8>_<cycle>.md``)와 action(``create``)은 여기서
     로컬 계산한다. 응답에 ``path`` 키가 있어도 **읽지 않는다.**
  2. **쓰기는 젤 안뿐이다.** 평소의 ``SafeAutoApply`` 세션(repo_root=state_dir).
  3. **자기 세션을 가진다.** 산출물이 자가수정 제안과 운명을 같이하면 안 된다 —
     2026-08-02 에 실제로 그렇게 죽었다(리뷰어가 무관한 제안을 거절했고 과제
     산출물이 같이 dry-run 됐다).

다르다 — **실행이 없다**:

  ⛔ 이 레인은 ``proposed_artifact_runs`` 를 **내지 않는다.** F3 는 모델이 쓴
  파이썬을 Stage 4 로 실행해야 산출물이 생기는데, 질문에 대한 답은 텍스트라서
  그 경로를 쓰면 *문자열 하나를 디스크에 올리려고 모델-저작 코드를 실행하는
  표면*을 사는 셈이다. 게다가 ``AGI_V8_SI_ARTIFACT_SANDBOX`` 는 2026-08-06 현재
  ``.env`` 에 **없다**(기본 ``""`` = 래퍼 없음)이고 이 호스트에는 실거래
  자격증명이 살아 있다. 실행이 필요 없으면 실행을 사지 않는다.

  구조적으로도 닿을 수 없다: 산출물이 ``.md`` 라서
  ``command_executor.plan_artifact_run`` 이 ``not a .py artifact`` 로 거절한다.

## 🔒 sanitize 계약과의 관계

``bridge/sanitize.py`` 가 raw_response 를 통째로 버리는 것은 **보안 계약**이고,
그 목적은 apply 경로 보호다 — 유출된 ``diff --git`` 이 apply-ready change 로
오인돼 자기수정이 도는 것을 막는 것. 읽기 전용 리포트로 가는 답변 텍스트는 그
계약을 위반하지 않지만, **구조적으로** apply 에 닿을 수 없어야 한다:

  - 이 레인의 change 는 pod 블록에도 ``proposed_file_changes`` 에도 안 섞인다.
    사다리의 **별도 인자**(``extra_answer_changes``)로만 들어간다.
  - 그리고 그건 문자열 검사가 아니라 **테스트가 구조로** 지킨다
    (``tests/v8_1/test_answer_product_2026_08_06.py``).

Default-OFF (``AGI_V8_SI_ANSWER_PRODUCT_ENABLED``): 게이트 off ⇒ 사이클이 이
모듈을 import 조차 하지 않고 payload 는 이전과 byte-identical 이다.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Callable, Mapping

from agi_v8_1.policy.fail_fast import (
    format_exception_for_log,
    safe_exception_type_name,
    swallowed as _swallowed,
)
# 정의는 한 곳에서만. 이 해시는 ``si_proposed/objective/<objhash8>/`` 와
# ``f1i3_<objhash>_<n>`` 제안 id 를 키하는 바로 그 값이다 — 사본을 만들면
# 답변 파일과 제안 id 를 **조인할 수 없게** 되고, 그게 오늘 오진의 뿌리였다.
from agi_v8_1.si_lanes.objective_proposer import _objective_hash as objective_hash

logger = logging.getLogger(__name__)

_ENABLE_ENV = "AGI_V8_SI_ANSWER_PRODUCT_ENABLED"
# 사다리 repo_root(== SI state_dir) 기준 상대 경로. ``si_proposed/`` 가 **아니다** —
# 거긴 자가수정 제안이 사는 곳이고, 답변이 거기 있으면 언젠가 제안으로 읽힌다.
_ANSWER_DIR_REL = "workspace/answers"
_AGENT_NAME = "si_answer_product"
_SCHEMA_VERSION = "agi_v8_1_si_answer_product_v1"
#: 한 답변의 상한. 넘으면 **조용히 자르지 않고 통째로 no-op** 한다 — 잘린 답을
#: 답이라고 내놓는 것이 이 레인의 유일한 치명적 실패 모양이다.
_MAX_ANSWER_CHARS = 120_000

_SYSTEM_PROMPT = (
    "You are answering a question for a human reader. You are NOT proposing "
    "code changes. Reply with JSON matching the schema: a Markdown document "
    "that answers the objective directly and completely, plus a short title. "
    "Write for someone who will read the document as-is: lead with the answer, "
    "then the reasoning and the numbers behind it. Use tables where a "
    "comparison is asked for. If you do not know something, say so explicitly "
    "instead of guessing — an admitted gap is useful, a confident invention is "
    "not. Do not emit a patch, a diff, or a source file. Output JSON only — "
    "no prose outside the JSON, no markdown fences around it."
)

_ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["answer"],
    "properties": {
        "title": {"type": "string"},
        "answer": {"type": "string"},
        "unknowns": {"type": "array", "items": {"type": "string"}},
    },
}

# (agent_name, prompt, payload, schema) -> json_str — inc2 와 같은 seam 모양.
ProposeFn = Callable[[str, str, Mapping[str, Any], Mapping[str, Any]], str]

__all__ = [
    "ProposeFn",
    "answer_rel_path",
    "build_payload",
    "enabled",
    "objective_hash",
    "propose_answer",
    "render_document",
]


def enabled() -> bool:
    """Default-OFF 게이트 (strict ``"true"``/``"1"``)."""
    return os.environ.get(_ENABLE_ENV, "") in ("true", "1")  # tier: T1


def answer_rel_path(objective: str, cycle_id: str) -> str:
    """젤 상대 경로 — **로컬 계산**, 모델이 준 값은 쓰지 않는다.

    파일명 앞부분이 ``objhash8`` 인 이유: 원장의 제안 id ``f1i3_<objhash>_<n>`` 과
    **같은 해시**라서, 답변 파일 하나를 보고 그 목표가 어느 사이클에서 어떤
    제안을 냈는지 원장에서 바로 찾을 수 있다.

    ``cycle_id`` 는 보수적 charset 으로 소독 + 길이 제한한다 — 악의적이거나
    깨진 cycle id 가 쓰기를 디렉터리 밖으로 끌고 갈 수 없게. (``SafeAutoApply``
    가 어차피 젤 밖을 거절하지만, 방어는 겹쳐 둔다.)
    """
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(cycle_id))[:48].strip("._-") or "cycle"
    return f"{_ANSWER_DIR_REL}/{objective_hash(str(objective))}_{safe}.md"


def build_payload(objective: str) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """``(system_prompt, user_payload, schema)``.

    시스템 프롬프트는 **상수**다 — 목표 텍스트는 데이터이고, 여기에도 아래
    어디에도 목표 종류별 분기가 없다(schema-driven, mode enum 없음).
    """
    return _SYSTEM_PROMPT, {"task": "answer_objective",
                            "objective": str(objective)}, dict(_ANSWER_SCHEMA)


def _parse_answer(raw_json: Any) -> tuple[str, str, list[str]] | None:
    """``(answer, title, unknowns)`` 또는 None.

    Fail-safe → None (절대 raise 안 함): 깨진 JSON, 객체 아님, ``answer`` 없음/빈
    문자열, 상한 초과가 전부 no-op 이다. **부분적으로 형성된 답변은 절대 내지
    않는다** — 반쪽 답변은 사람이 읽고 답이라고 믿기 때문에 무응답보다 나쁘다.

    모델이 ``path``/``action`` 키를 보내도 **여기서 읽지 않는다**(구성상 무시).
    """
    if isinstance(raw_json, (str, bytes)):
        try:
            raw_json = json.loads(raw_json)
        except (ValueError, TypeError) as exc:
            _swallowed(exc, site="si_lanes.answer_product._parse_answer",
                       category="verify")
            logger.warning(
                "answer_product: unparseable JSON (%s)",
                safe_exception_type_name(exc),
            )
            return None
    if not isinstance(raw_json, Mapping):
        logger.warning("answer_product: non-object JSON response")
        return None
    answer = raw_json.get("answer")
    if not isinstance(answer, str) or not answer.strip():
        logger.warning("answer_product: missing/empty answer field")
        return None
    if len(answer) > _MAX_ANSWER_CHARS:
        # ⛔ 자르지 않는다. 잘린 답을 답으로 내는 것보다 없는 게 낫다.
        logger.warning("answer_product: answer too long (%d > %d chars) — dropped",
                       len(answer), _MAX_ANSWER_CHARS)
        return None
    title = raw_json.get("title")
    unknowns = raw_json.get("unknowns")
    return (
        answer,
        title.strip() if isinstance(title, str) and title.strip() else "",
        [str(u) for u in unknowns] if isinstance(unknowns, list) else [],
    )


def render_document(
    *, objective: str, title: str, answer: str, unknowns: list[str], cycle_id: str,
) -> str:
    """디스크에 남는 최종 문서.

    ⚠️ 머리말은 **하네스가** 쓴다(모델이 아니라). 그래서 이 문서를 나중에 혼자
    발견한 사람도 *"이건 한 모델이 한 사이클에 낸 답이고, 아무도 검증하지
    않았다"* 는 것을 파일만 보고 안다 — 원장을 안 열어도.
    """
    head = [
        f"# {title or str(objective).strip()[:120]}",
        "",
        f"> 목표: {str(objective).strip()}",
        f"> 사이클: `{cycle_id}` · 레인: `{_AGENT_NAME}` · 스키마: `{_SCHEMA_VERSION}`",
        ">",
        "> ⚠️ 이건 **한 모델이 한 번에 낸 답**이다. 교차검증도 채점도 안 거쳤다.",
        "",
        "---",
        "",
        answer.strip(),
    ]
    if unknowns:
        head += ["", "---", "", "## 모델이 스스로 모른다고 한 것", ""]
        head += [f"- {u}" for u in unknowns]
    return "\n".join(head) + "\n"


def propose_answer(
    *, objective: str, cycle_id: str, propose_fn: ProposeFn,
) -> dict[str, Any]:
    """답변 문서 하나를 요청하고 사다리 모양으로 돌려준다.

    반환: ``{"proposed_answer_changes": [...], "no_op": bool, "reason": str,
    "title": str, "path": str|None, "chars": int, "cost": Any}``.

    ⛔ ``proposed_artifact_runs`` 를 **내지 않는다** — 이 레인엔 실행이 없다
    (모듈 독스트링의 "다르다" 참조).
    """
    def _no_op(reason: str) -> dict[str, Any]:
        return {"proposed_answer_changes": [], "no_op": True, "reason": reason,
                "title": "", "path": None, "chars": 0, "cost": None}

    if not str(objective or "").strip():
        return _no_op("empty objective")
    prompt, payload, schema = build_payload(objective)
    try:
        raw = propose_fn(_AGENT_NAME, prompt, payload, schema)
    except Exception as exc:  # noqa: BLE001 — provider 실패는 무응답 사이클이다
        _swallowed(exc, site="si_lanes.answer_product.propose_answer",
                   category="verify")
        logger.warning(
            "answer_product: propose_fn failed: %s",
            format_exception_for_log(exc),
        )
        return _no_op(f"propose_fn_error:{safe_exception_type_name(exc)}")
    parsed = _parse_answer(raw)
    if parsed is None:
        return _no_op("unusable_response")
    answer, title, unknowns = parsed
    rel = answer_rel_path(objective, cycle_id)
    doc = render_document(objective=objective, title=title, answer=answer,
                          unknowns=unknowns, cycle_id=cycle_id)
    return {
        "proposed_answer_changes": [{
            "path": rel,            # 로컬 계산
            "action": "create",     # 하드코딩
            "content": doc,         # 모델이 준 **유일한** 값(본문)
            "source": _AGENT_NAME,
            "schema_version": _SCHEMA_VERSION,
        }],
        "no_op": False,
        "reason": "ok",
        "title": title,
        "path": rel,
        "chars": len(doc),
        "cost": getattr(propose_fn, "last_usage", None),
    }
