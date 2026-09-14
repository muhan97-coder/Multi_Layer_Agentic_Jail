"""Local curriculum/evidence and offline signed-token import. No token issuance.

Run as ``python -m agi_v8_1.tools.unlock``. T1/T2 acknowledgements live in the
public curriculum asset. Evidence input is an explicitly selected CycleLogger
JSONL file, bounded to 1 MiB; only cycle IDs and statuses are persisted. Token
import uses the empty-by-default release trust map, never a CLI-supplied key.
No command contacts an issuer, deploys a Worker, or decrypts a payload.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from agi_v8_1.policy.fail_fast import format_exception_for_sink

from agi_v8_1.policy.tier_gate import (
    TierContext, TierRefused, _open_directory, _read_at, enabled, grant_local,
    import_token, local_curriculum, normalize_cycle_evidence, require_tier,
)


def _input(path: Path, *, limit: int = 1048576) -> bytes:
    """Explicit private regular input, with no-follow ancestors and size cap."""
    fd = _open_directory(path.absolute().parent, private=False)
    try:
        return _read_at(fd, path.name, limit=limit)
    finally:
        os.close(fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("curriculum", help="show only public T1/T2 tasks")
    summarize = commands.add_parser("evidence", help="count distinct completed cycles; no row echo")
    summarize.add_argument("--file", required=True, type=Path)
    for name in ("local", "check", "import-token"):
        command = commands.add_parser(name)
        command.add_argument("--state-dir", required=True, type=Path)
        command.add_argument("--repo-commit", default="")
        command.add_argument("--user-id", default="")
        command.add_argument("--issuer", default="")
        command.add_argument("--evidence-digest", default="")
        if name in {"local", "check"}:
            command.add_argument("--tier", required=True, type=int)
        if name == "local":
            command.add_argument("--ack", required=True)
            command.add_argument("--evidence-file", type=Path)
        if name == "import-token":
            command.add_argument("--file", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "curriculum":
            print(json.dumps(local_curriculum(), ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "evidence":
            summary = normalize_cycle_evidence(_input(args.file))
            print(json.dumps({"distinct_cycles": len(summary["cycles"])}))
            return 0
        context = TierContext(args.state_dir, repo_commit=args.repo_commit,
                              user_id=args.user_id, issuer=args.issuer,
                              evidence_digest=args.evidence_digest)
        if args.command == "local":
            evidence = _input(args.evidence_file) if args.evidence_file is not None else None
            grant_local(args.tier, context=context, ack=args.ack, evidence=evidence)
        elif args.command == "import-token":
            import_token(_input(args.file, limit=8192), context=context)
        else:
            require_tier(args.tier, context=context)
        # No token, signature, key, user identity, paths or submitted evidence.
        print(json.dumps({"status": "ok", "gate_enabled": enabled()}, sort_keys=True))
        return 0
    except TierRefused as exc:
        tier = getattr(args, "tier", 3)
        pointer = f"Plz_ReadMe.md §T{tier if type(tier) is int and 0 <= tier <= 9 else 9}"
        print(json.dumps({"status": "refused", "reason": format_exception_for_sink(exc),
                          "doc_pointer": pointer}, sort_keys=True))
        return 2
    except (OSError, ValueError, TypeError, RecursionError):
        print(json.dumps({"status": "refused", "reason": "input_invalid"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
