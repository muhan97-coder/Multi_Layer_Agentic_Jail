"""Default-OFF educational public-tier admission, not DRM or launch authority.

Local T1/T2 receipts are deliberately an inspectable speed bump. Their evidence
digest detects accidental drift, not a user rebuilding the entire local record.
T3+ additionally needs a release-provisioned Ed25519 issuer. No keys are trusted
by default. T9 runtime facts must come from a verified caller, never token data.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from agi_v8_1.policy.fail_fast import format_exception_for_sink

_GATE = "AGI_V8_PUBLIC_TIER_GATE_ENABLED"
_LOCAL_SCHEMA = "agi-v8-public-local-tier/v1"
_EVIDENCE_SCHEMA = "agi-v8-public-local-evidence/v1"
_LIMIT = 1048576
_T9_FACTS = frozenset({"killswitch_owner", "external_killswitch_owner", "cost_cap",
                       "jail_integrity", "promote_history"})


class TierRefused(PermissionError):
    def __init__(self, tier: int, reason: str) -> None:
        self.tier = tier
        self.reason = reason
        self.doc_pointer = f"Plz_ReadMe.md §T{tier}"
        super().__init__(f"T{tier} 미개방: {reason}; read {self.doc_pointer}")


@dataclass(frozen=True)
class TierContext:
    state_dir: Path
    repo_commit: str = ""
    user_id: str = ""
    issuer: str = ""
    evidence_digest: str = ""
    now: int | None = None
    t9_prerequisites: Mapping[str, bool] | None = None


_ACTIVE_CONTEXT: ContextVar[TierContext | None] = ContextVar("public_tier_context", default=None)


@contextmanager
def active_tier_context(context: TierContext):
    """Trusted public caller scope; restored even on errors and never global.

    Only the caller supplies this object. No token/request deserialization can
    install context. Context propagation to new threads must be explicit.
    """
    if type(context) is not TierContext:
        raise TierRefused(9, "invalid_trusted_context")
    token = _ACTIVE_CONTEXT.set(context)
    try:
        yield
    finally:
        _ACTIVE_CONTEXT.reset(token)


def enabled() -> bool:
    return os.environ.get(_GATE, "false").strip().lower() == "true"  # tier: T0


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _json(raw: bytes) -> object:
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise ValueError("duplicate key")
            out[key] = value
        return out
    return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)


def _open_directory(path: Path, *, create: bool = False, private: bool = True) -> int:
    """Walk with directory FDs and O_NOFOLLOW, including every ancestor."""
    path = Path(os.path.abspath(path))
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for component in path.parts[1:]:
            if create:
                try:
                    existing = os.stat(component, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    os.mkdir(component, 0o700, dir_fd=fd)
                else:
                    if not stat.S_ISDIR(existing.st_mode):
                        raise ValueError("existing ancestor is not a directory")
            new_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                             | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = new_fd
        info = os.fstat(fd)
        if private and (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077):
            raise ValueError("unlock directory permissions")
        result, fd = fd, -1
        return result
    finally:
        if fd >= 0:
            os.close(fd)


def _read_at(fd: int, name: str, *, limit: int = _LIMIT) -> bytes:
    file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                      dir_fd=fd)
    try:
        info = os.fstat(file_fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                or info.st_size > limit):
            raise ValueError("unlock file type or permissions")
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(file_fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > limit:
            raise ValueError("unlock file size")
        return raw
    finally:
        os.close(file_fd)


def _write_at(fd: int, name: str, raw: bytes) -> None:
    """Write private bytes atomically without following/replacing a symlink."""
    temporary = ".pending-" + secrets.token_hex(12)
    file_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                      | os.O_CLOEXEC, 0o600, dir_fd=fd)
    replaced = False
    try:
        os.fchmod(file_fd, 0o600)
        position = 0
        while position < len(raw):
            written = os.write(file_fd, raw[position:])
            if written <= 0:
                raise OSError("short write")
            position += written
        os.fsync(file_fd)
        try:
            old = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            old = None
        if old is not None and (not stat.S_ISREG(old.st_mode) or old.st_nlink != 1
                                or old.st_uid != os.geteuid()):
            raise ValueError("unsafe existing unlock file")
        os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
        replaced = True
        os.fsync(fd)
    finally:
        os.close(file_fd)
        if not replaced:
            os.unlink(temporary, dir_fd=fd)


def local_curriculum() -> dict:
    path = Path(__file__).resolve().parents[1] / "public_assets/curriculum/local_tiers.json"
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 8192:
            raise ValueError("curriculum size or type")
        data = _json(os.read(fd, 8193))
    finally:
        os.close(fd)
    if (type(data) is not dict or set(data) != {"schema", "T1", "T2"}
            or data["schema"] != "agi-v8-public-local-curriculum/v1"
            or type(data["T1"]) is not dict or type(data["T2"]) is not dict
            or type(data["T1"].get("ack")) is not str or not data["T1"]["ack"]
            or type(data["T2"].get("ack")) is not str or not data["T2"]["ack"]
            or type(data["T2"].get("minimum_distinct_cycles")) is not int
            or data["T2"]["minimum_distinct_cycles"] != 2):
        raise ValueError("curriculum schema")
    return data


def normalize_cycle_evidence(raw: bytes) -> dict:
    """Extract only cycle_id/status from an explicit, bounded CycleLogger file.

    This is local evidence, never server-time or remote-honesty proof. Existing
    production cycle_end records are consumed; timestamps, prompts and extra
    payload fields are neither copied nor printed. Duplicate IDs count once.
    """
    if type(raw) is not bytes or not raw or len(raw) > _LIMIT:
        raise ValueError("evidence size")
    cycles: dict[str, str] = {}
    for line in raw.splitlines():
        if not line.strip():
            continue
        row = _json(line)
        if type(row) is not dict or type(row.get("event_type")) is not str:
            raise ValueError("evidence row")
        if row["event_type"] != "cycle_end":
            continue
        cycle_id, payload = row.get("cycle_id"), row.get("payload")
        if (type(cycle_id) is not str or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", cycle_id) is None
                or type(payload) is not dict or payload.get("status") not in
                {"advisory_stub", "consensus", "split", "blocked"}):
            raise ValueError("cycle evidence")
        status_value = payload["status"]
        if cycle_id in cycles and cycles[cycle_id] != status_value:
            raise ValueError("contradictory cycle evidence")
        cycles[cycle_id] = status_value
    if not cycles:
        raise ValueError("empty cycle evidence")
    return {"schema": _EVIDENCE_SCHEMA, "cycles": [
        {"cycle_id": key, "status": cycles[key]} for key in sorted(cycles)]}


def _check_evidence(data: object, minimum: int) -> None:
    if type(data) is not dict or set(data) != {"schema", "cycles"} or data["schema"] != _EVIDENCE_SCHEMA:
        raise ValueError("evidence schema")
    rows = data["cycles"]
    if type(rows) is not list or len(rows) > 10000:
        raise ValueError("evidence rows")
    # Reuse the production-row parser to validate normalized rows, not a
    # self-declared count or success counter from the receipt.
    raw_rows = []
    for row in rows:
        if type(row) is not dict or set(row) != {"cycle_id", "status"}:
            raise ValueError("evidence row shape")
        raw_rows.append(_canonical({"event_type": "cycle_end", "cycle_id": row["cycle_id"],
                                    "payload": {"status": row["status"]}}))
    checked = normalize_cycle_evidence(b"\n".join(raw_rows))
    if checked != data or len(checked["cycles"]) < minimum:
        raise ValueError("insufficient or noncanonical evidence")


def _local_tier(fd: int, curriculum: dict) -> int:
    record = _json(_read_at(fd, "tier.json", limit=8192))
    if (type(record) is not dict or set(record) != {"schema", "tier", "evidence_digest", "ts", "acks"}
            or record["schema"] != _LOCAL_SCHEMA or type(record["tier"]) is not int
            or record["tier"] not in (1, 2) or type(record["ts"]) is not int or record["ts"] < 0
            or type(record["acks"]) is not dict):
        raise ValueError("local receipt schema")
    tier = record["tier"]
    expected_acks = {str(n): _digest(curriculum[f"T{n}"]["ack"]) for n in range(1, tier + 1)}
    if record["acks"] != expected_acks:
        raise ValueError("local acknowledgement drift")
    if (type(record["evidence_digest"]) is not str
            or re.fullmatch(r"[0-9a-f]{64}", record["evidence_digest"]) is None):
        raise ValueError("local evidence digest shape")
    try:
        evidence = (_json(_read_at(fd, "evidence-" + record["evidence_digest"] + ".json"))
                    if tier == 2 else {"acks": expected_acks})
    except FileNotFoundError:
        raise ValueError("existing tier evidence missing") from None
    if tier == 2:
        _check_evidence(evidence, curriculum["T2"]["minimum_distinct_cycles"])
    if record["evidence_digest"] != _digest(evidence):
        raise ValueError("local evidence digest drift")
    return tier


def grant_local(tier: int, *, context: TierContext, ack: str, evidence: bytes | None = None) -> None:
    """Explicit operator action only; no runtime consumer calls this writer."""
    if type(tier) is not int or tier not in (1, 2):
        raise TierRefused(2, "local_tier_only")
    fd = -1
    try:
        if context.now is not None and (type(context.now) is not int or context.now < 0):
            raise ValueError("invalid local time")
        curriculum = local_curriculum()
        if type(ack) is not str or ack != curriculum[f"T{tier}"]["ack"]:
            raise ValueError("ack mismatch")
        normalized = normalize_cycle_evidence(evidence) if tier == 2 else None
        if tier == 2:
            _check_evidence(normalized, curriculum["T2"]["minimum_distinct_cycles"])
        fd = _open_directory(Path(context.state_dir) / "unlock", create=True)
        try:
            previous = _local_tier(fd, curriculum)
        except FileNotFoundError:
            previous = 0
        if (tier == 2 and previous < 1) or previous > tier:
            raise ValueError("local transition refused")
        acks = {str(n): _digest(curriculum[f"T{n}"]["ack"]) for n in range(1, tier + 1)}
        if normalized is not None:
            # Content-addressed generations keep the old receipt usable if
            # writing/replacing the new receipt fails. Orphans have no authority.
            _write_at(fd, "evidence-" + _digest(normalized) + ".json", _canonical(normalized))
        record = {"schema": _LOCAL_SCHEMA, "tier": tier, "evidence_digest": _digest(
            normalized if normalized is not None else {"acks": acks}),
            "ts": int(time.time()) if context.now is None else context.now, "acks": acks}
        _write_at(fd, "tier.json", _canonical(record))
    except (OSError, ValueError, TypeError, RecursionError):
        raise TierRefused(tier, "local_grant_refused") from None
    finally:
        if fd >= 0:
            os.close(fd)


def _context() -> TierContext:
    # Identity is not inferred from untrusted token claims. Release provisioning
    # must supply trusted explicit context for T3+; this default cannot do so.
    root = Path(__file__).resolve().parents[1]
    state = os.environ.get("AGI_V8_STATE_DIR") or os.environ.get("AGI_STATE_DIR")
    return TierContext(Path(state) if state else root / "state")


def require_tier(tier: int, *, context: TierContext | None = None) -> None:
    if not enabled():
        return
    if type(tier) is not int or not 0 <= tier <= 9:
        raise TierRefused(9, "invalid_required_tier")
    if tier == 0:
        return
    if context is None:
        context = _ACTIVE_CONTEXT.get()
    context = _context() if context is None else context
    fd = -1
    try:
        if tier <= 2:
            bypass = os.environ.get("AGI_V8_I_HAVE_READ_THE_SOURCE", "")
            if (re.fullmatch(r"[0-9a-f]{40}", context.repo_commit) is not None
                    and bypass == context.repo_commit):
                return
            fd = _open_directory(Path(context.state_dir) / "unlock")
            if _local_tier(fd, local_curriculum()) < tier:
                raise TierRefused(tier, "local_tier_insufficient")
            return
        from agi_v8_1.policy.tier_token import parse_token, verify_token, TokenRefused
        fd = _open_directory(Path(context.state_dir) / "unlock")
        token = parse_token(_read_at(fd, "token.json", limit=8192))
        try:
            claims = verify_token(token, issuer=context.issuer, user_id=context.user_id,
                                  repo_commit=context.repo_commit, evidence_digest=context.evidence_digest,
                                  now=int(time.time()) if context.now is None else context.now)
        except TokenRefused as exc:
            raise TierRefused(tier, format_exception_for_sink(exc)) from None
        if claims["tier"] < tier:
            raise TierRefused(tier, "signed_tier_insufficient")
        if tier == 9 and (context.t9_prerequisites is None or any(
                context.t9_prerequisites.get(key) is not True for key in _T9_FACTS)):
            raise TierRefused(9, "runtime_prerequisites_missing")
    except TierRefused:
        raise
    except (OSError, ValueError, TypeError, RecursionError):
        raise TierRefused(tier, "state_or_evidence_invalid") from None
    finally:
        if fd >= 0:
            os.close(fd)


def import_token(raw: bytes, *, context: TierContext) -> None:
    """Accept a signed token privately. Never accepts/prints a payload key."""
    from agi_v8_1.policy.tier_token import parse_token, verify_token, TokenRefused
    fd = -1
    try:
        token = parse_token(raw)
        now = int(time.time()) if context.now is None else context.now
        incoming = verify_token(token, issuer=context.issuer, user_id=context.user_id,
                                repo_commit=context.repo_commit, evidence_digest=context.evidence_digest,
                                now=now)
        fd = _open_directory(Path(context.state_dir) / "unlock", create=True)
        try:
            old_raw = _read_at(fd, "token.json", limit=8192)
        except FileNotFoundError:
            old_raw = None
        if old_raw is not None:
            old = parse_token(old_raw)
            claims = old.get("claims")
            if type(claims) is not dict:
                raise ValueError("prior token shape")
            old_time = now
            issued, expiry = claims.get("issued_at"), claims.get("expiry")
            if type(issued) is int and type(expiry) is int and 0 <= issued < expiry <= now:
                old_time = expiry - 1
            # A previously acquired tier survives expiry/release rotation. The
            # old signature authenticates its old digest/release, while subject
            # and issuer remain bound to the current trusted caller. This check
            # only prevents a downgrade; it never admits execution on old data.
            prior = verify_token(old, issuer=context.issuer, user_id=context.user_id,
                                 repo_commit=claims.get("repo_commit"),
                                 evidence_digest=claims.get("evidence_digest"), now=old_time)
            if incoming["tier"] < prior["tier"]:
                raise ValueError("acquired tier cannot regress")
        _write_at(fd, "token.json", _canonical(token))
    except (OSError, ValueError, TypeError, RecursionError, TokenRefused):
        raise TierRefused(3, "token_import_refused") from None
    finally:
        if fd >= 0:
            os.close(fd)
