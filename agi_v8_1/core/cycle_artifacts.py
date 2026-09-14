"""V8 cycle artifact collection (R20 W1 — decomposed from v7.1 orchestrator.py).

v7.1 orchestrator.py:5677-5687 ``_persist_artifact`` writes per-cycle
artifacts to disk inline inside the monolith. The V8 port extracts the
*manifest building* into a deterministic free module that aggregates V8
message dataclass IDs (EvidenceCard.evidence_id, CriticTicket.ticket_id,
DecisionRecord.decision_id, HandoffPacket.handoff_id) into a single
:class:`CycleArtifactManifest`.

This module does NOT write to disk — that is the orchestrator's job (it
already has apply_chain + cycle_log JSONL writers in
self_improvement_v8.py). The manifest is an *advisory* aggregate that the
caller may pass into the cycle log payload.

Hard rules:
  - no I/O, no subprocess, no network
  - import-clean: only stdlib + agi_v8_1.core.{messages,orchestrator_schema}
  - deterministic given fixed inputs
"""

from __future__ import annotations

from typing import Sequence

from agi_v8_1.core.messages import (
    CriticTicket,
    DecisionRecord,
    EvidenceCard,
    HandoffPacket,
    SIPolicyTicket,
)
from agi_v8_1.core.orchestrator_schema import CycleArtifactManifest


def collect_manifest(
    *,
    cycle_id: str,
    evidence: Sequence[EvidenceCard] = (),
    tickets: Sequence[CriticTicket | SIPolicyTicket] = (),
    decisions: Sequence[DecisionRecord] = (),
    handoff: HandoffPacket | None = None,
    completed_phase: str = "",
) -> CycleArtifactManifest:
    """Build an immutable manifest from cycle outputs.

    Tickets accept both :class:`CriticTicket` and :class:`SIPolicyTicket`
    since both expose a ``ticket_id`` attribute. Mixed input is allowed.

    Ordering is preserved (callers control determinism by passing already-
    sorted sequences).
    """

    evidence_ids = tuple(str(e.evidence_id) for e in evidence)
    ticket_ids = tuple(str(getattr(t, "ticket_id", "")) for t in tickets)
    decision_ids = tuple(str(d.decision_id) for d in decisions)
    handoff_id = str(handoff.handoff_id) if handoff is not None else ""

    return CycleArtifactManifest(
        cycle_id=str(cycle_id),
        evidence_ids=evidence_ids,
        ticket_ids=ticket_ids,
        decision_ids=decision_ids,
        handoff_id=handoff_id,
        completed_phase=str(completed_phase),
    )


def manifest_to_dict(manifest: CycleArtifactManifest) -> dict:
    """JSON-friendly dict view for cycle_log payloads."""
    return {
        "cycle_id": manifest.cycle_id,
        "evidence_ids": list(manifest.evidence_ids),
        "ticket_ids": list(manifest.ticket_ids),
        "decision_ids": list(manifest.decision_ids),
        "handoff_id": manifest.handoff_id,
        "completed_phase": manifest.completed_phase,
        "counts": {
            "evidence": len(manifest.evidence_ids),
            "tickets": len(manifest.ticket_ids),
            "decisions": len(manifest.decision_ids),
        },
    }


# __R26_SLOT__ — optional eval-harness wiring.
# Default OFF (``AGI_V8_EVAL_HARNESS_ENABLED=false``). When enabled, callers
# may pass benchmark results into :func:`manifest_to_dict_with_eval` to attach
# a ``benchmark_results`` + ``benchmark_summary`` block to the cycle dict.
# Legacy ``manifest_to_dict`` is byte-untouched so R20 tests stay green.
def manifest_to_dict_with_eval(
    manifest: CycleArtifactManifest,
    *,
    benchmark_results: list | None = None,
    enabled: bool | None = None,
) -> dict:
    """Return manifest dict + (optionally) eval harness results.

    When ``enabled`` is False (or env ``AGI_V8_EVAL_HARNESS_ENABLED`` is
    not set), this is identical to :func:`manifest_to_dict`. When True and
    ``benchmark_results`` provided, the dict gets two extra keys.
    """

    base = manifest_to_dict(manifest)
    if enabled is None:
        import os

        enabled = (
            os.getenv("AGI_V8_EVAL_HARNESS_ENABLED", "false").strip().lower() == "true"  # tier: T5
        )
    if not enabled or not benchmark_results:
        return base
    # Lazy import to avoid cycle.
    from agi_v8_1.capabilities import PayloadPort, resolve_payload
    summarize_results = resolve_payload(PayloadPort(5, "agi_v8_1.eval", "summarize_results"))
    validate_benchmark_result = resolve_payload(PayloadPort(5, "agi_v8_1.eval", "validate_benchmark_result"))

    raw_results = list(benchmark_results)
    valid_results: list = []
    invalid_results: list[dict] = []
    for idx, row in enumerate(raw_results):
        problems = validate_benchmark_result(row)
        if problems:
            invalid_results.append({"index": idx, "reasons": problems})
            continue
        valid_results.append(row)
    base["benchmark_results"] = valid_results
    if invalid_results:
        base["benchmark_invalid_results"] = invalid_results
    base["benchmark_summary"] = summarize_results(raw_results)
    return base


__all__ = ["collect_manifest", "manifest_to_dict", "manifest_to_dict_with_eval"]
