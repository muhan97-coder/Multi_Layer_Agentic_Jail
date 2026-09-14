# Multi_Layer_Agentic_Jail

A self-improvement loop for LLM agents that is **default-OFF everywhere**, writes only inside an explicit jail directory, and unlocks capability one tier at a time as you demonstrate that you have read what each tier does.

> **Status: pre-release candidate**, MIT licensed (see `LICENSE`). Nothing here has been published as a tagged release and no unlock tokens have been issued. Until the first tagged release, treat every file as a preview.

## What this is

The system runs a closed loop: *propose → verify → sandbox → apply*, with a ledger row for every step and a gate in front of every effect. In the private research setup the loop drives real model workers, runs pytest in isolation, and can promote patches into a source tree only through a human-driven CLI. **None of that is in this first public cut.**

What ships now is the **free tier ladder T0–T2**: the parts of the loop that run with zero API keys, zero network access, zero third-party packages, and zero writes outside a directory you choose.

| Tier | What you can do | What it needs | Cost |
|---|---|---|---|
| **T0 — Tourist** | Create a private state directory, read status, preview the tick inbox, read ledgers and these docs | Nothing beyond a clone | $0 |
| **T1 — Apprentice** | Offline self-improvement cycles that each produce a *mock* proposal in a per-cycle jail. Never applies a change | An acknowledgement sentence (see `Plz_ReadMe.md`) | $0 |
| **T2 — Journeyman** | Instrumentation: the cycle ledger summary and the shipped ledger-join, cost-reconciliation, episode-accounting and gates-map modules | Two distinct completed T1 cycles, plus an acknowledgement | $0 |

Tiers T3 and above (real provider workers, parallel lanes, sandboxed verification, cross-model review, promotion, autonomous ticks) exist in the design but are **not provided, advertised, or unlockable in this cut**. Requests for them are refused with a pointer to the relevant `Plz_ReadMe.md` section. They are planned to be offered later through a separate enrolment site that issues signed tokens; that site is not part of this repository.

## What this is not

- Not a benchmark result. A passing cold-start receipt proves that the shipped files behave as documented on your machine; it says nothing about model quality or paid-tier performance.
- Not a security sandbox. The Python audit hooks used by the checks observe ordinary file, network, and process effects. They do not stop malicious native code and are not an OS sandbox.
- Not DRM. The tier ladder is a safety UX, in the spirit of an `unsafe` block: the goal is that you cannot enable something *by accident* or *without reading*, not that you cannot enable it at all. The source is open; the gate is honest about that.
- Not an autonomous agent out of the box. The tick runner that drives the loop is never started by the public CLI.

## Safety model, briefly

1. **Every effect is behind a gate, and every gate defaults to OFF.** The public CLI turns on only the tier gate, the master switch, and the state binding, for one call at a time.
2. **Writes stay in the jail.** Cycles run inside a state directory you pass explicitly; receipts and ledgers are written there with `0600` permissions and nowhere else.
3. **Clean environment contract.** The CLI refuses to start if any `AGI_*` or `TMI_*` variable is present in your shell, reads no `.env`, and never looks for provider keys. A configured operating environment cannot leak into a curriculum run.
4. **Fail-closed on missing pieces.** A missing component, a copy that is inconsistent with itself, an external access attempt, or an incomplete receipt turns the cold-start check into NO-GO rather than a warning. The check regenerates its manifest from the copy it is given, so it proves that the copy is complete and behaves as documented; comparing your files against the published `delivery-manifest.json` hashes is a separate step you do yourself.
5. **Human stop wins.** The real tick keeps its human HALT check even in the master-OFF path; an active HALT refuses the smoke test and is never mocked away.

Details, including what each command writes and how to remove everything afterwards, are in `INSTALL.md`.

## Quick start

```bash
# Python 3.12 or newer, Linux. No third-party packages.
git clone <this repository> mlaj && cd mlaj
python3 -m venv .venv && . .venv/bin/activate
pip install .                      # installs the `mlaj` command (no dependencies)

mlaj curriculum                    # the two sentences you will acknowledge
mlaj --state-dir /abs/private/dir init
mlaj --state-dir /abs/private/dir unlock --tier 1 --ack "<T1 sentence, verbatim>"
mlaj --state-dir /abs/private/dir cycle --cycle-id first --objective "Describe one small improvement to the proposal format."
```

Every command prints exactly one JSON line and exits 0 (ok) or 2 (refused, with a fixed `reason` code). `Plz_ReadMe.md` explains each tier; `INSTALL.md` explains the cold-start check and what is written where.

## What is in the box

The delivery manifest (`delivery-manifest.json`) lists every shipped file with its SHA-256: the `agi_v8_1` package (138 Python sources), four assets (a free-tier config used by the cold-start check, one offline strategist prompt skeleton, a curriculum file, and the free-tier README), the packaging metadata, and these documents. Anything not in the manifest is not part of the candidate. In particular the cut contains **no** research data, ledgers, personal memories, operator environment, credentials, benchmark cards, gold patches, tests, or the private prompt set.

| Directory (under `agi_v8_1/`) | Role |
|---|---|
| `runtime/` | the free-tier CLI, public entry, tick runner (never auto-started here), halt sentinel, cost cap, ledgers |
| `core/`, `orchestrator_v8.py`, `self_improvement_v8.py` | the loop itself: cycle logger, activation, acceptance gate, SI cycle |
| `si_lanes/`, `verifier/`, `bridge/`, `bus/` | proposer, verify gate, evidence bridge, event bus |
| `policy/`, `enforcement/`, `state/` | tier gate and tokens, secret masking, path guards, jail containment |
| `providers/` | provider base classes and egress masking (no provider is dispatched in T0–T2) |
| `tools/` | cold-start check, tier unlock helpers, gates map, ledger join check, inventory, env check |
| `public_assets/` | free-tier config asset, curriculum, offline prompt skeleton |

## Documents

- `Plz_ReadMe.md` — the tier curriculum: what each tier does, what you acknowledge, how to unlock locally.
- `INSTALL.md` — requirements, the clean-environment contract, the cold-start check, what gets written where, removal.
- `THIRD_PARTY_NOTICES.md` — dependency statement (none required) and the licenses of the optional imports.
- `LICENSE` — MIT.

## Reporting problems

Open an issue on this repository. Please do not include ledger rows, receipts, or environment dumps from your own runs; describe the command and the `reason` (and, for the cold-start check, the `phase` when one is printed) from the JSON line instead.
