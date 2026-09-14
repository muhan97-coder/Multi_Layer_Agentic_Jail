# __SLOT_TICK_DESCENDANT_SEAL_2026_08_18__ 자손 late-mutation 봉쇄 v1.
"""Kernel-enforced containment for dispatch descendants that outlive an
uncatchable parent crash.

``tick_deadline.run_with_deadline`` already closes the single-hop case: the
isolated leader arms ``PR_SET_PDEATHSIG(SIGKILL)`` against the calling
process, so a hard parent crash reliably kills the leaf. That module's own
docstring names the one residual it cannot close: **any process the
dispatched function itself further spawns** (``fn`` doing its own
``subprocess``/``fork``) has no ``PDEATHSIG`` of its own, and if the calling
parent is killed with an uncatchable ``SIGKILL`` before its ``finally`` block
can sweep adopted orphans, that descendant is reparented past the (now dead)
subreaper to PID 1 -- systemd in the live topology -- and keeps running. It
holds neither the Python tick lease nor the outer cron ``flock``, so nothing
stops it writing to ``state/`` after the transaction it belonged to is
already gone, up to 10 minutes before the next tick notices anything.

PDEATHSIG cannot be made to cascade on its own -- every additional fork level
has to re-arm it itself, and ``fn`` is arbitrary dispatched work outside this
module's control. Nor can anything run "after" the crash to sweep the mess:
a process killed with ``SIGKILL`` executes no ``finally``, anywhere, ever.

So the fix here is not "run more cleanup" -- it is "make death cascade
without any cleanup code needing to run at all". An unprivileged Linux PID
namespace provides exactly that, as a kernel invariant rather than a policy
this module implements:

  * ``unshare(CLONE_NEWUSER | CLONE_NEWPID)`` -- unprivileged: the fresh user
    namespace grants the capabilities the nested PID namespace needs, no
    root and no setuid helper required -- makes the *next* forked child the
    init process (PID 1) of a brand-new PID namespace.
  * The kernel guarantees, unconditionally, that when a PID namespace's init
    process dies, every other process still inside that namespace is
    immediately sent ``SIGKILL`` and the namespace stops accepting new
    processes. No userspace code has to run anywhere for this to happen.
  * This module chains two ``PDEATHSIG`` hops through that boundary:
    ``leader`` (forked by the caller) arms ``PDEATHSIG`` targeting the
    caller, then unshares and forks ``ns_init``, which arms its *own*
    ``PDEATHSIG`` targeting ``leader``. If the caller is ``SIGKILL``ed:
    ``leader`` dies (its existing PDEATHSIG) -> ``ns_init`` dies (its own
    PDEATHSIG, since ``leader`` is its real parent) -> the kernel force-kills
    every process ``fn`` spawned inside the namespace, at any depth, in the
    same step that kills ``ns_init``. No subreaper, no polling loop, no
    ``finally`` block has to execute anywhere in the chain -- this is why the
    guarantee survives even a caller crash with zero warning.

This module supplies only that containment boundary, not a transport -- it
is a smaller, decoupled primitive rather than a drop-in replacement for
``tick_deadline``'s IPC contract (bounded JSON frame in, bounded JSON frame
out, closely modeled on it for familiarity).

``runtime.tick_deadline`` is the live caller (2026-08-19): its isolated child
calls :func:`run_sealed` instead of ``fn(*args, **kwargs)`` whenever this
module's gate is on, so the dispatched function itself runs sealed rather than
merely under ``PR_SET_PDEATHSIG``. That caller keeps its own wall-clock
deadline authoritative and passes its *remaining* budget plus slack as this
module's ``timeout``, so the seal's timeout is a backstop and never the
deciding one on the normal path.

Fails closed: :func:`supported` probes the real primitives (an actual
``fork`` + ``unshare``, reaped immediately) rather than trusting a static
capability guess, and :func:`run_sealed` refuses to run unsealed when the
probe is negative or inconclusive.

**default-OFF** (``AGI_V8_TICK_DESCENDANT_SEAL_ENABLED``). With the gate off,
``tick_deadline``'s isolated child takes its original direct
``fn(*args, **kwargs)`` call and nothing in this module runs; the gate is
independent of ``AGI_V8_TICK_DEADLINE_ENABLED`` and has no effect while that
outer gate is off, since the sealed call site only exists inside the isolated
child.
"""
from __future__ import annotations

import ctypes
import json
import os
import select
import signal
import struct
import sys
import threading
import time
from typing import Any, Callable

from agi_v8_1.policy.fail_fast import (
    record_critical_failure,
    safe_exception_type_name,
)

_ENV_ENABLED = "AGI_V8_TICK_DESCENDANT_SEAL_ENABLED"  # master gate (default OFF)
_DEFAULT_TIMEOUT_SEC = 10.0

_CLONE_NEWUSER = 0x10000000
_CLONE_NEWPID = 0x20000000
_PR_SET_PDEATHSIG = 1

_IPC_VERSION = 1
_FRAME = struct.Struct("!I")
_MAX_IPC_BYTES = 64 * 1024
_TERM_GRACE_SEC = 0.10
_KILL_REAP_SEC = 1.0

# Child-local normal-path cleanup registry.  The namespace init deliberately
# parks after sending its result and is then terminated by the leader, so
# ordinary ``atexit`` handlers never run there.  Resources created by the
# sealed function can opt into this registry and are released *before* the
# result becomes visible to the parent.  This is not a hard-kill guarantee;
# resources that must survive SIGKILL need an outer-owner cleanup mechanism.
_child_cleanup_callbacks: "list[Callable[[], None]] | None" = None


def _register_child_cleanup(callback: "Callable[[], None]") -> bool:
    """Register a normal-path cleanup only while executing as sealed init.

    Returning ``False`` outside that process lets callers retain their normal
    owner/``atexit`` lifecycle without accidentally transferring ownership.
    """

    if _child_cleanup_callbacks is None:
        return False
    _child_cleanup_callbacks.append(callback)
    return True


def _child_cleanup_scope_active() -> bool:
    """Whether this process currently owns sealed normal-path resources."""

    return _child_cleanup_callbacks is not None


def _run_child_cleanups() -> None:
    global _child_cleanup_callbacks

    callbacks = _child_cleanup_callbacks
    _child_cleanup_callbacks = None
    if callbacks is None:
        return
    for callback in reversed(callbacks):
        try:
            callback()
        except BaseException as exc:
            # Cleanup must never replace the fixed-schema function result,
            # but it must remain visible in the central failure census.
            record_critical_failure(
                exc,
                site="runtime.tick_descendant_seal._run_child_cleanups",
                category="persist",
            )


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def enabled() -> bool:
    """Default-OFF gate. No live caller is wired to this yet (see module docstring)."""
    return _truthy(_ENV_ENABLED)  # tier: T2


class SealUnavailable(RuntimeError):
    """The kernel primitives this containment needs cannot be provided here."""


class SealChildError(RuntimeError):
    """The sealed function raised, or the IPC frame it sent was malformed."""

    def __init__(self, exception_type: str) -> None:
        super().__init__(f"sealed dispatch child raised {exception_type}")
        self.exception_type = exception_type


class SealTimeout(RuntimeError):
    """The sealed function did not report completion inside the deadline."""

    def __init__(self, budget_seconds: float) -> None:
        super().__init__(f"sealed dispatch exceeded {budget_seconds:.1f}s wall-clock budget")
        self.budget_seconds = budget_seconds


class _TimeoutSignal(BaseException):
    pass


class _ProtocolSignal(BaseException):
    pass


def _unsupported_reason() -> "str | None":
    if os.name != "posix" or not sys.platform.startswith("linux"):
        return "PID-namespace containment requires Linux"
    if not hasattr(os, "fork") or not hasattr(os, "waitpid"):
        return "POSIX fork/wait primitives are unavailable"
    if threading.current_thread() is not threading.main_thread():
        return "seal requires the Python main thread"
    if any(
        thread is not threading.current_thread() and thread.is_alive()
        for thread in threading.enumerate()
    ):
        return "seal requires a single-threaded caller process"
    return None


def _libc() -> Any:
    try:
        return ctypes.CDLL(None, use_errno=True)
    except OSError as exc:
        raise SealUnavailable("cannot load namespace control") from exc


def _unshare(flags: int) -> None:
    result = _libc().unshare(ctypes.c_int(flags))
    if result != 0:
        errno_val = ctypes.get_errno()
        raise OSError(errno_val, os.strerror(errno_val), "unshare")


def _arm_pdeathsig_raw() -> bool:
    result = _libc().prctl(
        ctypes.c_int(_PR_SET_PDEATHSIG), ctypes.c_ulong(signal.SIGKILL),
        ctypes.c_ulong(0), ctypes.c_ulong(0), ctypes.c_ulong(0),
    )
    return result == 0


def _arm_pdeathsig(expected_parent_pid: int) -> bool:
    """Arm SIGKILL-on-parent-death and close the parent-died-before-prctl race.

    Only valid for a caller that shares a PID namespace with
    ``expected_parent_pid`` -- see :func:`_arm_pdeathsig_in_namespace_root`
    for why the freshly-unshared ``ns_init`` hop cannot use this check.
    """
    return _arm_pdeathsig_raw() and os.getppid() == expected_parent_pid


def _arm_pdeathsig_in_namespace_root() -> bool:
    """Arm SIGKILL-on-parent-death for the PID-namespace init process.

    ``ns_init`` is PID 1 of a namespace it did not exist in until this
    ``fork``, and PID-namespace semantics mean ``getppid()`` called from
    inside a fresh namespace's init process always reports ``0`` -- the real
    parent (``leader``) lives in an ancestor namespace and is not visible
    from here by numeric PID at all, so the ``getppid() == expected`` race
    check :func:`_arm_pdeathsig` uses is not just unavailable, it is
    unsatisfiable by any correct value. This hop therefore trusts the
    ``prctl`` return code alone, leaving only the ordinary fork-to-prctl
    instruction window as a residual race -- the same window every
    ``PDEATHSIG`` arm has before its own confirmation can run, including the
    ``expected_parent_pid`` check above.
    """
    return _arm_pdeathsig_raw()


def supported() -> bool:
    """Best-effort probe: can this host actually provide the seal?

    Runs a real ``fork`` + ``unshare(CLONE_NEWUSER | CLONE_NEWPID)`` and reaps
    it immediately, rather than trusting a static capability guess. Fails
    closed (``False``) on any doubt -- an inconclusive probe is never treated
    as permission to run unsealed.
    """
    if _unsupported_reason() is not None:
        return False
    try:
        pid = os.fork()
    except OSError:
        return False
    if pid == 0:
        try:
            _unshare(_CLONE_NEWUSER | _CLONE_NEWPID)
            os._exit(0)
        except BaseException:
            os._exit(1)
        return False  # pragma: no cover - unreachable, os._exit above
    try:
        _, status = os.waitpid(pid, 0)
    except ChildProcessError:
        return False
    return os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0


def _reset_child_termination_signals() -> None:
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, signal.SIG_DFL)


def _pipe_cloexec() -> "tuple[int, int]":
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


def _encode_envelope(envelope: "dict[str, Any]") -> bytes:
    try:
        payload = json.dumps(
            envelope, ensure_ascii=False, allow_nan=False, separators=(",", ":"),
        ).encode("utf-8")
    except BaseException:
        payload = json.dumps(
            {"v": _IPC_VERSION, "kind": "error", "exception_type": "Exception"},
            separators=(",", ":"),
        ).encode("ascii")
    if len(payload) > _MAX_IPC_BYTES:
        payload = json.dumps(
            {"v": _IPC_VERSION, "kind": "error", "exception_type": "ResultTooLarge"},
            separators=(",", ":"),
        ).encode("ascii")
    return payload


def _write_result(result_write: int, envelope: "dict[str, Any]") -> None:
    payload = _encode_envelope(envelope)
    try:
        _write_all(result_write, _FRAME.pack(len(payload)) + payload)
    except OSError:
        pass


def _run_fn_envelope(
    fn: "Callable[..., Any]", args: tuple, kwargs: dict,
) -> "dict[str, Any]":
    try:
        value = fn(*args, **kwargs)
    except BaseException as exc:  # child boundary: only a fixed schema crosses
        return {"v": _IPC_VERSION, "kind": "error", "exception_type": safe_exception_type_name(exc)}
    return {"v": _IPC_VERSION, "kind": "ok", "value": value}


def _ns_init_main(
    result_write: int, fn: "Callable[..., Any]", args: tuple, kwargs: dict,
) -> None:
    """Runs as PID 1 of the fresh PID namespace. Its own death -- by any
    means, including the cascaded ``PDEATHSIG`` below -- forces the kernel to
    ``SIGKILL`` every other process left in the namespace, including
    anything ``fn`` spawned, in the same step, with no code of ours running.
    """
    global _child_cleanup_callbacks

    try:
        _reset_child_termination_signals()
        if not _arm_pdeathsig_in_namespace_root():
            os._exit(70)
        # Drop any registry inherited across ``fork``.  Only resources newly
        # created by this namespace init belong to its normal-path cleanup.
        _child_cleanup_callbacks = []
        try:
            envelope = _run_fn_envelope(fn, args, kwargs)
        finally:
            _run_child_cleanups()
        _write_result(result_write, envelope)
        try:
            os.close(result_write)
        except OSError:
            pass
        # Park as namespace init until the leader tears the namespace down
        # (normal path) or dies and cascades this process's own death (crash
        # path) -- either way nothing here needs to run for containment.
        _reset_child_termination_signals()
        while True:
            signal.pause()
    except BaseException:
        os._exit(71)


def _leader_main(
    result_write: int, fn: "Callable[..., Any]", args: tuple, kwargs: dict,
    expected_parent_pid: int,
) -> None:
    try:
        _reset_child_termination_signals()
        if not _arm_pdeathsig(expected_parent_pid):
            _write_result(result_write, {
                "v": _IPC_VERSION, "kind": "error", "exception_type": "SealSetupFailed",
            })
            os._exit(70)
        try:
            _unshare(_CLONE_NEWUSER | _CLONE_NEWPID)
        except OSError:
            _write_result(result_write, {
                "v": _IPC_VERSION, "kind": "error", "exception_type": "SealUnavailable",
            })
            os._exit(70)
        try:
            ns_pid = os.fork()
        except OSError:
            _write_result(result_write, {
                "v": _IPC_VERSION, "kind": "error", "exception_type": "SealUnavailable",
            })
            os._exit(70)
        if ns_pid == 0:
            _ns_init_main(result_write, fn, args, kwargs)
            os._exit(72)
        # Ownership of result_write passes to ns_init; the leader never
        # writes to it.
        try:
            os.close(result_write)
        except OSError:
            pass
        try:
            os.waitpid(ns_pid, 0)
        except ChildProcessError:
            pass
        os._exit(0)
    except BaseException:
        os._exit(71)


def _read_exact(fd: int, size: int, deadline_at: float) -> bytes:
    out = bytearray()
    while len(out) < size:
        remaining = deadline_at - time.monotonic()
        if remaining <= 0.0:
            raise _TimeoutSignal()
        try:
            readable, _, _ = select.select([fd], [], [], remaining)
        except InterruptedError:
            continue
        if not readable:
            raise _TimeoutSignal()
        try:
            chunk = os.read(fd, size - len(out))
        except InterruptedError:
            continue
        if not chunk:
            raise _ProtocolSignal()
        out.extend(chunk)
    return bytes(out)


def _kill_quiet(pid: int, signum: int) -> None:
    try:
        os.kill(pid, signum)
    except ProcessLookupError:
        pass


def _reap_once(pid: int) -> bool:
    try:
        waited, _ = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return True
    return waited == pid


def _terminate_and_reap_leader(pid: int) -> None:
    """Kill the leader and reap it. Everything past the namespace boundary
    (``ns_init`` and anything ``fn`` spawned) is torn down by the kernel the
    instant ``ns_init``'s own ``PDEATHSIG`` fires -- no adopted-child
    bookkeeping is needed on this side at all.
    """
    _kill_quiet(pid, signal.SIGTERM)
    term_end = time.monotonic() + _TERM_GRACE_SEC
    while time.monotonic() < term_end:
        if _reap_once(pid):
            return
        time.sleep(0.005)
    _kill_quiet(pid, signal.SIGKILL)
    kill_end = time.monotonic() + _KILL_REAP_SEC
    while time.monotonic() < kill_end:
        if _reap_once(pid):
            return
        time.sleep(0.005)
    raise SealUnavailable("sealed dispatch leader could not be reaped")


def _decode_envelope(payload: bytes) -> Any:
    try:
        envelope = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SealChildError("Protocol") from exc
    if not isinstance(envelope, dict) or envelope.get("v") != _IPC_VERSION:
        raise SealChildError("Protocol")
    kind = envelope.get("kind")
    if kind == "ok" and "value" in envelope:
        return envelope["value"]
    if kind == "error":
        exception_type = envelope.get("exception_type")
        if type(exception_type) is not str or len(exception_type) > 128:
            exception_type = "Exception"
        raise SealChildError(exception_type)
    raise SealChildError("Protocol")


def run_sealed(
    fn: "Callable[..., Any]", *args: Any,
    timeout: "float | None" = None, **kwargs: Any,
) -> Any:
    """Run ``fn`` as the init process of a fresh PID namespace and return its
    JSON-safe value.

    Any process ``fn`` spawns (at any depth) is guaranteed a kernel-enforced
    death within one PID-namespace teardown the instant the namespace's init
    process dies -- including when the OS process that called
    :func:`run_sealed` is itself killed with an uncatchable ``SIGKILL``
    before any of its own cleanup code can run. See the module docstring for
    the mechanism.
    """
    budget = float(timeout) if timeout is not None else _DEFAULT_TIMEOUT_SEC
    if not (budget == budget and budget > 0.0 and budget != float("inf")):
        raise SealUnavailable("timeout must be a finite positive number")
    if not supported():
        raise SealUnavailable("PID-namespace containment is unavailable here")

    result_read, result_write = _pipe_cloexec()
    expected_parent_pid = os.getpid()
    deadline_at = time.monotonic() + budget

    leader_pid = os.fork()
    if leader_pid == 0:  # pragma: no cover - observed from the parent
        try:
            os.close(result_read)
        except OSError:
            pass
        _leader_main(result_write, fn, args, dict(kwargs), expected_parent_pid)
        os._exit(72)

    try:
        os.close(result_write)
    except OSError as exc:
        raise SealUnavailable("cannot close parent IPC descriptor") from exc

    payload: "bytes | None" = None
    try:
        try:
            header = _read_exact(result_read, _FRAME.size, deadline_at)
            (payload_size,) = _FRAME.unpack(header)
            if payload_size <= 0 or payload_size > _MAX_IPC_BYTES:
                raise _ProtocolSignal()
            payload = _read_exact(result_read, payload_size, deadline_at)
        except _TimeoutSignal:
            raise SealTimeout(budget) from None
        except _ProtocolSignal:
            raise SealChildError("Protocol") from None
    finally:
        try:
            os.close(result_read)
        except OSError:
            pass
        _terminate_and_reap_leader(leader_pid)

    if payload is None:  # defensive fixed-schema failure
        raise SealChildError("Protocol")
    return _decode_envelope(payload)


__all__ = [
    "enabled",
    "supported",
    "run_sealed",
    "SealUnavailable",
    "SealChildError",
    "SealTimeout",
]
