"""V8 advisory chain message dataclasses.

Ported from agi_v7.1/agent_system/swarm/v2/messages.py (8 base v2 cards) plus
the V8-new :class:`SIPolicyTicket` (9th card) that carries SI 12-lane output.

All messages are frozen, slot-backed, and deterministic. They are advisory
artifacts — they do not call providers, run shell commands, or apply patches.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass


def make_message_id(prefix: str) -> str:
    """Return a deterministically-formatted but unique id.

    Uses ``uuid.uuid4().hex[:16]`` so two parallel calls do not collide. The
    prefix lets callers tag the card kind (``"tp"``, ``"ev"``, ``"si"``, etc.).
    """

    return f"{str(prefix).strip() or 'msg'}_{uuid.uuid4().hex[:16]}"


@dataclass(frozen=True, slots=True)
class TaskPacket:
    message_id: str
    created_at_unix: float
    task_id: str
    atomic_task_count: int
    route_hint: str
    risk_score: float
    dependency_ids: tuple[str, ...]
    verification_burden: float
    status: str


@dataclass(frozen=True, slots=True)
class EvidenceCard:
    message_id: str
    created_at_unix: float
    evidence_id: str
    source_task_id: str
    claim: str
    state: str
    confidence: float
    provenance: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ArtifactManifest:
    message_id: str
    created_at_unix: float
    manifest_id: str
    source_pod: str
    artifact_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class VerificationTicket:
    message_id: str
    created_at_unix: float
    ticket_id: str
    evidence_id: str
    verification_method: str
    passed: bool
    notes: str


@dataclass(frozen=True, slots=True)
class CriticTicket:
    message_id: str
    created_at_unix: float
    ticket_id: str
    target_id: str
    severity: str
    finding: str


@dataclass(frozen=True, slots=True)
class DisagreementTicket:
    message_id: str
    created_at_unix: float
    ticket_id: str
    between_pods: tuple[str, str]
    divergence_score: float
    summary: str


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    message_id: str
    created_at_unix: float
    decision_id: str
    evidence_ids: tuple[str, ...]
    outcome: str
    rationale: str


@dataclass(frozen=True, slots=True)
class HandoffPacket:
    message_id: str
    created_at_unix: float
    handoff_id: str
    decision_ids: tuple[str, ...]
    next_action: str
    blockers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SIPolicyTicket:
    """V8-new advisory card emitted by the SI 12-lane layer.

    Carries a *proposed* configuration change. SI tickets are advisory only —
    they never auto-apply. ``lane_kind`` records which of the four SI lane
    kinds emitted the ticket (proposer/rollback_guard/outcome_observer/
    ticket_writer). ``proposed_changes`` is a tuple of (key, value_json) pairs
    so the payload stays JSON-serializable.
    """

    message_id: str
    created_at_unix: float
    ticket_id: str
    source_decision_id: str
    config_namespace: str
    proposed_changes: tuple[tuple[str, str], ...]
    rollback_risk: float
    lane_kind: str


MESSAGE_TYPES: tuple[type, ...] = (
    TaskPacket,
    EvidenceCard,
    ArtifactManifest,
    VerificationTicket,
    CriticTicket,
    DisagreementTicket,
    DecisionRecord,
    HandoffPacket,
    SIPolicyTicket,
)


def _now_unix() -> float:
    """Test-overridable wall clock."""
    return time.time()


__all__ = [
    "ArtifactManifest",
    "CriticTicket",
    "DecisionRecord",
    "DisagreementTicket",
    "EvidenceCard",
    "HandoffPacket",
    "MESSAGE_TYPES",
    "SIPolicyTicket",
    "TaskPacket",
    "VerificationTicket",
    "make_message_id",
]
