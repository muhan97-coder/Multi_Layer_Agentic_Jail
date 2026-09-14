# __SLOT_TICK_DEADLINE_2026_08_18__ R3 잔여 ③ — dispatch() 벽시계 상한.
"""Process-isolated wall-clock deadline for one tick dispatch.

Threads cannot provide the required contract. A daemon can outlive
``tick_once`` and overlap the next lease holder; a non-daemon or nested
``ThreadPoolExecutor`` can keep interpreter shutdown and ``tick.sh``'s flock
alive indefinitely. A synchronous ``SIGALRM`` is insufficient too: exception
unwind may enter ``executor.shutdown(wait=True)`` and block on the same worker.

When enabled, :func:`run_with_deadline` therefore forks the whole call into a
dedicated POSIX session/process group. The parent retains the tick lease, reads
one bounded fixed-schema JSON frame, and terminates the entire child group on
*every* outcome. It does not return until the group leader has been reaped.
Timeout, catchable parent cancellation, child protocol failure, and normal
success all share that cleanup boundary, so no dispatch thread or descendant
process can survive lease release while the parent executes its cleanup.

The IPC contract uses no pickle and publishes no exception message. Generic
child failures carry only a bounded canonical exception type label. The one
control-flow exception whose semantics must cross the boundary,
``halt_sentinel.HumanHalt``, is reconstructed from a whitelisted fixed-shape
state record.

This is intentionally Linux-oriented and fail-closed before calling the
function when invoked outside the Python main thread, while any other thread or
pre-existing child is alive, or where the required fork/session/subreaper
primitives are unavailable. The parent temporarily becomes a Linux child
subreaper for the transaction. Descendants that create a new session are thus
adopted, killed, and reaped after the root process group is terminated; the
subreaper setting is restored only after that descendant set is empty.

The isolated leader also arms Linux ``PR_SET_PDEATHSIG(SIGKILL)`` (with a
parent-PID race check), and ``scripts/tick.sh`` uses ``flock -o``. Thus a hard
parent crash cannot leave the parked leader alive or either tick lock inherited.
There is one hard-crash boundary this module cannot close on its own: **any
already-spawned descendant** can outlive an uncatchable parent ``SIGKILL``
because PDEATHSIG targets the leader, not its tree, and the subreaper dies
before sweeping. Such descendants hold neither the Python lease nor the outer
cron flock, but may still mutate state late and overlap the next tick. Normal
return, timeout, halt, and catchable cancellation retain full descendant-tree
cleanup regardless.

That residual is what :mod:`runtime.tick_descendant_seal` closes, and this
module is its live caller. When ``AGI_V8_TICK_DESCENDANT_SEAL_ENABLED`` is on,
:func:`_child_envelope` runs ``fn`` as the init process of a fresh unprivileged
PID namespace instead of calling it directly, so the kernel force-kills every
descendant ``fn`` spawned — at any depth — the moment that namespace's init
dies, with no cleanup code of ours running anywhere. The seal is a strictly
inner boundary: this module's parent-side wall-clock deadline stays
authoritative (the seal's own timeout is only a backstop, deliberately slack),
so ``DeadlineExceeded`` remains the timeout outcome and the sealed subtree is
also torn down by the ordinary group cleanup on every non-crash path.

One measured behavior narrowing comes with that gate: the result now crosses
two IPC frames, and the inner one caps at 64 KiB against this module's 256 KiB
(measured 2026-08-19 — a 100 KB result returns unsealed and raises
``DispatchChildError(code="result_too_large")`` sealed). The *code* is the one
this module already publishes for an oversized result, so no caller learns a
new failure shape; only the threshold moves while the gate is on.

**default-OFF** on both gates (``AGI_V8_TICK_DEADLINE_ENABLED`` for this
module, ``AGI_V8_TICK_DESCENDANT_SEAL_ENABLED`` for the inner seal, which is
independently OFF-able while this module is ON). Deadline OFF leaves
``runtime.tick_runner`` on its original direct synchronous call path; seal OFF
leaves the isolated child on its original direct ``fn(*args, **kwargs)`` call.
"""
from __future__ import annotations

import ctypes
import functools
import json
import math
import os
from pathlib import Path
import select
import signal
import struct
import sys
import threading
import time
from typing import Any, Callable

from agi_v8_1.policy.fail_fast import (
    safe_exception_type_name,
    swallowed as _swallowed,
)
from agi_v8_1.runtime import tick_descendant_seal

_ENV_ENABLED = "AGI_V8_TICK_DEADLINE_ENABLED"   # master gate (default OFF)
_ENV_SEC = "AGI_V8_TICK_DEADLINE_SEC"           # wall-clock budget (default 900s)
_DEFAULT_DEADLINE_SEC = 900.0

# The inner seal's gate is read through ``tick_descendant_seal.enabled()``
# only — this module never parses that env name itself, so the gate keeps a
# single parser and cannot drift between two readers.

_IPC_VERSION = 1
_MAX_IPC_BYTES = 256 * 1024
_FRAME = struct.Struct("!I")
_TERM_GRACE_SEC = 0.10
_KILL_REAP_SEC = 1.0
_PR_SET_PDEATHSIG = 1
_PR_SET_CHILD_SUBREAPER = 36
_PR_GET_CHILD_SUBREAPER = 37


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def enabled() -> bool:
    """Default-OFF gate for process-isolated dispatch."""
    return _truthy(_ENV_ENABLED)  # tier: T2


def deadline_seconds() -> float:
    """Return a finite positive configured budget, otherwise the safe default."""
    try:
        value = float(os.environ.get(_ENV_SEC, str(_DEFAULT_DEADLINE_SEC)))
    except (TypeError, ValueError) as exc:
        _swallowed(exc, site="runtime.tick_deadline.deadline_seconds", category="config")
        return _DEFAULT_DEADLINE_SEC
    return value if math.isfinite(value) and value > 0 else _DEFAULT_DEADLINE_SEC


class DeadlineExceeded(RuntimeError):
    """The isolated dispatch exceeded its wall-clock budget and was reaped."""

    def __init__(self, budget_seconds: float) -> None:
        super().__init__(f"dispatch exceeded {budget_seconds:.1f}s wall-clock budget")
        self.budget_seconds = budget_seconds


class DeadlineUnavailable(RuntimeError):
    """The no-survivor process deadline cannot be provided in this context."""


class DispatchChildError(RuntimeError):
    """A child failed without exporting its exception text."""

    _MESSAGES = {
        "exception": "dispatch child raised",
        "result_not_json": "dispatch child result was not JSON-safe",
        "result_too_large": "dispatch child result exceeded IPC limit",
        "protocol": "dispatch child protocol failed",
    }

    def __init__(self, code: str, *, exception_type: str = "Exception") -> None:
        safe_code = code if code in self._MESSAGES else "protocol"
        super().__init__(self._MESSAGES[safe_code])
        self.code = safe_code
        self.exception_type = exception_type


class _ChildTimeout(BaseException):
    pass


class _ChildProtocol(BaseException):
    pass


class _ParentControl(BaseException):
    """A parent termination signal interrupted the owned transaction."""


def _unsupported_reason() -> str | None:
    required = ("fork", "setsid", "killpg", "waitpid", "pipe")
    if os.name != "posix" or not all(hasattr(os, name) for name in required):
        return "POSIX fork/process-group primitives are unavailable"
    if threading.current_thread() is not threading.main_thread():
        return "deadline requires the Python main thread"
    if any(
        thread is not threading.current_thread() and thread.is_alive()
        for thread in threading.enumerate()
    ):
        return "deadline requires a single-threaded parent process"
    if (
        not sys.platform.startswith("linux")
        or not _children_path().exists()
        or not hasattr(signal, "pthread_sigmask")
    ):
        return "Linux child-subreaper observation is unavailable"
    return None


def _children_path() -> Path:
    return Path(f"/proc/self/task/{os.getpid()}/children")


def _direct_children() -> set[int]:
    try:
        with _children_path().open(encoding="ascii") as stream:
            text = stream.read(65537)
    except OSError as exc:
        raise DeadlineUnavailable("cannot inspect child process set") from exc
    if len(text) > 65536:
        raise DeadlineUnavailable("child process set exceeded inspection limit")
    out: set[int] = set()
    for token in text.split():
        if not token.isascii() or not token.isdigit():
            raise DeadlineUnavailable("child process set was malformed")
        pid = int(token)
        if pid > 0:
            out.add(pid)
    if len(out) > 4096:
        raise DeadlineUnavailable("child process set exceeded count limit")
    return out


def _libc() -> Any:
    try:
        return ctypes.CDLL(None, use_errno=True)
    except OSError as exc:
        raise DeadlineUnavailable("cannot load child-subreaper control") from exc


def _get_subreaper() -> bool:
    value = ctypes.c_int(0)
    result = _libc().prctl(
        ctypes.c_int(_PR_GET_CHILD_SUBREAPER), ctypes.byref(value),
        ctypes.c_ulong(0), ctypes.c_ulong(0), ctypes.c_ulong(0),
    )
    if result != 0:
        raise DeadlineUnavailable("cannot read child-subreaper state")
    return value.value == 1


def _set_subreaper(enabled: bool) -> None:
    result = _libc().prctl(
        ctypes.c_int(_PR_SET_CHILD_SUBREAPER), ctypes.c_ulong(1 if enabled else 0),
        ctypes.c_ulong(0), ctypes.c_ulong(0), ctypes.c_ulong(0),
    )
    if result != 0:
        raise DeadlineUnavailable("cannot set child-subreaper state")


def _arm_parent_death_signal(expected_parent_pid: int) -> bool:
    """Arm SIGKILL and close the parent-death-before-prctl race."""
    try:
        result = _libc().prctl(
            ctypes.c_int(_PR_SET_PDEATHSIG), ctypes.c_ulong(signal.SIGKILL),
            ctypes.c_ulong(0), ctypes.c_ulong(0), ctypes.c_ulong(0),
        )
    except DeadlineUnavailable:
        return False
    return result == 0 and os.getppid() == expected_parent_pid


def assert_supported_context() -> None:
    """Fail before state/dispatch I/O unless fork isolation is safe to start."""
    reason = _unsupported_reason()
    if reason is not None:
        raise DeadlineUnavailable(reason)
    if _direct_children():
        raise DeadlineUnavailable("deadline requires no pre-existing child processes")
    _get_subreaper()


def _coerce_budget(deadline: "float | None") -> float:
    if deadline is None:
        return deadline_seconds()
    try:
        budget = float(deadline)
    except (TypeError, ValueError) as exc:
        raise DeadlineUnavailable("deadline must be a finite positive number") from exc
    if not math.isfinite(budget) or budget <= 0.0:
        raise DeadlineUnavailable("deadline must be a finite positive number")
    return budget


def _pipe_cloexec() -> tuple[int, int]:
    if hasattr(os, "pipe2"):
        return os.pipe2(getattr(os, "O_CLOEXEC", 0))
    read_fd, write_fd = os.pipe()
    os.set_inheritable(read_fd, False)
    os.set_inheritable(write_fd, False)
    return read_fd, write_fd


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short IPC write")
        view = view[written:]


# The seal's timeout must never be the one that fires. This module's parent
# already owns the authoritative wall-clock budget and reaps the whole group on
# expiry, so the inner seal gets that same remaining budget plus slack and acts
# only as a backstop for the case where the parent is no longer there to read.
# Without the slack the two deadlines expire in the same instant and the
# timeout outcome would race between ``DeadlineExceeded`` and a ``SealTimeout``
# error frame; without passing the remaining budget at all, ``run_sealed``'s
# own 10s default would abort every normal dispatch under a 900s budget.
_SEAL_TIMEOUT_SLACK_SEC = 5.0
_SEAL_TIMEOUT_FLOOR_SEC = 1.0

# ``run_sealed`` reports a failed sealed call as ``SealChildError`` carrying a
# bounded type label. These two labels are the seal's own IPC verdicts rather
# than ``fn``'s exception type, so they map back onto this module's equivalent
# codes; every other label is ``fn``'s real exception type and is reported
# exactly as the unsealed path would report it.
_SEAL_LABEL_TO_CODE = {
    "ResultTooLarge": "result_too_large",
    "Protocol": "protocol",
}
_SEAL_HUMAN_HALT_LABEL = "HumanHalt"


def _seal_timeout(deadline_at: "float | None") -> float:
    """Remaining parent budget plus slack, so the parent deadline wins."""
    if deadline_at is None:
        return deadline_seconds() + _SEAL_TIMEOUT_SLACK_SEC
    remaining = deadline_at - time.monotonic()
    if not math.isfinite(remaining) or remaining < _SEAL_TIMEOUT_FLOOR_SEC:
        remaining = _SEAL_TIMEOUT_FLOOR_SEC
    return remaining + _SEAL_TIMEOUT_SLACK_SEC


def _unwrap_seal_error(exc: BaseException) -> "tuple[str, str] | None":
    """Map a sealed-boundary failure onto this module's ``(code, type)`` pair."""
    if not isinstance(exc, tick_descendant_seal.SealChildError):
        return None
    label = exc.exception_type
    if type(label) is not str or not label:
        return ("exception", "Exception")
    mapped = _SEAL_LABEL_TO_CODE.get(label)
    if mapped is not None:
        return (mapped, "Exception")
    return ("exception", label)


def _child_envelope(fn: "Callable[..., Any]", args: tuple[Any, ...],
                    kwargs: dict[str, Any],
                    deadline_at: "float | None" = None) -> dict[str, Any]:
    try:
        if tick_descendant_seal.enabled():
            # ``functools.partial`` rather than forwarding ``*args, **kwargs``:
            # ``run_sealed`` takes its own keyword-only ``timeout``, and a
            # dispatch kwarg of that name would collide with it silently.
            value = tick_descendant_seal.run_sealed(
                functools.partial(fn, *args, **kwargs),
                timeout=_seal_timeout(deadline_at),
            )
        else:
            value = fn(*args, **kwargs)
    except BaseException as exc:  # child boundary: only fixed schema crosses
        try:
            from agi_v8_1.runtime import halt_sentinel

            is_human_halt = isinstance(exc, halt_sentinel.HumanHalt)
        except BaseException:
            is_human_halt = False
        unwrapped = _unwrap_seal_error(exc)
        if unwrapped is not None and unwrapped[1] == _SEAL_HUMAN_HALT_LABEL:
            # A halt raised *inside* the seal cannot cross two boundaries as an
            # exception object, only as its type label. Re-entering the same
            # human_halt branch keeps the parent's fail-closed re-probe (below)
            # as the single authority on whether a halt is actually in effect.
            is_human_halt = True
        if is_human_halt:
            # Do not serialize exception state. The parent re-probes the
            # canonical sentinel and falls closed to an unreadable halt if the
            # state changed between child observation and parent handling.
            return {"v": _IPC_VERSION, "kind": "human_halt"}
        code, exception_type = (
            unwrapped if unwrapped is not None
            else ("exception", safe_exception_type_name(exc))
        )
        return {
            "v": _IPC_VERSION,
            "kind": "error",
            "code": code,
            "exception_type": exception_type,
        }
    return {"v": _IPC_VERSION, "kind": "ok", "value": value}


def _encode_envelope(envelope: dict[str, Any]) -> bytes:
    try:
        payload = json.dumps(
            envelope,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except BaseException:
        payload = json.dumps({
            "v": _IPC_VERSION,
            "kind": "error",
            "code": "result_not_json",
            "exception_type": "Exception",
        }, separators=(",", ":")).encode("ascii")
    if len(payload) > _MAX_IPC_BYTES:
        payload = json.dumps({
            "v": _IPC_VERSION,
            "kind": "error",
            "code": "result_too_large",
            "exception_type": "Exception",
        }, separators=(",", ":")).encode("ascii")
    return payload


def _reset_child_termination_signals() -> None:
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, signal.SIG_DFL)


def _child_main(
    ready_read: int,
    ready_write: int,
    result_read: int,
    result_write: int,
    fn: "Callable[..., Any]",
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    expected_parent_pid: int,
    inherited_signal_mask: set[signal.Signals],
    deadline_at: "float | None" = None,
) -> None:
    # ``deadline_at`` is a CLOCK_MONOTONIC instant, which is system-wide and
    # therefore directly comparable across this fork — the child needs it to
    # size the inner seal's backstop timeout when that gate is on.
    try:
        os.close(ready_read)
        os.close(result_read)
        try:
            # The child must never run the parent's transaction handler. Keep
            # signals blocked across fork until defaults and PDEATHSIG are set.
            _reset_child_termination_signals()
            if not _arm_parent_death_signal(expected_parent_pid):
                raise OSError("parent-death signal setup failed")
            signal.pthread_sigmask(signal.SIG_SETMASK, inherited_signal_mask)
        except BaseException:
            try:
                _write_all(ready_write, b"0")
            except BaseException:
                pass
            os._exit(70)
        try:
            os.setsid()
        except BaseException:
            try:
                _write_all(ready_write, b"0")
            except BaseException:
                pass
            os._exit(70)
        try:
            from agi_v8_1.runtime import tick_lease

            if not tick_lease.close_inherited_lock_fds_in_child():
                raise OSError("inherited lease fd close failed")
        except BaseException:
            try:
                # ``2`` authenticates that setsid completed, so killpg is safe.
                _write_all(ready_write, b"2")
            except BaseException:
                pass
            os._exit(70)

        _write_all(ready_write, b"1")
        os.close(ready_write)
        payload = _encode_envelope(_child_envelope(fn, args, kwargs, deadline_at))
        _write_all(result_write, _FRAME.pack(len(payload)) + payload)
        os.close(result_write)

        # Keep the group leader alive until the parent has the complete frame.
        # The parent then terminates the whole group even on successful result,
        # which also removes nested workers/subprocesses before lease release.
        _reset_child_termination_signals()
        while True:
            signal.pause()
    except BaseException:
        os._exit(71)


def _read_exact(fd: int, size: int, deadline_at: float) -> bytes:
    out = bytearray()
    while len(out) < size:
        remaining = deadline_at - time.monotonic()
        if remaining <= 0.0:
            raise _ChildTimeout()
        try:
            readable, _, _ = select.select([fd], [], [], remaining)
        except InterruptedError:
            continue
        if not readable:
            raise _ChildTimeout()
        try:
            chunk = os.read(fd, size - len(out))
        except InterruptedError:
            continue
        if not chunk:
            raise _ChildProtocol()
        out.extend(chunk)
    return bytes(out)


def _signal_child(pid: int, group_ready: bool, signum: int) -> None:
    try:
        if group_ready:
            os.killpg(pid, signum)
        else:
            os.kill(pid, signum)
    except ProcessLookupError:
        pass


def _reap_once(pid: int) -> bool:
    try:
        waited, _ = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return True
    return waited == pid


def _group_exists(pid: int, group_ready: bool) -> bool:
    try:
        if group_ready:
            os.killpg(pid, 0)
        else:
            os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _signal_adopted_children(leader_pid: int, signum: int) -> set[int]:
    adopted = _direct_children() - {leader_pid}
    own_group = os.getpgrp()
    for child_pid in adopted:
        try:
            child_group = os.getpgid(child_pid)
        except ProcessLookupError:
            continue
        if child_group != own_group:
            try:
                os.killpg(child_group, signum)
            except ProcessLookupError:
                pass
        try:
            os.kill(child_pid, signum)
        except ProcessLookupError:
            pass
    return adopted


def _reap_adopted_children(leader_pid: int) -> set[int]:
    adopted = _direct_children() - {leader_pid}
    for child_pid in adopted:
        _reap_once(child_pid)
    return _direct_children() - {leader_pid}


def _terminate_and_reap(pid: int, group_ready: bool) -> None:
    """Bounded cleanup of the leader group and Linux-subreaper adoptees."""
    _signal_child(pid, group_ready, signal.SIGTERM)
    leader_reaped = False
    term_end = time.monotonic() + _TERM_GRACE_SEC
    while time.monotonic() < term_end:
        leader_reaped = leader_reaped or _reap_once(pid)
        _signal_adopted_children(pid, signal.SIGTERM)
        adopted = _reap_adopted_children(pid)
        if leader_reaped and not _group_exists(pid, group_ready) and not adopted:
            return
        time.sleep(0.005)

    _signal_child(pid, group_ready, signal.SIGKILL)
    kill_end = time.monotonic() + _KILL_REAP_SEC
    while time.monotonic() < kill_end:
        leader_reaped = leader_reaped or _reap_once(pid)
        _signal_adopted_children(pid, signal.SIGKILL)
        adopted = _reap_adopted_children(pid)
        if leader_reaped and not _group_exists(pid, group_ready) and not adopted:
            return
        time.sleep(0.005)
    if not leader_reaped:
        raise DeadlineUnavailable("dispatch child could not be reaped")
    if _group_exists(pid, group_ready):
        raise DeadlineUnavailable("dispatch child process group survived cleanup")
    if _direct_children():
        raise DeadlineUnavailable("adopted dispatch descendant survived cleanup")


_TRANSACTION_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


class _ParentSignalDeferral:
    """Own catchable termination from pre-fork setup through final reap."""

    def __init__(self) -> None:
        self.originals: dict[int, Any] = {}
        self.pending: set[int] = set()
        self.cleaning = False

    def _handle(self, signum: int, _frame: Any) -> None:
        self.pending.add(signum)
        if not self.cleaning:
            raise _ParentControl()

    def install(self) -> None:
        previous_mask = signal.pthread_sigmask(
            signal.SIG_BLOCK, _TRANSACTION_SIGNALS,
        )
        try:
            for signum in _TRANSACTION_SIGNALS:
                self.originals[signum] = signal.signal(signum, self._handle)
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)

    def begin_cleanup(self) -> None:
        previous_mask = signal.pthread_sigmask(
            signal.SIG_BLOCK, _TRANSACTION_SIGNALS,
        )
        try:
            self.cleaning = True
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)

    def restore_and_replay(self) -> None:
        previous_mask = signal.pthread_sigmask(
            signal.SIG_BLOCK, _TRANSACTION_SIGNALS,
        )
        try:
            for signum, original in self.originals.items():
                signal.signal(signum, original)
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        for signum in _TRANSACTION_SIGNALS:
            if signum in self.pending:
                signal.raise_signal(signum)


def _finish_parent_cleanup(
    pid: int | None,
    group_ready: bool,
    fds: tuple[int, ...],
    *,
    suppress_control: bool,
) -> None:
    """Idempotently finish cleanup before replaying an injected control exit."""
    try:
        if pid is None:
            for fd in fds:
                try:
                    os.close(fd)
                except OSError:
                    pass
            return
        ready_read, ready_write, result_read, result_write = fds
        if not group_ready:
            try:
                os.set_blocking(ready_read, False)
                group_ready = os.read(ready_read, 1) in (b"1", b"2")
            except (BlockingIOError, OSError):
                pass
        for fd in (ready_read, ready_write, result_read, result_write):
            try:
                os.close(fd)
            except OSError:
                pass
        _terminate_and_reap(pid, group_ready)
    except (KeyboardInterrupt, SystemExit):
        _finish_parent_cleanup(
            pid, group_ready, fds, suppress_control=True,
        )
        if not suppress_control:
            raise


def _restore_subreaper_without_interruption(*, suppress_control: bool) -> None:
    try:
        _set_subreaper(False)
    except (KeyboardInterrupt, SystemExit):
        _restore_subreaper_without_interruption(suppress_control=True)
        if not suppress_control:
            raise


def _decode_envelope(payload: bytes) -> Any:
    try:
        envelope = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DispatchChildError("protocol") from exc
    if not isinstance(envelope, dict) or envelope.get("v") != _IPC_VERSION:
        raise DispatchChildError("protocol")
    kind = envelope.get("kind")
    if kind == "ok" and "value" in envelope:
        return envelope["value"]
    if kind == "human_halt":
        from agi_v8_1.runtime import halt_sentinel

        observed = halt_sentinel.state()
        if not observed.halted:
            observed = halt_sentinel.HaltState(halted=True, readable=False)
        raise halt_sentinel.HumanHalt(observed)
    if kind == "error":
        code = envelope.get("code")
        exception_type = envelope.get("exception_type")
        if type(exception_type) is not str or len(exception_type) > 128:
            exception_type = "Exception"
        raise DispatchChildError(
            code if type(code) is str else "protocol",
            exception_type=exception_type,
        )
    raise DispatchChildError("protocol")


def run_with_deadline(
    fn: "Callable[..., Any]", *args: Any,
    deadline: "float | None" = None, **kwargs: Any,
) -> Any:
    """Run ``fn`` in one bounded child process group and return its JSON value."""
    budget = _coerce_budget(deadline)
    assert_supported_context()
    deadline_at = time.monotonic() + budget
    deferral = _ParentSignalDeferral()
    subreaper_changed = False
    fds: list[int] = []
    pid: int | None = None
    group_ready = False
    payload: bytes | None = None
    control_interrupted = False
    try:
        deferral.install()
        previous_subreaper = _get_subreaper()
        if not previous_subreaper:
            _set_subreaper(True)
            subreaper_changed = True

        try:
            pipe_mask = signal.pthread_sigmask(
                signal.SIG_BLOCK, _TRANSACTION_SIGNALS,
            )
            try:
                ready_read, ready_write = _pipe_cloexec()
                fds.extend((ready_read, ready_write))
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, pipe_mask)

            pipe_mask = signal.pthread_sigmask(
                signal.SIG_BLOCK, _TRANSACTION_SIGNALS,
            )
            try:
                result_read, result_write = _pipe_cloexec()
                fds.extend((result_read, result_write))
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, pipe_mask)

            expected_parent_pid = os.getpid()
            fork_mask = signal.pthread_sigmask(
                signal.SIG_BLOCK, _TRANSACTION_SIGNALS,
            )
            try:
                pid = os.fork()
            except BaseException:
                signal.pthread_sigmask(signal.SIG_SETMASK, fork_mask)
                raise

            if pid == 0:  # pragma: no cover - observed from the parent
                _child_main(
                    ready_read, ready_write, result_read, result_write,
                    fn, args, dict(kwargs), expected_parent_pid, fork_mask,
                    deadline_at,
                )
                os._exit(72)
            signal.pthread_sigmask(signal.SIG_SETMASK, fork_mask)
        except (OSError, ValueError) as exc:
            raise DeadlineUnavailable(
                "cannot launch isolated dispatch child"
            ) from exc

        try:
            try:
                os.close(ready_write)
                os.close(result_write)
            except OSError as exc:
                raise DeadlineUnavailable(
                    "cannot close parent IPC descriptors"
                ) from exc
            ready = _read_exact(ready_read, 1, deadline_at)
            # ``1`` and ``2`` are written only after setsid, so either makes
            # process-group cleanup safe. ``0`` means setsid itself failed.
            group_ready = ready in (b"1", b"2")
            if ready != b"1":
                raise DeadlineUnavailable("isolated dispatch child setup failed")
            header = _read_exact(result_read, _FRAME.size, deadline_at)
            (payload_size,) = _FRAME.unpack(header)
            if payload_size <= 0 or payload_size > _MAX_IPC_BYTES:
                raise _ChildProtocol()
            payload = _read_exact(result_read, payload_size, deadline_at)
        except _ChildTimeout:
            raise DeadlineExceeded(budget) from None
        except _ChildProtocol:
            raise DispatchChildError("protocol") from None
    except _ParentControl:
        control_interrupted = True
    finally:
        deferral.begin_cleanup()
        cleanup_had_primary = sys.exception() is not None
        try:
            _finish_parent_cleanup(
                pid,
                group_ready,
                tuple(fds),
                suppress_control=cleanup_had_primary,
            )
        finally:
            restore_had_primary = sys.exception() is not None
            try:
                if subreaper_changed:
                    _restore_subreaper_without_interruption(
                        suppress_control=restore_had_primary,
                    )
                    subreaper_changed = False
            finally:
                deferral.restore_and_replay()

    if control_interrupted:
        raise DeadlineUnavailable("isolated dispatch was cancelled")
    if payload is None:  # defensive fixed-schema failure
        raise DispatchChildError("protocol")
    return _decode_envelope(payload)


__all__ = [
    "enabled",
    "deadline_seconds",
    "assert_supported_context",
    "run_with_deadline",
    "DeadlineExceeded",
    "DeadlineUnavailable",
    "DispatchChildError",
]
