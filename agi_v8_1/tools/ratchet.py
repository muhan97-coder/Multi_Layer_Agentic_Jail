#!/usr/bin/env python3
# __SLOT_RATCHET_FRAME_2026_08_08__ 두 래칫(게이트 baseline · 조인 커버리지)이
# 공유하는 **최소** 부품. 사용처는 둘뿐이다 — 베이스클래스·레지스트리·플러그인 금지.
"""래칫 공용 부품 — baseline 적재 · 비교 · 렌더 · 동결.

## 계약 한 줄

**정확 일치**다. 악화도 개선도 **둘 다 실패**한다. 처방만 다르다:
악화면 *"코드를 고쳐라"*, 개선·중립이면 *"``--update`` 로 동결해라"*.

그래서 이 부품에는 **slack(여유분/허용오차)이 구조적으로 존재할 수 없다**.
"N 이하면 통과" 라는 임계가 없으므로 임계를 넣을 자리 자체가 없고, 넣으려면
`compare` 를 다시 써야 한다. 래칫이 썩는 흔한 경로 — *"조금 나빠진 건 봐준다"* —
가 이 모듈에서는 표현 불가능하다.

## seam 은 하나뿐

`classify` 만 주입받는다. 두 래칫의 **"악화"의 정의가 다르기 때문**이다
(게이트: 선언이 사라지면 악화 / 조인: 커버리지가 내려가면 악화). 그 외에는
주입점이 없다 — 확장점을 늘리는 순간 두 사용처가 서로 다른 계약으로 갈라진다.

## fail-closed

- baseline **부재는 통과가 아니다** — `BaselineMissing`. 파일이 없으면 비교한
  적이 없는 것이고, 비교 안 한 것은 초록이 아니다.
- 해시 불일치(손편집) → `BaselineTampered(reason="hash")`.
- schema/scope 불일치 → `BaselineTampered(reason=...)` 이되 이건 **"코드가
  나빠졌다"가 아니라 "비교 불가"** 다(`exc.is_refusal is True`). 다른 래칫의
  baseline 을 잘못 물린 것을 회귀로 보고하면 진단이 통째로 틀린다.

⚠️ 런타임 게이트 없음: 이 모듈은 테스트와 읽기전용 CLI 만 쓴다. 사이클 경로에서
import 하지 않으므로 default-OFF env 게이트 대상이 아니다(백로그 4번 명시).

⚠️ 침묵 0: 모든 `except` 는 raise 로 끝난다. 이 모듈에 `swallowed` 호출이
필요해지면 그건 fail-closed 계약이 깨졌다는 신호다.
"""
from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Final, Mapping

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO.parent) not in sys.path:
    sys.path.insert(0, str(_REPO.parent))

# 심볼릭 링크 대상 거부 + tempfile/os.replace + 부모 fsync 를 이미 하는 쓰기.
# ⛔ 여기서 다시 구현하면 그 방어가 **이 경로에서만 사라진다**.
from agi_v8_1.state.store import atomic_write_json  # noqa: E402
from agi_v8_1.policy.fail_fast import (  # noqa: E402
    format_exception_for_sink as _format_exception_for_sink,
    format_text_for_sink as _format_text_for_sink,
)

__all__ = [
    "BaselineMissing", "BaselineTampered", "Diff", "MISSING",
    "WORSE", "BETTER", "NEUTRAL",
    "canonical_sha256", "load_baseline", "compare", "render_diff",
    "write_baseline",
]

#: baseline 문서의 필드. 해시는 **자기 자신을 제외한 문서 전부**를 덮는다 —
#: payload 뿐 아니라 schema·scope·meta·_readme 까지. 손으로 scope 만 바꿔
#: 다른 래칫의 baseline 을 통과시키는 우회를 막는다.
_SCHEMA_KEY: Final = "schema"
_SCOPE_KEY: Final = "scope"
_PAYLOAD_KEY: Final = "payload"
_META_KEY: Final = "meta"
_HASH_KEY: Final = "payload_sha256"
_README_KEY: Final = "_readme"

WORSE: Final = "worse"
BETTER: Final = "better"
NEUTRAL: Final = "neutral"
_VERDICTS: Final = frozenset({WORSE, BETTER, NEUTRAL})

#: `schema`/`scope` 불일치는 **거절**이지 회귀가 아니다.
_REFUSAL_REASONS: Final = frozenset({"schema", "scope"})

_README: Final = (
    "래칫 baseline. 검사는 **정확 일치**다 — 악화도 개선도 둘 다 실패한다. "
    "처방만 다르다(악화=코드를 고쳐라, 개선/중립=소유 도구의 --update 로 동결해라). "
    "정확 일치이므로 slack(여유분/허용오차)은 구조적으로 불가능하다: 임계가 없어서 "
    "넣을 자리가 없다. ⛔ 손으로 고치지 마라 — payload_sha256 이 이 파일 전체를 "
    "(자기 자신만 빼고) 덮으므로 손편집은 BaselineTampered 로 잡힌다."
)


class BaselineMissing(Exception):
    """baseline 이 없다. **통과가 아니다** — 비교한 적이 없다는 뜻이다."""


class BaselineTampered(Exception):
    """baseline 을 그대로 믿을 수 없다 — 손편집·해시/스키마/scope 불일치.

    ``reason`` 은 어느 검사가 걸렸나: ``shape`` · ``unparseable`` · ``hash`` ·
    ``schema`` · ``scope``. ``is_refusal`` 이 참이면 *"코드가 나빠졌다"* 가
    아니라 *"이 baseline 으로는 비교 불가"* 다.
    """

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason

    @property
    def is_refusal(self) -> bool:
        return self.reason in _REFUSAL_REASONS


class _Missing:
    """`classify` 가 받는 '한쪽에 없음' 표식. ``None`` 과 구분된다 —
    ``None`` 은 **기록된 값**이고 이건 **행 자체가 없음**이다."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - 표시 전용
        return "<absent>"


MISSING: Final = _Missing()


@dataclass(frozen=True)
class Diff:
    """baseline 대비 차이. 비어 있으면(그리고 그때만) 통과다.

    ``worse``/``better`` 는 주입된 `classify` 가 붙인 이름표다. 중립 키는 어느
    쪽에도 없지만 **여전히 차이**이므로 `empty` 를 참으로 만들지 못한다.
    """

    added: dict[str, Any]
    removed: dict[str, Any]
    changed: dict[str, tuple[Any, Any]]
    worse: tuple[str, ...] = ()
    better: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not (self.added or self.removed or self.changed)

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(sorted({*self.added, *self.removed, *self.changed}))


def canonical_sha256(obj: Any) -> str:
    """정규 JSON 의 sha256.

    공식은 레포 선례와 **같아야 한다**: ``core/apply_chain.compute_entry_hash``
    와 ``enforcement/apply_chain_full.compute_entry_hash``.
    ⛔ 그쪽을 import 하지 않는다 — 그건 체인 전용 의미론이라 ``entry_hash``·
    ``seq`` 를 정규형에서 **뺀다**. 여기는 주어진 것을 통째로 센다.
    같은 공식임은 회귀 테스트가 **같은 입력을 먹여 값으로** 대조한다
    (텍스트 비교는 사본 드리프트를 못 잡는다).
    """
    canonical = json.dumps(obj, sort_keys=True, ensure_ascii=False,
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_baseline(path: Path | str, *, schema: str, scope: str) -> dict[str, Any]:
    """baseline 문서를 검증하고 돌려준다. 통과 못 하면 **예외**다.

    돌아오는 것은 문서 전체다 — 호출측은 ``doc["payload"]`` 로 비교하고
    ``doc["meta"]`` 로 출처를 말한다.
    """
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        safe_path = _format_text_for_sink(path, max_chars=1000, one_line=True)
        safe_exc = _format_exception_for_sink(exc, max_chars=1000, one_line=True)
        raise BaselineMissing(
            f"baseline 을 읽을 수 없다: {safe_path} ({safe_exc}). "
            f"부재는 통과가 아니다 — 먼저 --update 로 동결하라."
        ) from exc

    try:
        doc = json.loads(raw)
    except ValueError as exc:
        safe_exc = _format_exception_for_sink(exc, max_chars=1000, one_line=True)
        raise BaselineTampered(
            f"baseline 이 JSON 이 아니다: "
            f"{_format_text_for_sink(path, max_chars=1000, one_line=True)} "
            f"({safe_exc})",
            reason="unparseable",
        ) from exc

    if not isinstance(doc, dict):
        raise BaselineTampered(
            f"baseline 최상위가 object 가 아니다: {path} ({type(doc).__name__})",
            reason="shape",
        )
    missing = [k for k in (_SCHEMA_KEY, _SCOPE_KEY, _PAYLOAD_KEY, _HASH_KEY)
               if k not in doc]
    if missing:
        raise BaselineTampered(
            f"baseline 에 없는 필드: {path} — {missing}", reason="shape",
        )
    if not isinstance(doc[_PAYLOAD_KEY], dict):
        raise BaselineTampered(
            f"baseline payload 가 object 가 아니다: {path} — "
            f"{type(doc[_PAYLOAD_KEY]).__name__}",
            reason="shape",
        )

    # 무결성 먼저, 해석은 그 다음. 해시가 깨졌으면 schema/scope 필드 자체를
    # 믿을 근거가 없으므로 그것으로 진단을 만들면 안 된다.
    recorded = doc[_HASH_KEY]
    computed = canonical_sha256({k: v for k, v in doc.items() if k != _HASH_KEY})
    if recorded != computed:
        raise BaselineTampered(
            f"baseline 해시 불일치: {path} — 기록 {recorded!r} vs 계산 "
            f"{computed!r}. 손편집이거나 다른 도구가 썼다.",
            reason="hash",
        )
    if doc[_SCHEMA_KEY] != schema:
        raise BaselineTampered(
            f"비교 불가(회귀 아님): {path} 의 schema 는 {doc[_SCHEMA_KEY]!r} 인데 "
            f"{schema!r} 로 비교하려 했다. 다른 래칫의 baseline 이다.",
            reason="schema",
        )
    if doc[_SCOPE_KEY] != scope:
        raise BaselineTampered(
            f"비교 불가(회귀 아님): {path} 의 scope 는 {doc[_SCOPE_KEY]!r} 인데 "
            f"{scope!r} 로 비교하려 했다. 같은 스키마의 다른 대상이다.",
            reason="scope",
        )
    return doc


def compare(old: Mapping[str, Any], new: Mapping[str, Any], *,
            classify: Callable[[str, Any, Any], str]) -> Diff:
    """`old`(동결) 대비 `new`(실측) 차이.

    `classify(key, old_value, new_value) -> "worse"|"better"|"neutral"`.
    한쪽에 없는 키는 그 자리에 `MISSING` 이 온다. 셋 중 하나가 아닌 값을
    돌려주면 **에러**다 — 오타를 중립으로 접으면 악화가 조용히 사라진다.
    """
    added = {k: new[k] for k in new if k not in old}
    removed = {k: old[k] for k in old if k not in new}
    changed = {k: (old[k], new[k]) for k in old if k in new and old[k] != new[k]}

    worse: list[str] = []
    better: list[str] = []
    pairs: list[tuple[str, Any, Any]] = [
        *((k, MISSING, v) for k, v in added.items()),
        *((k, v, MISSING) for k, v in removed.items()),
        *((k, o, n) for k, (o, n) in changed.items()),
    ]
    for key, old_value, new_value in pairs:
        verdict = classify(key, old_value, new_value)
        if verdict not in _VERDICTS:
            raise ValueError(
                f"classify({key!r}) 가 {verdict!r} 를 돌려줬다 — "
                f"{sorted(_VERDICTS)} 중 하나여야 한다."
            )
        if verdict == WORSE:
            worse.append(key)
        elif verdict == BETTER:
            better.append(key)
    return Diff(added=added, removed=removed, changed=changed,
                worse=tuple(sorted(worse)), better=tuple(sorted(better)))


def _fmt(value: Any) -> str:
    if isinstance(value, _Missing):
        return "<absent>"
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=repr)
    return text if len(text) <= 80 else text[:77] + "..."


def render_diff(diff: Diff, *, fix_hint: str, freeze_hint: str) -> str:
    """실패 메시지. **키 이름을 반드시 적는다** — 개수만 적으면 못 고친다.

    차이가 없으면 빈 문자열(할 말 없음). 악화가 있으면 `fix_hint`, 악화가 아닌
    차이(개선·중립)가 있으면 `freeze_hint`. 둘 다면 둘 다 — 고치는 게 먼저다.
    """
    if diff.empty:
        return ""
    lines = [
        f"baseline 불일치 {len(diff.keys)}건 — 정확 일치가 계약이다"
        f"(악화도 개선도 실패, slack 없음)."
    ]
    if diff.added:
        lines.append(f"  추가 {len(diff.added)}: " + ", ".join(
            f"{k}={_fmt(v)}" for k, v in sorted(diff.added.items())))
    if diff.removed:
        lines.append(f"  삭제 {len(diff.removed)}: " + ", ".join(
            f"{k}={_fmt(v)}" for k, v in sorted(diff.removed.items())))
    if diff.changed:
        lines.append(f"  변경 {len(diff.changed)}: " + ", ".join(
            f"{k}: {_fmt(o)} -> {_fmt(n)}"
            for k, (o, n) in sorted(diff.changed.items())))
    if diff.worse:
        lines.append("  악화: " + ", ".join(diff.worse))
    if diff.better:
        lines.append("  개선: " + ", ".join(diff.better))
    neutral = [k for k in diff.keys if k not in diff.worse and k not in diff.better]
    if neutral:
        lines.append("  중립: " + ", ".join(neutral))
    if diff.worse:
        lines.append(fix_hint)
    if diff.better or neutral:
        lines.append(freeze_hint)
    return "\n".join(lines)


def write_baseline(path: Path | str, payload: Mapping[str, Any],
                   meta: Mapping[str, Any], *, schema: str, scope: str) -> None:
    """baseline 을 동결한다. **``--update`` 경로 전용** — 검사 경로에서 부르지 마라.

    `meta` 는 준 것 그대로 쓴다(자동 주입 없음 → 같은 입력이면 같은 바이트).
    출처를 적는 건 호출측 책임이다: 무엇으로 쟀나 · 언제 · 어느 커밋.
    `schema`/`scope` 는 명시 인자다 — meta 안의 키였다면 오타 하나가 *"영원히
    비교 거절되는 baseline"* 을 조용히 만든다.
    """
    doc = {
        _README_KEY: _README,
        _SCHEMA_KEY: schema,
        _SCOPE_KEY: scope,
        _META_KEY: dict(meta),
        _PAYLOAD_KEY: dict(payload),
    }
    doc[_HASH_KEY] = canonical_sha256(doc)
    atomic_write_json(Path(path), doc)
