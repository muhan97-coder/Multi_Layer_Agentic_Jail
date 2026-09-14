# __SLOT_GATES_MAP_2026_08_07__ 게이트 대장 — 선언과 실제를 **같은 칸에서** 만나게 한다.
"""`DOCS/GATES.ko.md` 생성기 — C0(runtime activation graph)의 마지막 두 칸.

## 왜 이 도구인가

`tools/wiring_map.py` 는 *"호출자가 있나"* 를 본다. `tools/last_fired.py` 는 *"실제로
돌았나"* 를 본다. 남은 칸이 **게이트를 읽는 코드 · 현재 유효값**이고, 그게 이 도구다.

### 🔴 그런데 wiring_map 의 reader 를 그대로 옮기면 안 된다 (2026-08-07 실측)

reader 간선 **443개 중 30개가 유령**이다 — 리더로 적힌 파일에 그 게이트 문자열이
0회 나온다. 그중 **25개가 게이트 하나**(``AGI_V8_SAFE_AUTO_APPLY_FUZZY_CUTOFF``,
레포 전체 리터럴 2곳)에 몰려 있다. 원인::

    tests/.../test_stage2_fuzzy_syntax.py:  env = "AGI_V8_SAFE_AUTO_APPLY_FUZZY_CUTOFF"
      → wiring_map 의 global_consts["env"] 가 **모듈 무관 bare 이름**으로 유일해짐
      → 레포 어디든 f(env) / d[env] 가 그 게이트 읽기로 둔갑

그 오염은 **이미 출고돼 있다**(``DOCS/WIRING.ko.md`` 의 해당 행). WIRING 은 자기 표를
*"의심 목록이지 판결문이 아니다"* 로 방어하지만, GATES 의 "읽는 코드" 칸은 **판결문**이다.

⇒ **스캐폴딩은 재사용, 판정 술어는 신규.** wiring_map 은 건드리지 않는다(그 테스트가
라이브 4팔로 물려 있다).

## 엄격 술어 — wiring_map 대비 다섯 가지를 바꾼다

1. 상수 이름 해소를 **로컬 + 명시 import** 로 제한. 전역 bare-name 폴백 삭제(유령의 원인)
2. 상수 수집에서 ``tests.`` 제외 — 테스트가 프로덕션 판정을 오염시키던 경로
3. ``ast.Subscript`` 는 ``ctx=Load`` 만 — **쓰기를 읽기로 세지 않는다**(40사이트)
4. 첫 인자 고정이 아니라 **전 인자 스캔** (``_is_falsy_env(env, name)`` 은 2번 인자다)
5. ``os.environ`` 뿐 아니라 **매핑 변수 경유**(``e.get``/``src.get``/``env.get``)도 본다

## 판정은 3단이 아니라 5단이다

⛔ *"읽는 코드 0"* 과 *"못 찾았다"* 를 같은 칸에 넣으면 표가 거짓말한다. 실측: 19개
게이트가 전 신호 0 인데 실체는 **테스트에만 언급**이다 — 팬텀으로 세면 "지워도 된다"는
결론이 나오고 테스트가 깨진다.

======================  ====================================================
판정                    뜻
======================  ====================================================
``direct``              프로덕션 코드가 이름으로 읽는다
``indirect``            자료구조/크로스모듈 경유 — 정적 미추적, **고발 아님**
``tests_only``          테스트에서만 언급. 팬텀 아니다
``phantom``             프로덕션 코드에 문자열조차 없다
``read_discarded``      읽어놓고 결과를 안 쓴다
======================  ====================================================

## ⚠️ 이 도구가 **못 하는** 것 — 사람 칸

- **선언등급**(배선됨/이름예약/폐기): CENSUS 4분류 중 셋이 라이브 신호에서 축자 동일
  하게 접힌다. 코드가 갈라내는 건 ``already_wired`` 하나뿐 ⇒ **자동 재생산률 1/4**.
  그래서 등급은 :data:`GRADES_REL` 원장(사람이 쓴다)에서 읽고, 기계 판정과 **다른 칸**에
  둔다. 둘이 모순이면 ⚠️ 만 찍고 **자동으로 어느 쪽도 이기게 하지 않는다.**
- **켜면 뭐가 달라지나**: 산문이다. 같은 원장에서 읽는다.

## 🔑 현재 유효값은 단일 사실이 아니다

    셸 env  >  .env  >  코드 기본값        (dotenv 는 override=False)
    그리고 런타임이 40곳에서 **덮어쓴다**

실측 예: ``.env`` 의 ``AGI_V8_COST_BUDGET_USD=10.0`` 은 실행 중 유효값이 아니다 —
``runtime/tick_runner.py`` 가 틱마다, ``bridge/swarm_dispatch.py`` 가 디스패치마다
다시 쓴다. **예산 게이트가 그렇다.** 그래서 ``runtime_set`` 칸을 따로 낸다.

## ⛔ 비밀

이름이 ``API_KEY``/``SECRET``/``TOKEN`` 류면 값 칸을 **렌더러 레벨에서 강제 마스킹**한다
(``set(len=N)``/``unset``). ``DOCS/`` 는 git 추적된다.

## 🔴 자기검사는 **표본이었다** (2026-08-10 수리)

``--selfcheck`` 는 2026-08-07 부터 팬텀 5 · direct 5 = **10개**만 봤다(``[:5]``,
알파벳 앞에서 자른 고정 표본). 라이브 분모는 431 이라 **2.3%** 였는데 화면은
``selfcheck: 통과`` 한 줄이었다 — *"도구가 자기 결론을 축자 대조한다"* 는 문장이
사거리를 안 적으면 다음 사람은 그걸 전수로 읽는다. 지금은 **전수**이고 화면이
사거리를 같이 찍는다. 근거와 그때 드러난 오탐 1건은 :func:`selfcheck_report`.

비용(라이브 실측, ``selfcheck_report`` 단독): 표본 10 일 때 1.8s → 전수 431 로
바꾸면서 0.5s(게이트마다 트리를 다시 읽던 것을 한 번으로) → 2026-08-10 저녁
``direct`` 축을 AST 문자열 상수 대조로 조이면서 **1.4s**. ⚠️ 세 번째 수치는 앞의
둘보다 **느리다** — 조임의 값이다. 여기 적어두는 이유는, 앞 판이 *"전수가 표본보다
싸다"* 를 자랑처럼 남겨서 다음 사람이 그 문장을 지금도 참인 줄로 읽기 때문이다
(``scan_gates`` 가 어차피 14s 라 전체 CLI 체감은 그대로다).

## 사용

    python3 -m agi_v8_1.tools.gates_map --out DOCS/GATES.ko.md
    python3 -m agi_v8_1.tools.gates_map --json
    python3 -m agi_v8_1.tools.gates_map --selfcheck

위 생성기 진입점의 Python scope는 Git index 추적 경로다(내용은 현재 worktree
바이트). 새 파일은 먼저 stage해야 하며, Git 없는 독립 snapshot은 원본에서 만든
NUL-종료 상대경로 목록을 ``--source-manifest`` 로 넘긴다. discovery 실패를
full-tree walk로 접지 않는다.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import logging
import os
import re
import stat
import subprocess
import time
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

from agi_v8_1.policy.fail_fast import (
    format_exception_for_critical_record,
    record_critical_failure,
    swallowed as _swallowed,
)

logger = logging.getLogger(__name__)

__all__ = [
    "GRADES_REL", "VERDICTS", "GateRow", "scan_gates", "render", "selfcheck",
    "selfcheck_report", "SourceSetError", "tracked_python_files",
    "source_python_files", "load_source_manifest", "main",
]

#: 사람이 쓰는 등급 원장. ⛔ 기계가 안 쓴다 — 자동 1/4 를 4/4 인 척하지 않으려고.
GRADES_REL = "data/gate_grades.json"

VERDICTS = ("direct", "indirect", "tests_only", "phantom", "read_discarded")

#: 게이트 이름 후보. ⚠️ wiring_map 은 ``AGI`` 만 봐서 12키를 놓친다 — 넓힌다.
_NAME_RE = re.compile(
    r"\b(?:AGI|DEEPSEEK|OPENAI|ANTHROPIC|GEMINI|KIS|DART|LOG|KW)_[A-Z0-9_]{2,}\b")
#: ⚠️ 접두 조각이 게이트로 승인되던 버그를 막는다(`AGI_V8_` 이 fullmatch 되던 것).
_PREFIX_FRAGMENT = re.compile(r"^[A-Z]+(?:_V\d+(?:_\d+)?)?_$")

#: 🔒 **머리-벗김 import 간선의 상한(래칫).** :func:`_resolve_import` 이
#: ``agi_v8_1.a.b`` 를 ``a/b.py`` 로 해소하는 경로는 레포 **밖** 패키지
#: (``from foo.bar import X``)도 동명 ``bar.py`` 가 있으면 해소한다 = fail-open.
#: 술어로 닫으려면 트리 이름·설치본에 물려야 하는데 둘 다 거짓 적색을 만든다
#: (워크트리 이름 · editable 설치). ⇒ **표면을 개수로 동결한다.**
#: 초기값은 2026-08-10 라이브 실측(머리-벗김 해소 3건 중 ``via_import`` 인정 간선 1건).
#: 올리는 것은 정당한 복구 경로지만 **리뷰를 받는 한 줄**이어야 한다.
# 2026-08-24 live review: both edges are repository-self imports.  The
# original dashboard edge is joined by runtime.tick_runner ->
# runtime.goal_campaign_feed.ENV_ENABLED (present since 6c9ccf05).  Keep the
# cap equal to the measured surface: any third head-stripped edge is red.
MAX_HEAD_STRIPPED_IMPORT_EDGES = 2

_ENV_ACCESSORS = frozenset({"get", "getenv", "environ"})
_SECRET_HINT = re.compile(r"(API_KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL)", re.I)
_SKIP_DIRS = frozenset({".git", "__pycache__", ".venv", "venv", "node_modules",
                        ".mypy_cache", ".pytest_cache", ".ruff_cache"})

# Generated inventory must not inherit PATH-selected Git or user/system Git
# configuration.  The command below only reads index metadata; it never checks
# out or hashes worktree content, so attributes/clean filters are not involved.
_TRUSTED_GIT = "/usr/bin/git"
_SOURCE_GIT_TIMEOUT_SECONDS = 30.0
_INDEX_MODES = frozenset({"100644", "100755"})


class SourceSetError(RuntimeError):
    """The canonical Python source set could not be established completely."""


def _source_git_env() -> dict[str, str]:
    """Minimal environment for index-only Git discovery.

    ``GIT_NO_LAZY_FETCH`` and protocol denial are defense in depth: ``ls-files``
    should not need an object, and a missing object must never turn discovery
    into execution of a repository-configured promisor/remote helper.
    """
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": "/nonexistent",
        "LANG": "C",
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/bin/false",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_OPTIONAL_LOCKS": "0",
    }


def _source_git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    """Run one non-mutating Git metadata command with executable/config guards."""
    try:
        git_stat = os.stat(_TRUSTED_GIT, follow_symlinks=True)
    except OSError as exc:
        raise SourceSetError("trusted Git executable is unavailable") from exc
    if (not stat.S_ISREG(git_stat.st_mode) or git_stat.st_uid != 0
            or git_stat.st_mode & 0o022):
        raise SourceSetError("trusted Git executable failed ownership/mode check")
    argv = [
        _TRUSTED_GIT, "-C", str(root),
        "-c", "core.hooksPath=/dev/null",
        "-c", "core.fsmonitor=false",
        "-c", "core.untrackedCache=false",
        "-c", "protocol.allow=never",
        "-c", "protocol.ext.allow=never",
        *args,
    ]
    try:
        return subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_source_git_env(),
            timeout=_SOURCE_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SourceSetError("Git index discovery could not run") from exc


def _safe_source_path(root: Path, raw: "str | Path") -> Path:
    """Validate one explicit manifest path without following local symlinks."""
    try:
        candidate = Path(raw)
    except (TypeError, ValueError) as exc:
        raise SourceSetError("source manifest contains an invalid path") from exc
    if "\x00" in os.fspath(candidate):
        raise SourceSetError("source manifest path contains NUL")

    if candidate.is_absolute():
        try:
            rel = candidate.relative_to(root)
        except ValueError as exc:
            raise SourceSetError("source manifest path escapes its root") from exc
    else:
        rel = candidate
    if not rel.parts or any(part in {"", ".", "..", ".git"} for part in rel.parts):
        raise SourceSetError("source manifest contains an unsafe relative path")
    if rel.suffix != ".py":
        raise SourceSetError("source manifest contains a non-Python path")

    current = root
    try:
        for part in rel.parts:
            current = current / part
            mode = current.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise SourceSetError("source manifest path traverses a symlink")
        if not stat.S_ISREG(mode):
            raise SourceSetError("source manifest path is not a regular file")
    except FileNotFoundError as exc:
        raise SourceSetError("source manifest names a missing worktree file") from exc
    except OSError as exc:
        raise SourceSetError("source manifest path could not be inspected") from exc
    return root.joinpath(*rel.parts)


def source_python_files(
    root: "str | Path", files: "Iterable[str | Path]",
) -> list[Path]:
    """Validate an explicit, portable Python source manifest.

    This path intentionally performs no Git call.  A caller may therefore copy
    indexed worktree bytes into an independent snapshot and carry the relative
    manifest alongside them.  Duplicate entries or invalid/missing paths that
    the manifest actually lists are errors, not silently deduplicated scope changes.
    """
    try:
        root_path = Path(root).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SourceSetError("source root is unavailable") from exc
    if not root_path.is_dir():
        raise SourceSetError("source root is not a directory")

    out: list[Path] = []
    seen: set[str] = set()
    try:
        iterator = iter(files)
    except TypeError as exc:
        raise SourceSetError("source manifest is not iterable") from exc
    for raw in iterator:
        path = _safe_source_path(root_path, raw)
        rel = path.relative_to(root_path).as_posix()
        if rel in seen:
            raise SourceSetError("source manifest contains a duplicate path")
        seen.add(rel)
        out.append(path)
    if not out:
        raise SourceSetError("source manifest is empty")
    return sorted(out, key=lambda p: p.relative_to(root_path).as_posix())


def tracked_python_files(root: "str | Path") -> list[Path]:
    """Return canonical index-tracked ``*.py`` paths, reading worktree bytes later.

    New files enter this set only after ``git add``.  Modified tracked files keep
    their worktree bytes, so generators still describe the proposed local edit.
    Index parse errors, unmerged entries, unsafe modes/paths, missing files, and
    non-repositories all fail closed; there is no filesystem-walk fallback.
    """
    try:
        root_path = Path(root).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SourceSetError("source root is unavailable") from exc
    if not root_path.is_dir():
        raise SourceSetError("source root is not a directory")

    top = _source_git(root_path, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        raise SourceSetError("source root is not a Git worktree")
    try:
        reported_top = Path(os.fsdecode(top.stdout.rstrip(b"\n"))).resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise SourceSetError("Git returned an invalid worktree root") from exc
    if reported_top != root_path:
        raise SourceSetError("source root must be the Git worktree top level")

    listed = _source_git(
        root_path, "ls-files", "-z", "--cached", "--stage", "--full-name",
        "--", "*.py",
    )
    if listed.returncode != 0:
        raise SourceSetError("git ls-files could not define the source set")

    rels: list[str] = []
    seen: set[str] = set()
    for record in listed.stdout.split(b"\0"):
        if not record:
            continue
        try:
            raw_meta, raw_path = record.split(b"\t", 1)
            mode, oid, stage = raw_meta.decode("ascii").split()
            rel = raw_path.decode("utf-8", errors="strict")
        except (UnicodeDecodeError, ValueError) as exc:
            raise SourceSetError("git ls-files returned malformed index data") from exc
        if (mode not in _INDEX_MODES or stage != "0"
                or len(oid) not in {40, 64}
                or any(ch not in "0123456789abcdef" for ch in oid)):
            raise SourceSetError("Git index contains an unsupported Python entry")
        parts = PurePosixPath(rel).parts
        if (not parts or PurePosixPath(rel).is_absolute()
                or any(part in {"", ".", "..", ".git"} for part in parts)
                or not rel.endswith(".py") or rel in seen):
            raise SourceSetError("Git index contains an unsafe Python path")
        seen.add(rel)
        rels.append(rel)
    return source_python_files(root_path, rels)


def load_source_manifest(path: "str | Path") -> list[str]:
    """Read a NUL-terminated UTF-8 path list (the ``git ls-files -z`` shape)."""
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise SourceSetError("source manifest could not be read") from exc
    if raw and not raw.endswith(b"\0"):
        raise SourceSetError("source manifest must be NUL-terminated")
    try:
        return [part.decode("utf-8", errors="strict")
                for part in raw.split(b"\0") if part]
    except UnicodeDecodeError as exc:
        raise SourceSetError("source manifest is not valid UTF-8") from exc


@dataclass
class GateRow:
    name: str
    verdict: str = "phantom"
    #: file:line 로 적는다 — 모듈명이 아니라. ⚠️ 판결문 칸이므로 정확해야 한다.
    readers: list[str] = field(default_factory=list)
    indirect: list[str] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)
    #: 읽는 지점의 2번째 인자(리터럴). 지점마다 다르면 **여러 개**가 담긴다.
    defaults: list[str] = field(default_factory=list)
    #: 진리값을 판정하는 헬퍼 file:symbol. 게이트마다 어휘가 다르다.
    parsers: list[str] = field(default_factory=list)
    #: 런타임이 이 게이트를 덮어쓰는 곳. 있으면 .env 값은 유효값이 아니다.
    runtime_set: list[str] = field(default_factory=list)
    in_env: bool = False
    in_example: bool = False
    env_value: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {k: (sorted(v) if isinstance(v, list) else v)
                for k, v in self.__dict__.items()}


def _py_files(root: Path) -> list[Path]:
    out = []
    for p in sorted(root.rglob("*.py")):
        if any(x in p.relative_to(root).parts for x in _SKIP_DIRS):
            continue
        out.append(p)
    return out


def _is_test(rel: str) -> bool:
    return rel.startswith("tests/") or "/tests/" in rel


def _candidate(name: str) -> bool:
    """게이트 이름인가. ⛔ 접두 조각은 아니다."""
    return bool(_NAME_RE.fullmatch(name)) and not _PREFIX_FRAGMENT.match(name)


def _env_file(path: Path) -> dict[str, str]:
    """`.env` 파싱. ⛔ ``^KEY=`` 만 보면 ``KEY = v`` 형태를 놓친다."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        _swallowed(exc, site="tools.gates_map._env_file", category="persist")
        return out
    for line in text.splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        k, v = raw.split("=", 1)
        k = k.strip()
        if not k or not k[0].isalpha():
            continue
        out[k] = v.split("#", 1)[0].strip()      # 뒤가 이긴다(dotenv 동작)
    return out


class _FileScan(ast.NodeVisitor):
    """한 파일에서 게이트 접근을 뽑는다. ⚠️ 이름 해소는 **로컬/명시 import 만**."""

    def __init__(self, rel: str, imported: Mapping[str, str]) -> None:
        self.rel = rel
        #: 이 파일 안에서 정의된 상수 + 명시 import 로 들어온 상수. ⛔ 전역 폴백 없음.
        self.consts: dict[str, str] = dict(imported)
        self.reads: list[tuple[str, int, str]] = []      # (게이트, 행, 종류)
        self.writes: list[tuple[str, int]] = []
        self.discarded: set[str] = set()
        self._used: set[str] = set()
        self._assigned: dict[str, tuple[str, int]] = {}

    # ── 상수 수집 ──────────────────────────────────────────────────────
    def visit_Assign(self, node: ast.Assign) -> None:
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            v = node.value.value
            if _candidate(v):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        self.consts[t.id] = v
        # ③ 쓰기: os.environ["X"] = ... 는 **읽기가 아니다**
        for t in node.targets:
            g = self._subscript_gate(t, want_store=True)
            if g:
                self.writes.append((g, node.lineno))
        # 읽고버림 탐지용: 대입 대상 이름 기록
        val_gate = self._call_gate(node.value) if isinstance(node.value, ast.Call) else None
        if val_gate:
            for t in node.targets:
                if isinstance(t, ast.Name):
                    self._assigned[t.id] = (val_gate, node.lineno)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if (isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
                and isinstance(node.target, ast.Name) and _candidate(node.value.value)):
            self.consts[node.target.id] = node.value.value
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self._used.add(node.id)
        self.generic_visit(node)

    # ── 접근 판정 ──────────────────────────────────────────────────────
    def _resolve(self, node: ast.AST) -> str | None:
        """인자 → 게이트 이름. ⛔ 로컬/명시 import 상수만 본다."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value if _candidate(node.value) else None
        if isinstance(node, ast.Name):
            return self.consts.get(node.id)
        if isinstance(node, ast.Attribute):        # mod.ENV_ENABLED — 간접
            return None
        return None

    @staticmethod
    def _is_env_base(node: ast.AST) -> bool:
        """``os.environ`` / 매핑변수(``e``·``src``·``env``…) 인가."""
        if isinstance(node, ast.Attribute) and node.attr == "environ":
            return True
        if isinstance(node, ast.Name):
            return node.id in {"environ", "e", "env", "src", "source", "source_env"}
        return False

    def _subscript_gate(self, node: ast.AST, *, want_store: bool) -> str | None:
        if not isinstance(node, ast.Subscript):
            return None
        is_store = isinstance(node.ctx, ast.Store)
        if want_store != is_store:
            return None
        if not self._is_env_base(node.value):
            return None
        return self._resolve(node.slice)

    def _call_gate(self, node: ast.Call) -> str | None:
        """④ 첫 인자 고정이 아니라 **전 인자**를 본다."""
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else (
            fn.id if isinstance(fn, ast.Name) else "")
        env_call = (
            (isinstance(fn, ast.Attribute) and name in _ENV_ACCESSORS
             and self._is_env_base(fn.value))
            or name in {"getenv"}
            # 래퍼: _truthy(NAME) / _env_flag(NAME, default=...) 류
            or (name.startswith("_") and ("env" in name or "truthy" in name
                                          or "flag" in name or "gate" in name))
        )
        if not env_call:
            return None
        for a in list(node.args) + [k.value for k in node.keywords]:
            g = self._resolve(a)
            if g:
                return g
        return None

    def visit_Call(self, node: ast.Call) -> None:
        g = self._call_gate(node)
        if g:
            fn = node.func
            helper = fn.attr if isinstance(fn, ast.Attribute) else (
                fn.id if isinstance(fn, ast.Name) else "?")
            self.reads.append((g, node.lineno, helper))
            # 기본값: 게이트 인자 다음의 리터럴 (2번째 위치인자 또는 default=)
            for a in node.args[1:]:
                if isinstance(a, ast.Constant):
                    self.reads.append((g, node.lineno, f"__default__{a.value!r}"))
                    break
            for k in node.keywords:
                if k.arg in ("default", "fallback") and isinstance(k.value, ast.Constant):
                    self.reads.append((g, node.lineno, f"__default__{k.value.value!r}"))
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        g = self._subscript_gate(node, want_store=False)      # ③ Load 만
        if g:
            self.reads.append((g, node.lineno, "[]"))
        self.generic_visit(node)

    def finish(self) -> None:
        """읽고버림 — 대입했는데 그 이름이 한 번도 Load 되지 않았다."""
        for var, (gate, _ln) in self._assigned.items():
            if var not in self._used:
                self.discarded.add(gate)


def _imported_consts(tree: ast.AST, by_module: Mapping[str, dict[str, str]],
                     rel: str) -> dict[str, str]:
    """② 명시 ``from X import NAME`` 로 들어온 상수만 받는다(전역 폴백 없음)."""
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        src = node.module.replace(".", "/")
        for cand, consts in by_module.items():
            if cand.endswith(src) or src.endswith(cand.rsplit("/", 1)[-1][:-3]):
                for a in node.names:
                    if a.name in consts:
                        out[a.asname or a.name] = consts[a.name]
    return out


def scan_gates(root: "str | Path", *,
               files: "Iterable[str | Path] | None" = None,
               include_live_env: bool = True) -> dict[str, Any]:
    """전 레포를 훑어 게이트 대장을 만든다.

    `files` 를 주면 그 목록만 훑는다(상대경로는 `root` 기준). 기본값
    ``None`` 은 합성 술어 테스트를 위한 기존 full-tree 동작이다. 반면 생성기
    :func:`main` 과 ``feature_inventory`` 는 반드시 :func:`tracked_python_files`
    또는 독립 snapshot의 명시 manifest를 이 인자에 넘긴다. Git/manifest 실패를
    full-tree로 폴백하면 미추적 산출물이 다시 생성물에 들어오므로 그 경로는 없다.

    ``include_live_env`` 의 기본 ``True`` 는 저수준 운영 진단 호환용이다.
    커밋되는 생성기와 baseline은 항상 ``False``를 넘겨 tracked
    ``.env.example``+코드만 투영하고, gitignored ``.env``를 읽지 않는다.
    """
    root = Path(root)
    _explicit_files = files is not None
    files = _py_files(root) if files is None else sorted({root / f for f in files})
    # __SLOT_SCAN_SCOPE_LOG_2026_08_23__ 규모를 **들어갈 때** 신고한다.
    # 2026-08-23 폭주(RSS 16.2GB, 21분)의 21분 내내 stdout/stderr 가 0바이트였고
    # 그게 진단을 비싸게 만들었다. 상한과 로그는 같이 가야 한다 — 상한만 있으면
    # "왜 걸렸는지"가 없다. ⛔ 실패해도 스캔을 막지 않는다(관측이 판정을 바꾸면 안 된다).
    _t0 = time.monotonic()
    try:
        _n_files = len(files)
        _n_bytes = sum(f.stat().st_size for f in files)
        logger.info(
            "gates_map.scan_gates 진입: files=%d bytes=%d (%.1f MB) root=%s scope=%s",
            _n_files, _n_bytes, _n_bytes / 1e6, root,
            "caller-supplied" if _explicit_files else "full-tree-fallback",
        )
    except OSError as _exc:
        _n_files, _n_bytes = len(files), -1
        _swallowed(_exc, site="tools.gates_map.scan_gates:scale_log",
                   category="observability")
    trees: dict[str, ast.AST] = {}
    consts_by_file: dict[str, dict[str, str]] = {}
    unparsed: list[str] = []

    for p in files:
        rel = p.relative_to(root).as_posix()
        # ⛔ 읽기/파싱은 :func:`_read_text`/:func:`_parse` **한 자리**를 지난다.
        #    2026-08-10 이전에는 여기만 자체 핸들러였고, 그래서 `_parse` 를
        #    *"SyntaxError 면 빈 Module"* 로 바꾸는 변이가 이 경로를 안 지나
        #    살아남았다(팬텀 축의 픽스처가 정확히 이 경로를 쓴다).
        src = _read_text(p, site="tools.gates_map.scan_gates:read")
        tree = None if src is None else _parse(
            src, site="tools.gates_map.scan_gates:parse")
        if tree is None:
            unparsed.append(rel)      # 못 읽음 == 못 파싱: 둘 다 **안 쟀다**
            continue
        trees[rel] = tree
        # ② 테스트 상수는 프로덕션 판정에 못 들어온다
        if _is_test(rel):
            continue
        local: dict[str, str] = {}
        for node in ast.walk(trees[rel]):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                    and isinstance(node.value.value, str) and _candidate(node.value.value):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        local[t.id] = node.value.value
        consts_by_file[rel] = local

    rows: dict[str, GateRow] = {}

    def row(name: str) -> GateRow:
        return rows.setdefault(name, GateRow(name=name))

    for rel, tree in trees.items():
        imported = {} if _is_test(rel) else _imported_consts(tree, consts_by_file, rel)
        sc = _FileScan(rel, imported)
        try:
            sc.visit(tree)
            sc.finish()
        except RecursionError as exc:
            _swallowed(exc, site="tools.gates_map.scan_gates:visit", category="verify")
            continue
        for gate, line, kind in sc.reads:
            r = row(gate)
            if kind.startswith("__default__"):
                d = kind[len("__default__"):]
                if d not in r.defaults:
                    r.defaults.append(d)
                continue
            if _is_test(rel):
                r.tests.append(f"{rel}:{line}")
            else:
                r.readers.append(f"{rel}:{line}")
                if kind not in ("[]", "get", "getenv") and kind not in r.parsers:
                    r.parsers.append(f"{rel}:{kind}")
        for gate, line in sc.writes:
            if not _is_test(rel):
                row(gate).runtime_set.append(f"{rel}:{line}")
        for gate in sc.discarded:
            if not _is_test(rel):
                row(gate).verdict = "read_discarded"

    # 간접 신호: 문자열은 있는데 위 술어가 못 잡은 파일
    for rel, p in ((r, root / r) for r in trees):
        text = _read_text(p, site="tools.gates_map.scan_gates:reread")
        if text is None:
            continue
        for name in set(_NAME_RE.findall(text)):
            if not _candidate(name):
                continue
            r = row(name)
            here = f"{rel}:"
            if any(x.startswith(here) for x in r.readers + r.tests):
                continue
            (r.tests if _is_test(rel) else r.indirect).append(rel)

    # Low-level callers keep the historical live overlay by default.  Every
    # committed generator/baseline passes ``include_live_env=False`` so its
    # rows and digest cannot be a function of a gitignored local file.
    env = _env_file(root / ".env") if include_live_env else {}
    example = _env_file(root / ".env.example")
    for name, val in env.items():
        if _candidate(name):
            r = row(name)
            r.in_env, r.env_value = True, val
    for name in example:
        if _candidate(name):
            row(name).in_example = True

    for r in rows.values():
        if r.verdict == "read_discarded":
            continue
        if r.readers:
            r.verdict = "direct"
        elif r.indirect:
            r.verdict = "indirect"
        elif r.tests:
            r.verdict = "tests_only"       # ⛔ 팬텀이 아니다
        else:
            r.verdict = "phantom"

    # __SLOT_SCAN_SCOPE_LOG_2026_08_23__ 나갈 때 경과를 신고하고, 규모를 산출에
    # 실어 원장이 **얼마나 큰 나무를 쟀는지** 기록하게 한다. 종전에는 이 수를
    # 아무 데도 안 적어서 `work_feeder._scan_scale` 의 규모표류 축이 이 스캐너의
    # 45,864 를 **구조적으로 볼 수 없었다**(그 축은 vocab_map 필터를 읽는다).
    logger.info(
        "gates_map.scan_gates 완료: files=%d bytes=%d gates=%d unparsed=%d %.1fs",
        len(files), _n_bytes, len(rows), len(unparsed), time.monotonic() - _t0,
    )
    return {
        "root": root.as_posix(),
        "n_files": len(files),
        "scanned_bytes": _n_bytes,          # -1 = 못 쟀다(0 아님)
        # ⛔ 여기에 scan_scope(caller-supplied/full-tree-fallback)를 넣지 마라.
        #    `files=None` 기본값과 `files=<같은 집합>` 명시는 **산출이 같아야**
        #    한다는 계약이 있고(test_files_default_is_todays_behaviour), 출처
        #    필드는 그 동등성을 깬다. 08-23 에 내가 넣었다가 되돌렸다.
        #    출처는 진입 로그에만 남긴다 — 관측은 산출을 바꾸면 안 된다.
        "unparsed": unparsed,
        "env_sha256": _sha(root / ".env") if include_live_env else None,
        "env_projection": "live" if include_live_env else "committed",
        "gates": {k: rows[k].as_dict() for k in sorted(rows)},
    }


def _sha(p: Path) -> str | None:
    if not p.exists():
        return None
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()[:16]
    except OSError as exc:
        _swallowed(exc, site="tools.gates_map._sha", category="persist")
        return None


def _public_env_value(value: str | None) -> str | None:
    """Return disclosure-safe metadata for one operator-controlled value."""

    if value is None:
        return None
    return f"set(len={len(value)})" if value else "unset"


def _mask(name: str, value: str | None) -> str:
    """⛔ 렌더러 레벨 하드 가드 — 모든 live 값은 길이만."""

    del name  # Names do not make arbitrary operator values safe to publish.
    if value is None:
        return "미선언"
    return _public_env_value(value) or "unset"


def _public_report(report: Mapping[str, Any]) -> dict[str, Any]:
    """Copy a scan report while replacing every live value before JSON I/O."""

    public = dict(report)
    gates: dict[str, dict[str, Any]] = {}
    raw_gates = report.get("gates", {})
    if isinstance(raw_gates, Mapping):
        for name, raw_row in raw_gates.items():
            if not isinstance(name, str) or not isinstance(raw_row, Mapping):
                continue
            row = dict(raw_row)
            value = row.get("env_value")
            row["env_value"] = _public_env_value(
                value if isinstance(value, str) or value is None else None
            )
            gates[name] = row
    public["gates"] = gates
    return public


def _grades(root: Path) -> dict[str, dict[str, str]]:
    """사람이 쓴 등급 원장. 없으면 빈 것 — **자동으로 채우지 않는다**."""
    p = root / GRADES_REL
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _swallowed(exc, site="tools.gates_map._grades", category="persist")
        return {}
    return data.get("gates", {}) if isinstance(data, dict) else {}


def render(report: Mapping[str, Any], grades: Mapping[str, Any]) -> str:
    gates = report["gates"]
    by = {v: [g for g, r in gates.items() if r["verdict"] == v] for v in VERDICTS}
    undeclared = [g for g, r in gates.items()
                  if r["verdict"] == "direct" and not r["in_env"] and not r["in_example"]]
    runtime_set = [g for g, r in gates.items() if r["runtime_set"]]

    projection_note = (
        f"live `.env` sha `{report['env_sha256']}`"
        if report.get("env_projection") == "live"
        else "committed `.env.example` projection (live `.env` excluded)"
    )
    L = [
        "# 게이트 대장 (GATES)",
        "",
        "> ⛔ **손으로 고치지 마라 — 생성물이다.**",
        f"> 재생성: `python3 -m agi_v8_1.tools.gates_map --out DOCS/GATES.ko.md`",
        f"> 스캔 파일 {report['n_files']} · {projection_note} ·"
        f" 게이트 {len(gates)}",
        "",
        "## 판정 어휘 — 5단인 이유",
        "",
        "| 판정 | 뜻 |",
        "|---|---|",
        "| `direct` | 프로덕션이 이름으로 읽는다 |",
        "| `indirect` | 자료구조/크로스모듈 경유 — 정적 미추적. **고발 아님** |",
        "| `tests_only` | 테스트에서만 언급. ⛔ **팬텀이 아니다** — 지우면 테스트가 깨진다 |",
        "| `phantom` | 프로덕션 코드에 문자열조차 없다 |",
        "| `read_discarded` | 읽어놓고 결과를 안 쓴다 |",
        "",
        "🔑 *\"읽는 코드 0\"* 과 *\"못 찾았다\"* 를 한 칸에 넣으면 표가 거짓말한다.",
        "",
        "## 요약",
        "",
        "| 판정 | 개수 |",
        "|---|---|",
    ]
    for v in VERDICTS:
        L.append(f"| `{v}` | {len(by[v])} |")
    L += [
        "",
        f"🔴 **코드가 읽는데 `.env`·`.env.example` 어디에도 선언 없음: {len(undeclared)}개**",
        "— 팬텀의 반대 방향이고 안전 관점에선 이쪽이 나쁘다"
        "(*\"없는 줄 알았는데 있다\"*).",
        "",
        f"⚠️ **런타임이 덮어쓰는 게이트 {len(runtime_set)}개** — 이 게이트들은 `.env` 값이"
        " 실행 중 유효값이 **아니다**.",
        "",
    ]

    if undeclared:
        L += ["## 🔴 미선언 — 코드는 읽는데 설정파일에 없다", "",
              "| 게이트 | 읽는 코드 | 기본값 |", "|---|---|---|"]
        for g in sorted(undeclared):
            r = gates[g]
            L.append(f"| `{g}` | {_first(r['readers'])} | "
                     f"{_join(r['defaults']) or '**없음**'} |")
        L.append("")

    if runtime_set:
        L += ["## ⚠️ 런타임 덮어쓰기 — `.env` 를 유효값으로 읽지 말 것", "",
              "| 게이트 | `.env` 선언 | 덮어쓰는 곳 |", "|---|---|---|"]
        for g in sorted(runtime_set):
            r = gates[g]
            L.append(f"| `{g}` | {_mask(g, r['env_value'])} | {_join(r['runtime_set'])} |")
        L.append("")

    L += ["## 전체", "",
          "| 게이트 | 판정 | `.env` | 읽는 코드 | 기본값 | 진리값 파서 |"
          " 등급(사람) | 켜면 |",
          "|---|---|---|---|---|---|---|---|"]
    for g in sorted(gates):
        r = gates[g]
        h = grades.get(g, {})
        L.append(
            f"| `{g}` | `{r['verdict']}` | {_mask(g, r['env_value'])} | "
            f"{_first(r['readers']) or _first(r['indirect']) or _first(r['tests']) or '**0**'} | "
            f"{_join(r['defaults']) or '—'} | {_join(r['parsers']) or '—'} | "
            f"{h.get('grade', '—')} | {h.get('effect', '—')} |")
    L += ["",
          "⚠️ **등급(사람)** 칸은 기계가 안 채운다 — CENSUS 4분류 중 셋이 라이브 신호에서",
          "축자 동일하게 접혀 자동 재생산률이 **1/4** 다. 자동인 척하면 표가 또 거짓말한다.",
          f"원장 = `{GRADES_REL}`.", ""]
    return "\n".join(L)


def _first(xs: Iterable[str]) -> str:
    xs = list(xs)
    if not xs:
        return ""
    extra = f" 외 {len(xs)-1}" if len(xs) > 1 else ""
    return f"`{xs[0]}`{extra}"


def _join(xs: Iterable[str], n: int = 3) -> str:
    xs = list(xs)
    if not xs:
        return ""
    head = " · ".join(f"`{x}`" for x in xs[:n])
    return head + (f" 외 {len(xs)-n}" if len(xs) > n else "")


def _read_text(path: Path, *, site: str) -> str | None:
    """읽기 한 자리. ``None`` = **못 읽었다**(빈 파일과 구분한다).

    ⛔ 이 모듈의 새 읽기는 전부 여기를 지난다 — 침묵 핸들러를 자리마다 복제하면
    ``tools/`` 침묵 래칫(선재 적색)이 수리마다 올라간다. 한 자리로 모으면
    *"읽었는데 비었다"* 와 *"못 읽었다"* 의 구분도 한 곳에만 산다.

    ⛔ ``UnicodeDecodeError`` 도 여기 잡힌다(2026-08-10). 그 전에는 ``OSError`` 만
    잡아서 **비-UTF8 바이트 한 개**가 ``--selfcheck`` 를 추적정보와 함께 죽였다 —
    독스트링은 *"못 읽음은 None 으로 계량한다"* 인데 그 경로에서만 거짓이었다.
    (loud 라 거짓 초록은 아니었지만, 계량된다는 문장이 거짓인 건 그것대로 결함이다.)
    """
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        _swallowed(exc, site=site, category="persist")
        return None


def _parse(text: str, *, site: str) -> ast.AST | None:
    """파싱 한 자리. ``None`` = **못 쟀다** — 호출자는 그걸 통과로 접지 않는다.

    ⛔ `_read_text` 와 같은 이유로 한 자리다: 자리마다 핸들러를 복제하면 침묵
    래칫이 수리마다 올라간다(``tools/`` 는 선재 적색이라 **내리는 방향만** 허용).
    """
    try:
        return ast.parse(text)
    except SyntaxError as exc:
        _swallowed(exc, site=site, category="verify")
        return None


def _resolve_import(root: Path, rel: str, node: ast.ImportFrom,
                    allowed: "set[str] | None" = None) -> "tuple[Path | None, bool]":
    """``from <module> import ...`` → (**레포 안** 파일 또는 None, 머리를 벗겼나).

    ⛔ `_imported_consts` 의 fuzzy 접미 매칭(``cand.endswith(src)``)을 **안 쓴다**.
    자기 결론을 자기 해소기로 대조하면 그 해소기의 버그가 검사에 그대로 상속된다
    — 이 도구가 태어난 이유(wiring_map 의 전역 bare-name 폴백)와 같은 형태다.

    ## 🔴 패키지 머리는 **디렉터리 이름이 아니다** (2026-08-10 수리)

    이전 판은 ``parts[0] == root.name`` 일 때만 머리를 벗겼다 — 즉 ``--root`` 가
    ``agi_v8_1`` 이라는 **철자**여야만 절대 import 가 해소됐다. 이름이 다른
    워크트리/심링크를 root 로 주면 유일한 ``via_import`` 간선이 유령으로 뒤집혀
    ``exit=1`` 거짓 적색이 났다(실측: ``notagiv8`` 심링크로 재현). 코드가 import 하는
    이름은 트리가 어디 놓였든 ``agi_v8_1`` 이므로 **디렉터리 철자에 물리면 안 된다**.

    ⇒ 축자 그대로 먼저 시도하고, 안 되면 머리 한 겹을 벗겨 다시 시도한다.

    ## ⚠️ 머리-벗김은 **fail-open** 이다 — 그래서 세어서 내보낸다 (2026-08-10 R2)

    레포 **밖** 패키지 ``foo.bar`` 도 ``root/bar.py`` 가 있으면 해소된다. 재현
    (합성 트리): ``reader.py`` 가 ``from foo.bar import THING`` 만 하고 게이트
    문자열은 어디에도 없는데, 무관한 동명 모듈 ``bar.py`` 가 그 게이트를 세우면
    ``verdict=direct`` · ``fails=[]`` 로 **진짜 유령 간선이 초록으로 접힌다**.

    ⛔ 이걸 철자 대조로 닫으려 하면 위의 ``notagiv8`` 거짓 적색이 그대로 돌아온다
    (레포가 자기를 부르는 이름은 트리 이름과 무관하고, 트리 안에는 그 이름을
    증언하는 것이 없다 — 설치본을 물으면 editable 설치가 워크트리를 이긴다:
    ``feedback_editable_install_defeats_worktree_2026_08_02``). ⇒ 술어로 닫는 대신
    **표면을 동결한다**: 이 경로로 인정된 ``via_import`` 간선 수가
    :data:`MAX_HEAD_STRIPPED_IMPORT_EDGES` 를 넘으면 :func:`selfcheck_report` 가
    적색이고, 넘지 않아도 개수가 화면과 ``coverage`` 에 **항상** 찍힌다.
    (2026-08-10 라이브 실측: 머리-벗김 해소 3건 중 ``via_import`` 로 인정된 간선 1건.)
    """
    if node.level:                                   # `from .x import y`
        base = Path(rel).parent
        for _ in range(node.level - 1):
            base = base.parent
        parts = list(base.parts) + (node.module.split(".") if node.module else [])
    else:
        if not node.module:
            return None, False
        parts = node.module.split(".")
    if not parts:
        return None, False
    #: 축자 → 머리 한 겹 벗김(`agi_v8_1.dashboard.x` → `dashboard/x.py`)
    spellings = [(parts, False)]
    if not node.level and len(parts) > 1:
        spellings.append((parts[1:], True))
    for spelling, stripped in spellings:
        if not spelling:
            continue
        for cand in (root.joinpath(*spelling).with_suffix(".py"),
                     root.joinpath(*spelling, "__init__.py")):
            cand_rel = cand.relative_to(root).as_posix()
            if (allowed is None or cand_rel in allowed) and cand.is_file():
                return cand, stripped
    return None, False


def _module_file(root: Path, rel: str, node: ast.ImportFrom) -> Path | None:
    """:func:`_resolve_import` 의 경로만. ⛔ 머리-벗김 여부를 버리므로 **판정에는
    쓰지 않는다** — 그 사실이 :data:`MAX_HEAD_STRIPPED_IMPORT_EDGES` 의 분자다."""
    return _resolve_import(root, rel, node)[0]


def _cached_text(path: Path, *, site: str, cache: "dict[Any, Any] | None") -> str | None:
    """:func:`_read_text` 의 판-수명 메모. ⛔ 전역이 아니다 — 한 판만 산다.

    (모듈 전역 캐시면 같은 프로세스의 다음 판이 앞 판의 트리를 물려받는다.
    테스트는 판마다 다른 ``tmp_path`` 를 준다.)
    """
    if cache is None:
        return _read_text(path, site=site)
    key = ("text", path)
    if key not in cache:
        cache[key] = _read_text(path, site=site)
    return cache[key]


def _cached_tree(path: Path, text: str, *, site: str,
                 cache: "dict[Any, Any] | None") -> "ast.AST | None":
    if cache is None:
        return _parse(text, site=site)
    key = ("tree", path)
    if key not in cache:
        cache[key] = _parse(text, site=site)
    return cache[key]


def _defines_literal(path: Path, gate: str,
                     cache: "dict[Any, Any] | None" = None) -> bool:
    """그 파일이 ``NAME = "<gate>"`` 를 **축자로** 세우나. AST — 주석/문서는 안 센다."""
    text = _cached_text(path, site="tools.gates_map.selfcheck:import", cache=cache)
    if text is None or gate not in text:
        return False
    tree = _cached_tree(path, text, site="tools.gates_map.selfcheck:import",
                        cache=cache)
    if tree is None:
        return False                                  # 못 파싱 = 못 쟀다 = 통과 아님
    for node in ast.walk(tree):
        target = None
        if isinstance(node, ast.Assign):
            target = node.value
        elif isinstance(node, ast.AnnAssign):
            target = node.value
        if (isinstance(target, ast.Constant) and isinstance(target.value, str)
                and target.value == gate):
            return True
    return False


#: ``direct`` 간선 한 줄의 축자 대조 결과. ⛔ 셋을 한 칸에 접지 않는다 —
#: ``via_import`` 는 *"통과"* 가 아니라 *"그 파일에서는 못 쟀고 한 홉 건너 쟀다"* 다.
_DIRECT_LITERAL, _DIRECT_VIA_IMPORT, _DIRECT_GHOST = "literal", "via_import", "ghost"


def _has_str_constant(tree: ast.AST, gate: str) -> bool:
    """AST 어딘가에 ``"<gate>"`` 문자열 **상수**가 있나. 주석·문서는 안 센다.

    f-string 안의 고정 조각도 :class:`ast.Constant` 라 여기 걸린다
    (``f"<접두>_MC_{i}"`` → ``"<접두>_MC_"``) — 그건 실제로 그 이름을 조립하는
    코드이므로 축자 근거가 맞다.
    """
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and node.value == gate):
            return True
    return False


def _direct_edge(root: Path, rel: str, gate: str,
                 cache: "dict[Any, Any] | None" = None,
                 allowed: "set[str] | None" = None) -> "tuple[str, bool]":
    """리더 한 줄이 축자로 뒷받침되나. 반환 = (판정, 머리-벗김 해소를 썼나).

    1. 그 파일이 ``"<gate>"`` 를 **문자열 상수로** 갖는다 → :data:`_DIRECT_LITERAL`.
    2. 없지만 그 파일이 **명시 import** 한 레포 안 모듈이 ``NAME = "<gate>"`` 를
       세운다 → :data:`_DIRECT_VIA_IMPORT`. 이건 이 도구의 엄격 술어 ②가
       **일부러 잡는** 경로다(``dashboard/endpoints.py:121`` 이 그 실측 사례).
       ⛔ 그래도 *통과*로 세지 않는다 — 축자 대조는 한 홉 옮겨진 것뿐이다.
    3. 둘 다 아니다 → :data:`_DIRECT_GHOST`. wiring_map 이 443 간선 중 30개를
       내놓던 그 부류다.

    ## 🔴 ①은 **파일 전체 부분문자열**이었다 (2026-08-10 R2 적대검증)

    ``if gate in text`` 이라, 내용이 ``# <게이트이름> is mentioned only in a
    comment`` 한 줄뿐인 파일도 ``literal`` 로 세어졌다(재현: ``fails=[]`` ·
    ``direct_literal=1``). ⇒ 화면의 *"축자 465"* 는 *"465 간선이 코드로
    뒷받침됐다"* 가 아니라 *"465 간선의 파일 어딘가에 그 문자열이 있다"* 였다.
    지금은 **AST 문자열 상수**를 요구한다(:func:`_has_str_constant`). 접두 충돌도
    같이 닫힌다 — ``<G>BAR`` 는 ``<G>`` 의 근거가 아니다.
    ⚠️ 라이브 실측으로 **산출 무변**임을 먼저 쟀다: 465 간선 전부가 새 술어도
    통과한다(strict-ast-miss 0). 즉 이 조임은 오늘의 초록을 하나도 안 깎는다.
    """
    text = _cached_text(root / rel, site="tools.gates_map.selfcheck:direct",
                        cache=cache)
    if text is None:
        return _DIRECT_GHOST, False                   # 못 읽었다 ⇒ 통과 아님
    tree = _cached_tree(root / rel, text, site="tools.gates_map.selfcheck:direct",
                        cache=cache)
    if tree is None:
        return _DIRECT_GHOST, False                   # 못 파싱 = 못 쟀다 = 통과 아님
    if gate in text and _has_str_constant(tree, gate):
        return _DIRECT_LITERAL, False
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        mod, head_stripped = _resolve_import(root, rel, node, allowed)
        if mod is not None and _defines_literal(mod, gate, cache):
            return _DIRECT_VIA_IMPORT, head_stripped
    return _DIRECT_GHOST, False


def selfcheck_report(report: Mapping[str, Any], root: Path, *,
                     files: "Iterable[str | Path] | None" = None) -> dict[str, Any]:
    """도구가 자기 결론을 축자 대조한다 — **전수**. 실패 목록 + 사거리.

    ## 🔴 왜 전수인가 (2026-08-10 실측)

    2026-08-07 판은 ``sorted(...)[:5]`` 로 팬텀 5 · direct 5, 합 **10개**만 봤다.
    라이브 실측은 팬텀 82 · direct 349 = **431 중 10(2.3%)** 이었고, 그 10 은
    무작위가 아니라 **알파벳 앞 10** 이라 항상 같다 ⇒ 그 밖의 오판은 확률적으로
    늦게 잡히는 게 아니라 **영원히 안 잡힌다**. 그런데 CLI 는 ``selfcheck: 통과``
    라고만 찍었고 독스트링·화면 어디에도 *표본*이라는 말이 없었다.
    (설계 문서 ``DOCS/GATES_PLAN_2026_08_07.md`` 는 *"무작위 표본 5개"* 라고 적었다 —
    구현은 무작위도 아니었고, 그 차이를 아무도 못 봤다.)

    표본이 정당화되던 근거는 비용인데 그것도 사실이 아니었다: 팬텀 축이 게이트마다
    ``_py_files`` 를 다시 돌아 트리를 5번 읽었다. **한 번만 읽고 전 이름을 한꺼번에
    맞추면** 전수가 표본보다 싸다 — 실측 1.8s(표본 10) → 0.4s(전수 431).

    ## 🔑 전수로 돌리자 direct 술어가 **오탐**을 냈다 (같은 날, 1/349)

    ``AGI_V8_DASHBOARD_SESSIONS_DIR`` 의 리더 ``dashboard/endpoints.py:121`` 에는
    그 문자열이 없다 — ``from …runtime_panel_view import ENV_PANEL_SESSIONS_DIR``
    로 들어온 상수다. 그건 이 도구의 **엄격 술어 ②가 일부러 잡는 경로**이므로
    유령 간선이 아니다. 표본 5개가 우연히 전부 리터럴이라 이 오탐이 가려져 있었다.
    ⇒ 축자 대조를 **import 한 홉까지** 따라가되(:func:`_direct_edge`), 그 결과를
    통과가 아니라 :data:`_DIRECT_VIA_IMPORT` 라는 **다른 칸**으로 센다.

    ## 🔴 그리고 direct 축은 **게이트 전수였지 간선 전수가 아니었다** (2026-08-10)

    같은 날 오후 적대검증: 이 함수는 게이트마다 ``readers[0]`` 하나만 봤다.
    라이브 direct 349게이트 · 493간선 ⇒ **144간선(29%)** 이 검사 밖이었고, 보이는
    쪽이 ``sorted`` 첫 리더로 고정이라 그 144는 *늦게* 잡히는 게 아니라 **안** 잡힌다.
    재현: ``readers=["orchestrator_v8.py:1", "tools/ratchet.py:999"]`` 처럼 꼬리에
    유령을 심으면 ``fails=[]`` · ``direct 1 전수`` 로 초록이었다.
    (그때 꼬리 144를 손으로 전수해 보니 유령 0 — 거짓 초록은 아니었고 거짓이던 건
    화면이 인쇄하던 **사거리**다. 지금은 간선이 단위이고 두 분모를 따로 찍는다.)

    반환::

        {"fails": [...], "coverage": {...}}

    ``coverage`` 는 화면에 그대로 나간다 — *"통과"* 가 **무엇에 대한 통과인지**
    말하지 않는 초록은 이 레포가 이미 여러 번 값을 치른 형태다.
    """
    fails: list[str] = []
    gates = report["gates"]
    checked_files = (_py_files(root) if files is None
                     else sorted({root / p for p in files}))
    allowed = {p.relative_to(root).as_posix() for p in checked_files}
    if report["unparsed"]:
        fails.append(f"파싱 실패 {len(report['unparsed'])}개: {report['unparsed'][:3]}")

    # ── 팬텀 전수: 트리를 **한 번** 읽고 전 이름을 동시에 맞춘다
    #
    # 🔴 2026-08-10 R2: 대조가 ``g in text`` 라 **접두 충돌에 거짓 적색**이 났다.
    #    재현: `prod.py` 에 `os.environ.get("<G>BAR")` 만 있는데 팬텀
    #    팬텀 `<G>` 가 *"prod.py 에 있다"* 로 반증됐다. 팬텀은 **부재 주장**이라
    #    반증은 넓어야 맞지만(주석도 근거다), 접두 조각은 그 이름의 근거가 아니다.
    #    ⇒ 토큰 경계(`\b…\b`)로 맞춘다. 라이브 실측 차이 0(82 팬텀 × 전 프로덕션
    #      파일에서 부분문자열 판정과 토큰 판정이 갈리는 자리는 없었다).
    phantoms = sorted(g for g, r in gates.items() if r["verdict"] == "phantom")
    unread = 0
    if phantoms:
        wanted = set(phantoms)
        # 긴 이름 먼저 — 대안 안에서 접두가 먼저 시도돼 뒤가 안 잡히는 걸 막는다.
        token_rx = re.compile(
            r"\b(?:" + "|".join(re.escape(g) for g in
                                sorted(phantoms, key=len, reverse=True)) + r")\b")
        for p in checked_files:
            rel = p.relative_to(root).as_posix()
            if _is_test(rel):
                continue
            text = _read_text(p, site="tools.gates_map.selfcheck")
            if text is None:
                unread += 1
                continue
            for g in sorted(set(token_rx.findall(text)) & wanted):
                fails.append(f"팬텀 오판: {g} 가 {rel} 에 있다")
                wanted.discard(g)
    if unread:
        # ⛔ 못 읽은 파일은 "없다"의 근거가 못 된다. 팬텀 판정은 **부재 주장**이라
        #    한 파일만 못 읽어도 그 주장이 안 선다 ⇒ 통과로 접지 않는다.
        fails.append(f"팬텀 전수 미완: 프로덕션 파일 {unread}개를 못 읽었다 — "
                     f"'코드에 문자열조차 없다'는 부재 주장이라 부분 스캔 위에서는 "
                     f"성립하지 않는다(fail-closed)")

    # ── direct 전수: **모든 리더 간선**이 축자로 뒷받침되나
    #
    # 🔴 2026-08-10 수리: 여기는 게이트마다 ``readers[0]`` **하나만** 봤다. 실패
    #    문구는 `유령 간선` 이고 모듈 독스트링도 문제를 간선(443)으로 세는데,
    #    검사 단위만 게이트였다 — 라이브 실측으로 게이트 349 · 간선 493 이라
    #    **144간선(29%)이 구조적으로 안 보였다**. 게다가 보이는 쪽이 `sorted` 첫
    #    리더로 **고정**이라 그 144는 늦게 잡히는 게 아니라 안 잡힌다(팬텀 축의
    #    `[:5]` 표본과 같은 병, 같은 파일 안에서).
    #    ⇒ 단위를 간선으로 옮긴다. 게이트 수는 `direct_checked` 로 그대로 남기고
    #      간선 수를 `direct_edges_checked` 로 **따로** 찍는다 — 두 분모를 한 칸에
    #      접으면 다음 사람이 또 사거리를 오독한다.
    tally = {_DIRECT_LITERAL: 0, _DIRECT_VIA_IMPORT: 0, _DIRECT_GHOST: 0}
    cache: dict[Any, Any] = {}
    gates_checked = 0
    head_stripped = 0
    for g in sorted(g for g, r in gates.items() if r["verdict"] == "direct"):
        readers = gates[g]["readers"]
        if not readers:
            fails.append(f"모순: {g} 가 direct 인데 readers 가 비었다")
            continue
        gates_checked += 1
        # 같은 파일의 여러 줄은 한 간선이다(축자 대조는 파일 단위).
        for rel in sorted({r.rsplit(":", 1)[0] for r in readers}):
            verdict, stripped = _direct_edge(root, rel, g, cache, allowed)
            tally[verdict] += 1
            head_stripped += int(stripped)
            if verdict == _DIRECT_GHOST:
                fails.append(f"유령 간선: {g} 가 {rel} 에 없고 그 파일이 명시 import 한 "
                             f"레포 안 모듈 어디에도 리터럴로 안 세워진다")
    if head_stripped > MAX_HEAD_STRIPPED_IMPORT_EDGES:
        # 🔒 fail-open 표면의 **동결**. 술어로는 못 닫는다(`_resolve_import` 참조) —
        #    그래서 개수를 동결하고, 늘어나면 리뷰를 받는 한 줄을 요구한다.
        fails.append(
            f"머리-벗김 import 로 인정된 간선 {head_stripped}개 > 상한 "
            f"{MAX_HEAD_STRIPPED_IMPORT_EDGES} — 이 경로는 레포 **밖** 패키지"
            f"(`from foo.bar import X`)도 동명 `bar.py` 가 있으면 해소하므로 "
            f"진짜 유령 간선을 via_import 로 접을 수 있다. 정말 새 자기-import "
            f"간선이면 MAX_HEAD_STRIPPED_IMPORT_EDGES 를 실측과 같은 값으로 올려라 "
            f"— ⛔ 그건 조용한 면제가 아니라 리뷰를 받는 한 줄이다")

    return {
        "fails": fails,
        "coverage": {
            "phantom_checked": len(phantoms),
            "phantom_files_unread": unread,
            "direct_checked": gates_checked,
            "direct_edges_checked": sum(tally.values()),
            "direct_literal": tally[_DIRECT_LITERAL],
            # ⚠️ 이 칸이 크면 "축자 대조"의 사거리가 그만큼 한 홉 밖이라는 뜻이다.
            "direct_via_import": tally[_DIRECT_VIA_IMPORT],
            # ⚠️ 그중 **머리-벗김**으로 해소된 것 — fail-open 표면의 크기다.
            "direct_via_import_head_stripped": head_stripped,
            "gates_total": len(gates),
        },
    }


def selfcheck(report: Mapping[str, Any], root: Path, *,
              files: "Iterable[str | Path] | None" = None) -> list[str]:
    """실패 목록만. 사거리까지 보려면 :func:`selfcheck_report` 를 써라."""
    return selfcheck_report(report, root, files=files)["fails"]


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(description="게이트 대장 생성기")
    ap.add_argument("--root", default=str(root))
    ap.add_argument("--out")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selfcheck", action="store_true")
    ap.add_argument(
        "--live-env",
        action="store_true",
        help="비커밋 로컬 진단에만 .env 이름/값 overlay를 포함한다",
    )
    ap.add_argument(
        "--source-manifest",
        help="독립 snapshot용 NUL-종료 UTF-8 Python 상대경로 목록; 없으면 Git index",
    )
    args = ap.parse_args(argv)

    try:
        r = Path(args.root).resolve(strict=True)
        manifest = (load_source_manifest(args.source_manifest)
                    if args.source_manifest else tracked_python_files(r))
        files = source_python_files(r, manifest)
    except (OSError, RuntimeError, SourceSetError) as exc:
        record_critical_failure(
            exc,
            site="tools.gates_map.main:source_set",
            category="telemetry",
        )
        print(
            "source-set error: "
            + format_exception_for_critical_record(exc, max_chars=240),
            file=sys.stderr,
        )
        return 2
    report = scan_gates(r, files=files, include_live_env=args.live_env)
    if args.selfcheck:
        out = selfcheck_report(report, r, files=files)
        fails, cov = out["fails"], out["coverage"]
        for f in fails:
            print("🔴", f)
        # ⛔ **무엇에 대한 통과인지 말하지 않는 초록은 안 낸다.** 2026-08-07 판은
        #    431 중 10 만 보고 "통과" 한 줄만 찍었다(그 10 은 항상 같은 10 이었다).
        # ⚠️ f-string 을 중첩하지 않는다 — PEP 701 은 3.12+ 이고 이 레포의 바닥은
        #    `pyproject.toml: requires-python >=3.11` 이다(3.11 에서는 SyntaxError).
        unread = cov["phantom_files_unread"]
        unread_note = f" (못 읽은 파일 {unread})" if unread else ""
        # ⚠️ `축자` 는 **AST 문자열 상수**다(주석 언급은 안 센다) — 2026-08-10 R2
        #    전까지는 파일 전체 부분문자열이라 주석 한 줄도 근거로 세어졌다.
        hop_note = (f", 그중 머리-벗김 {cov['direct_via_import_head_stripped']}"
                    f"/{MAX_HEAD_STRIPPED_IMPORT_EDGES}")
        print(f"사거리: 팬텀 {cov['phantom_checked']} 전수(토큰 대조){unread_note}"
              f" · direct 게이트 {cov['direct_checked']} / 간선 "
              f"{cov['direct_edges_checked']} 전수"
              f" (AST 축자 {cov['direct_literal']} / import 한 홉 "
              f"{cov['direct_via_import']}{hop_note})"
              f"  ← 게이트 총 {cov['gates_total']}")
        print(f"selfcheck: {'통과' if not fails else str(len(fails)) + '건 실패'}")
        return 1 if fails else 0
    if args.json:
        print(json.dumps(_public_report(report), ensure_ascii=False, indent=2))
        return 0
    text = render(report, _grades(r))
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out} ({len(text)}B)")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
