"""Bounded public T0 preview and T1 advisory-only, offline SI curriculum.

This entry never starts the autonomous tick runner. A stub requires a clean
process configuration and a new private cycle directory; it cannot inherit an
armed operating environment or apply a proposal. It is not benchmark evidence.
"""
from __future__ import annotations

from dataclasses import asdict
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import re
import stat

_LIMIT = 1024 * 1024
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\Z")


def _open_dir(path: Path) -> int:
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in Path(os.path.abspath(path)).parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                            | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = child
        result, fd = fd, -1
        return result
    finally:
        if fd >= 0:
            os.close(fd)


def _read_rows(fd: int, name: str) -> list[dict]:
    try:
        stream = os.open(name, os.O_RDONLY | os.O_NOFOLLOW
                         | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=fd)
    except FileNotFoundError:
        return []
    try:
        info = os.fstat(stream)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _LIMIT:
            raise ValueError("preview queries type or size")
        with os.fdopen(os.dup(stream), "rb") as source:
            raw = source.read(_LIMIT + 1)
        if len(raw) > _LIMIT:
            raise ValueError("preview queries size")
    finally:
        os.close(stream)
    rows = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if type(row) is not dict:
            raise ValueError("preview query shape")
        rows.append(row)
    return rows


def query_id(row: dict) -> str:
    """Production inbox identity, shared with autonomous tick at attachment."""
    rid = row.get("id")
    if isinstance(rid, str) and rid.strip():
        return rid.strip()
    raw = json.dumps(row, sort_keys=True, ensure_ascii=False)
    return "auto_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def tick_preview(state_dir: Path) -> dict:
    """Bounded inbox minus tombstones; observational, not a dispatch lease."""
    try:
        fd = _open_dir(Path(state_dir) / "tick")
    except FileNotFoundError:
        return {"pending": 0, "reason": "no_state", "read_only": True}
    try:
        inbox = _read_rows(fd, "queries.jsonl")
        consumed = {str(row["id"]) for row in _read_rows(fd, "consumed.jsonl")
                    if row.get("id") is not None}
    finally:
        os.close(fd)
    pending = sum(query_id(row) not in consumed for row in inbox)
    return {"pending": pending, "reason": "pending" if pending else "no_pending",
            "read_only": True}


def _check_offline_environment(state_dir: Path) -> None:
    # Do not toggle process-global flags or enumerate/print their values. Any
    # configured operating knob requires a separate, clean curriculum process.
    allowed = {"AGI_V8_PUBLIC_TIER_GATE_ENABLED", "AGI_V8_STATE_DIR", "AGI_STATE_DIR",
               "AGI_V8_ENABLED"}
    if any(name.startswith(("AGI_", "TMI_")) and name not in allowed
           for name in os.environ):
        raise ValueError("public stub requires a clean offline environment")
    # The canonical executor audit writer resolves its destination from env,
    # even for dry-run SI. Refuse an absent/divergent binding instead of writing
    # beside the checkout or mutating process-global configuration here.
    bound = os.environ.get("AGI_V8_STATE_DIR") or os.environ.get("AGI_STATE_DIR")
    if not bound or Path(os.path.abspath(bound)) != Path(os.path.abspath(state_dir)):
        raise ValueError("public stub requires an explicit matching audit state binding")


def run_stub_cycle(state_dir: Path, *, objective: str, cycle_id: str) -> dict:
    """Execute the canonical SI stub, then derive a real proposer-only ticket.

    The named receipt records an observed advisory outcome, never completed
    work. Reusing a cycle ID is refused before the canonical producer runs.
    """
    if (type(cycle_id) is not str or _ID.fullmatch(cycle_id) is None
            or type(objective) is not str or not objective.strip()
            or len(objective) > 2000):
        raise ValueError("public stub input")
    _check_offline_environment(state_dir)
    scope = nullcontext()
    if os.environ.get("AGI_V8_PUBLIC_TIER_GATE_ENABLED", "false").strip().lower() == "true":
        from agi_v8_1.policy.tier_gate import TierContext, active_tier_context, require_tier
        context = TierContext(Path(state_dir))
        require_tier(1, context=context)
        scope = active_tier_context(context)

    root = Path(os.path.abspath(state_dir))
    parent = _open_dir(root)
    try:
        info = os.fstat(parent)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError("public stub state must be private")
        # Exclusive directory creation also refuses symlinks and prior runs.
        os.mkdir("cycle-" + cycle_id, 0o700, dir_fd=parent)
    finally:
        os.close(parent)
    jail = root / ("cycle-" + cycle_id)

    from agi_v8_1.core.cycle_logger import CycleLogger
    from agi_v8_1.core.messages import DecisionRecord
    from agi_v8_1.policy.secret_masker import mask_obj
    from agi_v8_1.self_improvement_v8 import SelfImprovementV8
    from agi_v8_1.si_lanes.proposer import propose_tickets
    from agi_v8_1.state.store import atomic_write_json

    events = jail / "cycle_events.jsonl"
    with scope:
        observed = SelfImprovementV8(state_dir=jail).run_one_si_cycle(
            cycle_id, objective=objective, cycle_logger=CycleLogger(events))
    if observed.status != "advisory_stub":
        raise RuntimeError("public stub did not produce an advisory outcome")
    decision = DecisionRecord("public-" + cycle_id, 0.0, "decision-" + cycle_id,
                              (), observed.status, "offline curriculum observation")
    tickets = propose_tickets(decisions=(decision,),
                              recent_outcomes=({"status": observed.status},), now_unix=0.0)
    if not tickets or not tickets[0].proposed_changes:
        raise RuntimeError("public stub proposal missing")
    proposal = json.loads(json.dumps(mask_obj(asdict(tickets[0])), allow_nan=False))
    receipt = {"schema": "agi-v8-public-stub/v1", "cycle_id": cycle_id,
               "status": observed.status, "proposal": proposal, "applied": False,
               "benchmark_evidence": False}
    receipt_path = jail / "proposal-receipt.json"
    atomic_write_json(receipt_path, receipt)
    os.chmod(receipt_path, 0o600, follow_symlinks=False)
    return {**receipt, "receipt_path": str(receipt_path), "events_path": str(events)}
