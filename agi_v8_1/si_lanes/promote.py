"""# __SLOT_SI_PROMOTE_F1_INC6_2026_07_02__ verified-patch promotion to the live tree (F1 inc6).

Increment 6 — the LAST mile of the self-modification loop, and the ONLY place
that deliberately breaks the jail: it writes a verified patch to the REAL source
tree. Everything upstream (inc3/inc4 propose, inc5 verify) is jail-contained;
promotion is the single, guarded, human-gated step that lets a patch actually
touch live code.

This is NOT a per-cycle lane. It is NEVER wired into ``run_one_si_cycle`` — a
loop that rewrote live code every tick would be reckless. Promotion is a
DELIBERATE operator action (the ``main()`` CLI, or a direct ``promote_patch``
call), taken on a specific patch the operator chose to review.

FIVE fail-closed preconditions, ALL required before a single byte is written:
  1. Gate ``AGI_V8_SI_PROMOTE_ENABLED`` (default OFF).
  2. Explicit ``approve=True`` (CLI ``--approve-promote``) — a per-call decision,
     not something an env can arm-and-forget.
  3. A CLEAN git working tree (no uncommitted ``.py`` drift) — you cannot promote
     on top of unreviewed changes, and git is the revert path.
  4. A FRESH inc5 verification plus an explicit out-of-domain completion
     authority.  Candidate-controlled pytest alone never satisfies this bit;
     a stale or in-process-only verdict is never trusted.
  5. SafeAutoApply's own 2-env write gate (``AGI_V8_SAFE_AUTO_APPLY_ENABLED`` +
     ``_WRITE_ENABLED``). Without it the write is a dry-run.

Reversibility / human-in-the-loop:
  - The write goes through ``SafeAutoApply(repo_root=source_root)`` — backed up
    (``.bak``), audited (apply chain), py-syntax-checked, and protected-path
    guarded (so the SI control core cannot be promoted through this path).
  - It is left UNCOMMITTED. The operator reviews ``git diff``, then commits or
    ``git checkout -- <file>`` to revert. The irreversible step (commit/push)
    stays human. This module NEVER commits.

Observability-first: every precondition outcome logs; a promoted write logs a
clear review/revert message. ``promote_patch`` never raises.
"""
from __future__ import annotations

from agi_v8_1.capabilities import PayloadPort, PayloadUnavailable, resolve_payload

import argparse
import hashlib
import json
import logging
import os
import secrets
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

# __SLOT_FAIL_FAST_2026_07_25__ Swallowed failures route through one choke
# point: counted + named always, re-raised under AGI_V8_STRICT_FAIL_FAST.
from agi_v8_1.policy.fail_fast import (
    format_exception_for_critical_record,
    record_critical_failure,
    safe_exception_type_name,
    swallowed as _swallowed,
)

logger = logging.getLogger(__name__)

_PROMOTE_ENV = "AGI_V8_SI_PROMOTE_ENABLED"
_PROMOTE_MAX_PY_BYTES = 64 * 1024 * 1024

# __SLOT_SI_PROMOTE_CAS_2026_08_17__ (R5 §7-a) Round-5 track E security fix.
# Gate is default ON — this closes a real dirty-scan→verify→write TOCTOU, not
# a new feature. Set "false"/"0" to fall back to the pre-fix (byte-identical)
# behaviour: no preimage/HEAD re-check before the SafeAutoApply write.
_PROMOTE_CAS_ENV = "AGI_V8_SI_PROMOTE_CAS_ENABLED"

# __SLOT_SI_PROMOTE_APPROVAL_BIND_2026_08_17__ (R5 §7-b) Also default ON, also
# a security fix. Only takes effect when a caller passes ``approval_nonce`` —
# see ``promote_patches`` docstring for the (documented, honest) scope limit.
_PROMOTE_APPROVAL_BIND_ENV = "AGI_V8_SI_PROMOTE_APPROVAL_BIND_ENABLED"

# __SLOT_SI_PROMOTE_NONCE_STATE_DIR_REQUIRED_2026_08_17__ (R5 §7-b blocker
# fix) There used to be an in-process ``set()`` fallback here for when no
# ``state_dir`` was supplied. That fallback reset on every process start, so
# two independent CLI invocations (the normal way an operator uses this
# module — see ``main()``) each saw a fresh, empty set and BOTH nonce checks
# passed even when given the SAME nonce (ADV-R5-E2, confirmed with two real
# OS processes). A process-local set can never provide a cross-process
# single-use guarantee, so it has been removed entirely — see
# ``_check_and_bind_nonce``, which now refuses outright when ``state_dir`` is
# absent rather than silently downgrading the guarantee.


def _promote_enabled() -> bool:
    """Default-OFF promotion gate (strict ``"true"``/``"1"``)."""
    return os.environ.get(_PROMOTE_ENV, "") in ("true", "1")


def _cas_enabled() -> bool:
    """Preimage/HEAD compare-and-swap gate — default ON (security fix)."""
    return os.environ.get(_PROMOTE_CAS_ENV, "true") not in ("false", "0")


def _approval_bind_enabled() -> bool:
    """Approval-nonce binding gate — default ON (security fix)."""
    return os.environ.get(_PROMOTE_APPROVAL_BIND_ENV, "true") not in ("false", "0")


def _verify_export(name):
    """The public approval reader does not implicitly import the T5 verifier."""
    from agi_v8_1.capabilities import PayloadPort, resolve_payload
    return resolve_payload(PayloadPort(5, "agi_v8_1.si_lanes.verify_gate", name))


def _head_commit(git_root: Path) -> "str | None":
    """Current ``HEAD`` sha, or ``None`` on any failure (fail-closed caller)."""
    try:
        _snapshot_env, _snapshot_git = _verify_export("_snapshot_env"), _verify_export("_snapshot_git")

        r = subprocess.run(
            _snapshot_git(git_root, "rev-parse", "HEAD"),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            env=_snapshot_env(git_root),
            check=False,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        record_critical_failure(
            exc, site="si_lanes.promote._head_commit", category="apply"
        )
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def _preimage_state(path: Path) -> "tuple[str, str]":
    """Stable fingerprint of ``path``'s current on-disk bytes for CAS compare.

    ``("absent", "")`` / ``("file", sha256hex)`` / ``("other", "")`` — the
    third bucket (symlink/dir/special) is intentionally coarse: SafeAutoApply
    already refuses to write through a symlinked path on its own, so CAS only
    needs to notice a *transition into or out of* that state, not fingerprint
    its content.
    """
    try:
        st = path.lstat()
    except OSError:
        return ("absent", "")
    if stat.S_ISREG(st.st_mode):
        try:
            data = path.read_bytes()
        except OSError:
            return ("other", "")
        return ("file", hashlib.sha256(data).hexdigest())
    return ("other", "")


def _patch_digest(patches: "Sequence[tuple[str, str]]") -> str:
    """Order-independent digest binding a batch's targets AND content."""
    h = hashlib.sha256()
    for target, content in sorted(patches, key=lambda p: p[0]):
        h.update(target.encode("utf-8"))
        h.update(b"\0")
        h.update(hashlib.sha256(content.encode("utf-8")).hexdigest().encode("ascii"))
        h.update(b"\n")
    return h.hexdigest()


def _check_and_bind_nonce(
    state_dir: "Path | None", nonce: str, patch_digest: str
) -> "str | None":
    """Bind ``nonce`` to ``patch_digest`` exactly once. Never raises.

    Returns a refusal reason string when the nonce was already used or the
    guarantee cannot be made, or ``None`` when it was fresh and is now
    recorded. Persists under ``state_dir`` (the jail) via ``O_CREAT|O_EXCL``
    — that is the only mechanism that can catch reuse across SEPARATE
    process invocations, which is the normal way an operator uses the
    ``main()`` CLI (run it, review, run it again). ``state_dir`` is
    REQUIRED for that reason: without a persistent store there is no way to
    honor "single use" once the calling process exits, so this refuses
    (``nonce_requires_state_dir``) rather than silently downgrading to an
    in-process check that only catches reuse WITHIN one call (an in-process
    set previously stood in here and was removed after ADV-R5-E2 showed two
    real OS processes both promoting successfully with the same nonce).
    """
    if not state_dir:
        return "nonce_requires_state_dir"
    key = hashlib.sha256(nonce.encode("utf-8")).hexdigest()
    try:
        ledger_dir = Path(state_dir) / "si_promote_nonces"
        ledger_dir.mkdir(parents=True, exist_ok=True)
        target = ledger_dir / f"{key}.json"
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(target, flags, 0o600)
        try:
            os.write(
                fd,
                json.dumps({"patch_digest": patch_digest}).encode("utf-8"),
            )
        finally:
            os.close(fd)
        # __SLOT_PROVENANCE_MIRROR_V0_2026_08_18__ off-jail mirror of the
        # promotion-nonce bind (promotion provenance) — same content already
        # written into the jail ledger above (key=sha256(nonce), never the
        # raw nonce itself), just also appended to a hash-chained trail
        # outside the jail. Gated by AGI_V8_PROVENANCE_MIRROR_ENABLED
        # (default OFF); mirror_event is fully non-fatal, so OFF is a
        # zero-I/O no-op and this call is byte-identical to not existing.
        # ⚠️ `record_critical_failure`, not `_swallowed`: this block sits
        # INSIDE the same outer `try` as the `except OSError` below, whose
        # job is to classify a PRIMARY nonce-bind failure. The nonce bind
        # above this point already durably succeeded — `_swallowed` would
        # rethrow under AGI_V8_STRICT_FAIL_FAST and, for an OSError, fall
        # straight into that outer handler, misreporting a purely
        # secondary/best-effort mirror failure as `"nonce_ledger_error"`
        # even though the nonce WAS freshly bound (this function's own
        # docstring contract: "Never raises... returns None when it was
        # fresh and is now recorded").
        try:
            from agi_v8_1.enforcement.provenance_mirror import mirror_event

            mirror_event(
                "promote_nonce", "nonce_bind",
                {"nonce_key_sha256": key, "patch_digest": patch_digest},
            )
        except Exception as exc:  # noqa: BLE001 — mirroring must never affect the bind result
            record_critical_failure(exc, site="si_lanes.promote._check_and_bind_nonce:mirror",
                                     category="persist")
        return None
    except FileExistsError:
        return "nonce_reused"
    except OSError as exc:
        record_critical_failure(
            exc, site="si_lanes.promote._check_and_bind_nonce", category="apply"
        )
        return "nonce_ledger_error"


def _git_root(source_root: Path) -> Path | None:
    try:
        _snapshot_env, _snapshot_git = _verify_export("_snapshot_env"), _verify_export("_snapshot_git")

        r = subprocess.run(
            _snapshot_git(source_root, "rev-parse", "--show-toplevel"),
            capture_output=True,
            text=True,
            timeout=30,
            env=_snapshot_env(source_root),
            check=False,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as _ff_exc:
        record_critical_failure(
            _ff_exc, site="si_lanes.promote._git_root", category="apply"
        )
        return None
    return Path(r.stdout.strip()) if r.returncode == 0 else None


def _dirty_code_files(git_root: Path) -> list[str]:
    """Find tracked/untracked Python drift without checkout/filter execution.

    ``git status`` and ``diff-files`` may invoke repository-configured clean or
    process filters.  This scanner compares HEAD, index, and raw worktree bytes
    instead.  Any parse/read ambiguity returns a fixed dirty sentinel.
    """
    failure = ["<filter-free dirty scan failed>"]
    try:
        _raw_head_entries = _verify_export("_raw_head_entries")
        _snapshot_env, _snapshot_git = _verify_export("_snapshot_env"), _verify_export("_snapshot_git")

        env = _snapshot_env(git_root)
        index_run = subprocess.run(
            _snapshot_git(git_root, "ls-files", "--stage", "-z"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            env=env,
            check=False,
        )
        other_run = subprocess.run(
            _snapshot_git(git_root, "ls-files", "--others", "--exclude-standard", "-z"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            env=env,
            check=False,
        )
        format_run = subprocess.run(
            _snapshot_git(git_root, "rev-parse", "--show-object-format"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            env=env,
            check=False,
        )
        if any(run.returncode != 0 for run in (index_run, other_run, format_run)):
            return failure
        algorithm = format_run.stdout.decode("ascii", errors="strict").strip()
        if algorithm not in {"sha1", "sha256"}:
            return failure

        head = {
            path: (mode, oid)
            for mode, oid, _size, path in _raw_head_entries(git_root, env)
            if path.endswith(".py")
        }
        index: dict[str, tuple[str, str]] = {}
        dirty: set[str] = set()
        for record in index_run.stdout.split(b"\0"):
            if not record:
                continue
            meta, raw_path = record.split(b"\t", 1)
            mode, oid, raw_stage = meta.decode("ascii").split()
            path = raw_path.decode("utf-8", errors="strict")
            if not path.endswith(".py"):
                continue
            stage = int(raw_stage)
            if stage != 0 or path in index or set(oid) == {"0"}:
                dirty.add(path)
                continue
            index[path] = (mode, oid)

        dirty.update(set(head) ^ set(index))
        for path in set(head) & set(index):
            if head[path] != index[path]:
                dirty.add(path)

        for path, (mode, oid) in index.items():
            raw = Path(path)
            if (
                raw.is_absolute()
                or not raw.parts
                or any(part in {"", ".", "..", ".git"} for part in raw.parts)
            ):
                dirty.add(path)
                continue
            candidate = git_root.joinpath(*raw.parts)
            current = git_root
            unsafe_parent = False
            for part in raw.parts[:-1]:
                current = current / part
                if current.is_symlink():
                    unsafe_parent = True
                    break
            if unsafe_parent:
                dirty.add(path)
                continue
            try:
                metadata = candidate.lstat()
                if mode == "120000":
                    if not stat.S_ISLNK(metadata.st_mode):
                        dirty.add(path)
                        continue
                    data = os.readlink(candidate).encode("utf-8")
                else:
                    if not stat.S_ISREG(metadata.st_mode):
                        dirty.add(path)
                        continue
                    if metadata.st_size > _PROMOTE_MAX_PY_BYTES:
                        dirty.add(path)
                        continue
                    data = candidate.read_bytes()
                    expected_exec = mode == "100755"
                    if bool(metadata.st_mode & 0o111) != expected_exec:
                        dirty.add(path)
                digest = hashlib.new(algorithm)
                digest.update(f"blob {len(data)}\0".encode("ascii"))
                digest.update(data)
                if digest.hexdigest() != oid:
                    dirty.add(path)
            except (OSError, UnicodeError, ValueError) as exc:
                record_critical_failure(
                    exc,
                    site="si_lanes.promote._dirty_code_files:worktree",
                    category="apply",
                )
                dirty.add(path)

        for raw_path in other_run.stdout.split(b"\0"):
            if not raw_path:
                continue
            path = raw_path.decode("utf-8", errors="strict")
            if path.endswith(".py"):
                dirty.add(path)
        return sorted(dirty)
    except (OSError, UnicodeError, ValueError, subprocess.SubprocessError) as exc:
        record_critical_failure(
            exc, site="si_lanes.promote._dirty_code_files", category="apply"
        )
        return failure


def _read_regular_uncommitted(git_root: Path, relative: str) -> str:
    """Read a carry file without following any symlink component."""

    raw = Path(relative)
    if raw.is_absolute() or not raw.parts or any(
        part in {"", ".", "..", ".git"} for part in raw.parts
    ):
        raise OSError("unsafe uncommitted path")
    current = git_root
    for part in raw.parts:
        current = current / part
        if current.is_symlink():
            raise OSError("symlinked uncommitted path")
    metadata = current.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise OSError("uncommitted path is not a regular file")
    if metadata.st_size > _PROMOTE_MAX_PY_BYTES:
        raise OSError("uncommitted file exceeds carry budget")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(current, flags)
    try:
        with os.fdopen(descriptor, "r", encoding="utf-8", closefd=False) as handle:
            return handle.read()
    finally:
        os.close(descriptor)


def promote_patches(
    *,
    source_root: Path,
    patches: "Sequence[tuple[str, str]]",
    approve: bool = False,
    pytest_target: Sequence[str] | None = None,
    state_dir: "Path | None" = None,
    carry_uncommitted: bool = False,
    baseline_failures: "Sequence[str] | None" = None,
    approval_nonce: "str | None" = None,
) -> dict[str, Any]:
    """Promote a BATCH of verified patches in ONE gated transaction.

    # __SLOT_SI_PROMOTE_BATCH_2026_08_09__ 🔴 왜 복수형이 필요했나 — **승격 경로는
    구조적으로 1회용이었다.** 전제 3(clean git tree)은 실트리를 건드린 직후 스스로
    깨진다: 첫 승격이 성공하면 그 파일이 곧 uncommitted ``.py`` 드리프트가 되고,
    같은 세션의 **두 번째 승격은 항상** ``dirty_tree`` 로 거절된다. 커밋은 사람
    몫이므로(이 모듈은 절대 커밋하지 않는다) 루프가 한 번에 여러 결함을 고쳐도
    착지할 수 있는 것은 하나뿐이었다. 2026-08-09 실측으로 확인하고 여기서 닫는다.

    계약은 :func:`promote_patch` 와 **같다** — 다섯 전제를 배치 전체에 대해 한 번씩
    적용한다(게이트 · 명시 승인 · clean tree · **한 번의** inc5 검증에 모든 패치를
    같이 얹음 · SafeAutoApply 2-env 쓰기 게이트). 검증이 배치 단위인 것이 요점이다:
    패치들이 서로를 깨는 경우를 개별 검증은 못 본다.

    ⚠️ 원자성은 SafeAutoApply 의 ``apply_session`` 이 주는 만큼이다. 부분 적용이
    나오면 ``changes_applied`` 와 ``errors`` 로 그대로 보고한다 — 성공으로 접지 않는다.

    # __SLOT_SI_PROMOTE_CARRY_2026_08_09__ ``carry_uncommitted`` (기본 False =
    종전과 byte-identical) — **전제 3 의 목적을 대체가 아니라 이행으로 만든다.**

    전제 3 이 dirty tree 를 막는 진짜 이유는 *"검증은 HEAD 기준이라 미커밋 드리프트를
    안 비춘다"* 이다(``verify_gate._dirty_code_files`` 의 경고문 그대로). 그러면 초록
    verdict 가 **실제로 존재하게 될 트리**에 대한 진술이 아니다. 이 노브는 그 간극을
    가정이 아니라 **재서** 닫는다: 미커밋 ``.py`` 를 전부 실트리에서 읽어 검증 세트에
    같이 얹는다 ⇒ 워크트리 = HEAD + 드리프트 + 패치 = 쓰기 직후의 실트리.

    ⛔ fail-closed 세 겹: 미커밋 파일을 **하나라도** 못 읽으면(삭제·rename·바이너리)
    거절 · basename 이 겹치면 거절(``_map_changes`` 가 그 둘을 검증에서 빼버린다) ·
    커버리지가 전수가 아니면 거절. 즉 "일부만 얹고 초록"은 표현 불가능하다.

    ⚠️ 되돌리기는 이때 git 이 아니다(dirty tree 에서 ``git checkout --`` 는 옆 파일의
    미커밋 작업을 죽인다 — 이 레포가 이미 값을 치른 사고). SafeAutoApply 의 ``.bak``
    와 호출자가 들고 있는 원본 바이트가 revert 경로다.

    # __SLOT_SI_PROMOTE_BASELINE_2026_08_09__ ``baseline_failures`` (기본 None =
    종전과 byte-identical) — 전제 4 의 **절대 초록**을 "새 적색 없음"으로 바꾼다.
    ⛔ 기준선은 호출자가 **같은 워크트리·같은 pytest 대상**에서 패치 없이 재서
    넘겨야 한다. 다른 스코프의 기준선을 물리면 부분집합 판정이 통째로 거짓이 된다.

    # __SLOT_SI_PROMOTE_CAS_2026_08_17__ (R5 §7-a, security fix, default ON via
    # ``AGI_V8_SI_PROMOTE_CAS_ENABLED``) dirty-scan → verify (pytest, seconds)
    # → write used to have NO lock and NO compare-and-swap across that whole
    # window. A concurrent process could dirty a TARGET file (clean at scan
    # time) while pytest ran, and the write would silently clobber it with the
    # reviewed patch — the operator's "clean tree" precondition was true only
    # at t0, never re-checked at write time. This now captures each target's
    # ``(kind, sha256)`` fingerprint plus HEAD right after the dirty scan, and
    # refuses (``preimage_mismatch:<path>`` / ``head_moved``) if either moved
    # by the time SafeAutoApply is about to write. OFF restores the old,
    # unchecked behaviour byte-for-byte.

    # __SLOT_SI_PROMOTE_APPROVAL_BIND_2026_08_17__ (R5 §7-b, security fix,
    # default ON via ``AGI_V8_SI_PROMOTE_APPROVAL_BIND_ENABLED``) ``approve``
    # was a bare bool with nothing binding it to a specific reviewed patch —
    # any caller that could reach ``approve=True`` could promote ANY content,
    # repeatedly. ``approval_nonce`` (opt-in) binds a caller-chosen single-use
    # token to this batch's ``target:sha256(content)`` digest; reusing the
    # same nonce for a second call — same content or different — is refused
    # (``nonce_reused``), and the binding REQUIRES ``state_dir`` — a caller
    # that supplies a nonce but no persistent store is refused outright
    # (``nonce_requires_state_dir``, see ``_check_and_bind_nonce``) rather
    # than silently downgraded to a per-process check, which is exactly what
    # let ADV-R5-E2 promote the same nonce twice from two separate CLI
    # invocations of THIS module's own ``main()``. ``main()`` now always
    # resolves and passes a ``state_dir`` (arg > env > repo default), so a
    # bare CLI call is covered without any extra flag.
    # ⚠️ HONEST SCOPE LIMIT: this only protects callers that pass a nonce —
    # ``approval_nonce`` stays an OPT-IN parameter of THIS function; a direct
    # caller that never supplies one is unaffected, by design (that is what
    # ``test_adv_r5_021_no_nonce_gate_off_unaffected`` pins).
    # __SLOT_R9_T4_2026_08_17__ Round 9 track T4: the OTHER production driver
    # CLI (a batch-promotion tool that lives under ``tools/``, outside this
    # module — deliberately not named here by its literal filename, since a
    # sibling test elsewhere greps this module's body for that exact string
    # to prove no cycle-code path references it) used to call this function
    # with ``approve=True`` and no nonce at all — the exact gap this note
    # used to describe as open. It now resolves and passes an
    # ``approval_nonce`` (auto-generated, logged) by default, gated by its
    # OWN gate ``AGI_V8_PROMOTE_REQUIRE_NONCE`` (default ON) at its CLI
    # entry point. That closes the driver-level instance of this gap. It
    # does NOT change what THIS function accepts: any OTHER caller that
    # imports ``promote_patches`` directly and never passes a nonce is still
    # let through on ``approve=True`` alone — that is this function's
    # permanent, documented contract, not a bug. See
    # ``tests/security/test_adv_r5_E_2026_08_17.py``
    # (``test_adv_r5_021_no_nonce_still_exploitable_legacy``, still skipped —
    # it demonstrates that permanent library-level scope limit, not a live
    # production path) and ``tests/security/test_adv_r9_T4_2026_08_17.py``
    # for the driver-level regression coverage.

    Returns ``{promoted, reason, dry_run, write_enabled, action, verify,
    apply_success, changes_applied, errors, pre_hash, post_hash, patches}``.
    ``action`` is the single patch's action when exactly one was requested
    (legacy shape) and ``""`` otherwise; ``patches`` always carries the per-target
    detail. ``promoted`` is True ONLY when all five preconditions held AND every
    requested write landed.
    """
    # Direct library callers must cross the same T8 boundary as both CLIs.
    # Private master-OFF behavior, including legacy nonce semantics, is intact.
    if os.environ.get("AGI_V8_PUBLIC_TIER_GATE_ENABLED", "false").strip().lower() == "true":
        from agi_v8_1.policy.tier_gate import require_tier
        require_tier(8)
    label = "<invalid>"

    def _refuse(reason: str, **extra: Any) -> dict[str, Any]:
        logger.warning("F1-inc6 promotion REFUSED: %s (target=%s)", reason, label)
        return {"promoted": False, "reason": reason, "dry_run": True,
                "write_enabled": False, "action": "", "verify": extra.get("verify"),
                "apply_success": False, "changes_applied": 0, "errors": [],
                "pre_hash": "", "post_hash": "", "patches": []}

    # Public transaction boundary: malformed model/tool payloads must produce
    # a refusal record, never explode while tuple-unpacking for a log label.
    try:
        if isinstance(patches, (str, bytes)):
            return _refuse("invalid_patches")
        raw_patches = list(patches)
        normalised: list[tuple[str, str]] = []
        for item in raw_patches:
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                return _refuse("invalid_patches")
            target, content = item
            if not isinstance(target, str) or not isinstance(content, str):
                return _refuse("invalid_patches")
            normalised.append((target, content))
    except Exception as exc:  # noqa: BLE001 — untrusted batch container
        record_critical_failure(
            exc,
            site="si_lanes.promote.promote_patches:normalise",
            category="apply",
        )
        return _refuse("invalid_patches")
    patches = normalised
    targets = [target for target, _content in patches]
    label = ", ".join(targets) if targets else "<none>"

    # 1. gate ---------------------------------------------------------------
    if not _promote_enabled():
        return _refuse(f"gate_off:{_PROMOTE_ENV}")
    # 2. explicit approval --------------------------------------------------
    if not approve:
        return _refuse("not_approved (pass approve=True / --approve-promote)")
    if not patches:
        return _refuse("no_patches")
    if any(not isinstance(c, str) or not c.strip() for _, c in patches):
        return _refuse("empty_content")
    # ⛔ 같은 파일을 두 번 실으면 뒤엣것이 앞엣것을 말없이 덮는다 — 검증한 내용과
    # 적용한 내용이 갈라지는 자리라 거절한다.
    if len(set(targets)) != len(targets):
        return _refuse(f"duplicate_target: {sorted({t for t in targets if targets.count(t) > 1})}")
    # ⛔ inc5 는 패치를 **basename 으로** 대상에 맞춘다(``verify_gate._map_changes``).
    # 배치 안에 같은 basename 이 둘이면 그 둘은 "모호"로 분류돼 검증에서 **빠지고**,
    # 남은 것이 초록이면 배치 전체가 초록으로 보인다. 검증 밖에서 미리 막는다.
    bases = [Path(t).name for t in targets]
    if len(set(bases)) != len(bases):
        return _refuse(f"ambiguous_basenames: {sorted({b for b in bases if bases.count(b) > 1})}")
    # 2b. approval-nonce binding (R5 §7-b) — opt-in, see docstring for scope.
    if _approval_bind_enabled() and approval_nonce is not None:
        if not isinstance(approval_nonce, str) or not approval_nonce.strip():
            return _refuse("invalid_approval_nonce")
        bind_reason = _check_and_bind_nonce(
            state_dir, approval_nonce, _patch_digest(patches)
        )
        if bind_reason is not None:
            return _refuse(bind_reason)
    # 3. clean git tree (or: carry the drift INTO the verification) ---------
    git_root = _git_root(Path(source_root))
    if git_root is None:
        return _refuse("not_a_git_repo")
    dirty_all = _dirty_code_files(git_root)
    # ⛔ **대상 파일 자신의 드리프트는 어떤 경우에도 거절이다.** 그 미커밋 편집은
    # 승격이 통째로 덮어써서 소리 없이 사라진다 — carry 로도 못 구한다(같은 파일을
    # 두 내용으로 얹는 셈이라 검증한 것과 쓴 것이 갈라진다).
    dirty_targets = [d for d in dirty_all if d in targets]
    if dirty_targets:
        # ⚠️ 사유 문자열은 ``dirty_tree`` 로 **시작해야 한다** — 기존 계약 테스트가
        # 그 접두를 붙들고 있고, 이건 그 거절의 더 날카로운 판이지 다른 거절이 아니다.
        return _refuse(f"dirty_tree_target (uncommitted edits would be overwritten): "
                       f"{dirty_targets[:5]}")
    dirty = [d for d in dirty_all if d not in targets]
    carried: list[tuple[str, str]] = []
    if dirty:
        if not carry_uncommitted:
            return _refuse(f"dirty_tree (commit/stash first): {dirty[:5]}")
        # ⛔ 전수 아니면 거절. 하나라도 못 읽으면 그 초록은 다른 트리 이야기다.
        for rel in dirty:
            try:
                carried.append((rel, _read_regular_uncommitted(git_root, rel)))
            except (OSError, UnicodeDecodeError) as exc:
                record_critical_failure(
                    exc,
                    site="si_lanes.promote.promote_patches:carry",
                    category="apply",
                )
                return _refuse(
                    f"uncommitted_unreadable:{rel}:{safe_exception_type_name(exc)}"
                )
        logger.warning(
            "F1-inc6: carrying %d uncommitted .py file(s) INTO the verification so "
            "the verdict describes the post-write tree: %s", len(carried), dirty[:5])
    # __SLOT_SI_PROMOTE_CAS_2026_08_17__ CAS capture (T0) — right after the
    # dirty-tree checks pass, BEFORE the (possibly multi-second) verify run.
    # Re-checked at T2, right before the SafeAutoApply write, below.
    cas_head: "str | None" = None
    cas_preimages: dict[str, "tuple[str, str]"] = {}
    if _cas_enabled():
        cas_head = _head_commit(git_root)
        cas_preimages = {t: _preimage_state(git_root / t) for t in targets}
    # 4. FRESH inc5 verification (ONE run, ALL patches + carried drift) -----
    all_pairs = list(patches) + carried
    all_targets = targets + [rel for rel, _ in carried]
    all_bases = [Path(t).name for t in all_targets]
    if len(set(all_bases)) != len(all_bases):
        return _refuse(
            "ambiguous_basenames_with_uncommitted: "
            f"{sorted({b for b in all_bases if all_bases.count(b) > 1})}")
    try:
        verify_changes = _verify_export("verify_changes")
        changes = [{"path": t, "action": "create", "content": c,
                    "proposal_id": "promote"} for t, c in all_pairs]
        verify = verify_changes(changes, source_root=git_root,
                                target_files=all_targets, pytest_target=pytest_target)
    except Exception as exc:  # noqa: BLE001 — verify import/run never aborts; fail-closed
        _swallowed(exc, site="si_lanes.promote.promote_patches:verify", category="apply")
        return _refuse(f"verify_errored:{exc}")
    # ⛔ ``ran`` 이 참이 아닌 초록은 **스위트를 안 돌린 초록**이다
    # (``verify_changes`` 의 ``nothing_to_verify`` 는 ``ok=True, ran=False``).
    # 여기서 접으면 검증되지 않은 패치가 실트리로 간다 — 이 모듈의 존재 이유와 반대다.
    if verify.get("ran") is not True:
        return _refuse(f"verify_did_not_run:{verify.get('reason')}", verify=verify)
    # Candidate Python shares pytest's in-sandbox language privileges and can
    # forge any marker produced inside that process.  Baseline-subset recovery
    # must never reinterpret an explicitly unverified verdict as authorization.
    # Candidate-controlled pytest can forge both terminal output and its own
    # return code.  No verdict from this in-process runner is promotion
    # authority until an out-of-domain reviewer has set the explicit evidence
    # bit; baseline-subset recovery must not bypass this check for rc!=0.
    if verify.get("completion_verified") is not True:
        return _refuse(
            f"verify_completion_untrusted:{verify.get('reason')}", verify=verify
        )
    if not verify.get("ok"):
        # __SLOT_SI_PROMOTE_BASELINE_2026_08_09__ 🔴 전제 4 는 **절대 초록**을 요구했다.
        # 이 레포는 초록이 아니다 — 실측 2026-08-09, HEAD 워크트리 ``tests/v8
        # tests/v8_1`` 이 68 적색이다. 그래서 이 관문은 **패치의 좋고 나쁨과 무관하게
        # 언제나 거절**했고, 그것이 승격 경로가 46일간 한 번도 안 돈 이유의 한 축이다.
        #
        # ``baseline_failures`` 를 주면 판정이 "초록인가"에서 **"새 적색이 있는가"**
        # 로 바뀐다. 이건 이 레포가 회귀를 보는 방식 그대로다(개수가 아니라 신원).
        # ⛔ fail-closed 세 겹: 기준선을 안 주면 종전대로 절대 초록 요구 · 어느 쪽
        # 목록이든 ``+N more`` 로 **잘려 있으면** 비교 불가라 거절 · 새 신원이 하나라도
        # 있으면 거절. "부분집합이니까 괜찮다"를 개수로 판단하지 않는다.
        if baseline_failures is None:
            return _refuse(f"verify_failed:{verify.get('reason')}", verify=verify)
        now = [str(t) for t in (verify.get("failed_tests") or [])]
        base = [str(t) for t in baseline_failures]
        truncated = [t for t in now + base if t.endswith(" more") and t.startswith("+")]
        if truncated:
            return _refuse("baseline_compare_impossible:identity_list_truncated",
                           verify=verify)
        if not now:
            # 적색인데 신원이 하나도 안 잡혔다 = 파싱 실패/충돌 등 **다른 고장**이다.
            return _refuse(f"verify_failed_without_identities:{verify.get('reason')}",
                           verify=verify)
        new_reds = sorted(set(now) - set(base))
        if new_reds:
            return _refuse(f"verify_new_reds:{new_reds[:8]}", verify=verify)
        logger.warning(
            "F1-inc6: suite is red at baseline too — accepted on IDENTITY SUBSET "
            "(now=%d, baseline=%d, new=0). ⚠️ 이건 초록이 아니라 '악화 없음'이다.",
            len(now), len(base))

    # 5. write via SafeAutoApply (2-env gate → dry_run when off), UNCOMMITTED
    try:
        from agi_v8_1.enforcement.safe_auto_apply import FileChange, SafeAutoApply
        write_on = SafeAutoApply.write_enabled()
        # __SLOT_SI_PROMOTE_CAS_2026_08_17__ CAS re-check (T2) — the verified
        # preimage must still be the on-disk truth right before the write.
        # A mismatch here means some OTHER process moved the target (or HEAD)
        # during the verify window; the patch was reviewed against a tree
        # state that no longer exists, so refuse rather than write blind.
        if _cas_enabled():
            head_now = _head_commit(git_root)
            # ⛔ ``None != None`` is False — if HEAD couldn't be read at T0 AND
            # again at T2 (two independent ``git rev-parse`` failures), a bare
            # `!=` compare would silently treat that as "unchanged" and let
            # the write through with the HEAD check never having actually
            # fired once. A failed read is not evidence HEAD held; refuse.
            if head_now is None or cas_head is None or head_now != cas_head:
                return _refuse("head_moved", verify=verify)
            drifted = [t for t in targets
                       if _preimage_state(git_root / t) != cas_preimages.get(t)]
            if drifted:
                return _refuse(f"preimage_mismatch: {sorted(drifted)[:5]}",
                               verify=verify)
        file_changes, detail = [], []
        for target_rel, content in patches:
            action = "edit" if (git_root / target_rel).exists() else "create"
            file_changes.append(FileChange(path=target_rel, action=action,
                                           content=content))
            detail.append({"target_rel": target_rel, "action": action})
        # __SLOT_B2_SAFE_AUTO_APPLY_2026_08_10__ 원장 앵커를 **말한다**. 이 함수는
        # `state_dir`(이 실행의 젤)을 이미 쥐고도 안 넘겨, 원장 자리를 호출자가
        # 아니라 프로세스 env 가 정했다. SI 레인 3곳이 쓰는 그 형태 그대로 복제한다
        # (`self_improvement_v8.py` 의 `__SLOT_SI_AUDIT_JAIL_ANCHOR_2026_08_09__`).
        # ⛔ `repo_root` 는 그대로 `git_root` — 원장 위치와 쓰기 격리 앵커는 다른 축.
        # ⛔ 특례를 만들지 않는다(R6 판정): 종전 판이 `"."` 만 "미설정"으로 접었더니
        #    같은 뜻인 `x/..`·`relstate` 는 안 접혀 해석이 갈렸고, 아래 `emit_human`
        #    까지 그 접기가 번져 HEAD 가 내던 HUMAN 행이 **사라졌다**(실측). 규칙은
        #    하나다 — 상대 철자는 CWD 기준(파이썬 상대경로의 뜻 그대로), 폴백은
        #    `None` 일 때만. `or None` 은 아래 `_sd` 와 **같은 접기**라 한 함수에
        #    해석이 둘이 되지 않는다. env(`AGI_V8_STATE_DIR`)는 여전히 인자를 이긴다.
        # 🔑 델타: PIN=true + 젤==`<repo>/state/si_jail` → 0. PIN=off → HEAD
        #    `<git_root>/state/si_audit` vs FIX `<state_dir>/si_audit`. 판독기도
        #    `--jail` 을 받으므로 실행의 젤에 쓰는 쪽이 조인 축과 맞는다.
        applier = SafeAutoApply(repo_root=git_root, dry_run=not write_on,
                                audit_root=state_dir or None)
        res = applier.apply_session(file_changes, decision_source="si_promote")
    except Exception as exc:  # noqa: BLE001 — apply never aborts; fail-closed
        _swallowed(exc, site="si_lanes.promote.promote_patches:apply", category="apply")
        return _refuse(f"apply_errored:{exc}", verify=verify)

    action = detail[0]["action"] if len(detail) == 1 else ""
    promoted = bool(write_on and res.success
                    and res.changes_applied >= len(file_changes))
    if promoted:
        logger.warning(
            "F1-inc6 PROMOTED %s (%s) to the WORKING TREE — UNCOMMITTED. "
            "Review: git -C %s diff -- %s | Revert: git -C %s checkout -- %s | "
            "pre=%s post=%s",
            label, action or "batch", git_root, " ".join(targets), git_root,
            " ".join(targets),
            res.pre_apply_hash[:12], res.post_apply_hash[:12],
        )
        # __SLOT_HUMAN_EVENT_2026_08_06__ 🔴 계약의 HUMAN — **자율성에서 차감된다.**
        #
        # 2026-08-06 확인: `episode_log.emit_human` 에 프로덕션 호출자가 **0** 이었다.
        # 그래서 우리 원장의 HUMAN 개수는 항상 0 이고, 리더보드는 그 0 을 **만점
        # 자율성**으로 읽는다. `halt_label()` 이 막고 있는 `refused` 인플레이션과
        # 같은 종류이고 방향도 같다 — 우리에게 유리한 쪽으로 틀린다.
        #
        # 여기가 **도달 가능한 유일한 개입 지점**이다. swarm 쪽 `operator_approved`
        # 는 같은 날 확인한 caller 0 이라 거기 붙이면 연극이다.
        #
        # ⚠️ **성공했을 때만** 찍는다. 거절된 승격은 사람이 시도는 했지만 루프에
        # 도움이 되지 않았다 — 자율성 차감은 "루프가 혼자 못 해서 사람이 대신 해준
        # 것"을 세는 축이다. (거절 횟수는 다른 축이고 여기 섞지 않는다.)
        try:
            from agi_v8_1.runtime import episode_log as _el

            # ⛔ 젤을 못 정하면 **안 쓴다**(D0② 와 같은 규칙). 추측 경로에 쓰면
            # 한 에피소드의 개입이 다른 에피소드 원장에 나타난다.
            import os as _os

            # ⛔ __SLOT_B2_SAFE_AUTO_APPLY_2026_08_10__ 이 줄은 HEAD 그대로 둔다 —
            # 위 `audit_root` 와 **같은 `or` 접기**다. R6 판정: 여기에 `"."` 특례를
            # 끼우면 `state_dir=Path(".")` 에서 HUMAN 행의 **생산 자체가 사라져**
            # 자율성 지표가 우리에게 유리한 쪽으로 틀린다(실측: CWD 트리 = []).
            _sd = state_dir or (_os.environ.get("AGI_V8_STATE_DIR")
                                or _os.environ.get("AGI_STATE_DIR"))
            if _sd:
                _el.emit_human(
                    _sd,
                    # 승격은 에피소드 **뒤에** 일어나므로 사이클을 모른다.
                    # ⚠️ 모름은 None — 아무 사이클에나 귀속시키지 않는다.
                    cycle_id=None,
                    note=(f"operator promoted {label} to the live tree "
                          f"({action or 'batch'}, post={res.post_apply_hash[:12]}) — "
                          "the loop could not land this by itself"),
                    # __SLOT_R9_T4_2026_08_17__ "무엇을" 승격했는지는 이 호출자만
                    # 안다(episode_log 는 OS 컨텍스트만 자동으로 채운다) — 대상
                    # 경로 + 내용 다이제스트를 실어, 승인 기록이 그 승격된 바이트
                    # 자체를 가리키게 한다. 이건 여전히 감사 기록이지 서명이
                    # 아니다: 이 dict 는 이 프로세스가 스스로 계산해 스스로
                    # 적는 값이다.
                    actor={
                        "targets": sorted(targets),
                        "patch_digest": _patch_digest(patches),
                        "post_hash": res.post_apply_hash,
                    },
                )
        except Exception as exc:  # noqa: BLE001 — 기록 실패가 승격을 되돌리진 않는다
            _swallowed(exc, site="si_lanes.promote.promote_patches:human_event",
                       category="telemetry")
    else:
        logger.info(
            "F1-inc6 promotion did not write: write_enabled=%s apply_success=%s "
            "changes_applied=%d errors=%s",
            write_on, res.success, res.changes_applied, res.errors,
        )
    return {
        "promoted": promoted,
        "reason": "promoted" if promoted else ("dry_run" if not write_on else "apply_failed"),
        "dry_run": not write_on, "write_enabled": write_on, "action": action,
        "verify": verify, "apply_success": res.success,
        "changes_applied": res.changes_applied, "errors": list(res.errors),
        "pre_hash": res.pre_apply_hash, "post_hash": res.post_apply_hash,
        "patches": detail,
        # 검증에 **같이 얹었지만 쓰지는 않은** 미커밋 파일들. 초록이 어느 트리에
        # 대한 진술인지가 원장에서 읽혀야 한다.
        "carried_uncommitted": [rel for rel, _ in carried],
    }


def promote_patch(
    *,
    source_root: Path,
    target_rel: str,
    content: str,
    approve: bool = False,
    pytest_target: Sequence[str] | None = None,
    state_dir: "Path | None" = None,
) -> dict[str, Any]:
    """Promote ONE verified patch to the live tree. Never raises; never commits.

    Thin single-patch spelling of :func:`promote_patches` — the five
    preconditions, the return shape and the refusal reasons are that function's,
    unchanged (``patches`` is the one added key).

    ⛔ Parameter set is a locked contract (``test_promote_patch_is_the_single_
    patch_spelling_of_the_batch``) — the R5 §7-b ``approval_nonce`` addition
    stays on :func:`promote_patches` only; callers that want nonce binding for
    a single patch call the batch function directly with a one-item list.
    """
    return promote_patches(source_root=source_root,
                           patches=[(target_rel, content)], approve=approve,
                           pytest_target=pytest_target, state_dir=state_dir)


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(
        description="Promote a verified patch to the live source tree (deliberate, gated).")
    ap.add_argument("--target", required=True,
                    help="relpath in the source tree to promote the patch to")
    ap.add_argument("--content-file", required=True,
                    help="file holding the patched content (e.g. a jail shadow patch)")
    ap.add_argument("--source-root", default=None,
                    help="git repo root (default: the agi_v8_1 package root)")
    ap.add_argument("--approve-promote", action="store_true",
                    help="explicit operator approval — REQUIRED to write")
    ap.add_argument("--pytest-target", default=None,
                    help="pytest target for the fresh verify (default: tests/v8_1)")
    ap.add_argument("--approval-nonce", default=None,
                    help="single-use token binding this approval to this patch's "
                         "digest (default: auto-generated + logged, since this CLI "
                         "call IS the deliberate one-off approval)")
    ap.add_argument("--state-dir", default=None,
                    help="jail/state dir the approval-nonce ledger persists under "
                         "(default: $AGI_V8_STATE_DIR / $AGI_STATE_DIR, else "
                         "<source-root>/state/si_jail). REQUIRED for the nonce's "
                         "single-use guarantee to survive across separate CLI "
                         "invocations — see __SLOT_SI_PROMOTE_NONCE_STATE_DIR_"
                         "REQUIRED_2026_08_17__")
    a = ap.parse_args(argv)

    if os.environ.get("AGI_V8_PUBLIC_TIER_GATE_ENABLED", "false").strip().lower() == "true":
        from agi_v8_1.policy.tier_gate import require_tier
        require_tier(8)

    try:
        driver = resolve_payload(PayloadPort(
            8, "agi_v8_1.si_lanes.promote_driver_payload", "run_single"))
    except PayloadUnavailable as exc:
        record_critical_failure(exc, site="si_lanes.promote.payload_unavailable",
                                category="apply")
        print("T8 미개방: promotion payload unavailable; read Plz_ReadMe.md §T8",
              file=sys.stderr)
        return 2

    source_root = Path(a.source_root) if a.source_root else Path(__file__).resolve().parents[1]
    try:
        content = Path(a.content_file).read_text("utf-8")
    except OSError as exc:
        safe_exc = format_exception_for_critical_record(
            exc, max_chars=2000, one_line=True
        )
        _swallowed(exc, site="si_lanes.promote.main:184", category="apply")
        print(f"cannot read --content-file: {safe_exc}", file=sys.stderr)
        return 2

    # __SLOT_SI_PROMOTE_NONCE_STATE_DIR_REQUIRED_2026_08_17__ arg > env > repo
    # default — same precedence the OTHER driver CLI's own jail-resolution
    # helper (see the report's integration note) uses for the same concept.
    # Resolved unconditionally (never left ``None``) so
    # ``_check_and_bind_nonce`` always has a persistent store to bind
    # against; a bare CLI invocation with no flags is this module's most
    # common call shape, and it is exactly the shape ADV-R5-E2 broke.
    if a.state_dir:
        state_dir = Path(a.state_dir)
    else:
        env_state_dir = os.environ.get("AGI_V8_STATE_DIR") or os.environ.get("AGI_STATE_DIR")
        state_dir = Path(env_state_dir) if env_state_dir else source_root / "state" / "si_jail"

    nonce = a.approval_nonce
    if nonce is None and a.approve_promote and _approval_bind_enabled():
        nonce = secrets.token_urlsafe(24)
        logger.info("F1-inc6: auto-generated approval nonce for this call: %s", nonce)

    # ``promote_patch``'s parameter set is a locked single-patch contract (see
    # its docstring) — call the batch function directly so the CLI can pass
    # ``approval_nonce`` without touching that contract.
    return driver(sys.modules[__name__], source_root=source_root, a=a,
                  state_dir=state_dir, nonce=nonce, content=content)


if __name__ == "__main__":
    raise SystemExit(main())
