# Plz_ReadMe — the tier curriculum

This file is the contract between you and the gate. Each tier below says what it enables, what it never does, and what you acknowledge before it opens. The gate refuses anything above your current tier and points you back to the matching section here.

The ladder is a **safety UX, not DRM**: the source is open and you can read every gate. The point is that you cannot flip something on by accident, and that you cannot flip it on without having read what it does.

Only **T0, T1 and T2** are part of this public cut. Sections for higher tiers are kept short and only explain why they are refused.

## The one command

Everything in this cut goes through one offline CLI. After `pip install .` it is the `mlaj` command; without installing, `python3 -m agi_v8_1.runtime.free_tier_cli` is the same entry point (examples below use `mlaj`):

```bash
mlaj curriculum
mlaj --state-dir /abs/private/dir <command> [options]
```

- `curriculum` prints the canonical acknowledgement sentences and takes no state directory.
- Every other command needs `--state-dir` **before** the command, and the path must be absolute (no `.`/`..`, not `/`).
- Commands: `init`, `status`, `preview`, `unlock --tier {1,2} --ack TEXT [--evidence-file /abs/file]`, `cycle --cycle-id ID --objective TEXT`, `evidence`, `ledger`.
- Output is exactly one JSON line with sorted keys. Exit code 0 means ok; 2 means refused, and the line is `{"reason": "<fixed code>", "status": "refused"}`. A refusal never echoes your input, a path, a token, or an exception message.

## Ground rules that apply to every tier

- **Clean process.** The CLI refuses to start if *any* environment variable beginning with `AGI_` or `TMI_` is present (`clean_environment_required`). Do not export anything for it. For the duration of one call it sets the public tier gate, the master switch, and the state binding itself, and removes them again on exit. No `.env` file is read.
- **Explicit private state.** All writes go under the `--state-dir` you named: `unlock/` (local grants), `public-cycles.jsonl` (one `cycle_end` row per completed T1 cycle, at most 1 MiB), `cycle-<id>/` (the per-cycle jail: `cycle_events.jsonl`, `cycle_log.jsonl`, `apply_chain.jsonl` with their `.chain`/`.lock` companions, and `proposal-receipt.json`, mode `0600`), and `runtime_logs/executor_log.jsonl` (the canonical executor audit, which is on even for advisory runs). Two cycles leave well under 100 KiB. Nothing is written anywhere else.
- **One invocation at a time** per process (`invocation_busy` otherwise), and every command takes a file lock on the state directory.
- Receipts and ledgers are inspectable local evidence. They are not benchmark evidence and never grant publication, paid dispatch, or a higher tier.

## T0 — Tourist (read only)

**Enables:** `init` (create the private state directory and report `local_tier`), `status` (`local_tier`, `gate_enabled: true`, `providers_enabled: false`, `proposal_only: true`), `preview` (bounded tick inbox count minus tombstones, `read_only: true`), `evidence` (distinct completed cycles and their statuses, `benchmark_evidence: false`), plus these documents.

**Never does:** dispatch, propose, or start the tick runner. `preview` opens the state read-only and is an observation, not a dispatch lease.

**Acknowledgement:** none.

```bash
mlaj --state-dir /abs/private/dir init
mlaj --state-dir /abs/private/dir status
```

## T1 — Apprentice (offline cycles, mock proposals)

**Enables:** `cycle --cycle-id ID --objective TEXT`. Each call runs one self-improvement cycle inside `cycle-<ID>/`, using the offline strategist skeleton in `public_assets/free_tiers/prompts/strategist.md`, and must come back as an **advisory stub**: `{"applied": false, "benchmark_evidence": false, "proposal_count": N, "status": "advisory_stub", "distinct_cycles": M}`. The proposal is written to `cycle-<ID>/proposal-receipt.json`; the cycle is appended to `public-cycles.jsonl` only after the producer and the event normalisation both succeed.

**Never does:** call a model provider, apply a proposal, touch a source tree, or claim performance. If the cycle does not come back as an advisory stub, or the events do not match, the command refuses (`cycle_evidence_invalid`) instead of pretending.

**Input rules:** `ID` matches `[A-Za-z0-9][A-Za-z0-9_-]{0,79}` and is single-use (`cycle_already_used`); the objective is non-empty and at most 2000 characters (`cycle_input_invalid`).

**Acknowledgement:** the T1 sentence printed by `curriculum`. Its substance is the T1 promise itself: the system writes nothing outside the state directory. Pass it verbatim:

```bash
mlaj curriculum
mlaj --state-dir /abs/private/dir unlock --tier 1 --ack "<the T1 sentence, verbatim>"
mlaj --state-dir /abs/private/dir cycle --cycle-id first --objective "Describe one small improvement to the proposal format."
```

`--evidence-file` is refused at T1 (`evidence_not_used_at_t1`).

## T2 — Journeyman (instrumentation)

**Enables:** `ledger` (the same read-only summary as `evidence`, gated at T2) and the shipped instrumentation modules — ledger join coverage, cost reconciliation, episode accounting, and the gates map — which read what T1 wrote and report. They do not run cycles.

**Evidence:** at least **two distinct completed cycles**. By default the CLI reads your own `public-cycles.jsonl`; pass `--evidence-file /abs/file` to submit a cycle-events file from elsewhere instead. Count first if you like:

```bash
mlaj --state-dir /abs/private/dir evidence
mlaj --state-dir /abs/private/dir unlock --tier 2 --ack "<the T2 sentence, verbatim>"
mlaj --state-dir /abs/private/dir ledger
```

**Acknowledgement:** the T2 sentence printed by `curriculum`. Its substance: you check the ledger evidence of *distinct* cycles, not a success count. The minimum of two is a local-only calibration, not a server promotion threshold.

**Never does:** unlock anything paid or networked. Synthetic cost entries produced by the checks are not invoices.

## Refusal codes you may see

| `reason` | What it means |
|---|---|
| `absolute_path_required` | `--state-dir` (or `--evidence-file`) is not an absolute, normalised path |
| `clean_environment_required` | an `AGI_*`/`TMI_*` variable is set in your shell |
| `state_not_used_by_curriculum` | `curriculum` was given `--state-dir` |
| `invocation_busy` | another call in the same process holds the environment lock |
| `evidence_not_used_at_t1` | `--evidence-file` passed to `unlock --tier 1` |
| `tier_refused` (usually with a `doc_pointer` such as `Plz_ReadMe.md §T1`) | your local tier is below what the command needs — `cycle` before the T1 unlock, `ledger` before the T2 unlock, or an acknowledgement that does not match the curriculum |
| `cycle_input_invalid`, `cycle_already_used`, `ledger_full`, `ledger_invalid`, `cycle_evidence_invalid` | see T1 |
| `input_or_state_refused` | catch-all for unreadable or malformed state and input; nothing about the cause is echoed |

## T3 and above — not in this cut

| Tier | What it would add | Why it is refused here |
|---|---|---|
| T3 | one real provider worker lane with your own API key and a hard cost cap | requires a server-issued, signed unlock token; no issuer exists for this cut |
| T4 | parallel swarm lanes with heterogeneous-model voting | same, plus T3 cost-ledger reconciliation history |
| T5 | sandboxed verification (isolated pytest gate) | same, plus at least one recorded REJECT |
| T6–T7 | cross-model review with budget caps; falsifier bus and stall oracles | same |
| T8 | promotion CLI into a real source tree, human driver required every time | same, plus a refusal ledger |
| T9 | autonomous loop, always-on tick, memory recall, swarm executor | same, plus kill-switch ownership and safety attestations that this cut does not produce |

Higher tiers are planned to be offered through a separate enrolment site that issues signed tokens and delivers encrypted per-tier modules; that site is not part of this repository, and higher-tier issuance and delivery are not yet publicly open. The gate module ships a token importer, but with an empty built-in trust-key list it refuses explicitly. Already-held higher tiers are never overwritten by an older, lower token.

## Two honest notes about the gate

- The shipped `public_assets/free_tiers/config.json` leaves `AGI_V8_PUBLIC_TIER_GATE_ENABLED` at `false`. The CLI turns the gate on for each call; library callers who leave it off get no enforcement at all, which is documented behaviour, not a bug.
- For library callers (not the CLI, which refuses any `AGI_*` variable) the gate module honours `AGI_V8_I_HAVE_READ_THE_SOURCE=<40-hex commit id>` as a T1/T2 bypass when it matches the checkout's commit. It exists because this ladder is a reading contract, not DRM. It does not apply to T3 and above.
