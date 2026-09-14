# __SLOT_ISOLATION_BACKEND_2026_08_20__ 격리 실행 백엔드 단일화 — 1보.
"""Common isolation-execution contract shared by sandbox consumers.

Why this exists (2026-08-20 정찰): isolated-execution wiring had drifted into
four separate surfaces (``core/sandbox_runner.py``, ``si_lanes/verify_gate.py``,
``si_lanes/exec_arm.py``, ``verifier/independent_reproduction_verifier.py``) —
the same bwrap/docker plumbing rebuilt four times. This module is step one:
extract the **narrow shared contract** — run one command (argv or a shell
string) under {network blocked, host root read-only, an explicit list of
writable directories, a wall-clock timeout, an explicit env} and get back a
typed result — and migrate exactly one consumer (``si_lanes/exec_arm.py``) to
it. ``core/sandbox_runner.py`` itself, ``verify_gate.py`` and the reproduction
verifier are **not** touched or migrated in this step.

## Reuse, not copies

``_bwrap_executable`` (binary resolution: PATH lookup + symlink resolution +
``is_file`` check) and ``is_mask_runtime_sockets_enabled`` (the
``AGI_V8_SANDBOX_MASK_RUNTIME_SOCKETS`` gate — default ON, closes the
``/run/docker.sock`` exposure a read-only ``/`` bind would otherwise carry
into the jail) are imported straight from ``core.sandbox_runner`` — never
re-implemented. Same for the two docker-availability probes
(``_docker_executable``, ``_docker_backend_available``) and the image
digest-pin check (``_image_id_trusted`` — reads the SAME
``AGI_V8_SWARM_SANDBOX_IMAGE_ID`` pin ``sandbox_runner`` itself consumes, so
arming that one env var hardens every caller of this module too, not just
``sandbox_runner``'s own harness). A second definition of any of these is
exactly the trap this repo has already paid for once (``distilled_embed``
dim split, memory: a value written in two places is a value neither place
gets checked against).

``_build_bwrap_argv`` in ``core/sandbox_runner.py`` is deliberately **not**
imported or called here — it is a fixed contract for one specific caller (the
``docker/sandbox/entry.py`` harness: input/work/entry paths baked into the
mount layout) and that file's own comments already record the conclusion that
it cannot be reused directly for a different caller. What *is* reusable, and
is generalised into :func:`build_bwrap_argv` below, is the **pattern**: the
unshare flags, ``--die-with-parent``/``--new-session``, ``--ro-bind / /``, the
conditional ``--tmpfs /run`` runtime-socket mask, and rebinding specific
directories read-write at their own host path so nothing outside them is
writable.

## No gate of its own

This module holds no ``AGI_V8_*_ENABLED`` env gate. It is a pure library —
:func:`run` and :func:`build_bwrap_argv` do exactly what their arguments say,
every time they are called. The safety boundary is the **caller's** gate
(``si_lanes/exec_arm.py``'s own ``AGI_V8_GOAL_CAMPAIGN_EXEC_ARM_ENABLED``,
today; a future ``verify_gate``/reproduction-verifier consumer's own gate,
later). Giving this module a second, independent gate would let a caller be
"armed" while this module is quietly OFF (or vice versa) — the exact
predicate-promotion shape (memory: ``feedback_predicate_promotion_regression``)
this repo has already been burned by once. ``core.sandbox_runner``'s own
``is_bwrap_isolation_enabled()`` gate is for *that* module's docker-fallback
consumer specifically — it is deliberately never read here either.

## Fail-closed availability

:func:`probe_backend` tries bwrap, then docker, then reports ``"none"``.
:func:`run` never falls back to an unisolated host subprocess: when no
backend is available it returns a **typed refusal**
(``refused=True, refusal_reason="no_isolation_available"``) — the exact
token ``core.sandbox_runner`` already uses for the same condition (reused,
not reinvented, so callers/tests that already grep for this string keep
working).

## Docker backend scope (explicit, not silent)

A docker backend for *this* module's contract needs a container image with a
shell — there is no single correct default here (unlike ``sandbox_runner``'s
own docker path, which ships a purpose-built image for its harness). Rather
than guess a base image, :func:`run` requires an explicit ``image=`` argument
whenever the probed backend is docker; if none is given it returns a typed
refusal (``refusal_reason="docker_image_required"``) instead of silently
picking one. No current caller of this module exercises the docker branch
(``exec_arm``'s docker_image cards bypass this module entirely and go
straight to ``goal_grader.grade``, which owns its own docker gate) — this
branch exists so the contract in the module docstring is honoured end to end,
and so the next consumer that *does* need docker has somewhere to land.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from agi_v8_1.policy.fail_fast import swallowed as _swallowed

# 🔑 재사용, 사본 아님 — see module docstring "Reuse, not copies".
from agi_v8_1.core.sandbox_runner import (
    _bwrap_executable,
    _docker_backend_available,
    _docker_executable,
    # re-exported for callers (P1-C(b)) — not called inside this file
    # itself, same posture as the other reused names above.
    _image_id_trusted,  # noqa: F401
    is_mask_runtime_sockets_enabled,
)

SCHEMA_VERSION = "agi_v8_1_isolation_backend_v1"

BACKEND_BWRAP = "bwrap"
BACKEND_DOCKER = "docker"
BACKEND_NONE = "none"

#: Reused verbatim from ``core.sandbox_runner.STUB_REASONS`` — a new token
#: here would mean two names for the same fact, and only one of them would
#: ever get grepped for.
REFUSAL_NO_ISOLATION_AVAILABLE = "no_isolation_available"

__all__ = [
    "SCHEMA_VERSION",
    "BACKEND_BWRAP",
    "BACKEND_DOCKER",
    "BACKEND_NONE",
    "REFUSAL_NO_ISOLATION_AVAILABLE",
    "IsolationResult",
    "probe_backend",
    "build_bwrap_argv",
    "run",
    "clearenv_enabled",
    "rlimits_enabled",
    "docker_hardening_enabled",
    "build_docker_card_argv",
]


@dataclass(frozen=True)
class IsolationResult:
    """Typed result of :func:`run`. Frozen — treat as immutable.

    ``refused`` is True whenever no command was ever executed (no backend
    available, docker requested without an image, or the subprocess call
    itself could not even start). ``timed_out`` is True when the command DID
    start but was killed for exceeding ``timeout_sec`` — a distinct case from
    ``refused`` (a refusal never spawned anything; a timeout did).
    """

    schema_version: str = SCHEMA_VERSION
    backend: str = BACKEND_NONE
    refused: bool = False
    refusal_reason: str = ""
    rc: int = -1
    stdout: str = ""
    stderr: str = ""
    elapsed_sec: float = 0.0
    timed_out: bool = False


def probe_backend() -> str:
    """``"bwrap"`` if the bwrap binary resolves, else ``"docker"`` if the
    docker CLI resolves AND its backend answers, else ``"none"``.

    Presence-only for bwrap (matching ``core.sandbox_runner``'s own
    treatment: a resolved ``bwrap`` binary is trusted without a liveness
    probe, since bwrap has no separate daemon to be unreachable). Docker
    additionally checks backend reachability
    (``_docker_backend_available``) — a docker CLI can be installed with no
    daemon running, and reporting that as "available" would just move the
    fail-closed refusal one step later, to the actual ``docker run`` call.
    """

    if _bwrap_executable() is not None:
        return BACKEND_BWRAP
    docker_path = _docker_executable()
    if docker_path is not None and _docker_backend_available(docker_path):
        return BACKEND_DOCKER
    return BACKEND_NONE


#: __SLOT_JAIL_CLEARENV_2026_08_20__ ``--clearenv`` 뒤에 되돌려 넣는 최소 환경.
#: 셸/인터프리터가 동작하는 데 필요한 것만 — credential 은 정의상 여기 없다.
#: ``HOME`` 은 host 홈이 아니라 jail 안 ``/tmp`` 아래를 가리켜, ``~/.config``·
#: ``~/.env`` 같은 경로 조회가 host 사용자 파일로 새지 않게 한다.
_MINIMAL_JAIL_ENV: dict[str, str] = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "HOME": "/tmp",
    "TMPDIR": "/tmp",
    "PYTHONSAFEPATH": "1",
}

_CLEARENV_ENV = "AGI_V8_JAIL_CLEARENV_ENABLED"


def clearenv_enabled() -> bool:
    """default-OFF (strict ``true``/``1``) — jail 이 host 환경변수 상속을 끊는가.

    OFF 면 :func:`build_bwrap_argv` 가 ``--clearenv``/``--setenv`` 를 **한 개도**
    붙이지 않아 argv 가 이 게이트 신설 이전과 byte-identical 이다.
    """
    return os.environ.get(_CLEARENV_ENV, "") in ("true", "1")  # tier: T9


#: __SLOT_JAIL_RLIMITS_2026_08_20__ Sol Pro R15 P1-C(a). ``bwrap`` itself has
#: no rlimit flags — network/PID/mount-namespace isolation says nothing about
#: how much memory, how many processes, how much CPU time, or how large a
#: file the jailed command may consume before ``timeout_sec`` (a WALL-CLOCK
#: bound, ``core.isolation_backend.run``'s own or ``exec_arm``'s
#: ``tick_deadline``) ever fires — a fork bomb or a memory-bomb can degrade
#: the HOST (and every other jail sharing it) long before the wall-clock
#: kill lands. Enforced by wrapping the final command in ``prlimit`` (a
#: setrlimit(2) front-end, not a shell builtin — this matters: the default
#: ``/bin/sh`` this repo's hosts resolve to is ``dash``, whose ``ulimit``
#: builtin has NO ``-u``/nproc option at all — live-verified 2026-08-20,
#: see the smoke test this slot's tests reference. ``prlimit`` enforces the
#: same four limits regardless of which shell (or no shell, for a
#: ``Sequence[str]`` command) ends up running).
_RLIMITS_ENV = "AGI_V8_JAIL_RLIMITS_ENABLED"

#: Conservative defaults for a single graded card command (typically a
#: `pytest`/`python` invocation). ``as_bytes`` bounds virtual address space
#: (``prlimit --as``), ``nproc`` bounds live process/thread count for the
#: jailed UID (``--nproc`` — this is what actually stops a fork bomb),
#: ``cpu_sec`` bounds CPU-seconds consumed (``--cpu`` — distinct from wall
#: clock: a multi-process command can burn CPU-seconds faster than wall
#: time), ``fsize_bytes`` bounds the size of any single file the command
#: writes (``--fsize``). Deliberately generous relative to a normal graded
#: run — this is a circuit breaker against runaway/adversarial behaviour,
#: not a tight resource budget.
DEFAULT_RLIMITS: dict[str, int] = {
    "as_bytes": 2 * 1024 * 1024 * 1024,      # 2 GiB
    "nproc": 256,
    "cpu_sec": 600,
    "fsize_bytes": 512 * 1024 * 1024,        # 512 MiB
}


def rlimits_enabled() -> bool:
    """default-OFF (strict ``true``/``1``) — see :data:`_RLIMITS_ENV` slot
    comment. OFF (and ``rlimits=None`` passed to :func:`build_bwrap_argv`)
    means the ``prlimit`` prefix is never added — argv byte-identical to
    before this slot existed."""
    return os.environ.get(_RLIMITS_ENV, "") in ("true", "1")  # tier: T9


def _prlimit_executable() -> "str | None":
    """Resolve the ``prlimit`` binary — same defensive shape as
    ``sandbox_runner._bwrap_executable``/``_docker_executable`` (PATH lookup
    + symlink resolution + ``is_file`` check, never raises). Kept local
    rather than imported: those two resolve a DIFFERENT binary name each: a
    third near-identical resolver for a third binary name is the established
    pattern in this repo (``sandbox_runner`` already carries two), not a
    duplicated judgment call the "reuse, not copies" rule above is about."""
    found = shutil.which("prlimit")
    if found is None:
        return None
    try:
        path = Path(found).resolve(strict=True)
    except OSError as exc:
        _swallowed(exc, site="core.isolation_backend._prlimit_executable:resolve",
                   category="verify")
        return None
    if not path.is_file():
        return None
    return str(path)


def build_bwrap_argv(
    *,
    bwrap_path: str,
    command: "str | Sequence[str]",
    writable_dirs: Sequence["Path | str"] = (),
    readonly_binds: Sequence[
        "tuple[Path | str, Path | str]"
    ] = (),
    masked_dirs: Sequence["Path | str"] = (),
    empty_dirs: Sequence["Path | str"] = (),
    cwd: "Path | str | None" = None,
    mask_runtime_sockets: "bool | None" = None,
    clearenv: "bool | None" = None,
    extra_env: "Mapping[str, str] | None" = None,
    rlimits: "bool | Mapping[str, int] | None" = None,
    prlimit_path: "str | None" = None,
) -> list[str]:
    """Assemble a ``bwrap`` argv wrapping ``command`` in isolation.

    Generalises the pattern shared by ``core.sandbox_runner._build_bwrap_argv``
    (the ``entry.py``-harness-specific caller) and what was
    ``si_lanes.exec_arm._bwrap_wrap_command``'s inline argv (the
    arbitrary-shell-command caller) — see the module docstring for why
    neither of those functions is called directly instead.

    Contract, in argv order (order matters — a later bwrap mount shadows an
    earlier one at the same path, never the reverse):

      - ``--unshare-net``/``--unshare-pid``/``--unshare-ipc``/``--unshare-uts``/
        ``--unshare-cgroup-try``: no outbound network, no visibility into (or
        signaling of) host processes.
      - ``--die-with-parent``/``--new-session``: no orphaned survivors, no
        controlling terminal.
      - ``--ro-bind / /``: the host root is visible (so the interpreter,
        shell, and any other host binary the command needs resolve) but
        READ-ONLY.
      - ``--tmpfs /run`` (only when ``mask_runtime_sockets`` — defaulting to
        :func:`core.sandbox_runner.is_mask_runtime_sockets_enabled`, the same
        gate ``sandbox_runner`` itself uses): masks ``/run`` (where
        ``/run/docker.sock`` lives; ``/var/run`` is a symlink to it) so code
        inside the jail cannot reach the Docker control socket the
        ``--ro-bind / /`` above would otherwise carry straight in — see that
        function's docstring in ``sandbox_runner.py`` for the live-verified
        escape this closes. Placed directly after the root bind so it reads
        as "root comes in, then /run is immediately re-sealed".
      - ``--tmpfs <d>`` for each ``masked_dirs`` entry, in order: hides a
        sensitive host subtree before narrowly reviewed read-only binds
        restore only the required toolchain and candidate workspace.  This
        prevents the generic root bind from resurrecting a live repository's
        excluded cards or other files under the candidate's absolute paths.
      - ``--proc /proc``, ``--dev /dev``, ``--tmpfs /tmp``: a working process
        table, device nodes, and fresh scratch space independent of the
        host's ``/tmp``.
      - ``--dir <d>`` for each ``empty_dirs`` entry, in order: creates an
        EMPTY directory at that host path inside the fresh tmpfs — no host
        content is exposed there. Used by callers that need a sibling
        directory to exist (e.g. so a ``PYTHONPATH`` pointed at its parent
        resolves) without exposing everything else that lives alongside the
        directory they actually want writable.
      - ``--bind <d> <d>`` for each ``writable_dirs`` entry, in order:
        rebinds that host directory READ-WRITE at the *same* path — the
        only writable host locations in the whole jail, and the only
        directories whose real content (not an empty placeholder) is
        exposed.
      - ``--ro-bind <source> <destination>`` for each ``readonly_binds``
        entry, in order: expose an exact trusted tree read-only at a neutral
        in-jail path.  This is used by regression grading when the host
        workspace itself lives below ``/tmp``: the fresh ``--tmpfs /tmp``
        hides the host path, then this explicit bind brings back only the
        reviewed snapshot rather than the rest of host ``/tmp``.
      - ``--chdir <cwd>`` (only when ``cwd`` given).
      - ``<prlimit_path> --as=<> --nproc=<> --cpu=<> --fsize=<> --`` (only
        when ``rlimits`` is active — see ``rlimits``/``prlimit_path`` params
        below): a resource-limit prefix wrapping whatever comes next, so the
        limits apply regardless of what the command turns out to be.
      - the command itself: a ``str`` is wrapped as ``sh -c <command>``
        (matching how ``goal_grader.grade`` already invokes card commands via
        ``subprocess.run(..., shell=True)`` — the caller hands this argv's
        *string form* to that same shell contract); a ``Sequence[str]`` is
        appended verbatim as an argv (no shell parsing).

    Every path argument is stringified with ``str()`` — callers pass
    ``pathlib.Path`` or plain strings interchangeably.

    ``rlimits`` (Sol Pro R15 P1-C(a), :data:`_RLIMITS_ENV`): ``None``
    (default) defers to :func:`rlimits_enabled`; ``True``/a non-empty
    mapping forces the ``prlimit`` prefix on (a mapping overrides individual
    keys of :data:`DEFAULT_RLIMITS`, e.g. ``rlimits={"nproc": 32}``);
    ``False``/an empty mapping forces it off regardless of the env gate.
    When active, ``prlimit_path`` MUST already be resolved and passed by the
    caller (mirroring how ``bwrap_path`` itself is a caller-resolved,
    required argument, never re-discovered here) — a ``ValueError`` is
    raised immediately if it is missing, so a caller that forgets to check
    ``prlimit`` availability fails loudly at the call site rather than
    silently shipping an unbounded jail. When ``rlimits`` is OFF (the
    default), ``prlimit_path`` is never even inspected — OFF-parity argv is
    byte-identical to this slot never having existed.
    """

    argv: list[str] = [
        bwrap_path,
        "--unshare-net",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--unshare-cgroup-try",
        "--die-with-parent",
        "--new-session",
        "--ro-bind", "/", "/",
    ]
    # __SLOT_JAIL_CLEARENV_2026_08_20__ 🔴 Sol Pro R15 P0-B.
    #
    # 네트워크를 끊어도 **환경변수는 그대로 상속된다** — ``goal_grader.
    # grading_env()`` 가 ``{**os.environ, ...}`` 라서, jail 안에서 도는
    # (모델이 쓴) 코드가 provider API key 를 그냥 읽는다. 그 값이 stdout 으로
    # 나가면 grader 가 tail 을 수집하고, 그 tail 이 exec_arm 관측 → 재급식
    # objective → 다음 LLM 프롬프트/원장으로 흐른다. 즉 **네트워크와 무관한
    # 유출 채널**이 하나 열려 있었다(감사 사슬 B).
    #
    # ⇒ ``--clearenv`` 로 상속을 끊고, 명령이 실제로 필요로 하는 최소만
    # 다시 넣는다. ``extra_env`` 는 호출자가 의도적으로 통과시키는 값만
    # (예: ``PYTHONPATH``/``PYTHONSAFEPATH``) — 여기서도 os.environ 를
    # 통째로 읽지 않는다.
    #
    # default-OFF: 이 하드닝은 argv 를 바꾸므로 기존 소비자의 계약을 흔든다.
    # 게이트 OFF 면 아래 블록 전체가 안 돌아 **argv byte-identical**.
    if clearenv_enabled() if clearenv is None else clearenv:
        argv += ["--clearenv"]
        for key, value in _MINIMAL_JAIL_ENV.items():
            argv += ["--setenv", key, value]
        for key, value in (extra_env or {}).items():
            argv += ["--setenv", str(key), str(value)]
    mask = (
        is_mask_runtime_sockets_enabled()
        if mask_runtime_sockets is None
        else mask_runtime_sockets
    )
    if mask:
        argv += ["--tmpfs", "/run"]
    for d in masked_dirs:
        argv += ["--tmpfs", str(d)]
    argv += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
    for d in empty_dirs:
        argv += ["--dir", str(d)]
    for d in writable_dirs:
        d_str = str(d)
        argv += ["--bind", d_str, d_str]
    for source, destination in readonly_binds:
        argv += ["--ro-bind", str(source), str(destination)]
    if cwd is not None:
        argv += ["--chdir", str(cwd)]
    active_rlimits = (rlimits_enabled() if rlimits is None
                      else bool(rlimits) if not isinstance(rlimits, Mapping)
                      else bool(dict(rlimits)))
    if active_rlimits:
        if not prlimit_path:
            raise ValueError(
                "rlimits active but prlimit_path not resolved — caller must "
                "resolve+verify prlimit availability (via "
                "core.isolation_backend._prlimit_executable()) before "
                "requesting rlimits, same contract as bwrap_path")
        values = dict(DEFAULT_RLIMITS)
        if isinstance(rlimits, Mapping):
            values.update(rlimits)
        argv += [
            prlimit_path,
            f"--as={int(values['as_bytes'])}",
            f"--nproc={int(values['nproc'])}",
            f"--cpu={int(values['cpu_sec'])}",
            f"--fsize={int(values['fsize_bytes'])}",
            "--",
        ]
    if isinstance(command, str):
        argv += ["sh", "-c", command]
    else:
        argv += list(command)
    return argv


def _build_docker_argv(
    *,
    docker_path: str,
    image: str,
    command: "str | Sequence[str]",
    writable_dirs: Sequence[Path],
    cwd: "Path | None",
    env: Mapping[str, str],
) -> list[str]:
    """Docker equivalent of :func:`build_bwrap_argv`'s contract.

    ``--read-only`` makes the CONTAINER's own filesystem read-only (docker
    containers start from the image's rootfs, never the host's — there is no
    host-root-bind equivalent to reproduce here, unlike bwrap); each
    ``writable_dirs`` entry is bind-mounted read-write at the *same* path
    inside the container (mirroring the bwrap rebind-at-same-path pattern so
    a command written against one backend's paths works under the other).
    """

    argv: list[str] = [docker_path, "run", "--rm", "--network=none", "--read-only"]
    for d in writable_dirs:
        d_str = str(d)
        argv += ["-v", f"{d_str}:{d_str}:rw"]
    if cwd is not None:
        argv += ["-w", str(cwd)]
    for key, value in env.items():
        argv += ["-e", f"{key}={value}"]
    argv.append(image)
    if isinstance(command, str):
        argv += ["sh", "-c", command]
    else:
        argv += list(command)
    return argv


# __SLOT_JAIL_DOCKER_HARDENING_2026_08_20__ Sol Pro R15 P1-C(b). A goal card
# that declares ``success_criteria.spec.docker_image`` bypasses this
# module's bwrap path entirely — ``goal_grader.grade`` (READ-ONLY from this
# track's scope; see ``exec_arm.py``'s own docstring on why that module
# isn't touched) builds its OWN, much weaker ``docker run`` invocation: no
# ``--network=none``, no ``--read-only``, no memory/pids/user bound, no
# image identity check at all (a `docker tag` repoint is invisible to it).
#
# This module cannot edit ``goal_grader.py`` to harden that invocation
# directly. What it CAN do — and what :func:`build_docker_card_argv` is for
# — is hand the exec_arm caller (the only other file this track owns) an
# ALREADY-hardened, complete ``docker run ...`` command string that IT
# assigns as the card's ``spec["command"]`` while clearing
# ``spec["docker_image"]`` — so ``goal_grader.grade`` sees a plain shell
# command (no docker_image key left to re-wrap) and just executes the
# already-hardened invocation via its normal ``subprocess.run(shell=True)``
# path. See ``exec_arm.py``'s docstring for the availability/trust check
# that MUST run before this is used (fail-closed if docker is unavailable
# or the image identity pin doesn't match — never silently fall through to
# the unhardened ``goal_grader`` docker branch, and never silently run
# unpinned).
_DOCKER_HARDENING_ENV = "AGI_V8_JAIL_DOCKER_HARDENING_ENABLED"

#: Same rationale as :data:`DEFAULT_RLIMITS` — generous circuit-breaker
#: values, not a tight budget. ``DEFAULT_DOCKER_USER`` is ``nobody:nogroup``
#: (uid/gid 65534, present in effectively every Linux base image without
#: needing the image to declare it) — the card's model-authored command
#: never runs as container root.
DEFAULT_DOCKER_MEMORY_BYTES = 2 * 1024 * 1024 * 1024   # 2 GiB
DEFAULT_DOCKER_PIDS_LIMIT = 256
DEFAULT_DOCKER_USER = "65534:65534"


def docker_hardening_enabled() -> bool:
    """default-OFF (strict ``true``/``1``). OFF ⇒ ``exec_arm`` never calls
    :func:`build_docker_card_argv` — a docker_image card is graded exactly
    as before this slot existed (``goal_grader``'s own weaker docker
    branch)."""
    return os.environ.get(_DOCKER_HARDENING_ENV, "") in ("true", "1")  # tier: T9


def build_docker_card_argv(
    *,
    docker_path: str,
    image: str,
    command: str,
    workspace: "Path | str",
    env: "Mapping[str, str] | None" = None,
    memory_bytes: int = DEFAULT_DOCKER_MEMORY_BYTES,
    pids_limit: int = DEFAULT_DOCKER_PIDS_LIMIT,
    user: str = DEFAULT_DOCKER_USER,
) -> list[str]:
    """Hardened ``docker run`` argv for a goal-card's ``docker_image``
    branch — a FIXED contract for exactly one caller (mirrors
    ``core.sandbox_runner._build_bwrap_argv``'s own "fixed contract for one
    specific caller" posture, see this module's docstring). Deliberately
    NOT built on :func:`_build_docker_argv` above: that function's
    ``writable_dirs`` bind host paths at the SAME path inside the
    container, but a goal card's ``command`` is authored assuming
    ``goal_grader.grade``'s existing convention — workspace mounted at
    ``/ws``, cwd ``/ws`` — so this reproduces THAT mount shape exactly
    (grading semantics unchanged) while adding what that branch lacks:
    ``--network=none``, ``--read-only``, ``--memory``, ``--pids-limit``,
    and a non-root ``--user``.

    Image identity (``AGI_V8_SWARM_SANDBOX_IMAGE_ID`` digest pin) is
    deliberately NOT checked here — this function only assembles argv, same
    posture as :func:`build_bwrap_argv`. The caller MUST call
    ``core.sandbox_runner._image_id_trusted(image, docker_path)`` (reused
    verbatim below, re-exported from this module) and refuse fail-closed
    before ever reaching this function, exactly as it must resolve
    ``docker_path``/verify docker availability first.
    """
    ws = str(Path(workspace).resolve())
    argv: list[str] = [
        docker_path, "run", "--rm",
        "--network=none", "--read-only",
        f"--memory={int(memory_bytes)}",
        f"--pids-limit={int(pids_limit)}",
        "--user", str(user),
        "-v", f"{ws}:/ws:rw", "-w", "/ws",
    ]
    for key, value in (env or {}).items():
        argv += ["-e", f"{key}={value}"]
    argv.append(image)
    argv += ["sh", "-c", command]
    return argv


def run(
    *,
    command: "str | Sequence[str]",
    writable_dirs: Sequence["Path | str"] = (),
    empty_dirs: Sequence["Path | str"] = (),
    cwd: "Path | str | None" = None,
    timeout_sec: float = 30.0,
    env: "Mapping[str, str] | None" = None,
    image: "str | None" = None,
) -> IsolationResult:
    """Execute ``command`` under the best available isolation backend.

    Never raises and never falls back to an unisolated host subprocess —
    every failure path (no backend, docker requested without ``image``,
    timeout, or a subprocess launch error) returns a typed
    :class:`IsolationResult` instead.

    ``env``: ``None`` inherits the current process's environment (matching
    ``subprocess.run``'s own default); a mapping REPLACES it entirely for the
    launched process — matching how the bwrap backend execs its child
    directly with exactly the environment it is given (no implicit merge).
    """

    backend = probe_backend()
    if backend == BACKEND_NONE:
        return IsolationResult(
            backend=BACKEND_NONE, refused=True,
            refusal_reason=REFUSAL_NO_ISOLATION_AVAILABLE,
        )
    if backend == BACKEND_DOCKER and not image:
        return IsolationResult(
            backend=BACKEND_DOCKER, refused=True,
            refusal_reason="docker_image_required",
        )

    writable = [Path(d) for d in writable_dirs]
    empties = [Path(d) for d in empty_dirs]
    cwd_path = Path(cwd) if cwd is not None else None

    if backend == BACKEND_BWRAP:
        bwrap_path = _bwrap_executable()
        if bwrap_path is None:
            # Race: available at probe time, gone by now (binary removed
            # mid-call). Fail-closed, same as the initial probe miss.
            return IsolationResult(
                backend=BACKEND_BWRAP, refused=True,
                refusal_reason=REFUSAL_NO_ISOLATION_AVAILABLE,
            )
        argv = build_bwrap_argv(
            bwrap_path=bwrap_path, command=command,
            writable_dirs=writable, empty_dirs=empties, cwd=cwd_path,
        )
        run_env = dict(env) if env is not None else None
    else:
        docker_path = _docker_executable()
        if docker_path is None:
            return IsolationResult(
                backend=BACKEND_DOCKER, refused=True,
                refusal_reason=REFUSAL_NO_ISOLATION_AVAILABLE,
            )
        argv = _build_docker_argv(
            docker_path=docker_path, image=str(image), command=command,
            writable_dirs=writable, cwd=cwd_path, env=env or {},
        )
        # The docker CLI client process itself always inherits the current
        # environment (distinct from the CONTAINER's env, set via -e above)
        # — matching build_bwrap_argv's own env=None-inherits contract.
        run_env = None

    t0 = time.time()
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True,
            timeout=timeout_sec, env=run_env,
        )
    except subprocess.TimeoutExpired as exc:
        _swallowed(exc, site="core.isolation_backend.run:timeout", category="verify")
        return IsolationResult(
            backend=backend, timed_out=True,
            elapsed_sec=round(time.time() - t0, 3),
            stderr=str(exc)[:2000],
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        _swallowed(exc, site="core.isolation_backend.run:exec", category="verify")
        return IsolationResult(
            backend=backend, refused=True,
            refusal_reason=f"exec_error:{type(exc).__name__}",
            elapsed_sec=round(time.time() - t0, 3),
        )
    return IsolationResult(
        backend=backend, rc=proc.returncode,
        stdout=proc.stdout or "", stderr=proc.stderr or "",
        elapsed_sec=round(time.time() - t0, 3),
    )
