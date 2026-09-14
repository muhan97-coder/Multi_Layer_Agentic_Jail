"""Run free-tier behavior in a fresh payload-zero Python -I copy.

Only synthetic inputs are created. The Python audit hook is an offline test
instrument, not an OS security sandbox or proof against malicious native code.
No passing receipt grants publication, paid dispatch, or upper-tier authority.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from agi_v8_1.capabilities import public_release as release
from agi_v8_1.capabilities.delivery import DeliveryError


RECEIPT_SCHEMA = "agi_v8_1.free_tier_coldstart.v1"
REQUIRED_CHECKS = frozenset({
    "t0_tick", "t0_ledger", "t0_demo", "t1_si",
    "t2_ledger_join", "t2_cost_reconcile", "t2_episode", "t2_gates_map",
    "local_tier_unlock",
    "upper_tier_absence",
    "t0_tick_master_off",
})

_FD_PATH_RESOLVER = r'''
def _fd_path(fd, *, directory=False):
    # This source is embedded verbatim in the isolated child, before its audit
    # hook is installed. Tests execute the same source with injected OS APIs.
    try:
        if type(fd) is not int or fd < 0:
            raise ValueError('invalid_descriptor')
        held = os.fstat(fd)
        if held.st_nlink < 1 or (directory and not stat.S_ISDIR(held.st_mode)):
            raise ValueError('unlinked_or_not_directory')
        if sys.platform == 'linux':
            raw = os.readlink('/proc/self/fd/' + str(fd))
        elif sys.platform == 'darwin':
            # Apple MAXPATHLEN == PATH_MAX == 1024; Python fcntl's bytes
            # interface returns the same-sized buffer and permits <=1024.
            # Use the published constant, never a guessed numeric command.
            if fcntl is None or type(getattr(fcntl, 'F_GETPATH', None)) is not int:
                raise ValueError('getpath_unavailable')
            buffer = fcntl.fcntl(fd, fcntl.F_GETPATH, b'\0' * 1024)
            if type(buffer) is not bytes or len(buffer) != 1024:
                raise ValueError('invalid_getpath_buffer')
            raw, terminator, padding = buffer.partition(b'\0')
            if not terminator or any(padding):
                raise ValueError('unterminated_or_malformed_getpath')
        else:
            raise ValueError('unsupported_descriptor_platform')
        raw = os.fsdecode(raw)
        if not raw or '\0' in raw or not os.path.isabs(raw):
            raise ValueError('invalid_descriptor_path')
        resolved = Path(os.path.realpath(raw, strict=True))
        named, current = os.stat(resolved, follow_symlinks=False), os.fstat(fd)
        identity = lambda value: (value.st_dev, value.st_ino, stat.S_IFMT(value.st_mode))
        if (named.st_nlink < 1 or current.st_nlink < 1
                or not identity(held) == identity(named) == identity(current)):
            raise ValueError('descriptor_path_identity_changed')
        return resolved
    except (OSError, ValueError, TypeError, AttributeError, OverflowError, NotImplementedError):
        # A caught refusal still poisons the final receipt; no cwd fallback.
        refuse('fd_path_unavailable')
def resolved_path(raw, dir_fd=None):
    if isinstance(raw, int):
        return _fd_path(raw)
    raw = os.fsdecode(raw)
    if not os.path.isabs(raw) and dir_fd not in (None, -1):
        raw = os.path.join(_fd_path(dir_fd, directory=True), raw)
    return Path(os.path.realpath(raw))
'''


_BOOTSTRAP = r'''
import json, os, stat, sys
from pathlib import Path
# Import the Darwin extension before installing the import/read audit boundary.
fcntl = None
if sys.platform == 'darwin':
    try:
        import fcntl
    except ImportError:
        fcntl = None
root, original, jail = map(lambda p: Path(p).resolve(), sys.argv[1:4])
writable = [False, 'imports']
effects = []
effect_sites = []
fd_open_checked = [0]
libraries = tuple(Path(p).resolve() for p in {sys.prefix, sys.base_prefix})
def within(path, parent):
    return path == parent or path.is_relative_to(parent)
def refuse(code):
    effects.append(code)
    frame, sites = sys._getframe(1), []
    while frame is not None and len(sites) < 9:
        sites.append({'file': Path(frame.f_code.co_filename).name, 'line': frame.f_lineno})
        frame = frame.f_back
    effect_sites.append(sites)
    raise PermissionError(code)
''' + _FD_PATH_RESOLVER + r'''
def check_open_path(path, writing):
    if within(path, original):
        refuse('original_checkout')
    if path.name == '.env' or path.name.startswith('.env.'):
        refuse('operator_env')
    if writing:
        if not writable[0] or not within(path, jail):
            refuse('write_outside_jail_or_readonly_phase')
    elif not (within(path, root) or within(path, jail)
              or any(within(path, p) for p in libraries)
              or str(path) in {'/etc/localtime', '/dev/null'}
              or within(path, Path('/usr/share/zoneinfo'))):
        refuse('read_outside_delivery')
original_os_open = os.open
def checked_os_open(path, flags, mode=0o777, *, dir_fd=None):
    resolved = resolved_path(path, dir_fd)
    writing = bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
    # Held directory FDs permit no-follow walks; reading/listing or writing
    # through them is checked separately against its resolved destination.
    if writing or not flags & os.O_DIRECTORY:
        check_open_path(resolved, writing)
    fd_open_checked[0] += 1
    try:
        return original_os_open(path, flags, mode, dir_fd=dir_fd)
    finally:
        fd_open_checked[0] -= 1
os.open = checked_os_open
def audit(event, args):
    if event.startswith('socket.') or event in {
                 'subprocess.Popen', 'os.system', 'os.exec', 'os.posix_spawn', 'os.fork', 'os.forkpty'}:
        refuse('external_effect')
    if event == 'open' and args and not fd_open_checked[0]:
        if isinstance(args[0], int) and args[0] in (0, 1, 2):
            return
        path = resolved_path(args[0])
        mode = args[1] if len(args) > 1 else None
        flags = args[2] if len(args) > 2 else 0
        writing = ((isinstance(mode, str) and any(c in mode for c in 'wax+'))
                   or bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)))
        check_open_path(path, writing)
    if event in {'os.listdir', 'os.scandir'} and args:
        path = resolved_path(args[0])
        if within(path, original) or not (path == root.parent or within(path, root) or within(path, jail)
                or any(within(path, p) for p in libraries)):
            refuse('directory_outside_delivery')
    if event in {'os.mkdir', 'os.remove', 'os.rmdir', 'os.rename', 'os.chmod',
                 'os.link', 'os.symlink', 'os.truncate'}:
        paths = args[:2] if event in {'os.rename', 'os.link', 'os.symlink'} else args[:1]
        for index, raw in enumerate(paths):
            if event == 'os.mkdir' or event == 'os.chmod':
                dir_fd = args[2] if len(args) > 2 else None
            elif event in {'os.remove', 'os.rmdir'}:
                dir_fd = args[1] if len(args) > 1 else None
            elif event in {'os.rename', 'os.link'}:
                dir_fd = args[index + 2] if len(args) > index + 2 else None
            else:
                dir_fd = None
            path = resolved_path(raw, dir_fd)
            if not writable[0] or not within(path, jail):
                refuse('write_outside_jail_or_readonly_phase')
sys.addaudithook(audit)
# -I still permits installed .pth files. Drop editable checkout paths instead of
# trusting isolation mode to remove them; library imports remain permitted.
sys.path[:] = [str(root.parent)] + [p for p in sys.path if p and any(
    within(Path(p).resolve(), library) for library in libraries)]
try:
    from agi_v8_1.tools.public_coldstart_check import _exercise
    result = _exercise(root, jail, writable, effects)
except BaseException as error:
    sites, cursor = [], error.__traceback__
    while cursor is not None and len(sites) < 12:
        sites.append({'file': Path(cursor.tb_frame.f_code.co_filename).name,
                      'line': cursor.tb_lineno})
        cursor = cursor.tb_next
    print(json.dumps({'failed_type': type(error).__name__, 'phase': writable[1],
                      'effects': effects, 'sites': sites, 'effect_sites': effect_sites}, sort_keys=True))
    raise SystemExit(1)
print(json.dumps(result, sort_keys=True, separators=(',', ':')))
'''


def _exercise(root, jail, writable, effects):
    """Actual consumers; no provider, SI, or accounting function is replaced."""
    from agi_v8_1.providers.mock import MockModelProvider
    from agi_v8_1.runtime import public_entry, cost_reconcile, episode_ledger, tick_runner
    from agi_v8_1.runtime.ledger_join import row_cycle, row_ts
    from agi_v8_1.tools import gates_map, ledger_join_check
    from agi_v8_1.prompts.loader import PromptLoader
    from agi_v8_1.policy.tier_gate import (
        TierContext, TierRefused, grant_local, local_curriculum, require_tier,
    )
    from agi_v8_1.capabilities.bundles import payload_source_paths
    from agi_v8_1.memory.external_tmi import TMIEndpoint, encode

    checks = {}
    writable[1] = "assets"
    assets = root / "public_assets" / "free_tiers"
    config = json.loads((assets / "config.json").read_text())
    assert config and all(value == "false" for value in config.values())
    os.environ.update(config)
    prompt = PromptLoader(prompts_dir=assets / "prompts").resolve_prompt("strategist")
    assert prompt.status == "available" and prompt.path == assets / "prompts" / "strategist.md"
    assert "Free-tier cold-start candidate" in (assets / "README.md").read_text()

    writable[1] = "t0_tick_master_off"
    stopped = tick_runner.tick_once(jail, now_ts=1700000010)
    assert stopped["dispatched"] is False and stopped["reason"] == "gate_off"
    # The real production HALT metadata check precedes the master gate. We do
    # not replace it or ignore a human HALT to obtain a green cold-start probe.
    assert len(tick_runner.pending_queries(jail)) == 1
    checks["t0_tick_master_off"] = {"passed": True, "observed": 1}
    writable[1] = "t0_tick"
    preview = public_entry.tick_preview(jail)
    assert preview["pending"] == 1 and preview["read_only"] is True
    checks["t0_tick"] = {"passed": True, "observed": 1}
    writable[1] = "t0_ledger"
    survey = ledger_join_check.survey(jail)
    assert survey["ledger_files"] >= 3 and survey["rows_total"] >= 3
    checks["t0_ledger"] = {"passed": True, "observed": survey["rows_total"]}
    writable[1] = "t0_demo"
    demo = json.loads(MockModelProvider().generate("strategist", prompt.text, {
        "goal_card": {"id": "cold-demo", "user_objective": "Inspect a synthetic example",
                      "success_criteria": ["Return an inspectable plan"], "constraints": []},
    }, {}))
    assert demo["agent"] == "strategist" and len(demo["tasks"]) >= 1
    checks["t0_demo"] = {"passed": True, "observed": len(demo["tasks"])}

    writable[0] = True
    os.environ.pop("AGI_V8_PROVIDERS_ENABLED", None)
    os.environ.pop("AGI_V8_SWARM_ENABLED", None)
    os.environ["AGI_V8_ENABLED"] = "true"
    os.environ["AGI_V8_STATE_DIR"] = str(jail)
    os.environ["AGI_V8_PUBLIC_TIER_GATE_ENABLED"] = "true"
    writable[1] = "local_tier_unlock"
    context = TierContext(jail)
    try:
        require_tier(1, context=context)
    except TierRefused:
        pass
    else:
        raise AssertionError("unearned local tier was admitted")
    curriculum = local_curriculum()
    grant_local(1, context=context, ack=curriculum["T1"]["ack"])
    require_tier(1, context=context)
    receipt_bytes, cycle_evidence = [], []
    for cycle_id in ("cold-si-cycle-1", "cold-si-cycle-2"):
        writable[1] = "t1_si"
        proposal = public_entry.run_stub_cycle(jail, objective="Propose one synthetic example",
                                               cycle_id=cycle_id)
        assert proposal["applied"] is False and proposal["cycle_id"] == cycle_id
        assert isinstance(proposal["proposal"], dict) and proposal["proposal"]
        receipt_path, events_path = Path(proposal["receipt_path"]), Path(proposal["events_path"])
        assert all(p.is_absolute() and p.is_relative_to(jail) for p in (receipt_path, events_path))
        stored = json.loads(receipt_path.read_text())
        assert stored["cycle_id"] == cycle_id and stored["applied"] is False
        assert stored["proposal"] == json.loads(json.dumps(proposal["proposal"]))
        receipt_bytes.append(receipt_path.read_bytes())
        cycle_evidence.append(events_path.read_bytes())
        try:
            public_entry.run_stub_cycle(jail, objective="Propose one synthetic example", cycle_id=cycle_id)
        except FileExistsError:
            pass
        else:
            raise AssertionError("duplicate cycle was not refused")
        assert receipt_path.read_bytes() == receipt_bytes[-1]
        assert events_path.read_bytes() == cycle_evidence[-1]
    checks["t1_si"] = {"passed": True, "observed": len(receipt_bytes),
                       "receipt_sha256": hashlib.sha256(b"\n".join(receipt_bytes)).hexdigest()}
    writable[1] = "local_tier_unlock"
    grant_local(2, context=context, ack=curriculum["T2"]["ack"],
                evidence=b"\n".join(cycle_evidence))
    require_tier(2, context=context)
    checks["local_tier_unlock"] = {"passed": True, "observed": 2}
    writable[0] = False

    writable[1] = "t2_ledger_join"
    joined = ledger_join_check.join_cycle(jail, "cold-accounting-cycle")
    assert joined["ledgers_hit"] == 2 and joined["rows_total"] == 2
    assert row_cycle({"cycle_id": "cold-accounting-cycle"}) == "cold-accounting-cycle"
    assert row_ts({"ts": 1700000000}) == 1700000000
    checks["t2_ledger_join"] = {"passed": True, "observed": joined["ledgers_hit"]}
    writable[1] = "t2_cost_reconcile"
    accounting = cost_reconcile.reconcile(jail, dashboard_usd=0.15)
    assert accounting["internal_total_usd"] == 0.15 and accounting["diff_usd"] == 0
    assert accounting["ok"] is True
    assert accounting["executor_log"]["rows_with_cost"] == 1
    assert accounting["cross_model"]["rows"] == 1
    checks["t2_cost_reconcile"] = {"passed": True, "observed": 2}
    writable[1] = "t2_episode"
    assert episode_ledger.episode_spend(jail) == 0.15
    checks["t2_episode"] = {"passed": True, "observed": 2}
    writable[1] = "t2_gates_map"
    gate_report = gates_map.scan_gates(root, files=list(release.PUBLIC_SOURCE_PATHS),
                                       include_live_env=False)
    assert gate_report["gates"]
    checks["t2_gates_map"] = {"passed": True, "observed": len(gate_report["gates"])}
    writable[1] = "upper_tier_absence"
    try:
        encode(("Synthetic unprivileged request",), endpoint=TMIEndpoint(
            "/run/user/99999/synthetic-not-running.sock", "synthetic-vector-model", 2))
    except (ImportError, PermissionError) as refusal:
        assert getattr(refusal, "tier", None) == 9
    else:
        raise AssertionError("missing T9 adapter was not refused")
    present_payloads = [p for p in payload_source_paths() if (root / p).exists()]
    assert not present_payloads
    checks["upper_tier_absence"] = {"passed": True, "observed": 1}
    for name, module in tuple(sys.modules.items()):
        if name == "agi_v8_1" or name.startswith("agi_v8_1."):
            origin = getattr(module, "__file__", None)
            if origin:
                assert Path(origin).resolve().is_relative_to(root)
    assert not effects
    return {"schema": RECEIPT_SCHEMA, "checks": checks, "payload_count": len(present_payloads),
            "external_effects": [], "applied": False}


def validate_receipt(receipt):
    """No empty/partial/self-counted receipt can become a successful G result."""
    if type(receipt) is not dict or receipt.get("schema") != RECEIPT_SCHEMA:
        return False
    checks = receipt.get("checks")
    if type(checks) is not dict or set(checks) != REQUIRED_CHECKS:
        return False
    for row in checks.values():
        if (type(row) is not dict or row.get("passed") is not True
                or type(row.get("observed")) is not int or row["observed"] < 1):
            return False
    digest = checks["t1_si"].get("receipt_sha256")
    return (type(digest) is str and len(digest) == 64
            and all(c in "0123456789abcdef" for c in digest)
            and type(receipt.get("payload_count")) is int and receipt["payload_count"] == 0
            and receipt.get("external_effects") == [] and receipt.get("applied") is False)


def _synthetic_jail(jail):
    jail.mkdir(mode=0o700)
    rows = {
        "tick/queries.jsonl": {"id": "cold-query", "objective": "Synthetic preview"},
        "runtime_logs/executor_log.jsonl": {
            "cycle_id": "cold-accounting-cycle", "ts": 1700000000,
            "executor": "synthetic-local", "cost_usd": 0.1,
        },
        "runtime_logs/cross_model_verify_spend.jsonl": {
            "schema_version": "cross_model_verify_spend_v2", "kind": "actual",
            "transaction_id": "xmv2-" + "1" * 32, "model": "synthetic-local-model",
            "usage_measured": True, "cycle_id": "cold-accounting-cycle",
            "ts": 1700000001, "usd": 0.05,
        },
    }
    for relative, row in rows.items():
        path = jail / relative
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(json.dumps(row, sort_keys=True) + "\n", encoding="utf-8")


def check_release(source_root, work_root, *, timeout_s=30):
    """Create a new offline candidate. Work root must exist and be empty."""
    root, work = (Path(os.path.abspath(os.fspath(path))) for path in (source_root, work_root))
    if (work == root or work.is_relative_to(root)
            or type(timeout_s) is not int or not 1 <= timeout_s <= 120):
        raise ValueError("invalid_coldstart_work_root_or_timeout")
    work_fd = release._io._open_root(work)
    try:
        if os.listdir(work_fd):
            raise ValueError("coldstart_work_root_not_empty")
    finally:
        os.close(work_fd)
    manifest = release.build_manifest(root)
    copied = release.copy_bundle(root, work / "agi_v8_1", manifest)
    gaps = release.closure_report(copied)
    if not gaps["eager_closed"]:
        return {"status": "NO-GO", "reason": "eager_dependency_gap", "closure": gaps}
    jail = work / "synthetic-jail"
    _synthetic_jail(jail)
    try:
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", _BOOTSTRAP, str(copied), str(root), str(jail)],
            cwd=work, env={"PATH": os.defpath, "HOME": str(work),
                           "PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "0"},
            capture_output=True, text=True, timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return {"status": "NO-GO", "reason": "coldstart_timeout"}
    # Child text/tracebacks are deliberately not copied into external reports.
    if result.returncode != 0:
        try:
            diagnostic = json.loads(result.stdout)
        except (ValueError, UnicodeError):
            diagnostic = {}
        phase = diagnostic.get("phase") if type(diagnostic) is dict else None
        return {"status": "NO-GO", "reason": "coldstart_failed", "returncode": result.returncode,
                "phase": phase if phase in REQUIRED_CHECKS | {"assets", "imports"} else "unknown"}
    try:
        receipt = json.loads(result.stdout)
    except (ValueError, UnicodeError):
        return {"status": "NO-GO", "reason": "receipt_invalid"}
    if not validate_receipt(receipt):
        return {"status": "NO-GO", "reason": "receipt_incomplete"}
    release.validate_bundle(copied, manifest)
    return {"status": "GO", "scope": release.SCOPE, "receipt": receipt,
            "manifest_sha256": release.manifest_digest(manifest), "closure": gaps,
            "host_metadata_boundary": "production_halt_sentinel_metadata_checked_not_overridden",
            "stage_c_complete": False, "release_authorized": False, "paid_calls": 0}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--work-root", required=True)
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args(argv)
    try:
        result = check_release(args.source_root, args.work_root, timeout_s=args.timeout)
    except (ReleaseError, DeliveryError, ValueError, OSError):
        result = {"status": "NO-GO", "reason": "delivery_or_input_invalid"}
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result["status"] == "GO" else 1


ReleaseError = release.ReleaseError

if __name__ == "__main__":
    raise SystemExit(main())
