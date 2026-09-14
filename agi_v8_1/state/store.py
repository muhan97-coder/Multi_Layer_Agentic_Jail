"""agi_v8_1 state store — atomic JSON / JSONL writes.

v7.1 의 agent_system.storage 의 V8 버전. Self-contained — agi_v7.1 의존성 0.
모든 V8 state 변경은 이 store 통해야 함 (apply_chain 의 입력).

Design points:
  - atomic_write_json / atomic_write_text use tempfile + os.replace for
    crash-safe writes (no partial file ever visible), then fsync the parent
    directory so the rename is durable on POSIX filesystems.
  - atomic_append_jsonl serializes append writes with a sidecar advisory lock
    when fcntl is available, writes one pre-encoded line with O_APPEND, and
    fsyncs each line.
  - read_jsonl / read_json are tolerant of missing files (return default).
"""

from __future__ import annotations
import errno
import json
import os
import stat
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

# __SLOT_FAIL_FAST_2026_07_25__ Swallowed failures route through one choke
# point: counted + named always, re-raised under AGI_V8_STRICT_FAIL_FAST.
from agi_v8_1.policy.fail_fast import swallowed as _swallowed

try:  # POSIX advisory lock; absent on Windows.
    import fcntl as _fcntl  # type: ignore

    _HAS_FCNTL = True
except ImportError:  # pragma: no cover
    _fcntl = None  # type: ignore
    _HAS_FCNTL = False


@contextmanager
def _file_lock(lock_path: Path) -> Iterator[None]:
    """Serialize cooperating writers with a sidecar lock file."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if not _HAS_FCNTL:
        yield
        return

    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        _fcntl.flock(fd, _fcntl.LOCK_EX)  # type: ignore[union-attr]
        try:
            yield
        finally:
            _fcntl.flock(fd, _fcntl.LOCK_UN)  # type: ignore[union-attr]
    finally:
        os.close(fd)


def _fsync_directory(path: Path) -> None:
    """Best-effort directory fsync for durable rename/create metadata."""
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError as _ff_exc:
        _swallowed(_ff_exc, site="state.store._fsync_directory:57", category="persist")
        return
    try:
        os.fsync(fd)
    except OSError as _ff_exc:
        _swallowed(_ff_exc, site="state.store._fsync_directory:61", category="persist")
        pass
    finally:
        os.close(fd)


def _fsync_directory_strict(path: Path) -> None:
    """Fsync *path* or propagate failure to a durability authority caller."""
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    fd = os.open(str(path), flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_directory_chain_strict(path: Path) -> None:
    """Fsync *path* and every ancestor through the filesystem root.

    Fsyncing only the ledger's immediate parent makes the ledger entry durable
    *inside that directory*, but it does not make a newly-created directory
    chain durable.  A cold-start authority can create ``state/runtime_logs``
    and otherwise acknowledge a reserve before the parent entry naming
    ``runtime_logs`` is crash-stable.  Walking to the root also closes the
    retry-after-ambiguous-fsync case: an in-memory directory that survived a
    failed acknowledgement is not mistaken for a previously durable one.

    This strict path is reserved for accounting authorities; ordinary JSONL
    appenders retain the single best-effort parent fsync below.
    """
    current = path.absolute()
    while True:
        _fsync_directory_strict(current)
        parent = current.parent
        if parent == current:
            return
        current = parent


def _reject_symlink_target(path: Path, *, what: str) -> None:
    """Refuse to write through a symlink at the destination path.

    # __SLOT_W1A3__ symmetric guard with the kernel-level O_NOFOLLOW used in
    atomic_append_jsonl. atomic_write_json/atomic_write_text use tempfile +
    os.replace so the rename would replace the symlink (not its target), but
    that still lets an attacker who can create a symlink in our state dir
    silently destroy it — surface it as ValueError so callers see the refusal
    explicitly. lstat() so we inspect the link itself, not its target.
    """
    try:
        st = os.lstat(str(path))
    except FileNotFoundError:
        return
    except OSError as _ff_exc:
        _swallowed(_ff_exc, site="state.store._reject_symlink_target:81", category="persist")
        return
    import stat as _stat

    if _stat.S_ISLNK(st.st_mode):
        raise ValueError(f"refusing symlink {what}: {path}")


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written == 0:
            raise OSError("short write while appending JSONL")
        view = view[written:]


def atomic_write_json(path: Path | str, data: Mapping[str, Any]) -> None:
    """Write JSON atomically via tempfile rename.

    Creates parent dirs if missing. Uses sort_keys=True for deterministic
    layout (callers may rely on canonical form for hashing).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink_target(path, what="JSON output")  # __SLOT_W1A3__
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, sort_keys=True, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        _fsync_directory(path.parent)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError as _ff_exc:
            _swallowed(_ff_exc, site="state.store.atomic_write_json:120", category="persist")
            pass
        raise


#: __SLOT_R9_T1_2026_08_17__ 새로 만드는 JSONL 데이터 파일의 권한을 0o600 으로
#: 좁힐지. 기본 True(신규 방어) — 라이브 실측: ``state/`` 아래 group/world-readable
#: jsonl 54개, 전부 이 함수를 거쳐 만들어진 것들이다. off 로 낮추면 0o666(+umask)
#: 그대로(byte-identical). 판정불가/오타 값은 falsy 집합에 없으면 켜짐 쪽으로
#: 접힌다 — fail-closed, 이 게이트의 "안전"은 기본값 쪽이다.
_ENV_LEDGER_STRICT_PERMS = "AGI_V8_LEDGER_STRICT_PERMS"
_STRICT_PERMS_FALSY = ("false", "0")


def strict_perms_enabled(env: Mapping[str, str] | None = None) -> bool:
    """새로 만드는 JSONL 데이터 파일을 0o600 으로 만들지 (default True).

    ⚠️ **이미 존재하는 파일의 모드는 절대 안 건드린다** — ``os.open`` 의 mode
    인자는 ``O_CREAT`` 로 실제 새로 만들 때만 커널이 적용한다(POSIX 계약, 기존
    파일엔 무영향). 회수/스크럽은 사람이 따로 한다.
    """
    e = os.environ if env is None else env
    return str(e.get(_ENV_LEDGER_STRICT_PERMS, "true")).strip().lower() not in _STRICT_PERMS_FALSY


#: __SLOT_R14_LEDGERMODE_2026_08_17__ ``strict_perms_enabled`` 의 자기 경고
#: ("이미 존재하는 파일의 모드는 절대 안 건드린다... 회수는 사람이 따로
#: 한다")를 실측이 뒤집었다 — 그 사람이 아무도 없었다. 라이브 젤
#: (``state/si_jail/``) 의 ``*.jsonl`` 23개 중 22개가 R9 패치 이후에도
#: 여전히 0644/0664 였다(``os.open`` mode 인자는 ``O_CREAT`` 로 실제 새로
#: 만들 때만 커널이 적용하는 POSIX 계약이므로, 기존 파일은 그 호출로 절대
#: chmod 되지 않는다). 이 함수는 그 사실을 **고치지 않고 센다** — 실제 회수는
#: ``tools/scrub_ledger_modes.py`` 의 몫(사람이 ``--apply`` 로 확인 후 실행).
#: 기본 ON — 읽기전용(``os.lstat`` 만, 내용은 절대 안 읽는다)이라 무해.
_ENV_LEDGER_MODE_AUDIT = "AGI_V8_LEDGER_MODE_AUDIT"
_LEDGER_MODE_AUDIT_FALSY = ("false", "0")
_LEDGER_MODE_TARGET = 0o600
#: 계기판 요약용 상한 — 전체 스크럽 리포트가 아니라 "몇 개나 위험한가"만
#: 답하는 자리라 경로 목록을 무제한으로 안 돌려준다.
_LEDGER_MODE_AUDIT_MAX_PATHS = 20


def ledger_mode_audit_enabled(env: Mapping[str, str] | None = None) -> bool:
    """``AGI_V8_LEDGER_MODE_AUDIT`` 게이트 (default True — 읽기만 하므로 무해).

    ``false``/``0`` 이면 :func:`audit_ledger_modes` 가 스캔 자체를 안 하고
    빈 결과를 돌려준다(OFF-parity). 판정불가/오타 값은 켜짐 쪽으로 접힌다
    — 이 게이트의 "안전"은 기본값(계기판에 뜬다) 쪽이다.
    """
    e = os.environ if env is None else env
    return str(e.get(_ENV_LEDGER_MODE_AUDIT, "true")).strip().lower() not in _LEDGER_MODE_AUDIT_FALSY


def _mode_risk_score(mode: int) -> int:
    """group/other 로 열린 권한 비트 개수 — "최악" mode 를 고르는 기준."""
    bits = (stat.S_IRGRP, stat.S_IWGRP, stat.S_IXGRP,
            stat.S_IROTH, stat.S_IWOTH, stat.S_IXOTH)
    return sum(1 for b in bits if mode & b)


def audit_ledger_modes(
    state_dir: Path | str,
    *,
    pattern: str = "*.jsonl",
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """*state_dir* 아래 ``pattern`` 파일 중 0o600 이 아닌 것의 개수 + 최악 mode.

    읽기전용 계기판 함수: ``os.lstat`` 만 쓴다 — 파일 **내용은 절대 안 읽는다**
    (원장은 append-only 이고 손편집 금지). 심링크는 타깃을 따라가지 않고
    (``os.lstat`` 자체가 링크를 본다) 건너뛴 것으로만 센다 — chmod 를 하는
    함수가 아니므로 심링크를 만나도 아무것도 바꾸지 않는다.

    게이트 OFF(``AGI_V8_LEDGER_MODE_AUDIT=false``) 면 디렉터리를 훑지도 않고
    빈 결과를 돌려준다. ``tools/scrub_ledger_modes.py`` 의 ``--check`` 가 이
    함수를 소비해 CI 게이트로 쓴다 — 실제 chmod 는 이 함수의 몫이 아니다.
    """
    if not ledger_mode_audit_enabled(env):
        return {
            "enabled": False, "risky_count": 0, "worst_mode": None,
            "symlink_count": 0, "total_scanned": 0, "paths": [],
            "paths_truncated": False,
        }
    root = Path(state_dir)
    risky: list[str] = []
    symlink_count = 0
    total = 0
    worst_mode: int | None = None
    if root.is_dir():
        for p in sorted(root.rglob(pattern)):
            try:
                st = os.lstat(str(p))
            except OSError as _ff_exc:
                _swallowed(_ff_exc, site="state.store.audit_ledger_modes:lstat",
                           category="persist")
                continue
            if stat.S_ISLNK(st.st_mode):
                symlink_count += 1
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            total += 1
            mode = stat.S_IMODE(st.st_mode)
            if mode == _LEDGER_MODE_TARGET:
                continue
            risky.append(p.relative_to(root).as_posix())
            if worst_mode is None or (
                _mode_risk_score(mode), mode
            ) > (_mode_risk_score(worst_mode), worst_mode):
                worst_mode = mode
    return {
        "enabled": True,
        "risky_count": len(risky),
        "worst_mode": (oct(worst_mode) if worst_mode is not None else None),
        "symlink_count": symlink_count,
        "total_scanned": total,
        "paths": risky[:_LEDGER_MODE_AUDIT_MAX_PATHS],
        "paths_truncated": len(risky) > _LEDGER_MODE_AUDIT_MAX_PATHS,
    }


def _append_jsonl_unlocked(
    path: Path,
    entry: Mapping[str, Any],
    *,
    require_directory_fsync: bool = False,
) -> None:
    """Append one durable line while the caller owns ``<path>.lock``."""
    line = (json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    # __SLOT_W1A3__ refuse to follow symlinks at the kernel level (TOCTOU-safe).
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW  # __SLOT_W1A3__
    _mode = 0o600 if strict_perms_enabled() else 0o666
    try:
        fd = os.open(str(path), flags, _mode)
    except OSError as exc:  # __SLOT_W1A3__
        if exc.errno in (errno.ELOOP, getattr(errno, "EMLINK", -1)):
            raise OSError(
                exc.errno,
                f"refusing to append through symlink: {path}",
            ) from exc
        raise
    try:
        _write_all(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)
    if require_directory_fsync:
        # A newly created ledger *and its directory chain* are not
        # crash-durable until every naming parent has been persisted.
        # Accounting authorities must see any error; treating it as
        # acknowledged can reopen a spent cap after power loss.
        _fsync_directory_chain_strict(path.parent)
    else:
        _fsync_directory(path.parent)


@contextmanager
def atomic_append_jsonl_transaction(
    path: Path | str,
    *,
    require_directory_fsync: bool = False,
) -> Iterator[Callable[[Mapping[str, Any]], None]]:
    """Hold the canonical JSONL writer lock across a caller transaction.

    The yielded callback appends and fsyncs one canonical line without trying
    to reacquire the lock.  This lets accounting code serialize
    read→validate→append against both current and older writers, all of which
    already use the same ``<path>.lock`` through :func:`atomic_append_jsonl`.
    Calling ``atomic_append_jsonl`` recursively from inside this context would
    self-deadlock; callers must use only the yielded callback.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_suffix(target.suffix + ".lock")
    active = False
    owner_pid = os.getpid()
    owner_thread = threading.get_ident()

    def append_while_locked(entry: Mapping[str, Any]) -> None:
        if not active:
            raise RuntimeError("JSONL transaction callback used outside its lock lease")
        if os.getpid() != owner_pid or threading.get_ident() != owner_thread:
            raise RuntimeError("JSONL transaction callback used by a non-owner")
        _append_jsonl_unlocked(
            target,
            entry,
            require_directory_fsync=require_directory_fsync,
        )

    with _file_lock(lock_path):
        active = True
        try:
            yield append_while_locked
        finally:
            active = False


def atomic_append_jsonl(path: Path | str, entry: Mapping[str, Any]) -> None:
    """Append a single JSON line atomically and durably.

    Cooperating writers are serialized with ``<path>.lock``.  Code that must
    keep a read/check in the same critical section uses
    :func:`atomic_append_jsonl_transaction`.
    """
    with atomic_append_jsonl_transaction(path) as append_locked:
        append_locked(entry)


def atomic_write_text(path: Path | str, text: str) -> None:
    """Write text atomically via tempfile rename."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink_target(path, what="text output")  # __SLOT_W1A3__
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        _fsync_directory(path.parent)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError as _ff_exc:
            _swallowed(_ff_exc, site="state.store.atomic_write_text:183", category="persist")
            pass
        raise


def read_jsonl(path: Path | str) -> list[dict[str, Any]]:
    """Read JSONL file as list of dicts. Empty list if missing.

    Skips blank lines. Malformed lines raise json.JSONDecodeError (callers
    that need tolerant parsing should catch).
    """
    path = Path(path)
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def read_json(path: Path | str, default: Any = None) -> Any:
    """Read JSON file. Return *default* if file missing."""
    path = Path(path)
    if not path.exists():
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


__all__ = [
    "atomic_write_json",
    "atomic_append_jsonl",
    "atomic_append_jsonl_transaction",
    "atomic_write_text",
    "read_jsonl",
    "read_json",
    "strict_perms_enabled",
    "ledger_mode_audit_enabled",
    "audit_ledger_modes",
]
