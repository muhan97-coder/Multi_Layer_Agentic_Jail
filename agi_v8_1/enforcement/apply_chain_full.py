# R25 W4 (ported from v7.1 core/auto_apply_audit_chain.py __RPLAN_R12_S6__)
"""Hash-chained audit log for V8 auto-apply events — full superset.

Superset of ``agi_v8_1.core.apply_chain``. The R18 chain keeps the backward
compatible API; this module ships the fully enriched form:

  * ``entry_hash`` field stored on each record.
  * ``ts`` field auto-injected (record creation wall-clock).
  * ``compute_entry_hash`` excludes ``entry_hash`` from canonical form so
    the record's stored hash is the hash of the record minus that field.
  * Critical section around tail-read → hash → append, serialised by a
    sidecar lock file (``chain.jsonl.chain`` flock). Prevents two
    processes from reading the same tail and appending sibling records
    with identical ``prev_hash`` values.
  * Tail-only ``_read_last_hash`` scans backward to the final complete JSONL
    record, verifies its stored ``entry_hash``, and refuses to append after a
    corrupt tail.

R12 invariants preserved:
  - sha256 of canonical JSON (sort_keys=True, separators=(",", ":"),
    ensure_ascii=False)
  - genesis sentinel = ``"0" * 64``
  - ``verify_chain`` walks from genesis and reports the first broken idx

V8 storage: reuses ``agi_v8_1.state.store.atomic_append_jsonl``. The file
lock falls back to a no-op manager on platforms without ``fcntl`` (Windows
test rigs) — the lock is a *performance / safety* guarantee, not a
correctness one when only one writer is active.

Round-5 §12 additions (ADV-R5-017 / ADV-R5-018, 2026-08-17):

  * §12-a — ``compute_entry_hash`` excludes ``seq`` from the hash (see
    ``_HASH_EXCLUDED_FIELDS``; unchanged, required by the live seed chain's
    formula). That means the hash alone does NOT detect a ``seq`` tamper.
    ``AGI_V8_APPLY_CHAIN_SEQ_VERIFY`` (default ``"1"``) makes ``append``
    auto-assign a monotonic ``seq`` and makes ``verify_chain`` independently
    check ``seq`` continuity as a second, hash-independent invariant. A
    ``seq`` key that IS present but is not a plain ``int`` (str/float/bool/
    null — reachable only by tampering or a writer bug, never by a genuine
    legacy record that simply lacks the key) is rejected outright, not
    silently skipped as legacy. The first int ``seq`` seen is anchored to
    ``0``, which also catches a *uniform* shift of every ``seq`` by the same
    constant (plain continuity alone does not — see ``verify_chain``'s
    docstring for that mitigation's honest limits). ``"0"`` restores
    byte-identical pre-Round-5 behaviour (no seq is assigned or checked).
  * §12-b — ``append``/``verify_chain`` only ever record an event AFTER it
    already happened; a crash between "mutation applied" and "chain append"
    leaves no trace of the mutation (same crash-window-B shape as Round-3
    T1). ``begin_intent`` / ``commit`` / ``find_orphaned_intents`` are
    additive write-ahead primitives for a caller that wants two-phase
    logging; nothing in this module's own call sites uses them yet — no
    mutation site was rewired in this track (``self_improvement_v8.py`` is
    off-limits here), so this ships primitives only.
  * §12-c — there is no external anchor. Any principal with write access to
    the whole chain file can recompute every ``entry_hash``/``prev_hash``
    from a rewritten history; the hash chain only proves *internal*
    consistency (no entry was edited without also rewriting everything
    after it), not that the file wasn't wholesale replaced. Introducing a
    signature or external notary is out of scope for this track.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

try:  # POSIX file lock; absent on Windows
    import fcntl as _fcntl  # type: ignore
    _HAS_FCNTL = True
except ImportError:  # pragma: no cover
    _fcntl = None  # type: ignore
    _HAS_FCNTL = False

from agi_v8_1.state.store import atomic_append_jsonl

# __SLOT_FAIL_FAST_2026_07_25__ Swallowed failures route through one choke
# point: counted + named always, re-raised under AGI_V8_STRICT_FAIL_FAST.
from agi_v8_1.policy.fail_fast import swallowed as _swallowed

SCHEMA_VERSION = "agi_v8_apply_chain_full_v1"
GENESIS_PREV_HASH = "0" * 64
# __SLOT_W1A1__  W1.A1 hash-formula reconciliation: kept in sync with
# core/apply_chain.py. Live seed evidence_promotion_chain.jsonl excludes
# {entry_hash, seq} from the canonical form. See W3.L4 §2/F1.
_HASH_EXCLUDED_FIELDS = frozenset({"entry_hash", "seq"})  # __SLOT_W1A1__

# __SLOT_ADV_R5_018__ §12-a gate: default ON (security-fix exception — see
# repo rule "새 증분/기능은 전부 default-OFF", this is the documented carve-out
# for a security repair). "0"/"false" restores byte-identical pre-Round-5
# behaviour: no ``seq`` auto-assignment in ``append``, no seq-continuity
# check in ``verify_chain``. Does NOT change the hash formula (see
# ``_HASH_EXCLUDED_FIELDS`` above — that exclusion is load-bearing for the
# live seed chain and stays as-is regardless of this gate).
_SEQ_VERIFY_ENV = "AGI_V8_APPLY_CHAIN_SEQ_VERIFY"


def _seq_verify_enabled() -> bool:
    return os.environ.get(_SEQ_VERIFY_ENV, "1") in ("1", "true")


def _canonicalize(record: dict[str, Any]) -> str:
    """Canonical JSON for hashing (sort_keys, no whitespace, UTF-8)."""
    return json.dumps(
        record, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def compute_entry_hash(record: dict[str, Any]) -> str:
    """sha256 of canonical form, excluding ``entry_hash`` and ``seq``.

    Deterministic across dict insertion order. ``entry_hash`` is excluded so
    the stored hash is the hash of "the record minus the hash" (no
    self-reference fixpoint). ``seq`` is excluded to match the original seed
    writer's formula — it is positional bookkeeping.

    ⚠️ Correction (ADV-R5-018, 2026-08-17): earlier revisions of this
    docstring claimed "any tamper of seq still breaks the prev_hash
    linkage" — that is FALSE. Because ``seq`` is excluded from this hash,
    changing only a record's ``seq`` does not change its ``entry_hash`` and
    therefore does not change the next record's ``prev_hash`` either; the
    tamper is invisible to this function and to the chain-linkage check
    alone. Detecting a ``seq`` tamper requires the independent seq-continuity
    check ``verify_chain`` runs under ``AGI_V8_APPLY_CHAIN_SEQ_VERIFY``
    (default ON; see that gate's docstring above and module docstring §12-a).
    """
    record_for_hash = {
        k: v for k, v in record.items() if k not in _HASH_EXCLUDED_FIELDS  # __SLOT_W1A1__
    }
    return hashlib.sha256(
        _canonicalize(record_for_hash).encode("utf-8")
    ).hexdigest()


@contextmanager
def _file_lock(lock_path: Path) -> Iterator[None]:
    """Cross-process advisory file lock; no-op fallback if fcntl missing."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if not _HAS_FCNTL:
        # No-op fallback (correctness preserved for single-writer test rigs)
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


def _resolve_chain_path(state_dir: Optional[Path] = None) -> Path:
    """Resolve chain JSONL path. Default: state/auto_apply_chain.jsonl."""
    if state_dir is None:
        env_dir = os.environ.get("AGI_V8_STATE_DIR") or os.environ.get(
            "AGI_STATE_DIR"
        )
        if env_dir:
            state_dir = Path(env_dir)
        else:
            # agi_v8_1/enforcement/apply_chain_full.py -> agi_v8_1/state
            state_dir = Path(__file__).resolve().parent.parent / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / "auto_apply_chain.jsonl"


def _read_last_record(chain_path: Path) -> dict[str, Any] | None:
    """Read the final non-blank JSONL record by scanning backward."""
    if not chain_path.exists() or chain_path.stat().st_size == 0:
        return None

    with chain_path.open("rb") as f:
        f.seek(0, 2)
        pos = f.tell()
        if pos == 0:
            return None

        buffer = b""
        while pos > 0:
            read_size = min(8192, pos)
            pos -= read_size
            f.seek(pos)
            buffer = f.read(read_size) + buffer
            stripped = buffer.rstrip(b"\r\n")
            if not stripped:
                continue

            line_start = stripped.rfind(b"\n")
            if line_start != -1 or pos == 0:
                raw_line = stripped[line_start + 1 :] if line_start != -1 else stripped
                try:
                    record = json.loads(raw_line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ValueError(
                        f"malformed final apply-chain record: {chain_path}"
                    ) from exc
                if not isinstance(record, dict):
                    raise ValueError(
                        f"final apply-chain record is not an object: {chain_path}"
                    )
                return record

    return None


def _read_last_hash(chain_path: Path) -> str:
    """Read and verify the last record's stored ``entry_hash``.

    Returns the genesis sentinel when the chain is empty. A corrupt or
    incomplete tail raises ``ValueError`` so appends cannot silently fork the
    chain from genesis or from a stale predecessor hash.
    """
    last = _read_last_record(chain_path)
    if last is None:
        return GENESIS_PREV_HASH

    stored_hash = last.get("entry_hash")
    recomputed = compute_entry_hash(last)
    if stored_hash != recomputed:
        raise ValueError(
            f"final apply-chain record entry_hash mismatch: {chain_path}"
        )
    return str(stored_hash)


def append(
    record: dict[str, Any],
    state_dir: Optional[Path] = None,
    *,
    chain_path: Optional[Path] = None,
) -> str:
    """Append a record to the audit chain.

    Augments ``record`` with ``prev_hash`` (current tail) and ``ts`` (wall
    clock) if not already set, then computes ``entry_hash`` over the
    canonical form (minus the entry_hash field) and stores it on the record.

    Returns the new ``entry_hash`` so callers can correlate the event id to
    downstream stores.

    The read-hash-append section is wrapped in an advisory file lock so
    concurrent writers cannot produce sibling records with identical
    ``prev_hash`` values.

    Also mutates ``record`` in place (sets ``prev_hash``, ``ts``,
    ``schema_version``, ``entry_hash``) so callers that need the augmented
    fields after the call (e.g. for the predecessor's ``prev_hash``) can read
    them straight from the dict they passed in.

    # __SLOT_W2A2__ — W2.A2 migration: additive ``chain_path`` kwarg lets the
    # SI caller keep its on-disk filename (``apply_chain.jsonl``) while moving
    # off the weaker ``core.apply_chain``. When ``chain_path`` is None the
    # original resolver runs (env or ``state_dir/auto_apply_chain.jsonl``), so
    # existing callers/tests that pass only ``state_dir`` are unaffected.
    """
    # __SLOT_W2A2__  chain_path override resolves first; fall back to legacy
    # state_dir resolution to preserve backward compat.
    if chain_path is not None:
        chain_path = Path(chain_path)
        chain_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        chain_path = _resolve_chain_path(state_dir)
    lock_path = chain_path.with_suffix(chain_path.suffix + ".chain")
    with _file_lock(lock_path):
        last_record = _read_last_record(chain_path)
        if last_record is None:
            prev_hash = GENESIS_PREV_HASH
            last_seq: Any = None
        else:
            stored_hash = last_record.get("entry_hash")
            recomputed = compute_entry_hash(last_record)
            if stored_hash != recomputed:
                raise ValueError(
                    f"final apply-chain record entry_hash mismatch: {chain_path}"
                )
            prev_hash = str(stored_hash)
            last_seq = last_record.get("seq")

        record["prev_hash"] = prev_hash
        # __SLOT_ADV_R5_018__ gate ON: auto-assign a monotonic seq (caller
        # may pre-set "seq" to opt out of auto-assignment either way). Gate
        # OFF: byte-identical to pre-Round-5 — no seq key is touched here.
        if _seq_verify_enabled() and "seq" not in record:
            record["seq"] = (last_seq + 1) if isinstance(last_seq, int) else 0
        record.setdefault("ts", time.time())
        record.setdefault("schema_version", SCHEMA_VERSION)

        entry_hash = compute_entry_hash(record)
        record["entry_hash"] = entry_hash

        atomic_append_jsonl(chain_path, record)
        return entry_hash


def begin_intent(
    record: dict[str, Any],
    state_dir: Optional[Path] = None,
    *,
    chain_path: Optional[Path] = None,
) -> str:
    """§12-b write-ahead half 1/2: append an ``event_phase="intent"`` record.

    Call this BEFORE the mutation ``record`` describes actually runs, then
    call ``commit`` with the returned ``txn_id`` AFTER the mutation
    succeeds. Reuses ``append`` unchanged (inherits its fsync-before-return
    durability from ``atomic_append_jsonl`` — no new fsync code needed).

    Additive-only: nothing in this module calls this yet. No mutation site
    is rewired by this track — ``self_improvement_v8.py`` (the real S5
    apply-ladder mutation site, see its module docstring) is off-limits
    here. This ships the primitive; wiring it in front of a real mutation
    is a follow-up for whichever track owns that file.
    """
    txn_id = uuid.uuid4().hex
    record["event_phase"] = "intent"
    record["txn_id"] = txn_id
    append(record, state_dir, chain_path=chain_path)
    return txn_id


def commit(
    txn_id: str,
    record: dict[str, Any],
    state_dir: Optional[Path] = None,
    *,
    chain_path: Optional[Path] = None,
) -> str:
    """§12-b write-ahead half 2/2: append the matching ``"committed"`` record.

    ``txn_id`` must be the value ``begin_intent`` returned for the same
    logical operation. Returns the new record's ``entry_hash``.
    """
    record["event_phase"] = "committed"
    record["txn_id"] = txn_id
    return append(record, state_dir, chain_path=chain_path)


def find_orphaned_intents(chain_path: Path) -> list[dict[str, Any]]:
    """§12-b reconciler: intents with no matching commit. Read-only.

    One forward walk over the chain, grouping records by ``txn_id``.
    Returns every ``event_phase == "intent"`` record for which no later
    ``event_phase == "committed"`` record shares its ``txn_id`` — i.e. a
    transaction that started (and, per the write-ahead discipline, may or
    may not have actually run its mutation before a crash) but never
    confirmed completion.

    Deliberately does NOT mutate the chain file, auto-clear an orphan, or
    replay/undo anything — per the Round-3 T1 crash-window-B prescription,
    an orphaned intent cannot be safely reverted without a real
    idempotency/lease-with-owner-id mechanism (this reconciler alone cannot
    tell "never ran" from "ran, then crashed before commit"); it only
    surfaces the fact for an operator or the owning mutation track to act
    on. Never modifies ``chain_path`` — safe to run against a live chain.
    """
    chain_path = Path(chain_path)
    if not chain_path.exists():
        return []
    intents: dict[str, dict[str, Any]] = {}
    committed_txns: set[str] = set()
    with chain_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as _ff_exc:
                _swallowed(
                    _ff_exc,
                    site="enforcement.apply_chain_full.find_orphaned_intents",
                    category="apply",
                )
                continue
            if not isinstance(record, dict):
                continue
            txn_id = record.get("txn_id")
            if not txn_id:
                continue
            phase = record.get("event_phase")
            if phase == "intent":
                intents[txn_id] = record
            elif phase == "committed":
                committed_txns.add(txn_id)
    return [rec for txn, rec in intents.items() if txn not in committed_txns]


def verify_chain(
    chain_path: Path,
) -> tuple[bool, Optional[int], Optional[str]]:
    """Walk the chain from genesis, verify ``prev_hash`` + ``entry_hash``.

    Returns ``(True, None, None)`` on success. On tamper the second element
    is the zero-based line index of the first broken record; the third is
    normally the hash that *should* have been observed at that position.

    ADV-R5-018 addition: under ``AGI_V8_APPLY_CHAIN_SEQ_VERIFY`` (default
    ON) a ``seq`` tamper is ALSO detected — a second, hash-independent
    invariant (``seq`` is excluded from the hash itself; see
    ``compute_entry_hash``). On a seq break the third element is a
    human-readable reason string instead of a hex hash — callers that treat
    the third element as hex-only should re-check that assumption. This
    check is fail-closed on the *type* of ``seq``: a record with no ``seq``
    key at all is legacy-tolerated (skipped, doesn't reset the running
    counter) so pre-Round-5 chains keep verifying, but a record that HAS a
    ``seq`` key whose value is not a plain ``int`` (string, float, bool,
    null, ...) is rejected as a break — that shape is only reachable by
    tampering or a writer bug, never by a genuine legacy record. The first
    ``int``-typed ``seq`` seen is additionally anchored to ``0`` (what a
    fresh chain's first entry always gets under this gate), which catches a
    *uniform* shift of every ``seq`` by the same constant — plain continuity
    does not, since a uniform shift preserves every adjacent delta. This
    anchor is a partial mitigation, not a general defense: a chain seeded
    with a caller-supplied non-zero starting ``seq`` will legitimately fail
    it, and it says nothing about a uniform shift applied before the first
    int-seq record ever existed. Malformed JSON line is reported the same
    way as a hash break.
    """
    if not chain_path.exists():
        return (True, None, None)

    prev_hash = GENESIS_PREV_HASH
    seq_verify = _seq_verify_enabled()
    last_seq: Any = None
    seq_anchor_checked = False
    with chain_path.open("r", encoding="utf-8") as f:
        for idx, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as _ff_exc:
                _swallowed(_ff_exc, site="enforcement.apply_chain_full.verify_chain", category="apply")
                return (False, idx, prev_hash)
            if not isinstance(record, dict):
                return (False, idx, prev_hash)

            if record.get("prev_hash") != prev_hash:
                return (False, idx, prev_hash)

            stored_hash = record.get("entry_hash")
            recomputed = compute_entry_hash(record)
            if stored_hash != recomputed:
                return (False, idx, recomputed)

            if seq_verify:  # __SLOT_ADV_R5_018__
                has_seq = "seq" in record
                seq = record.get("seq")
                if has_seq and not (isinstance(seq, int) and not isinstance(seq, bool)):
                    return (
                        False,
                        idx,
                        f"seq_type_error: expected int, got "
                        f"{type(seq).__name__}",
                    )
                if has_seq:
                    if not seq_anchor_checked:
                        seq_anchor_checked = True
                        if seq != 0:
                            return (
                                False,
                                idx,
                                f"seq_anchor_mismatch: first seq-bearing "
                                f"entry must start at 0, got {seq}",
                            )
                    if last_seq is not None and seq != last_seq + 1:
                        return (
                            False,
                            idx,
                            f"seq_mismatch: expected {last_seq + 1}, got {seq}",
                        )
                    last_seq = seq

            prev_hash = str(stored_hash)

    return (True, None, None)


__all__ = [
    "SCHEMA_VERSION",
    "GENESIS_PREV_HASH",
    "compute_entry_hash",
    "append",
    "verify_chain",
    "begin_intent",
    "commit",
    "find_orphaned_intents",
]
