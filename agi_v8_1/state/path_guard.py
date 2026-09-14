"""agi_v8_1 path-guard primitives — single source of truth for slug + containment.

# __SLOT_W2A1__

W3.L3 audit (raw/w3_l3_path_containment_audit.md, HIGH §6) flagged 5 copies of
``_validate_slug`` / ``_require_under`` scattered across audit_history,
swarm_runs_seed, stores/memory_store_v8, stores/dsup_shield_v8 and
stores/checkpoint_manager_v8 (under-only). Bodies are byte-identical
(checkpoint_manager_v8 uses ``resolve(strict=False)`` which is the default), so
this module consolidates them without changing semantics.

Containment semantics are deliberately conservative:

* ``validate_slug`` accepts a single path component, refuses anything outside
  ``[A-Za-z0-9][A-Za-z0-9._-]*`` or any string containing ``..`` (so an
  attacker cannot smuggle a traversal via embedded characters even if the
  regex allowed it). Null bytes / slashes / backslashes are rejected
  implicitly by the character class.
* ``require_under`` resolves *root* and *target* (including ``~`` expansion)
  then asserts the result is ``relative_to(root)``. Raises ``RuntimeError`` if
  the target escapes the root. Symlink chains are followed during ``resolve``
  so the post-resolution path is checked, not the pre-resolution one — this
  matches the legacy behaviour exactly.
* ``resolve_sink`` (__SLOT_LEDGER_SINK_2026_08_08__) is the *ledger-sink* entry
  point used by the 8 resolvers that take a ledger destination from a dedicated
  env var. Unlike the two above it is **normalize-always, contain-only-on-gate**:
  containment cannot default ON because pinning a ledger into ``tmp`` via those
  env vars is a legitimate, load-bearing pattern (the P0-a live-pollution guard
  assumes it). It normalizes the **parent only** and hands the final component
  back untouched, precisely so the ``O_NOFOLLOW`` refusal in ``state/store``
  keeps working — see the function docstring. It must not ``.strip()``.

This module is import-time pure (regex compile + stdlib only); no
provider / subprocess / network call. Importable from every callsite without
risk of cycles because ``state/__init__.py`` only re-exports ``store`` symbols
and never reaches into stores/audit_history/swarm_runs_seed.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Mapping

__all__ = ["validate_slug", "require_under", "resolve_sink", "default_sink_root"]


# __SLOT_W2A1__ canonical slug regex — must equal the per-module copies it
# replaces (audit_history/loader.py:51, swarm_runs_seed/loader.py:47,
# stores/memory_store_v8.py:25, stores/dsup_shield_v8.py:25).
_SAFE_SLUG_RE: re.Pattern[str] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def validate_slug(value: str, *, what: str) -> str:
    """Validate a single path-component slug and return it unchanged.

    # __SLOT_W2A1__

    Raises:
        ValueError: if *value* is not a safe slug. Safe slugs match
            ``^[A-Za-z0-9][A-Za-z0-9._-]*$`` and contain no ``..`` substring
            (so ``foo..bar`` is also rejected, matching the legacy behaviour).
    """

    text = str(value)
    if not _SAFE_SLUG_RE.fullmatch(text) or text in {".", ".."} or ".." in text:
        raise ValueError(f"unsafe {what}: {value!r}")
    return text


def require_under(root: Path, target: Path, *, what: str) -> Path:
    """Resolve *target* and assert it stays under *root*.

    # __SLOT_W2A1__

    Both *root* and *target* are resolved (with ``expanduser`` on *target* to
    match the legacy bodies). Symlink targets count toward containment because
    ``Path.resolve`` follows them — this is identical to the five legacy
    copies and intentionally preserved here. Use ``state/store._reject_symlink_target``
    or kernel-level ``O_NOFOLLOW`` for write-time symlink refusal; this helper
    handles the *containment* concern only.

    Raises:
        RuntimeError: if *target* resolves outside *root*.

    Returns:
        The resolved target path.
    """

    root_resolved = root.resolve()
    resolved = target.expanduser().resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError:
        raise RuntimeError(
            f"refusing {what}: {resolved} escapes {root_resolved}"
        )
    return resolved


# --- ledger sink resolution ------------------------------------------------
# __SLOT_LEDGER_SINK_2026_08_08__ 원장 목적지를 전용 env 에서 축자 그대로 받던
# 해석기 8곳(executor_log / falsifier_bus / tuple_store / homeostasis /
# g9_shadow / cross_model_budget / sia.ledger / sia.ablation)의 공용 입구.
#
# ⚠️ containment 를 기본으로 켜지 않는다: 테스트·운영이 이 env 들로 원장을 tmp 에
# 핀하는 것이 정당한 패턴이고 P0-a 라이브 오염 가드가 그 패턴을 전제한다. 따라서
# 항상 하는 일은 **정규화(expanduser+resolve)** 뿐이고, state_dir 밑 강제는
# ``AGI_V8_LEDGER_SINK_STRICT`` (default-OFF) 가 켜졌을 때만 한다.
#
# ⛔ truthiness 판정은 여기서 하지 않는다. ``.strip()`` 으로 공백만 값을 '미설정'
# 으로 접으면 executor_log._pytest_unpinned_live_fallback / runtime.first_run 의
# **원시 env truthiness** 판정과 갈라져 "핀이 있다고 믿는데 라이브 소스트리 state/
# 에 append" (P0-a 가 막은 오염 경로) 가 재발한다. 호출자가 이미 걸러 낸 non-empty
# 값만 받는다.
_ENV_SINK_STRICT = "AGI_V8_LEDGER_SINK_STRICT"


def _sink_strict_enabled(env: Mapping[str, str] | None = None) -> bool:
    """``AGI_V8_LEDGER_SINK_STRICT`` 게이트. 미설정/빈값/그 외 = OFF."""

    e: Mapping[str, str] = os.environ if env is None else env
    return e.get(_ENV_SINK_STRICT, "").strip().lower() in {"1", "true", "yes", "on"}  # tier: T2


def _expanduser_total(value: str) -> str:
    """``~`` 확장의 **총함수** 판 — 확장 불가면 입력을 그대로 돌려준다.

    ``pathlib.Path.expanduser`` 는 확장 불가한 ``~user`` 에서 ``RuntimeError``
    를 던진다(실측 py3.13). 원장 해석기는 삼키는 호출자 안에서 도니 던지면 행이
    조용히 사라진다 — :func:`resolve_sink` 독스트링의 D2 문단 참조.
    """

    return os.path.expanduser(value)


def default_sink_root(env: Mapping[str, str] | None = None) -> Path:
    """strict 모드의 containment 루트.

    ``AGI_V8_STATE_DIR`` → ``AGI_STATE_DIR`` → 이 파일의 부모(= 레포 ``state/``).
    strict ON + STATE_DIR 핀 조합이 정상 운영 패턴(tmp 핀)이라 루트도 env 를
    따라가야 tmp 핀이 strict 아래에서 산다.
    """

    e: Mapping[str, str] = os.environ if env is None else env
    raw = e.get("AGI_V8_STATE_DIR") or e.get("AGI_STATE_DIR")
    if raw:
        return Path(raw).expanduser().resolve()
    # agi_v8_1/state/path_guard.py -> agi_v8_1/state
    return Path(__file__).resolve().parent


def resolve_sink(value: Path | str, *, what: str, root: Path | None = None) -> Path:
    """원장 싱크 경로를 정규화(+게이트 ON 시 containment)해서 돌려준다.

    항상: ``~`` 확장 + **부모만** ``resolve`` — 리터럴 ``~`` / 상대경로 / ``..``
    가 파일시스템에 그대로 새겨지는 것을 막는다.

    🔑 최종 컴포넌트는 **역참조하지 않는다.** 무조건 ``.resolve()`` 를 걸면 싱크가
    심링크일 때 반환값이 이미 착지점이 되어, ``state/store.atomic_append_jsonl``
    의 ``O_NOFOLLOW`` 심링크 거부(__SLOT_W1A3__)가 볼 심링크가 사라진다 —
    '심링크를 통한 원장 append' 가 성공해 버린다(2026-08-08 라이브 재현). 이
    모듈이 :func:`require_under` 독스트링에서 스스로 선언한 분업("심링크 거부는
    store/O_NOFOLLOW 몫")을 지키려면 최종 컴포넌트가 호출자에게 그대로 가야 한다.
    부모 쪽 심링크는 어차피 커널이 traversal 에서 따라가므로 resolve 해도 잃는
    방어가 없고, ``..`` 접기·절대화라는 T4 의 목적은 전부 부모에서 달성된다.

    ``AGI_V8_LEDGER_SINK_STRICT`` 가 켜졌을 때만 :func:`require_under` 로
    *root* (기본 :func:`default_sink_root`) 밑을 강제하고, 벗어나면
    ``RuntimeError`` (fail-closed). ⚠️ **검사용 경로와 반환 경로가 다르다**:
    containment 은 ``require_under`` 의 의미론대로 *완전* resolve 한 착지점을
    검사해야 최종 심링크를 통한 탈출을 잡고, 반환값은 위 이유로 최종 컴포넌트를
    보존해야 쓰기 시점 ``O_NOFOLLOW`` 가 산다. 두 층이 각각 다른 질문("어디에
    떨어지나" / "무엇을 open 하나")에 답하므로 경로가 갈라지는 게 정상이다.

    ⛔ ``Path.expanduser`` 대신 ``os.path.expanduser`` 를 쓴다 — 전자는 확장
    불가한 ``~user`` 에서 ``RuntimeError`` 를 던진다(실측 py3.13:
    ``Path('~nosuchuser/x').expanduser()``). 이 함수는 쓰기 사이트의 광역
    ``except`` 안에서 불리므로(executor_log:223 · falsifier_bus ``_swallowed``)
    여기서 던지면 그 원장 행이 **통째로 조용히 유실**된다. ``os.path.expanduser``
    는 확장 불가면 문자열을 그대로 돌려주는(POSIX 셸과 같은) 총함수라, 병든 값도
    최소한 어딘가에 기록은 남는다. 확장 실패를 '거부'로 승격하고 싶다면 그건
    STRICT 게이트가 할 일이지(그 아래선 containment 가 잡는다) OFF 경로가 할
    일이 아니다.

    🔴 2026-08-18 정정 — 위 2026-08-08 서명은 py3.13 에서만 쟀고 이 레포의
    테스트 인터프리터(py3.12, 로컬 테스트 환경)에서는 **틀렸다**
    (``tests/v8_1/test_ledger_sink_resolve_2026_08_08.py`` 를 그 인터프리터로
    돌려 직접 재현): ``Path.resolve(strict=False)`` 는 py3.12 부터 realpath
    뒤에 ``.stat()`` 재확인을 하나 더 걸어 심링크 루프를 ``RuntimeError`` 로
    승격한다(py3.13 의 realpath-직결 구현과 다름). 게다가
    ``os.path.realpath(..., strict=False)`` 자체도 **권한거부 컴포넌트**
    (예: 비-root 에서 ``/proc/1/root``)에서는 최선경로 대신 ``PermissionError``
    를 던진다는 게 실측으로 드러났다 — "심링크 루프·부재·권한거부 셋 다 예외
    없이 접는다"던 원 주장은 권한거부 쪽이 거짓이었다. 그래서 이 함수 하나
    범위로만 방어를 되살린다: 정규화 실패 시 파일시스템을 더 건드리지 않는
    ``os.path.abspath`` (``os.getcwd()`` 말고 아무 syscall 도 안 함 — 위에서
    유일하게 인정됐던 raise 원천과 동일) 로 물러난다. ``os.path.realpath`` 를
    쓰는 이유는 py3.12 pathlib 의 추가 ``.stat()`` 승격을 피해서다.
    """

    expanded = Path(_expanduser_total(str(value)))
    if _sink_strict_enabled():
        # 검사만 한다 — 반환값은 아래의 최종-컴포넌트-보존 경로다 (독스트링 참조).
        require_under(root or default_sink_root(), expanded, what=what)
    try:
        parent = Path(os.path.realpath(str(expanded.parent), strict=False))
    except OSError:
        # 심링크 루프는 위 realpath 가 이미 예외 없이 접는다 — 여기 걸리는 건
        # 권한거부 등 realpath 조차 못 접는 잔여 경로뿐이다(실측, 위 참조).
        parent = Path(os.path.normpath(os.path.abspath(str(expanded.parent))))
    # ``/`` 처럼 name 이 빈 경로는 부모가 곧 자신이다 (실측 왕복 보존).
    return parent / expanded.name if expanded.name else parent
