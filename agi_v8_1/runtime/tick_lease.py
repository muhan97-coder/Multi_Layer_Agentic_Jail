# __SLOT_TICK_LEASE_2026_08_18__ R3 잔여 ② — single-flight lease/CAS.
"""Kernel-authoritative single-flight ownership for one tick.

``scripts/tick.sh`` already holds an outer ``flock``, but direct/manual/future
non-cron callers of ``tick_once`` need the same exclusion inside Python. The
JSON record is useful diagnostics, not an authority primitive.

The lock file is opened once and an exclusive non-blocking POSIX ``flock`` is
held across the *entire* :func:`acquire` context. A live holder can therefore
never be replaced merely because a wall-clock TTL elapsed. On process death the
kernel closes the fd and releases the lock immediately; the next owner may then
overwrite the stale JSON record without waiting for a timeout or manual cleanup.

``expires_ts`` remains an operator-facing expected-completion timestamp. It is
not consulted to authorize takeover, so clock jumps, injected event timestamps,
and a slow but live owner cannot create two concurrent dispatches. The random
nonce protects cleanup from deleting a later record, but downstream fencing is
not needed for mutual exclusion because the kernel lock remains held until work
has fully unwound.

This module fails closed on hosts without POSIX ``flock`` or when the lock
cannot be inspected safely. It never falls back to an unlocked JSON CAS.

**default-OFF** (``AGI_V8_TICK_LEASE_ENABLED``). OFF leaves
``runtime.tick_runner`` on its original path.
"""
from __future__ import annotations

import errno
import math
import os
import socket
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from agi_v8_1.state.store import atomic_write_json, read_json

# __SLOT_FAIL_FAST_2026_07_25__ 삼킴은 이름 붙여 세고 STRICT 면 다시 던진다.
from agi_v8_1.policy.fail_fast import swallowed as _swallowed

try:  # POSIX-only authority; absence must fail closed.
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Linux production has fcntl
    _fcntl = None  # type: ignore[assignment]

_ENV_ENABLED = "AGI_V8_TICK_LEASE_ENABLED"      # master gate (default OFF)
_ENV_TTL = "AGI_V8_TICK_LEASE_TTL_SEC"          # diagnostic horizon (default 1800s)
_DEFAULT_TTL_SEC = 1800.0

_LEASE_REL = ("tick", "lease.json")
_LEASE_LOCK_REL = ("tick", "lease.json.lock")
# Forked deadline children must not inherit the parent's authoritative lock fd.
# The set is process-local; after ``fork`` the child closes only its copied fds.
_HELD_LOCK_FDS: set[int] = set()


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def enabled() -> bool:
    """Default-OFF gate for in-process tick single-flight enforcement."""
    return _truthy(_ENV_ENABLED)  # tier: T2


def ttl_seconds() -> float:
    """Return the finite positive diagnostic horizon, or the safe default."""
    try:
        value = float(os.environ.get(_ENV_TTL, str(_DEFAULT_TTL_SEC)))
    except (TypeError, ValueError) as exc:
        _swallowed(exc, site="runtime.tick_lease.ttl_seconds", category="config")
        return _DEFAULT_TTL_SEC
    return value if math.isfinite(value) and value > 0 else _DEFAULT_TTL_SEC


def _lease_path(state_dir: Path) -> Path:
    return state_dir.joinpath(*_LEASE_REL)


def _lock_path(state_dir: Path) -> Path:
    return state_dir.joinpath(*_LEASE_LOCK_REL)


class LeaseBusy(RuntimeError):
    """Another live process currently owns the kernel lock."""

    def __init__(self, holder: "dict[str, Any]") -> None:
        super().__init__(f"tick lease held by {holder.get('owner')!r} "
                         f"until {holder.get('expires_ts')}")
        self.holder = holder


class LeaseUnavailable(RuntimeError):
    """The platform or filesystem cannot provide the required kernel lock."""


def _default_owner() -> str:
    try:
        host = socket.gethostname()
    except OSError as exc:  # pragma: no cover - host lookup failure is rare
        _swallowed(exc, site="runtime.tick_lease._default_owner", category="config")
        host = "unknown-host"
    return f"{host}:{os.getpid()}"


def _read_lease(path: Path) -> "dict[str, Any] | None":
    try:
        data = read_json(path, default=None)
    except Exception as exc:  # noqa: BLE001 - diagnostics never outrank kernel lock
        _swallowed(exc, site="runtime.tick_lease._read_lease", category="persist")
        return None
    return data if isinstance(data, dict) else None


def _try_lock(lock_path: Path) -> int | None:
    """Return an owned lock fd, ``None`` when busy, or fail closed."""
    if _fcntl is None:
        raise LeaseUnavailable("POSIX flock is unavailable")
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_RDWR
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(str(lock_path), flags, 0o600)
    except OSError as exc:
        raise LeaseUnavailable("cannot open tick lease lock") from exc
    try:
        _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
    except OSError as exc:
        try:
            os.close(fd)
        except OSError as close_exc:
            _swallowed(close_exc, site="runtime.tick_lease._try_lock:close",
                       category="persist")
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            return None
        raise LeaseUnavailable("cannot acquire tick lease lock") from exc
    return fd


def _release_lock(fd: int) -> None:
    """Best-effort teardown — kernel already releases the lock on process exit,
    so a failure here must never escape and crash the caller mid-``tick_once``.

    # __SLOT_TICK_LEASE_RELEASE_NEVER_RAISES_2026_08_18__ 🔴 결함 수리 — 이전
    # 구현은 ``os.close(fd)``/``flock(LOCK_UN)`` 이 던지는 ``OSError``(예: NFS
    # ``EIO``, 디스크 풀)를 그대로 흘려보냈다. 이 함수는 ``acquire()`` 의
    # ``finally:`` 에서 불리고, ``acquire()`` 자체는 ``@contextmanager`` 라
    # 여기서 raise 하면 ``tick_runner.tick_once`` 의 ``finally: _lease_ctx.
    # __exit__(...)`` 를 그대로 관통해 프로세스를 죽인다 — 이 파일의 다른 모든
    # OSError 지점(``_try_lock``·``_default_owner``·acquire() 의 lease-record
    # 삭제)이 이미 지키는 "프로세스는 안 죽는다" 원칙을 여기만 어겼다. 실측:
    # ``os.close`` 를 ``OSError(EIO)`` 로 패치하고 ``tick_once`` 를 부르면
    # 수리 전엔 그 예외가 그대로 던져졌다. 커널은 프로세스 종료 시 fd/lock 을
    # 어차피 회수하므로, 여기서 실패해도 잃는 안전성은 없다 — 그래서
    # best-effort 로 접어도 된다.
    """
    _HELD_LOCK_FDS.discard(fd)
    try:
        if _fcntl is not None:
            _fcntl.flock(fd, _fcntl.LOCK_UN)
    except OSError as exc:
        _swallowed(exc, site="runtime.tick_lease._release_lock:flock_un",
                   category="persist")
    finally:
        try:
            os.close(fd)
        except OSError as exc:
            _swallowed(exc, site="runtime.tick_lease._release_lock:close",
                       category="persist")


def close_inherited_lock_fds_in_child() -> bool:
    """Close fork-copied lease fds without unlocking the parent's descriptor."""
    ok = True
    for fd in tuple(_HELD_LOCK_FDS):
        try:
            os.close(fd)
        except OSError:
            ok = False
    _HELD_LOCK_FDS.clear()
    return ok


def _probe_lock_held(lock_path: Path) -> bool | None:
    """Observe the kernel lock without trusting the JSON timestamp.

    The non-blocking probe owns the lock only for the few instructions needed
    to establish that nobody else holds it, then releases it immediately. A
    failure to inspect returns ``None`` so :func:`status` can report the
    record-presence direction instead of falsely declaring a live owner absent.
    """
    if _fcntl is None:
        return None
    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(lock_path), flags)
    except FileNotFoundError:
        return False
    except OSError as exc:
        _swallowed(exc, site="runtime.tick_lease._probe_lock_held:open",
                   category="persist")
        return None
    try:
        try:
            _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                return True
            _swallowed(exc, site="runtime.tick_lease._probe_lock_held:flock",
                       category="persist")
            return None
        _fcntl.flock(fd, _fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


@contextmanager
def acquire(
    state_dir: "Path | str",
    *,
    ttl: "float | None" = None,
    owner: "str | None" = None,
    now: "float | None" = None,
) -> Iterator["dict[str, Any]"]:
    """Own one tick until the context exits; never overlap a live holder.

    ``ttl`` and ``now`` shape diagnostic timestamps only. The held kernel fd is
    the sole authority: expiration cannot authorize takeover, while a crash
    releases ownership immediately through normal kernel fd cleanup.
    """
    sd = Path(state_dir)
    if ttl is None:
        diagnostic_ttl = ttl_seconds()
    else:
        try:
            candidate_ttl = float(ttl)
        except (TypeError, ValueError):
            candidate_ttl = _DEFAULT_TTL_SEC
        diagnostic_ttl = (
            candidate_ttl
            if math.isfinite(candidate_ttl) and candidate_ttl > 0
            else _DEFAULT_TTL_SEC
        )
    acquired_ts = float(now) if now is not None else time.time()
    owner_id = owner or _default_owner()
    lock_path = _lock_path(sd)
    lease_path = _lease_path(sd)
    nonce = uuid.uuid4().hex

    fd = _try_lock(lock_path)
    if fd is None:
        holder = _read_lease(lease_path) or {
            "owner": "unknown-live-holder",
            "expires_ts": None,
        }
        raise LeaseBusy(holder)
    _HELD_LOCK_FDS.add(fd)

    record = {
        "owner": owner_id,
        "nonce": nonce,
        "pid": os.getpid(),
        "acquired_ts": acquired_ts,
        "expires_ts": acquired_ts + diagnostic_ttl,
    }
    try:
        try:
            atomic_write_json(lease_path, record)
        except Exception as exc:
            raise LeaseUnavailable("cannot publish tick lease record") from exc
        try:
            yield record
        finally:
            current = _read_lease(lease_path)
            if current is not None and current.get("nonce") == nonce:
                try:
                    lease_path.unlink()
                except OSError as exc:
                    _swallowed(exc, site="runtime.tick_lease.acquire:release",
                               category="persist")
    finally:
        _release_lock(fd)


def status(state_dir: "Path | str", *, now: "float | None" = None) -> "dict[str, Any]":
    """Read-only kernel ownership plus a separate diagnostic expiry view."""
    sd = Path(state_dir)
    current_ts = float(now) if now is not None else time.time()
    record = _read_lease(_lease_path(sd))
    kernel_held = _probe_lock_held(_lock_path(sd))
    if record is None:
        return {"held": kernel_held is True, "expired": None, "lease": None}
    try:
        expires_ts = float(record.get("expires_ts", 0.0))
    except (TypeError, ValueError) as exc:
        _swallowed(exc, site="runtime.tick_lease.status:expires_ts", category="persist")
        expires_ts = 0.0
    expired_by_timestamp = expires_ts <= current_ts
    return {
        # The kernel answer is authoritative. If it cannot be observed, an
        # extant record falls closed toward "held" rather than authorizing work.
        "held": kernel_held if kernel_held is not None else True,
        "expired": expired_by_timestamp,
        "lease": record,
    }


__all__ = [
    "enabled",
    "ttl_seconds",
    "acquire",
    "status",
    "LeaseBusy",
    "LeaseUnavailable",
    "close_inherited_lock_fds_in_child",
]
