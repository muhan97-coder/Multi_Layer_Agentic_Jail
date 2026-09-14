"""Offline native-platform smoke for the supported T0..T2 user commands.

Run with a NEW EMPTY mode-0700 work directory, using its physical absolute
path (for example Path(tempfile.mkdtemp()).resolve(), not macOS /tmp aliases).
Only synthetic test grants/state are created. This does not grant a real user
account a tier, contact a provider, install anything, or register a service.
The JSON result contains platform facts and fixed check names, never local
paths, environment values, exception text, prompts, or credentials.

A Linux PASS is a Linux result, NOT a macOS certification. A macOS result
requires running this checker on that actual machine. These functional checks
are not an OS sandbox, remote attestation, or trusted-shipment authenticity.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import stat
import subprocess
import sys


SCHEMA = "agi_v8_1.public_platform_smoke.v1"
_CLI = "import sys; sys.path.insert(0, sys.argv.pop(1)); from agi_v8_1.runtime.free_tier_cli import main; raise SystemExit(main())"
_COLD = "import sys,json; from pathlib import Path; sys.path.insert(0, sys.argv.pop(1)); from agi_v8_1.tools.public_coldstart_check import check_release; print(json.dumps(check_release(Path(sys.argv[1]), Path(sys.argv[2]), timeout_s=120),sort_keys=True))"


class _CheckFailed(ValueError):
    """An internal failure, never echoed into the public result."""


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise _CheckFailed("arguments_invalid")


def _report():
    return {"schema": SCHEMA, "status": "NO-GO", "platform": {},
            "native_macos_arm64_passed": False, "checks": {},
            "scope": "synthetic-free-tier-native-platform-smoke",
            "release_authorized": False, "benchmark_evidence": False}


def _platform_info():
    return {"system": platform.system(), "machine": platform.machine(),
            "python": platform.python_version(),
            "macos": platform.mac_ver()[0] if sys.platform == "darwin" else None}


def _command(argv, *, work, env, contract, expected=0, timeout=45):
    result = subprocess.run(argv, cwd=work, env=env, stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=timeout,
                            close_fds=True)
    if result.returncode != expected or result.stderr or len(result.stdout) > 131072:
        raise _CheckFailed("command_failed")
    value = json.loads(result.stdout)
    if type(value) is not dict or contract(value) is not True:
        raise _CheckFailed("output_invalid")
    # Child text is consumed only by a boolean contract. Never return or copy
    # it to the report, including fields unexpected by today's CLI schema.
    return True


def _source_witness(source):
    from agi_v8_1.capabilities import public_release as release
    manifest = release.build_manifest(source)
    fd = release._io._open_root(source)
    try:
        extras = {name: hashlib.sha256(release._io._read_regular(fd, name)).hexdigest()
                  for name in ("runtime/free_tier_cli.py", "tools/public_platform_check.py")}
    finally:
        os.close(fd)
    return {"free_copy_manifest_sha256": release.manifest_digest(manifest), **extras}


def check_platform(work_root):
    """Exercise real CLI processes and G, leaving only a fresh synthetic work tree."""
    report = _report()
    phase = "platform"
    try:
        facts = _platform_info()
        report["platform"] = facts
        if sys.version_info < (3, 12) or sys.platform not in {"linux", "darwin"}:
            raise _CheckFailed("unsupported_platform")
        from agi_v8_1.policy import tier_gate as gate
        from agi_v8_1.tools import public_coldstart_check as cold
        source = Path(__file__).resolve().parents[1]
        phase = "source_inventory"
        before = _source_witness(source)
        phase = "work_directory"
        raw = os.fspath(work_root)
        if (not isinstance(raw, str) or not raw.startswith("/") or raw.startswith("//")
                or len(raw) > 4096 or any(ord(char) < 32 or ord(char) == 127 for char in raw)
                or ".." in raw.split("/")):
            raise _CheckFailed("physical_absolute_work_required")
        work = Path(raw)
        if str(work) != raw or raw == "/" or work == source or work.is_relative_to(source):
            raise _CheckFailed("work_directory_invalid")
        # Keep the same descriptor walk/no-symlink policy as actual consumers.
        # Never resolve a supplied alias and quietly turn it into permission.
        fd = gate._open_directory(work)
        try:
            if os.listdir(fd):
                raise _CheckFailed("work_directory_not_empty")
            for name in ("home", "tmp", "coldstart"):
                os.mkdir(name, 0o700, dir_fd=fd)
        finally:
            os.close(fd)
        env = {"PATH": os.defpath, "HOME": str(work / "home"),
               "TMPDIR": str(work / "tmp"), "PYTHONDONTWRITEBYTECODE": "1",
               "PYTHONHASHSEED": "0"}
        state = work / "state"
        prefix = ["--state-dir", str(state)]
        checks = report["checks"]

        def cli(label, arguments, *, expected=0, reason=None, contract=None, extra_env=None):
            nonlocal phase
            phase = label
            if reason is not None:
                contract = lambda value: value.get("status") == "refused" and value.get("reason") == reason
            if not callable(contract):
                raise _CheckFailed("missing_contract")
            if _command([sys.executable, "-I", "-B", "-c", _CLI,
                         str(source.parent), *arguments], work=work,
                        env={**env, **(extra_env or {})}, contract=contract, expected=expected) is not True:
                raise _CheckFailed("command_not_verified")
            checks[label] = {"passed": True, "returncode": expected}

        curriculum = gate.local_curriculum()
        cli("curriculum", ["curriculum"], contract=lambda value: value == curriculum)
        cli("init", [*prefix, "init"], contract=lambda value: value == {
            "status": "ok", "initialized": True, "local_tier": 0, "gate_enabled": True})
        cli("status", [*prefix, "status"], contract=lambda value: value == {
            "status": "ok", "local_tier": 0, "gate_enabled": True,
            "providers_enabled": False, "proposal_only": True})
        cli("preview", [*prefix, "preview"], contract=lambda value: value == {
            "pending": 0, "read_only": True, "reason": "no_state"})
        cli("cycle_before_ack", [*prefix, "cycle", "--cycle-id", "before-ack",
                                 "--objective", "Synthetic platform example"],
            expected=2, reason="tier_refused")
        cli("ledger_before_t2", [*prefix, "ledger"], expected=2, reason="tier_refused")
        cli("wrong_ack", [*prefix, "unlock", "--tier", "1", "--ack", "synthetic wrong ack"],
            expected=2, reason="tier_refused")
        cli("unlock_t1", [*prefix, "unlock", "--tier", "1", "--ack", curriculum["T1"]["ack"]],
            contract=lambda value: value == {"status": "ok", "local_tier": 1, "gate_enabled": True})
        for number in (1, 2):
            cli(f"cycle_{number}", [*prefix, "cycle", "--cycle-id", f"platform-{number}",
                                   "--objective", "Synthetic platform example"], contract=lambda value: (
                value.get("status") == "advisory_stub" and value.get("applied") is False
                and value.get("benchmark_evidence") is False and value.get("distinct_cycles") == number
                and type(value.get("proposal_count")) is int and value["proposal_count"] >= 1))
        summary = {"status": "ok", "read_only": True, "distinct_cycles": 2,
                   "benchmark_evidence": False, "statuses": {"advisory_stub": 2}}
        cli("evidence", [*prefix, "evidence"], contract=lambda value: value == summary)
        cli("unlock_t2", [*prefix, "unlock", "--tier", "2", "--ack", curriculum["T2"]["ack"]],
            contract=lambda value: value == {"status": "ok", "local_tier": 2, "gate_enabled": True})
        cli("ledger", [*prefix, "ledger"], contract=lambda value: value == summary)
        cli("duplicate_cycle", [*prefix, "cycle", "--cycle-id", "platform-1", "--objective", "Synthetic duplicate"],
            expected=2, reason="cycle_already_used")
        cli("relative_state", ["--state-dir", "relative", "status"], expected=2,
            reason="absolute_path_required")
        cli("inherited_environment", ["curriculum"], expected=2, reason="clean_environment_required",
            extra_env={"AGI_SYNTHETIC_PLATFORM_TEST": "synthetic-only"})
        cli("bad_arguments", ["--synthetic-unsupported"], expected=2, reason="arguments_invalid")
        phase = "symlink_refusal"
        alias = work / "state-alias"
        alias.symlink_to(state, target_is_directory=True)
        cli("symlink_refusal", ["--state-dir", str(alias), "status"], expected=2,
            reason="input_or_state_refused")
        phase = "filesystem_permissions"
        for path in (state, state / "unlock"):
            info = path.lstat()
            if (not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700
                    or info.st_uid != os.geteuid()):
                raise _CheckFailed("directory_permissions")
        info = (state / "public-cycles.jsonl").lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            raise _CheckFailed("ledger_permissions")
        checks[phase] = {"passed": True}
        phase = "coldstart"
        if _command([sys.executable, "-I", "-B", "-c", _COLD, str(source.parent),
                     str(source), str(work / "coldstart")], work=work, env=env, timeout=135,
                    contract=lambda value: value.get("status") == "GO"
                    and cold.validate_receipt(value.get("receipt"))) is not True:
            raise _CheckFailed("coldstart_not_go")
        report["coldstart_checks"] = {name: True for name in sorted(cold.REQUIRED_CHECKS)}
        checks[phase] = {"passed": True}
        phase = "source_unchanged"
        if _source_witness(source) != before:
            raise _CheckFailed("source_changed")
        checks[phase] = {"passed": True}
        report.update(status="PASS", source=before, paid_calls=0, applied=False,
                      native_macos_arm64_passed=(sys.platform == "darwin" and facts["machine"] == "arm64"))
    except (Exception, SystemExit):
        if phase in report["checks"]:
            report["checks"][phase]["passed"] = False
        report.update(reason="platform_check_failed", phase=phase)
    return report


def main(argv=None):
    parser = _Parser(prog="mlaj-platform-check", description=__doc__.splitlines()[0], allow_abbrev=False)
    parser.add_argument("--work-root", required=True, help="new empty private physical absolute directory")
    try:
        args = parser.parse_args(argv)
    except _CheckFailed:
        result = _report()
        result.update(reason="arguments_invalid", phase="arguments")
        print(json.dumps(result, sort_keys=True))
        return 2
    result = check_platform(args.work_root)
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
