# __SLOT_SI_PROPOSAL_ARCHIVE_2026_08_23__ 거부된 제안의 **본문**을 보존한다.
"""제안된 변경을 본문까지 에피소드 아래에 durable 하게 남기는 보존기.

## 왜 이 파일이 있나 (2026-08-23 트랙1 실측)

유효 목표카드 5장 × 3반복 = 15 캠페인을 완주시켰더니 루프가 **40건의 패치를
제안**했고 관문이 **전부** 거부했다. 문제는 거부가 아니라 **제안 내용이 아무
데도 안 남는다**는 것이다 — 실측:

* ``<episode>/si_dispatcher_v8_chain.jsonl`` 7행의 payload 가 **전부 ``{}``**
* ``<episode>/apply_chain.jsonl`` 은 ``apply_proposal_ids: []`` /
  ``apply_change_count: 0``
* ``<episode>/runtime_logs/si_verify_gate_log.jsonl`` 은 집계 수치(ok/ran/
  passed/mapped/dropped)만 있고 **패치 본문이 없다**

⇒ *"관문이 막아서 실패했나, 패치가 애초에 틀렸나"* 를 영원히 못 가른다. 특히
``pytest_verdict_requires_trusted_review`` 로 거부된 3건은 보호 테스트 31개를
**전부 통과한** 패치였는데, 그게 카드를 실제로 만족시켰을지는 지금 측정 불가다.
그 3건의 본문이 있었다면 사람이 5분 만에 판정했을 질문이다.

이 모듈은 그 본문을 남긴다. **판정은 하지 않는다** — 보존기다.

## 조사해서 확정한 입력 스키마 (추측 아님)

``si_lanes/exec_arm.py::_parse_changes`` 가 실제로 만드는 change 원소이고,
``si_lanes/verify_gate.py::_map_changes`` 가 실제로 소비하는 모양이다::

    {
      "path": "runtime/goal_campaign.py",   # 레포상대. 카드가 준 값(모델 값 아님)
      "action": "modify",                    # "create" 도 있다(advisory 경로)
      "content": "<파일의 새 **전체 내용**>",  # 🔑 diff 가 아니다. 전문이다.
      "source": "<제안 에이전트 이름>",
      "schema_version": "...",
      "proposal_id": "...",                 # 있을 수도, 없을 수도(없으면 부재)
    }

🔑 ``content`` 는 **diff 가 아니라 새 파일 전문**이다. 이걸 읽어서 확인했다
(``exec_arm._parse_changes`` 는 ``MAX_FILE_CHARS`` 초과 시 자르지 않고 통째로
버린다 — 잘린 소스는 문법도 의미도 망가지므로). 따라서 이 보존기가 남기는
본문도 전문이고, 상한을 넘길 때 무슨 일이 있었는지를 **명시**해야 한다.

``verdict`` 는 ``verify_gate.verify_changes(...)`` 의 반환 dict 그대로다::

    {"ok", "ran", "returncode", "passed", "failed", "error",
     "mapped", "unmapped", "dropped", "reason", "accepted_changes",
     "tail", "backend", "completion_verified", "pytest_exit_zero",
     "output_truncated", "failed_tests"}

## 산출물 배치 (에피소드 상대)

    <episode>/runtime_logs/proposal_archive/index.jsonl
    <episode>/runtime_logs/proposal_archive/bodies/<cycle>/<seq>__<flat>.txt

``si_proposed/`` 를 쓰지 않는 이유: 거긴 advisory 레인이 **실제로 쓰는** 곳이라
(실측: ``si_proposed/objective/b0063849/oak_planks_bot.js``) 보존기 산출물과
제안 산출물이 섞이면 "이건 누가 썼나"가 다시 안 갈린다.

## 계약

* **default-OFF** (:data:`ENV_ENABLED`, strict ``"true"``/``"1"``). OFF 면
  :func:`archive_cycle` 은 파일시스템을 **건드리지 않는다** — 디렉터리도 안
  만든다. 종전과 byte-identical.
* **호출자는 SI 오케스트레이터 한 곳이다.** ``self_improvement_v8`` 이 검증 전
  제안 본문과 검증 판정을 보존해 :func:`archive_cycle` 로 넘긴다. 이 모듈은
  여전히 순수 입력→파일 함수이며, 판정·적용 권한을 갖지 않는다.
* **비밀**: 민감 경로(``.env``·``*.pem``·``*.key``·credential 류)는 본문을
  **안 남기고 경로+길이만** 남긴다. 그 밖의 본문은 레포 정본 약한 마스커
  (``policy.secret_masker.mask_text``) + 이 모듈의 보강 엔트로피 패스
  (:func:`_looks_like_prefixless_secret` — 접두사도 키워드도 없는 32자+
  랜덤 base64/hex)를 통과시키고, 무엇이든 바꿨으면 ``body_redacted: true``
  로 남긴다. 파일명(:func:`_flatten`)과 ``row['path']`` 는 **둘 다** 강한
  마스커(``policy.fail_fast.format_text_for_critical_record`` →
  ``mask_secrets_strict_guarded``)를 쓴다 — 짧은 경로 문자열이라 그 층의
  40+ 캐치올이 물어도 legibility 손실만 있지 소스 의미가 깨지지 않는다.

  🔴 2026-08-24 (H3 적대검증 수리) — 왜 본문엔 강한 마스커를 그대로 안
  쓰는지 **실측**했다: 실 레포 ``.py`` 소스 무작위 200파일 표본에 강한
  마스커(``mask_secrets_strict_guarded``)를 그대로 돌리면 정상 식별자가
  대량으로 부서진다(초기 80파일 표본으로 39/80 — ``__SLOT_..._2026_08_10__``
  류 상수, ``def test_아주_긴_함수명(...)`` 류 스네이크케이스 전부 그 층의
  ``\b[A-Za-z0-9_-]{40,}\b`` 캐치올에 걸렸다 — ``providers.base._mask_secret``
  실측). 보강 엔트로피 패스(레포 identifier idiom 화이트리스트 포함, 아래
  :func:`_is_repo_identifier_idiom`)로 같은 200파일 표본을 다시 재면 그
  idiom 밖에서 걸린 오탐이 **3건**으로 줄었다 — 전부 sha256 파일해시
  체크섬 리터럴이었다(``tests/v8_1/test_dashboard_recovery_2026_07_26.py``
  등). ⚠️ 이건 못 닫은 잔여다: hex 체크섬과 진짜 무작위 비밀은 정규식·
  엔트로피만으로 구별 불가능하고, 이 모듈은 안전 쪽(과다차단)으로 접는다.

  ✅ **2026-08-25 수리(R21 §9 구멍 a)** — DB 연결문자열(``postgres://
  user:PASS@host/db``)의 userinfo 자격증명은 이제 약한 마스커
  (``policy.secret_masker.mask_text``)가 잡는다(``__SLOT_MASKER_URI_
  USERINFO_2026_08_25__``). 강한 마스커(``providers.base._mask_secret``)는
  여전히 손 안 댔다 — 그쪽은 이 모듈 소유 밖이고, 본문 파이프라인은 약한
  마스커만 쓰므로(위 실측 이유) 이 수리로 충분하다.

  ✅ **2026-08-25 수리(구멍 b+c)** — :func:`_is_repo_identifier_idiom` 의
  대문자 전용 분기가 밑줄 유무를 안 봐서 밑줄 없는 대문자+숫자 랜덤 값이
  그대로 통과했고(b), 소문자+밑줄 분기는 세그먼트 *내용*을 안 봐서 무작위
  세그먼트가 섞여도 "snake_case 다"로 접었다(c). 두 분기를 대소문자 무관
  단일 세그먼트-모양 규칙으로 합쳤다(:func:`_segments_look_like_words`).
  자세한 실측(200+1638파일 전수 재스윕, 회귀 0건)은 그 함수 주석 참조.

  ⛔ **여전히 못 닫은 것**: 위 두 수리 뒤에도 SHA-256 등 hex **체크섬**은
  진짜 무작위 비밀과 구별 불가능해 여전히 마스킹된다(과다차단, 안 고침 —
  이 함수 docstring 의 측정된 한계 그대로).
* **파일 권한 · TTL/retention · 저장 위치** (2026-08-25 확인, 트랙 C §9
  4번 — 구현은 이 트랙 소유가 아니라 **명세만** 남긴다):
  - 저장 위치는 이미 젤 안이다 — :func:`archive_dir` = ``episode_dir /
    runtime_logs / proposal_archive``, ``episode_dir`` 는 모듈 docstring이
    명시한 대로 SI 의 ``state_dir`` 이다. 실트리 쓰기 없음(확인함).
  - 본문 파일(:func:`_write_body` → ``atomic_write_text``)은 ``tempfile.
    mkstemp`` 로 만들어져 커널 기본값 0o600 이다(확인함, 코드 재작성 불필요).
  - ``index.jsonl``(``atomic_append_jsonl``)은 레포 정본 게이트
    (``state.store.strict_perms_enabled()``)를 그대로 물려받는다 — 이 모듈이
    독자 정책을 만들지 않는다(단일 진실원 유지).
  - **TTL/retention 은 미구현이다** — 이 모듈에 오래된 사이클 디렉터리를
    지우거나 상한을 두는 코드가 없다(확인함, grep 0건). 스펙만 남긴다: 별도
    보존기(예: 에피소드 GC 트랙)가 ``runtime_logs/proposal_archive/`` 를
    다른 에피소드 산출물과 같은 정책으로 청소해야 한다 — 이 모듈 자신이
    스스로를 지우는 코드를 갖는 건 범위 밖(별건, 트랙 C 소유 아님).
* **상한**: 초과분은 자르되 **잘랐다는 사실을 행과 파일 양쪽에 명시**한다.
  조용한 절단은 이 레포가 이미 한 번 덴 결함이다(``output_truncated`` 가
  ran=True 인 채 passed=0 을 만들어 "실패 0개"라는 거짓 신호를 투표에 넣었다).
* **미측정은 미측정**: verdict 가 없거나 ``accepted_changes`` 를 안 실었으면
  ``disposition`` 은 ``None`` 이다. ⛔ 모르는 값을 "blocked"/0 으로 접지 않는다.
* 예외는 전부 이름 붙은 지점에서 ``swallowed`` 로 센다. 조용한 except 없음.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from agi_v8_1.policy.fail_fast import (
    CATEGORY_CONFIG,
    CATEGORY_PERSIST,
    CATEGORY_TELEMETRY,
    format_text_for_critical_record as _fmt,
    safe_exception_type_name as _exc_name,
    swallowed as _swallowed,
)
from agi_v8_1.policy.secret_masker import mask_text
from agi_v8_1.state.store import atomic_append_jsonl, atomic_write_text

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "si_proposal_archive_v1"

#: default-OFF 게이트. ⛔ 이름을 조합으로 만들지 않는다 — 리터럴 상수 하나를
#: 그대로 ``os.environ.get`` 에 먹인다(2026-08-22 실측: 식별자를 계산으로 만들면
#: 게이트 스캐너가 그 이름을 못 본다).
ENV_ENABLED = "AGI_V8_SI_PROPOSAL_ARCHIVE_ENABLED"  # tier: T9
#: 본문 1건당 문자 상한 override. 값이 없거나 못 읽으면 :data:`DEFAULT_MAX_BODY_CHARS`.
ENV_MAX_BODY_CHARS = "AGI_V8_SI_PROPOSAL_ARCHIVE_MAX_BODY_CHARS"  # tier: T9

#: 본문 1건당 상한. ``exec_arm.MAX_FILE_CHARS`` 보다 넉넉히 잡되 무한은 아니다 —
#: 한 사이클이 원장 디스크를 통째로 먹는 경로를 막는다.
DEFAULT_MAX_BODY_CHARS = 200_000
#: 본문 파일을 만드는 change 개수 상한(사이클당). 초과분도 **행은 남는다** —
#: 본문만 빠지고 ``body_omitted_reason="over_count_cap"`` 이 붙는다.
MAX_BODIES_PER_CYCLE = 64
#: 문자 상한의 하한선. 0/음수를 주면 본문이 전부 빈 문자열이 되어 "보존했다"는
#: 행만 남고 내용은 없는, 이 모듈이 존재하는 이유와 정반대인 상태가 된다.
MIN_MAX_BODY_CHARS = 1_024

_ARCHIVE_REL = ("runtime_logs", "proposal_archive")
_INDEX_NAME = "index.jsonl"
_BODIES_DIRNAME = "bodies"
_OBJECTIVE_HEAD_CHARS = 120
_PATH_FIELD_CHARS = 400
_REASON_FIELD_CHARS = 300
_FLAT_NAME_CHARS = 96

#: 절단 마커. 행(``body_truncated``)만이 아니라 **파일 안에도** 박는다 —
#: 나중에 본문 파일만 열어본 사람이 완전본으로 오독하는 걸 막는다.
TRUNCATION_MARKER_PREFIX = "\n\n<<<PROPOSAL_ARCHIVE_TRUNCATED"


# ---------------------------------------------------------------------------
# 게이트 / 설정
# ---------------------------------------------------------------------------
def enabled() -> bool:
    """Default-OFF (strict ``"true"``/``"1"``, 형제 SI 게이트와 같은 판정).

    OFF 면 :func:`archive_cycle` 은 경로 계산조차 하지 않고 즉시 돌아온다.
    truthy 검사도 ``.lower() != "false"`` 도 쓰지 않는다.
    """
    return os.environ.get(ENV_ENABLED, "") in ("true", "1")  # tier: T9


def max_body_chars() -> int:
    """본문 1건당 문자 상한.

    env 가 비었거나 정수가 아니면 :data:`DEFAULT_MAX_BODY_CHARS`. ⚠️ 파싱
    실패를 조용히 삼키지 않는다(이름 붙은 지점에서 센다) — 오타 난 상한이
    "쟀는데 기본값"과 구분되지 않으면 나중에 원장이 거짓말을 한다.
    """
    raw = os.environ.get(ENV_MAX_BODY_CHARS, "").strip()  # tier: T9
    if not raw:
        return DEFAULT_MAX_BODY_CHARS
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        _swallowed(exc, site="si_lanes.proposal_archive.max_body_chars:parse",
                   category=CATEGORY_CONFIG)
        return DEFAULT_MAX_BODY_CHARS
    return value if value >= MIN_MAX_BODY_CHARS else MIN_MAX_BODY_CHARS


def archive_dir(episode_dir: Path | str) -> Path:
    """보존기 산출물 루트(에피소드 아래). 게이트와 무관한 순수 경로 계산."""
    return Path(episode_dir).joinpath(*_ARCHIVE_REL)


def index_path(episode_dir: Path | str) -> Path:
    """``index.jsonl`` 절대경로."""
    return archive_dir(episode_dir) / _INDEX_NAME


# ---------------------------------------------------------------------------
# 민감 경로 판별
# ---------------------------------------------------------------------------
# ⚠️ **좁게** 잡는다. ``runtime/workspace_snapshot.py`` 가 2026-08-02 에 실측한
# 함정: 이름에 ``secret`` 이 들어간다고 거르면 ``policy/secret_masker.py`` 와 그
# 테스트 3종이 통째로 사라진다. 그래서 ``secret`` 은 **데이터 파일 확장자와
# 함께**일 때만 잡고, 소스 확장자(``.py`` 등)는 절대 안 걸린다.
#
# 여기서 workspace_snapshot 의 술어를 import 하지 않는 이유: 그쪽 질문은 *"무엇을
# 젤로 복사할 것인가"*(추적 여부에 따라 답이 갈린다)이고 이쪽 질문은 *"무엇을
# 원장에 본문으로 적어도 되는가"* 다. 전자는 추적된 ``*.pem`` 을 통과시키는데
# (프로젝트가 넣기로 결정한 자산이므로) 원장에는 그래도 안 적는다.
_ENV_FILE_RE = re.compile(
    r"(^|/)\.env(\.(?!example$|sample$|template$|dist$).*)?$", re.I)
_SECRET_FILE_RE = re.compile(
    r"(^|/)("
    r"\.netrc|\.npmrc|\.pypirc|\.git-credentials|\.htpasswd|"
    r"credentials?|"                                  # 확장자 없는 그 이름 자체
    r"id_(?:rsa|dsa|ecdsa|ed25519)(?:\.[^/]*)?|"
    r"[^/]*\.(?:pem|key|p12|pfx|jks|keystore|ppk|asc|kdbx)|"
    # 비밀스러운 **이름 + 설정/데이터 확장자** 조합일 때만. 확장자 조건을 빼면
    # ``runtime/credentials_docs.md`` 같은 산문까지 본문을 잃는다(실측: 이
    # 파일의 첫 정규식이 그랬다). 소스 확장자(``.py``/``.md``)는 여기 없고,
    # 그런 파일에 박힌 키는 본문 마스커가 2차로 잡는다 — 방어는 2겹이다.
    r"[^/]*(?:secret|password|passwd|token|apikey|api_key|credential)[^/]*"
    r"\.(?:json|ya?ml|ini|toml|txt|cfg|conf|env|xml|properties)"
    r")$", re.I)
_SECRET_DIR_RE = re.compile(
    r"(^|/)(\.ssh|\.aws|\.gnupg|\.docker|\.kube|gcloud)(/|$)", re.I)


def is_sensitive_path(path: Any) -> bool:
    """이 경로의 **본문**을 원장에 적으면 안 되는가.

    True 면 :func:`archive_cycle` 은 본문 파일을 만들지 않고 경로와 길이만
    남긴다(``body_omitted_reason="sensitive_path"``). 판별 불가능한 입력
    (non-str 등)은 **fail-closed 로 True** 다 — 모르는 경로에 본문을 적느니
    한 건 덜 보존하는 게 낫다.
    """
    if not isinstance(path, str) or not path:
        return True
    norm = path.replace("\\", "/").strip()
    if not norm:
        return True
    return bool(
        _ENV_FILE_RE.search(norm)
        or _SECRET_FILE_RE.search(norm)
        or _SECRET_DIR_RE.search(norm)
    )


# ---------------------------------------------------------------------------
# 파일명 만들기 (경로 탈출 불가)
# ---------------------------------------------------------------------------
_UNSAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def _flatten(rel_path: str) -> str:
    """제안 경로를 **단일 파일명**으로 접는다 — ``..`` 도 절대경로도 못 빠져나간다.

    구분자를 ``__`` 로 바꾸고 나머지 불안전 문자를 ``_`` 로 만든 뒤 길이를 자른다.
    자르면 충돌이 생길 수 있으므로 짧은 digest 를 항상 뒤에 붙인다.

    ⚠️ 마스킹을 **먼저** 한다. 원장의 ``path`` 필드는 마스커를 통과하는데
    파일명만 원문이면, 경로 자체에 키가 박힌 제안에서 디렉터리 목록이 그
    키를 그대로 드러낸다(마스킹 층이 하나만 있으면 그게 바로 새는 구멍이다).

    🔴 2026-08-24 (H3 적대검증 수리) — **강한** 마스커
    (:data:`_fmt` = ``policy.fail_fast.format_text_for_critical_record``)를
    쓴다. 종전엔 ``mask_text``(약한 마스커)만 썼는데, ``row['path']`` 는
    이미 강한 마스커를 쓰고 있어(:func:`_archive_cycle_inner` 의 ``path``
    필드) 두 층이 어긋나 있었다 — 경로에 접두사 없는 랜덤 키가 박히면
    ``row['path']`` 는 가려지는데 디렉터리의 실제 파일명은 그대로 샜다.
    본문과 달리 여기서 강한 마스커의 오탐(40+ 캐치올이 긴 식별자를 문다)은
    안전하다 — 파일명은 이미 sanitize + digest 로 한 번 더 접히므로 소스
    의미가 깨질 자리가 없고, 잃는 건 사람이 읽는 legibility 뿐이다(모듈
    docstring 의 실측 트레이드오프 참조).
    """
    masked = _fmt(rel_path, max_chars=_PATH_FIELD_CHARS, one_line=True,
                  keep="head")
    if not isinstance(masked, str):
        masked = rel_path
    flat = _UNSAFE_NAME_RE.sub("_", masked.replace("\\", "/").replace("/", "__"))
    flat = flat.strip("._") or "unnamed"
    return f"{flat[:_FLAT_NAME_CHARS]}-{_digest(masked)[:8]}"


def _cycle_slug(cycle_id: Any) -> str:
    """사이클 디렉터리명. 임의 문자열이 와도 한 컴포넌트 안에 갇힌다."""
    raw = "" if cycle_id is None else str(cycle_id)
    slug = _UNSAFE_NAME_RE.sub("_", raw).strip("._")[:64] or "cycle"
    return f"{slug}-{_digest(raw)[:8]}"


# ---------------------------------------------------------------------------
# 판정 귀속
# ---------------------------------------------------------------------------
def _same_change(a: Any, b: Any) -> bool:
    """같은 제안인가 — 동일 객체 우선, 아니면 (path, action, content) 동치."""
    if a is b:
        return True
    if not isinstance(a, Mapping) or not isinstance(b, Mapping):
        return False
    return (
        a.get("path") == b.get("path")
        and a.get("action") == b.get("action")
        and a.get("content") == b.get("content")
    )


def _in(change: Any, pool: Sequence[Any] | None) -> bool:
    return any(_same_change(change, other) for other in (pool or ()))


def _attribute(
    change: Any,
    *,
    verdict: Mapping[str, Any] | None,
    mapped_changes: Sequence[Any] | None,
    dropped_changes: Sequence[Any] | None,
) -> tuple[str | None, str | None, str | None]:
    """``(disposition, mapped_attribution, basis)`` — 모르면 ``None``.

    🔑 ``verify_changes`` 는 mapped/dropped 를 **개수로만** 반환하고 목록은
    안 준다(``accepted_changes`` = unmapped 만). 그래서 호출자가
    ``mapped_changes``/``dropped_changes`` 를 직접 넘겨주지 않으면 per-change
    mapped-vs-dropped 귀속은 **측정되지 않는다** — 그걸 batch 수치로 추정해
    적으면 원장이 "쟀다"고 거짓말한다. ``None`` 을 남긴다.
    """
    mapped_attr: str | None = None
    if mapped_changes is not None or dropped_changes is not None:
        if _in(change, mapped_changes):
            mapped_attr = "mapped"
        elif _in(change, dropped_changes):
            mapped_attr = "dropped"

    if verdict is None:
        return None, mapped_attr, None
    if "accepted_changes" not in verdict:
        # verdict 는 왔는데 수용 목록이 없다 = 귀속 근거가 없다.
        return None, mapped_attr, "verdict_missing_accepted_changes"
    accepted = verdict.get("accepted_changes")
    if not isinstance(accepted, (list, tuple)):
        return None, mapped_attr, "verdict_accepted_changes_not_a_sequence"
    disposition = "accepted" if _in(change, accepted) else "blocked"
    if mapped_attr is None and disposition == "accepted":
        # unmapped(advisory) 만이 accepted 로 나온다 — verify_gate 의 계약이다.
        mapped_attr = "unmapped"
    return disposition, mapped_attr, "verdict_accepted_changes"


def _int_or_none(value: Any) -> int | None:
    """정수면 정수, 아니면 ``None``. ⛔ ``or 0`` 로 접지 않는다."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    return None


def _bool_or_none(value: Any) -> bool | None:
    return bool(value) if isinstance(value, bool) else None


# ---------------------------------------------------------------------------
# 보강 엔트로피 마스킹 — 접두사도 키워드도 없는 본문 비밀
# ---------------------------------------------------------------------------
# 🔴 2026-08-24 (H3 적대검증) — ``policy.secret_masker.mask_text`` 는 알려진
# 접두사(``sk-``/``AKIA``/``AIza``/``gh*``/``xox*``)나 키워드+구분자
# (``token:``/``password=``)가 있어야만 잡는다. 접두사도 키워드도 없는 순수
# 랜덤 base64/hex 값(Datadog/NewRelic 류 실제 키 포맷)은 그대로 통과한다.
# 이 패스가 그 틈을 잡는다 — **본문 전용**이다(파일명/경로는 위 :func:`_flatten`
# 처럼 강한 마스커를 쓴다. 이유는 모듈 docstring 의 실측 트레이드오프 참조).
_ENTROPY_RUN_RE = re.compile(r"[A-Za-z0-9+/_-]{32,}={0,2}")
_HEX_RUN_RE = re.compile(r"^[0-9a-fA-F]{32,64}$")
_ENTROPY_MASK = "<PROPOSAL_ARCHIVE_ENTROPY_MASKED>"


#: 코드네임 세그먼트 허용 길이 — 이 레포의 라운드/슬롯 코드네임 관용구
#: (``B3``/``R25``/``W7``/``P0``/``H3``/``A2``/``C8``/``V8``/``T9``/``R14``/
#: ``W1A2``/``EX1``/``EX2`` 류, 이 파일 자신의 docstring 에도 ``R21``/``H3``
#: 실측(2026-08-25, ``__SLOT_..___`` 전수 grep, mixed alnum 세그먼트만):
#: 2~4자가 대부분이고 5자(``RANK1``/``STEP2``/``W1A10``), 6자(``BASE64``/
#: ``STAGE1``/``STAGE2``/``STAGE3``, + 레포 다른 곳의 ``SHA256``)까지 실존,
#: 7자 이상은 0건. 순수 알파/순수 숫자만 허용했던 1라운드는
#: ``__SLOT_..._2026_08_10__`` 류가 대량 회귀했다(``B3``/``W7``/``RANK1``
#: 같은 코드네임 세그먼트가 섞여 있어서). 길이 상한을 두면 그 회귀는 닫히고,
#: 진짜 무작위 비밀 세그먼트(엔트로피 run 안에서 문자+숫자가 뒤섞인 조각)는
#: 보통 이보다 훨씬 길다(합성 재현: ``9x7k2m4p1q`` 10자) — 그래도 공격자가
#: **일부러** 6자 이하로 잘게 쪼갠 비밀은 이 휴리스틱을 피해간다(측정된
#: 잔여, 안 고침 — 이 함수의 다른 잔여들과 같은 부류).
_CODENAME_SEGMENT_MAX_LEN = 6
#: __SLOT_MASKER_ALPHA_RUN_CAP_2026_08_25__ 긴 알파 세그먼트를 "영어 단어"로
#: 인정할 최소 모음 비율. ⛔ **길이만으로는 못 가른다** — 실측으로 확인했다:
#: ``DENOMINATOR``(11)·``RESERVATION``(11) 같은 정당한 레포 단어와
#: ``GHIJKLMNOPQ``(11, base32 계열 비밀 모양)가 **같은 길이**다. 길이 상한 10 을
#: 걸었더니 `__SLOT_CAP_COUNTS_ITS_OWN_DENOMINATOR_2026_08_03__` 이 비밀로
#: 오탐돼 proposal_archive wiring 테스트가 `redacted 0→1` 로 깨졌다.
#: 모음 비율은 그 둘을 가른다 — 0.45/0.45 vs 0.18.
_WORD_VOWEL_RATIO_MIN = 0.25
# ⚠️ ``y`` 를 포함한다 — ``STRINGIFY``(I,I,Y=3/9=0.33) 같은 실제 레포 단어가
# 없으면 2/9=0.22 로 문턱 아래에 떨어진다(실측: test_repo_identifiers_survive_
# the_entropy_pass 가 잡았다). 비밀 모양과의 간격도 넓어진다(0.18 vs 0.33).
_VOWELS = frozenset("aeiouyAEIOUY")


def _alpha_segment_is_wordlike(s: str) -> bool:
    """긴 알파 세그먼트가 발음 가능한 단어처럼 보이는가.

    __SLOT_MASKER_ALPHA_RUN_CAP_2026_08_25__ 🔴 자기정정 2단계. 1단계(길이 상한)는
    적대검증 F-2 가 잡은 회귀(``s.isalpha()`` 무제한 → 비밀이 화이트리스트 통과)를
    닫았지만 정당한 긴 단어를 같이 죽였다. 짧은 것은 종전대로 길이로,
    긴 것은 **모음 비율**로 가른다.
    """
    if len(s) <= _CODENAME_SEGMENT_MAX_LEN:
        return True
    return (sum(1 for ch in s if ch in _VOWELS) / len(s)) >= _WORD_VOWEL_RATIO_MIN


def _segments_look_like_words(core: str) -> bool:
    """``core`` 를 ``_`` 로 나눈 각 세그먼트가 identifier 조각으로 보이는가.

    # __SLOT_MASKER_IDENTIFIER_SHAPE_2026_08_25__ R21 §9 구멍 (c) 수리.
    레포 identifier idiom(``__SLOT_..._2026_08_10__``, ``test_dashboard_
    recovery_2026_07_26``, ``AGI_V8_SI_PROPOSAL_ARCHIVE_ENABLED``)은 항상 이
    모양이다 — 날짜/버전 숫자는 별도 세그먼트로 떨어지고(``2026``,``08``,
    ``24``), 코드네임 세그먼트(``B3``/``V8``)는 짧다. 진짜 랜덤 비밀
    (패스워드 생성기 산출물 포함, ``my_super_secret_9x7k2m4p1q`` 류)은
    세그먼트 하나가 길고 문자+숫자가 뒤섞이는 게 흔하다 — 그런 세그먼트가
    하나라도 있으면 identifier idiom 이 아니다로 판정한다.
    """
    # 엔트로피 run 자체가 ``_``/``/``/``-`` 를 전부 구분자로 허용하므로(레포
    # 상대경로 리터럴이 run 에 그대로 들어온다 — 실측: 이 200파일 스윕에서
    # ``tests/v8_1/test_..._2026_08_09`` 류 경로 문자열이 잡혔다), 세그먼트
    # 분리도 같은 구분자 집합을 쓴다 — 안 그러면 ``tests/v8`` 처럼 ``/`` 가
    # 안 나뉜 조각이 통째로 "짧지 않은 mixed 세그먼트"로 오판된다.
    segments = [s for s in re.split(r"[_/+-]", core) if s]
    if len(segments) < 2:
        return False
    # __SLOT_MASKER_ALPHA_RUN_CAP_2026_08_25__ 🔴 자기정정 — 적대검증 F-2 회수.
    # ``s.isalpha()`` 에 **길이 상한이 없어서** 순수 알파 long run 이 통째로
    # "단어 세그먼트"로 통과했다. 증인: 이 레포 자신의 테스트 상수
    # ``_OVERLAP_SECRET = "GHIJKLMNOPQrest_of_secret_1234567890"``
    # (tests/v8_1/test_publish_scan_2026_08_08.py:833) — 76cab96 이전에는
    # ``_looks_like_prefixless_secret`` 이 True 였는데(마스킹됨) 이 수리 뒤
    # ``_is_repo_identifier_idiom`` 이 True 가 되어(화이트리스트 통과)
    # **마스킹이 사라졌다.** 구멍 (c) 를 막으려던 수리가 새 구멍을 판 것이다.
    #
    # 진짜 영어 단어 세그먼트는 길이가 유계다(``PROPOSAL``·``dashboard``·
    # ``recovery``·``IDENTIFIER`` 전부 ≤10). base32/hex 계열 비밀은 그보다
    # 길게 이어진다. 알파 세그먼트에도 상한을 걸어 그 둘을 가른다.
    # ⛔ 숫자 세그먼트에는 상한을 안 건다 — 날짜/버전(``20260825``)이 정당하게 길다.
    return all(
        (s.isalpha() and _alpha_segment_is_wordlike(s))
        or s.isdigit()
        or len(s) <= _CODENAME_SEGMENT_MAX_LEN
        for s in segments
    )


def _is_repo_identifier_idiom(run: str) -> bool:
    """이 레포가 실제로 쓰는 식별자 관용구인가 — 보강 패스의 화이트리스트.

    실측(2026-08-24, 실 레포 ``.py`` 소스 무작위 200파일): 이 화이트리스트
    없이 엔트로피 패스만 돌리면 ``__SLOT_..._2026_08_10__`` 류 상수와
    ``test_아주_긴_함수명`` 류 스네이크케이스가 다수 걸렸다(둘 다 32자+
    연속 알파벳/숫자/밑줄 run 이라 길이 조건만으로는 못 가른다). 이 술어가
    그 두 idiom 을 걸러낸다 — 같은 표본으로 재측정하면 idiom 밖 오탐이
    3건으로 줄었다(아래 :func:`_looks_like_prefixless_secret` 참조).

    # __SLOT_MASKER_IDENTIFIER_SHAPE_2026_08_25__ R21 §9 구멍 (b)+(c) 수리
    # (2026-08-25, 재검증 실측) — 종전엔 대문자 전용 분기가 **밑줄 여부를 안
    # 봤다**(``core.upper() == core`` 만 확인) — ``QWERTYUIOPASDFGHJKLZXCVBNM
    # 123456`` 처럼 밑줄 없는 대문자+숫자 랜덤 run 이 이 idiom 으로 그대로
    # 통과했다(구멍 b). 그리고 소문자 분기는 밑줄만 요구하고 **세그먼트 모양은
    # 안 봤다** — ``my_super_secret_9x7k2m4p1q`` 처럼 세그먼트 하나가 문자+
    # 숫자 뒤섞인 run 도 "밑줄 섞인 소문자"라는 이유만으로 통과했다(구멍 c).
    # 두 분기를 대소문자 무관 단일 규칙으로 합친다 — **밑줄 필수 + 모든
    # 세그먼트가 순수 알파 또는 순수 숫자**(:func:`_segments_look_like_words`).
    # 이건 "엔트로피/문맥 판정을 대소문자에 안 기대게"(구멍 b 지시)를 identifier
    # 판정에도 적용한 것이고, 세그먼트 모양 조건이 "문맥으로 좁혀라"(구멍 c
    # 지시)가 요구한 좁히기다 — 원안대로 따옴표/``=``/``:`` 우변 문맥을 직접
    # 보는 대신 세그먼트 모양을 썼다: 이 파일의 실제 ``ENV_ENABLED =
    # "AGI_V8_SI_PROPOSAL_ARCHIVE_ENABLED"`` 같은 관용구(env 변수 이름을 따옴표
    # 안에서 ``=`` 우변으로 참조)가 레포 전역에 흔해, 따옴표/``=`` 문맥만으로
    # 좁히면 그 흔한 관용구 자체가 새 오탐이 됐다(직접 재현: 이 파일 자체의
    # ``ENV_ENABLED``/``ENV_MAX_BODY_CHARS`` 대입문). 세그먼트 모양은 그 함정
    # 없이 같은 목적을 달성한다(아래 회귀 스윕으로 확인).
    """
    if run.startswith("__") and run.endswith("__") and "_" in run.strip("_"):
        inner = run.strip("_")
        if inner and _segments_look_like_words(inner):
            return True  # __SLOT_..._2026_08_10__ 류
        return False
    core = run.strip("_")
    if not core:
        return False
    return _segments_look_like_words(core)


def _looks_like_prefixless_secret(run: str) -> bool:
    """엔트로피 휴리스틱 — 레포 식별자가 아니면서 무작위로 보이는가.

    두 형태를 잡는다:

    1. 순수 hex 32~64자(대문자 전용 또는 소문자 전용) — 접두사 없는 실제
       API 키가 흔히 이 모양이다(Datadog/NewRelic/Mailgun 류).
    2. 문자+숫자가 섞이고 그 비중이 충분히 높은 run — 무작위 base64
       본문의 특징(``policy.fail_fast._looks_like_base64_body`` 와 같은
       밀도 공식, 다만 그쪽은 ``+``/``/`` 필수 조건이 있고 여긴 없다 — 본문은
       ``+``/``/`` 없는 순수 alnum 랜덤 값도 흔하기 때문이다).

    ⚠️ **측정된 한계, 안 고침**: SHA-256 등 hex **체크섬**은 진짜 무작위
    비밀과 정규식·엔트로피만으로는 구별 불가능하다(둘 다 32~64자 균등분포
    hex). 이 함수는 안전 쪽으로 접는다 — 체크섬 리터럴도 마스킹된다(과다
    차단이지 과소 차단이 아니다). 200파일 표본에서 idiom 밖 오탐 3건이
    전부 이 경우였다(``tests/v8_1/test_dashboard_recovery_2026_07_26.py``
    등 파일해시 매니페스트 리터럴).

    # __SLOT_MASKER_IDENTIFIER_SHAPE_2026_08_25__ R21 §9 구멍 (b) 수리
    # (2026-08-25) — 종전엔 ``has_upper and has_lower and has_digit`` 를 전부
    # 요구했다. 대문자 전용(또는 소문자 전용)이면서 접두사 없는 진짜 비밀은
    # 구조적으로 이 게이트를 못 넘어 **밀도 계산 자체에 안 닿았다**(대/소문자
    # 대비 신호가 없으면 그 계산을 하나마나였다는 뜻이 아니라, 대비가 아예
    # 존재하지 않는 입력을 무조건 통과시켰다는 뜻이다). 대소문자가 섞였을 때와
    # 안 섞였을 때를 나눠서, 안 섞였을 때는 대비 대신 숫자 밀도만으로 판정한다
    # — 대비 신호가 없는 만큼 문턱을 더 낮게(더 적은 숫자로도 걸리게) 잡았다.
    """
    body = run.rstrip("=")
    if _is_repo_identifier_idiom(body):
        return False
    if _HEX_RUN_RE.fullmatch(body):
        return True
    has_upper = any(c.isupper() for c in body)
    has_lower = any(c.islower() for c in body)
    has_digit = any(c.isdigit() for c in body)
    if not ((has_upper or has_lower) and has_digit):
        return False
    if has_upper and has_lower:
        dense = sum(1 for c in body if c.isupper() or c.isdigit())
        return dense * 5 >= len(body)
    # 대/소문자 전용(둘 중 하나만) — 대비 신호가 없으므로 숫자 밀도로 대신
    # 판정한다. 단일-케이스 36진법(a-z 또는 A-Z + 0-9) 랜덤 값은 숫자 비중이
    # 대략 10/36 ≈ 28% 근방에 몰린다. 실측(2026-08-25, 이 파일이 소유한 실
    # 레포 200파일 재스윕): 레포 자신의 SCREAMING_SNAKE/snake_case 식별자는
    # 숫자 밀도가 5% 를 거의 안 넘는다(``AGI_V8_STRICT_FAIL_FAST`` 4.3%,
    # ``AGI_V8_SI_PROPOSAL_ARCHIVE_MAX_BODY_CHARS`` 2.4%) — 문턱 12.5%
    # (``dense * 8 >= len``)는 그 위 3배 여유를 두면서도 합성 대문자/소문자
    # 비밀(밀도 12.8~18.8% 로 구성해 재현)은 잡는다.
    digit_dense = sum(1 for c in body if c.isdigit())
    return digit_dense * 8 >= len(body)


def _mask_body_text(content: str) -> str:
    """본문 마스킹 파이프라인 — 약한 마스커(정본) + 보강 엔트로피 패스.

    순서: ``mask_text`` 먼저(알려진 접두사/키워드 형태를 잡는다), 그 결과에
    엔트로피 패스를 돈다(접두사/키워드 없는 랜덤 값을 잡는다). 강한 마스커를
    본문에 통째로 쓰지 않는 이유는 이 모듈 docstring 의 "비밀" 계약 항목과
    2026-08-24 실측 트레이드오프 문단 참조 — 그 층의 40+ 캐치올이 이 레포
    자신의 소스 식별자를 대량으로 부순다.
    """
    out = mask_text(content)
    if not isinstance(out, str):
        return content
    return _ENTROPY_RUN_RE.sub(
        lambda m: (
            _ENTROPY_MASK if _looks_like_prefixless_secret(m.group(0))
            else m.group(0)
        ),
        out,
    )


# ---------------------------------------------------------------------------
# 본문 쓰기
# ---------------------------------------------------------------------------
def _write_body(
    body_file: Path,
    content: str,
    *,
    cap: int,
) -> dict[str, Any]:
    """마스킹 → 상한 절단 → 원자적 쓰기. 절단은 **파일 안에도** 기록한다.

    반환의 ``chars_written`` 은 실제로 보존된 **제안 내용**의 문자 수이고
    절단 마커는 세지 않는다 — :func:`read_archive` 가 이 값으로 정확히
    잘라내어 round-trip 을 복원한다.
    """
    masked = _mask_body_text(content)
    if not isinstance(masked, str):  # 방어적: 파이프라인은 str 만 돌려준다
        masked = content
    original_chars = len(content)
    redacted = masked != content
    truncated = len(masked) > cap
    kept = masked[:cap] if truncated else masked
    payload = kept
    if truncated:
        payload = (
            f"{kept}{TRUNCATION_MARKER_PREFIX} "
            f"kept={len(kept)} masked_total={len(masked)} "
            f"original={original_chars} chars>>>\n"
        )
    atomic_write_text(body_file, payload)
    return {
        "chars_original": original_chars,
        "chars_masked": len(masked),
        "chars_written": len(kept),
        "truncated": truncated,
        "redacted": redacted,
    }


# ---------------------------------------------------------------------------
# 공개 API
# ---------------------------------------------------------------------------
def archive_cycle(
    episode_dir: Path | str,
    *,
    cycle_id: Any,
    changes: Sequence[Any] | None,
    verdict: Mapping[str, Any] | None = None,
    objective: Any = None,
    mapped_changes: Sequence[Any] | None = None,
    dropped_changes: Sequence[Any] | None = None,
    now_ts: float | None = None,
) -> dict[str, Any]:
    """한 사이클이 제안한 변경을 본문까지 보존한다(게이트 OFF 면 no-op).

    입력
    ----
    ``episode_dir``
        에피소드 디렉터리(= SI 의 ``state_dir``). 산출물은 전부 이 아래
        ``runtime_logs/proposal_archive/`` 안에만 생긴다.
    ``cycle_id``
        아무 값이나 와도 된다 — 파일명으로 쓸 땐 sanitize + digest 한다.
    ``changes``
        ``verify_changes`` 에 **들어간** 원본 제안 목록. 각 원소는 위 모듈
        docstring 의 스키마(``path``/``action``/``content``/``source``/
        ``proposal_id``). Mapping 이 아닌 원소도 행은 남는다(신원 없음으로).
    ``verdict``
        ``verify_gate.verify_changes(...)`` 반환 dict. ``None`` 이면 거부
        사유는 **미측정**으로 남는다(추정하지 않는다).
    ``objective``
        목표 문자열(있으면 머리 120자만, 마스킹 후).
    ``mapped_changes`` / ``dropped_changes``
        관문 내부가 실제로 나눈 목록. 넘기면 per-change mapped/dropped 귀속이
        **측정**되고, 안 넘기면 그 필드는 ``None``(미측정)이다.
    ``now_ts``
        테스트용 고정 시각. ``None`` 이면 ``time.time()``.

    반환
    ----
    ``{"archived": bool, "reason": str, "written": int|None,
    "bodies_written": int|None, "truncated": int|None, "redacted": int|None,
    "sensitive_skipped": int|None, "over_count_cap": int|None,
    "index_path": str|None}``

    수치가 ``None`` 이면 **안 쟀다**는 뜻이다(게이트 OFF 또는 보존 자체가 실패).
    0 과 구분된다.
    """
    unmeasured: dict[str, Any] = {
        "written": None, "bodies_written": None, "truncated": None,
        "redacted": None, "sensitive_skipped": None, "over_count_cap": None,
        "index_path": None,
    }
    if not enabled():
        return {"archived": False, "reason": "gate_off", **unmeasured}
    if not changes:
        # 게이트는 켜져 있었다 = 쟀고, 제안이 0건이었다. 파일은 안 만든다.
        return {
            "archived": False, "reason": "no_changes",
            "written": 0, "bodies_written": 0, "truncated": 0, "redacted": 0,
            "sensitive_skipped": 0, "over_count_cap": 0, "index_path": None,
        }
    try:
        return _archive_cycle_inner(
            Path(episode_dir),
            cycle_id=cycle_id,
            changes=list(changes),
            verdict=verdict,
            objective=objective,
            mapped_changes=mapped_changes,
            dropped_changes=dropped_changes,
            now_ts=now_ts,
        )
    except Exception as exc:  # noqa: BLE001 — 보존기가 사이클을 죽이면 안 된다
        _swallowed(exc, site="si_lanes.proposal_archive.archive_cycle:outer",
                   category=CATEGORY_PERSIST)
        logger.warning("proposal_archive: 보존 실패 (%s)", _exc_name(exc))
        return {
            "archived": False,
            "reason": f"archive_error:{_exc_name(exc)}",
            **unmeasured,
        }


def _archive_cycle_inner(
    episode_dir: Path,
    *,
    cycle_id: Any,
    changes: list[Any],
    verdict: Mapping[str, Any] | None,
    objective: Any,
    mapped_changes: Sequence[Any] | None,
    dropped_changes: Sequence[Any] | None,
    now_ts: float | None,
) -> dict[str, Any]:
    root = archive_dir(episode_dir)
    slug = _cycle_slug(cycle_id)
    bodies_root = root / _BODIES_DIRNAME / slug
    cap = max_body_chars()
    ts = float(now_ts) if now_ts is not None else time.time()
    cycle_text = _fmt(cycle_id, max_chars=_REASON_FIELD_CHARS, one_line=True,
                      keep="head")
    objective_head = (
        None if objective is None
        else _fmt(objective, max_chars=_OBJECTIVE_HEAD_CHARS, one_line=True,
                  keep="head")
    )
    v = verdict if isinstance(verdict, Mapping) else None
    verify_reason = (
        None if v is None
        else _fmt(v.get("reason", ""), max_chars=_REASON_FIELD_CHARS,
                  one_line=True, keep="head")
    )

    written = bodies_written = truncated = redacted = 0
    sensitive_skipped = over_count_cap = 0

    for seq, change in enumerate(changes):
        is_mapping = isinstance(change, Mapping)
        raw_path = str(change.get("path", "")) if is_mapping else ""
        content = change.get("content") if is_mapping else None
        disposition, mapped_attr, basis = _attribute(
            change, verdict=v,
            mapped_changes=mapped_changes, dropped_changes=dropped_changes,
        )
        row: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "ts": ts,
            "cycle_id": cycle_text,
            "objective_head": objective_head,
            "seq": seq,
            # ⚠️ ``proposal_id`` 부재는 ``None`` 이다. 빈 문자열로 접으면
            # "신원이 없었다"와 "신원이 빈 문자열이었다"가 같아진다.
            "proposal_id": (
                str(change["proposal_id"])
                if is_mapping and change.get("proposal_id") is not None
                else None
            ),
            "path": _fmt(raw_path, max_chars=_PATH_FIELD_CHARS, one_line=True,
                         keep="head") if is_mapping else None,
            "action": (
                _fmt(change.get("action"), max_chars=64, one_line=True,
                     keep="head")
                if is_mapping and change.get("action") is not None else None
            ),
            "source": (
                _fmt(change.get("source"), max_chars=120, one_line=True,
                     keep="head")
                if is_mapping and change.get("source") is not None else None
            ),
            "well_formed": is_mapping,
            "disposition": disposition,
            "disposition_basis": basis,
            "mapped_attribution": mapped_attr,
            "verify_reason": verify_reason,
            "verdict_ok": None if v is None else _bool_or_none(v.get("ok")),
            "verdict_ran": None if v is None else _bool_or_none(v.get("ran")),
            "verdict_mapped": None if v is None else _int_or_none(v.get("mapped")),
            "verdict_unmapped": None if v is None else _int_or_none(v.get("unmapped")),
            "verdict_dropped": None if v is None else _int_or_none(v.get("dropped")),
            "verdict_passed": None if v is None else _int_or_none(v.get("passed")),
            "verdict_failed": None if v is None else _int_or_none(v.get("failed")),
            "body_path": None,
            "body_omitted_reason": None,
            "body_chars_original": None,
            "body_chars_masked": None,
            "body_chars_written": None,
            "body_truncated": False,
            "body_redacted": False,
            "body_cap_chars": cap,
        }

        if not isinstance(content, str):
            row["body_omitted_reason"] = (
                "not_a_mapping" if not is_mapping else "content_not_a_string"
            )
        elif is_sensitive_path(raw_path):
            # 🔒 본문 대신 **경로 + 길이만**. 길이는 "쟀다"는 사실이라 남긴다.
            row["body_omitted_reason"] = "sensitive_path"
            row["body_chars_original"] = len(content)
            sensitive_skipped += 1
        elif bodies_written >= MAX_BODIES_PER_CYCLE:
            # ⚠️ 조용히 건너뛰지 않는다 — 행에 이유가 박힌다.
            row["body_omitted_reason"] = "over_count_cap"
            row["body_chars_original"] = len(content)
            over_count_cap += 1
        else:
            body_file = bodies_root / f"{seq:04d}__{_flatten(raw_path)}.txt"
            try:
                stats = _write_body(body_file, content, cap=cap)
            except Exception as exc:  # noqa: BLE001 — 한 건 실패가 원장을 못 지운다
                _swallowed(
                    exc,
                    site="si_lanes.proposal_archive._archive_cycle_inner:body",
                    category=CATEGORY_PERSIST,
                )
                row["body_omitted_reason"] = f"write_error:{_exc_name(exc)}"
                row["body_chars_original"] = len(content)
            else:
                row["body_path"] = str(
                    PurePosixPath(*_ARCHIVE_REL) / _BODIES_DIRNAME / slug
                    / body_file.name
                )
                row["body_chars_original"] = stats["chars_original"]
                row["body_chars_masked"] = stats["chars_masked"]
                row["body_chars_written"] = stats["chars_written"]
                row["body_truncated"] = stats["truncated"]
                row["body_redacted"] = stats["redacted"]
                bodies_written += 1
                truncated += int(stats["truncated"])
                redacted += int(stats["redacted"])

        atomic_append_jsonl(index_path(episode_dir), row)
        written += 1

    logger.info(
        "proposal_archive: cycle=%s rows=%d bodies=%d truncated=%d "
        "redacted=%d sensitive=%d over_cap=%d",
        cycle_text, written, bodies_written, truncated, redacted,
        sensitive_skipped, over_count_cap,
    )
    return {
        "archived": True,
        "reason": "ok",
        "written": written,
        "bodies_written": bodies_written,
        "truncated": truncated,
        "redacted": redacted,
        "sensitive_skipped": sensitive_skipped,
        "over_count_cap": over_count_cap,
        "index_path": str(PurePosixPath(*_ARCHIVE_REL) / _INDEX_NAME),
    }


def read_archive(
    episode_dir: Path | str,
    *,
    cycle_id: Any = None,
    with_bodies: bool = True,
) -> list[dict[str, Any]]:
    """보존된 제안을 되읽는다(round-trip). **게이트와 무관** — 읽기는 안 막는다.

    없는 원장을 읽으면 ``[]`` 다. 각 행에 ``body`` 키가 붙는데:

    * ``str``  — 보존된 제안 내용. ``body_truncated`` 가 ``True`` 면 이건
      **앞부분만**이다(``body_chars_written`` 만큼). 절단 마커는 잘라내고
      돌려주되 그 사실은 행의 ``body_truncated`` 가 계속 말한다.
    * ``None`` — 본문이 없다. 이유는 ``body_omitted_reason`` 또는
      ``body_read_error`` 가 말한다. ⛔ 빈 문자열로 접지 않는다.

    ``cycle_id`` 를 주면 그 사이클 행만 돌려준다.
    """
    path = index_path(episode_dir)
    if not path.exists():
        return []
    wanted = (
        None if cycle_id is None
        else _fmt(cycle_id, max_chars=_REASON_FIELD_CHARS, one_line=True,
                  keep="head")
    )
    rows: list[dict[str, Any]] = []
    try:
        raw_lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        _swallowed(exc, site="si_lanes.proposal_archive.read_archive:open",
                   category=CATEGORY_TELEMETRY)
        return []
    for lineno, line in enumerate(raw_lines):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError as exc:
            # 깨진 줄을 **건너뛰지 않는다** — 건너뛰면 "제안이 없었다"와
            # "원장이 깨졌다"가 같아진다. 이 레포가 반복해 데는 그 혼동이다.
            _swallowed(exc, site="si_lanes.proposal_archive.read_archive:parse",
                       category=CATEGORY_TELEMETRY)
            rows.append({
                "schema_version": SCHEMA_VERSION,
                "malformed_line": lineno,
                "body": None,
                "body_read_error": f"malformed_json:{_exc_name(exc)}",
            })
            continue
        if not isinstance(row, dict):
            rows.append({
                "schema_version": SCHEMA_VERSION,
                "malformed_line": lineno,
                "body": None,
                "body_read_error": "row_not_an_object",
            })
            continue
        if wanted is not None and row.get("cycle_id") != wanted:
            continue
        row["body"] = None
        row["body_read_error"] = None
        if with_bodies and row.get("body_path"):
            row.update(_load_body(Path(episode_dir), row))
        rows.append(row)
    return rows


def _load_body(episode_dir: Path, row: Mapping[str, Any]) -> dict[str, Any]:
    """행이 가리키는 본문 파일을 읽어 절단 마커를 걷어낸 내용을 돌려준다."""
    rel = str(row.get("body_path") or "")
    # ⚠️ ``body_path`` 는 원장에서 온 값이다. 원장이 손상·변조되면 이 조인이
    # 에피소드 밖 파일을 읽어 반환값에 실어 나른다 — 보존기가 유출기가 된다.
    # 그래서 읽기 **전에** 보존 루트 안인지 대조한다(fail-closed).
    root = archive_dir(episode_dir).resolve()
    try:
        target = (episode_dir / rel).resolve()
    except OSError as exc:
        _swallowed(exc, site="si_lanes.proposal_archive._load_body:resolve",
                   category=CATEGORY_TELEMETRY)
        return {"body": None, "body_read_error": f"unresolvable:{_exc_name(exc)}"}
    if root != target and root not in target.parents:
        return {"body": None, "body_read_error": "body_path_outside_archive"}
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        _swallowed(exc, site="si_lanes.proposal_archive._load_body:read",
                   category=CATEGORY_TELEMETRY)
        return {"body": None, "body_read_error": f"unreadable:{_exc_name(exc)}"}
    kept = row.get("body_chars_written")
    if isinstance(kept, int) and 0 <= kept <= len(text):
        return {"body": text[:kept], "body_read_error": None}
    # 행이 길이를 안 실었다 = 몇 자가 내용인지 모른다. 마커까지 통째로 준다.
    return {"body": text, "body_read_error": "body_chars_written_missing"}


__all__ = [
    "SCHEMA_VERSION",
    "ENV_ENABLED",
    "ENV_MAX_BODY_CHARS",
    "DEFAULT_MAX_BODY_CHARS",
    "MIN_MAX_BODY_CHARS",
    "MAX_BODIES_PER_CYCLE",
    "TRUNCATION_MARKER_PREFIX",
    "enabled",
    "max_body_chars",
    "archive_dir",
    "index_path",
    "is_sensitive_path",
    "archive_cycle",
    "read_archive",
]
