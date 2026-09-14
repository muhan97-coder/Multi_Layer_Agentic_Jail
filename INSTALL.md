# INSTALL — requirements, the clean-environment contract, what gets written where

## Requirements

- **Python 3.12 or newer** on **Linux** (the CLI uses POSIX file locking and `O_DIRECTORY` opens; other platforms are untested and unsupported in this cut).
- **No third-party packages.** `pyproject.toml` declares `dependencies = []`, and the supported free-tier path was verified in a virtual environment with nothing installed. Three optional imports exist in modules outside that path; see `THIRD_PARTY_NOTICES.md`.
- No API keys. No network. No Docker.
- Building from source with `pip install .` needs `setuptools` and `wheel` at build time only (declared in `pyproject.toml`); the runtime needs nothing beyond the standard library.

## Install

The repository root holds `pyproject.toml`, `delivery-manifest.json`, and the package directory `agi_v8_1/`. Installing the project gives you the `mlaj` command:

```bash
git clone <this repository> mlaj && cd mlaj
python3 -m venv .venv && . .venv/bin/activate
pip install .
mlaj curriculum
```

Without installing, the same CLI runs straight from the checkout:

```bash
export PYTHONPATH="$(pwd)"                     # the repository root, i.e. the parent of agi_v8_1/
python3 -m agi_v8_1.runtime.free_tier_cli curriculum
```

`mlaj` and `python3 -m agi_v8_1.runtime.free_tier_cli` are the same entry point; the examples below use `mlaj`.

## The clean-environment contract

The CLI refuses to start if **any** environment variable beginning with `AGI_` or `TMI_` is set in the calling process (`{"reason": "clean_environment_required", "status": "refused"}`). Do not export anything for it. For the duration of a single call it sets the public tier gate, the master switch, and the audit state binding itself, then removes them again. It reads no `.env` file and never looks for provider keys.

This is intentional. A machine that runs the private, armed configuration of this loop must not be able to hand that configuration to a curriculum run by accident. If your shell carries such variables, run through `env -i`:

```bash
env -i PATH="$PATH" mlaj curriculum
```

The shipped `agi_v8_1/public_assets/free_tiers/config.json` (all masters `false`) is a test asset consumed by the cold-start check, not something you copy into your environment.

## Step 1 — cold-start check (recommended first run)

Run it from a **new, empty** working directory. It copies the shipped files into a payload-zero `python -I` environment, creates only synthetic inputs, and exercises T0 → T1 → T2 plus an upper-tier-absence probe:

```bash
mkdir -p /tmp/mlaj-work && cd /tmp/mlaj-work
python3 <checkout>/agi_v8_1/tools/public_coldstart_check.py --source-root <checkout>/agi_v8_1 --work-root /tmp/mlaj-work
```

Output is a single JSON line. Each sub-check (`t0_tick_master_off`, `t0_tick`, `t0_ledger`, `t0_demo`, `t1_si`, `local_tier_unlock`, `t2_ledger_join`, `t2_cost_reconcile`, `t2_episode`, `t2_gates_map`, `upper_tier_absence`) reports `passed` and an `observed` count; `t1_si` also reports `receipt_sha256`. Any of the following is **NO-GO**, not a warning:

- a shipped file missing, or a copy inconsistent with the manifest the check regenerates from that copy (it does not compare against a manifest from elsewhere — verify your files against the published `delivery-manifest.json` hashes yourself)
- an external access attempt observed by the audit hook
- an incomplete receipt
- an active human HALT (the real tick's master-OFF path keeps its HALT check; the smoke test refuses instead of mocking it away)

A NO-GO prints a `reason` (and a `phase` when one applies). Report those, not your ledgers.

## Step 2 — walk the tiers by hand

See `Plz_ReadMe.md` for what each command does and acknowledges. The shortest path, with the outputs you should see:

```bash
S=/tmp/mlaj-state                          # any absolute path you own; init creates it
mlaj curriculum                            # prints the T1 and T2 sentences (JSON)
mlaj --state-dir $S init                   # {"gate_enabled": true, "initialized": true, "local_tier": 0, "status": "ok"}
mlaj --state-dir $S status                 # {..., "local_tier": 0, "proposal_only": true, "providers_enabled": false, "status": "ok"}
mlaj --state-dir $S unlock --tier 1 --ack "<T1 sentence, verbatim>"
                                           # {"gate_enabled": true, "local_tier": 1, "status": "ok"}
mlaj --state-dir $S cycle --cycle-id first  --objective "Describe one small improvement to the proposal format."
                                           # {"applied": false, "benchmark_evidence": false, "distinct_cycles": 1, "proposal_count": 1, "status": "advisory_stub"}
mlaj --state-dir $S cycle --cycle-id second --objective "Describe another."
mlaj --state-dir $S evidence               # {"benchmark_evidence": false, "distinct_cycles": 2, "read_only": true, "status": "ok", "statuses": {"advisory_stub": 2}}
mlaj --state-dir $S unlock --tier 2 --ack "<T2 sentence, verbatim>"
mlaj --state-dir $S ledger                 # same summary as evidence, now permitted at T2
```

`--state-dir` goes **before** the command and must be absolute. Every command prints one JSON line; exit code 2 means refused and the `reason` field is a fixed code listed in `Plz_ReadMe.md`. Running `cycle` before the T1 unlock, or `ledger` before the T2 unlock, is refused with `tier_refused` and a `doc_pointer` back to the matching section.

## What gets written, and where

| Path | Written by | Contents |
|---|---|---|
| `<work-root>/…` | cold-start check | a payload-zero copy of the shipment plus synthetic inputs and a receipt |
| `<state-dir>/` | `init` | the private state directory itself |
| `<state-dir>/unlock/` | `unlock` | local tier grants |
| `<state-dir>/public-cycles.jsonl` | `cycle` | one `cycle_end` row per completed T1 cycle (`status: advisory_stub`), mode `0600`, at most 1 MiB |
| `<state-dir>/cycle-<id>/` | `cycle` | the per-cycle jail: `cycle_events.jsonl`, `cycle_log.jsonl`, `apply_chain.jsonl` (with `.chain`/`.lock` companions) and `proposal-receipt.json` — the mock proposal with `applied: false`, `benchmark_evidence: false`, mode `0600` |
| `<state-dir>/runtime_logs/executor_log.jsonl` | `cycle` | the canonical executor audit (on even for advisory runs), bound to this state directory |

Two T1 cycles leave a state directory of well under 100 KiB. Nothing is written beside the checkout, in your home directory, or anywhere outside the two directories you named. There is no daemon, timer, or background process. `status`, `preview`, `evidence` and `ledger` are read-only.

## Removing everything

Delete the work root, the state directory, and the checkout (or `pip uninstall multi-layer-agentic-jail`). There are no registered services, no caches outside those directories, and no tokens to revoke.

```bash
rm -rf /tmp/mlaj-work /tmp/mlaj-state
```

## What this install does *not* give you

- No model calls, no paid dispatch, no autonomous tick — those are tiers T3+ and are refused in this cut (`Plz_ReadMe.md`).
- No sandbox guarantee: the audit hook observes ordinary file/network/process effects only.
- No benchmark claims: a green cold-start receipt means "the shipment behaves as documented here", nothing more.
