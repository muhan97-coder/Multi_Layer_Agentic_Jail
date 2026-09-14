"""Offline public T0..T2 CLI; no environment discovery or operating entry.

``python -m agi_v8_1.runtime.free_tier_cli curriculum`` prints the canonical
acknowledgements. Other commands require ``--state-dir /absolute/private/dir``
BEFORE the command: init, status, preview, unlock, cycle, evidence, ledger.
Unlock requires ``--tier {1,2} --ack TEXT``. T2 consumes this state's observed
cycle ledger unless ``--evidence-file /absolute/private/file`` is explicit.
Cycle requires ``--cycle-id ID --objective TEXT`` and is proposal-only.
Each cycle first checks the canonical human HALT sentinel. This entry admission
check preserves privileged/weak stop semantics; it is not OS-level enforcement.

Use a fresh process with no inherited AGI_/TMI_ variables. Only this invocation
enables the public gate and binds the audit state; no .env file is read. Library
main() calls restore their environment, but must not overlap operating threads.
This curriculum is inspectable local evidence, not DRM or benchmark evidence.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import threading

_LIMIT = 1048576
_LEDGER = "public-cycles.jsonl"
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\Z")
_ENV_LOCK = threading.Lock()


class _Refused(ValueError):
    """Fixed public error code, never an exception or argument echo."""


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise _Refused("arguments_invalid")


class _Discard:
    def write(self, value):
        return len(value)

    def flush(self):
        return None


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(description=__doc__.splitlines()[0], allow_abbrev=False)
    parser.add_argument("--state-dir", type=str, help="explicit absolute private state directory")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("curriculum", "init", "status", "preview", "evidence", "ledger"):
        commands.add_parser(name, allow_abbrev=False)
    unlock = commands.add_parser("unlock", allow_abbrev=False)
    unlock.add_argument("--tier", required=True, choices=(1, 2), type=int)
    unlock.add_argument("--ack", required=True)
    unlock.add_argument("--evidence-file")
    cycle = commands.add_parser("cycle", allow_abbrev=False)
    cycle.add_argument("--cycle-id", required=True)
    cycle.add_argument("--objective", required=True)
    return parser


def _absolute(raw: str | None) -> Path:
    if (not raw or len(raw) > 4096 or any(ord(char) < 32 or ord(char) == 127 for char in raw)
            or not raw.startswith("/") or raw.startswith("//")
            or raw == "/" or str(Path(raw)) != raw
            or any(part in (".", "..") for part in raw.split("/"))):
        raise _Refused("absolute_path_required")
    return Path(raw)


@contextmanager
def _offline(state: Path | None):
    if not _ENV_LOCK.acquire(blocking=False):
        raise _Refused("invocation_busy")
    additions = {}
    try:
        if any(name.startswith(("AGI_", "TMI_")) for name in os.environ):
            raise _Refused("clean_environment_required")
        additions = {"AGI_V8_PUBLIC_TIER_GATE_ENABLED": "true", "AGI_V8_ENABLED": "true"}
        if state is not None:
            additions["AGI_V8_STATE_DIR"] = str(state)
        os.environ.update(additions)
        yield
    finally:
        for name in additions:
            os.environ.pop(name, None)
        _ENV_LOCK.release()


def _local_tier(state: Path, gate) -> int:
    try:
        fd = gate._open_directory(state / "unlock")
    except FileNotFoundError:
        return 0
    try:
        tier = gate._local_tier(fd, gate.local_curriculum())
        gate.require_tier(tier, context=gate.TierContext(state))
        return tier
    finally:
        os.close(fd)


def _ledger(fd: int, gate) -> tuple[bytes, dict]:
    try:
        raw = gate._read_at(fd, _LEDGER, limit=_LIMIT)
    except FileNotFoundError:
        return b"", {"cycles": []}
    normalized = gate.normalize_cycle_evidence(raw)
    # This convenience ledger contains only our observed, minimal end records.
    # External production files remain available through --evidence-file.
    rows = [gate._json(line) for line in raw.splitlines() if line.strip()]
    if len(rows) != len(normalized["cycles"]):
        raise _Refused("ledger_invalid")
    for row in rows:
        if (set(row) != {"event_type", "cycle_id", "payload"}
                or row["event_type"] != "cycle_end"
                or _ID.fullmatch(row["cycle_id"]) is None
                or row["payload"] != {"status": "advisory_stub"}):
            raise _Refused("ledger_invalid")
    return raw, normalized


def _summary(normalized: dict) -> dict:
    counts = {}
    for row in normalized["cycles"]:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return {"distinct_cycles": len(normalized["cycles"]), "statuses": counts,
            "benchmark_evidence": False}


def _private_audit_directory(state: Path, fd: int, gate) -> None:
    # The canonical executor audit is default-ON even in advisory mode. Bind
    # and validate its real shared sink before that consumer can write.
    try:
        os.mkdir("runtime_logs", 0o700, dir_fd=fd)
    except FileExistsError:
        pass
    audit_fd = gate._open_directory(state / "runtime_logs")
    try:
        for name in ("executor_log.jsonl", "executor_log.jsonl.lock"):
            try:
                gate._read_at(audit_fd, name, limit=_LIMIT)
            except FileNotFoundError:
                continue
    finally:
        os.close(audit_fd)


def _cycle(args, state: Path, fd: int, gate) -> dict:
    from agi_v8_1.runtime.halt_sentinel import HumanHalt, raise_if_halted
    try:
        raise_if_halted()
    except HumanHalt:
        # HumanHalt deliberately inherits BaseException. Translate only this
        # canonical stop, without echoing its sentinel location or claiming an
        # unreadable stop was an observed human action in the durable ledger.
        raise _Refused("human_halt") from None
    gate.require_tier(1, context=gate.TierContext(state))
    if (_ID.fullmatch(args.cycle_id) is None or not args.objective.strip()
            or len(args.objective) > 2000):
        raise _Refused("cycle_input_invalid")
    raw, normalized = _ledger(fd, gate)
    if any(row["cycle_id"] == args.cycle_id for row in normalized["cycles"]):
        raise _Refused("cycle_already_used")
    row = {"event_type": "cycle_end", "cycle_id": args.cycle_id,
           "payload": {"status": "advisory_stub"}}
    line = gate._canonical(row) + b"\n"
    if len(raw) + len(line) + 1 > _LIMIT:
        raise _Refused("ledger_full")
    # Refuse prior, partial and symlinked cycles before touching the audit sink.
    try:
        os.stat("cycle-" + args.cycle_id, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        raise _Refused("cycle_already_used")
    _private_audit_directory(state, fd, gate)
    from agi_v8_1.runtime.public_entry import run_stub_cycle
    observed = run_stub_cycle(state, objective=args.objective, cycle_id=args.cycle_id)
    cycle_fd = gate._open_directory(state / ("cycle-" + args.cycle_id))
    try:
        actual = gate.normalize_cycle_evidence(gate._read_at(cycle_fd, "cycle_events.jsonl"))
    finally:
        os.close(cycle_fd)
    if (actual["cycles"] != [{"cycle_id": args.cycle_id, "status": "advisory_stub"}]
            or observed["status"] != "advisory_stub" or observed["applied"] is not False
            or observed["benchmark_evidence"] is not False):
        raise _Refused("cycle_evidence_invalid")
    # Publish only after the canonical producer and event normalization succeed.
    # A failed write does not report success, and its reserved ID stays consumed.
    gate._write_at(fd, _LEDGER, raw.rstrip(b"\n") + (b"\n" if raw else b"") + line)
    return {"status": "advisory_stub", "applied": False, "benchmark_evidence": False,
            "proposal_count": len(observed["proposal"]["proposed_changes"]),
            "distinct_cycles": len(normalized["cycles"]) + 1}


def _execute(args, state: Path | None) -> dict:
    # No real consumer is imported until the clean public environment is bound.
    from agi_v8_1.policy import tier_gate as gate
    if args.command == "curriculum":
        return gate.local_curriculum()
    fd = gate._open_directory(state, create=args.command == "init")
    try:
        write = args.command in {"init", "unlock", "cycle"}
        fcntl.flock(fd, (fcntl.LOCK_EX if write else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        context = gate.TierContext(state)
        if args.command == "init":
            return {"status": "ok", "initialized": True, "gate_enabled": True,
                    "local_tier": _local_tier(state, gate)}
        if args.command == "status":
            return {"status": "ok", "local_tier": _local_tier(state, gate),
                    "gate_enabled": True, "providers_enabled": False, "proposal_only": True}
        if args.command == "preview":
            from agi_v8_1.runtime.public_entry import tick_preview
            return tick_preview(state)
        if args.command == "unlock":
            evidence = None
            if args.tier == 1 and args.evidence_file is not None:
                raise _Refused("evidence_not_used_at_t1")
            if args.tier == 2:
                if args.evidence_file is None:
                    evidence, _ = _ledger(fd, gate)
                else:
                    path = _absolute(args.evidence_file)
                    source = gate._open_directory(path.parent, private=False)
                    try:
                        evidence = gate._read_at(source, path.name)
                    finally:
                        os.close(source)
            gate.grant_local(args.tier, context=context, ack=args.ack, evidence=evidence)
            gate.require_tier(args.tier, context=context)
            return {"status": "ok", "local_tier": args.tier, "gate_enabled": True}
        if args.command == "cycle":
            return _cycle(args, state, fd, gate)
        if args.command == "ledger":
            gate.require_tier(2, context=context)
        _, normalized = _ledger(fd, gate)
        return {"status": "ok", "read_only": True, **_summary(normalized)}
    except gate.TierRefused as exc:
        # Exceptions select constant public outcomes; their payload is never
        # copied into another exception or output. Exact types also avoid
        # invoking hostile subclasses' attribute/equality hooks here.
        if type(exc) is gate.TierRefused and type(exc.tier) is int:
            if exc.tier == 0:
                raise _Refused("tier_refused", 0) from None
            if exc.tier == 1:
                raise _Refused("tier_refused", 1) from None
        raise _Refused("tier_refused", 2) from None
    finally:
        os.close(fd)


def main(argv: list[str] | None = None) -> int:
    """Return 0 on success, 2 on refusal; never print input or exception text."""
    try:
        try:
            args = _parser().parse_args(argv)
        except SystemExit as exc:
            if type(exc) is SystemExit and type(exc.code) is int and exc.code == 0:
                return 0
            return 2
        state = _absolute(args.state_dir) if args.command != "curriculum" else None
        if args.command == "curriculum" and args.state_dir is not None:
            raise _Refused("state_not_used_by_curriculum")
        with _offline(state), redirect_stdout(_Discard()), redirect_stderr(_Discard()):
            result = _execute(args, state)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))
        return 0
    except _Refused as exc:
        # Keep the literal allowlist at this output boundary. Even a malformed
        # internal _Refused must not echo arbitrary .args or call its values'
        # __str__/__eq__ methods. A validated payload controls a branch only;
        # every emitted reason and document pointer is a reviewed constant.
        result = {"status": "refused", "reason": "input_or_state_refused"}
        if type(exc) is _Refused and len(exc.args) == 1 and type(exc.args[0]) is str:
            if exc.args[0] == "arguments_invalid":
                result["reason"] = "arguments_invalid"
            elif exc.args[0] == "absolute_path_required":
                result["reason"] = "absolute_path_required"
            elif exc.args[0] == "invocation_busy":
                result["reason"] = "invocation_busy"
            elif exc.args[0] == "clean_environment_required":
                result["reason"] = "clean_environment_required"
            elif exc.args[0] == "ledger_invalid":
                result["reason"] = "ledger_invalid"
            elif exc.args[0] == "human_halt":
                result["reason"] = "human_halt"
            elif exc.args[0] == "cycle_input_invalid":
                result["reason"] = "cycle_input_invalid"
            elif exc.args[0] == "cycle_already_used":
                result["reason"] = "cycle_already_used"
            elif exc.args[0] == "ledger_full":
                result["reason"] = "ledger_full"
            elif exc.args[0] == "cycle_evidence_invalid":
                result["reason"] = "cycle_evidence_invalid"
            elif exc.args[0] == "evidence_not_used_at_t1":
                result["reason"] = "evidence_not_used_at_t1"
            elif exc.args[0] == "state_not_used_by_curriculum":
                result["reason"] = "state_not_used_by_curriculum"
        elif (type(exc) is _Refused and len(exc.args) == 2
              and type(exc.args[0]) is str and exc.args[0] == "tier_refused"
              and type(exc.args[1]) is int):
            if exc.args[1] == 0:
                result = {"status": "refused", "reason": "tier_refused", "doc_pointer": "Plz_ReadMe.md §T0"}
            elif exc.args[1] == 1:
                result = {"status": "refused", "reason": "tier_refused", "doc_pointer": "Plz_ReadMe.md §T1"}
            elif exc.args[1] == 2:
                result = {"status": "refused", "reason": "tier_refused", "doc_pointer": "Plz_ReadMe.md §T2"}
        print(json.dumps(result, sort_keys=True))
        return 2
    except (Exception, SystemExit):  # Only parser help may return a successful exit.
        # Includes canonical TierRefused (PermissionError). No potentially
        # identifying paths, objectives, tokens or lower-level exception text.
        # Unexpected consumer SystemExit must not escape and print its payload.
        print(json.dumps({"status": "refused", "reason": "input_or_state_refused"}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
