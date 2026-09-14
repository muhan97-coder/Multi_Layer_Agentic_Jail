"""R33 — Unified multimodal task abstraction layer.

This package is a *new* abstraction (no v7.1 counterpart) that wraps the
existing R18.5 :class:`agi_v8_1.core.messages.TaskPacket` and
:class:`agi_v8_1.core.messages.EvidenceCard` with modality-aware enrichment.

It is purely *additive* — every constructor/signature in
``agi_v8_1.core.messages`` is untouched. ``MultimodalTask`` carries a
``TaskPacket`` field, so existing R17-R26 pipelines that consume
``TaskPacket`` continue to work unchanged.

Public surface:
  * :class:`ModalityType` — text / vision / audio / sensor / world
  * :class:`MultimodalTask` — TaskPacket wrapper with modality metadata
  * :class:`MultimodalEvidenceCard` — EvidenceCard wrapper
  * :class:`MultimodalDispatcher` — modality -> lane router (additive)
  * :class:`VisionFrame` / :class:`AudioClip` / :class:`SensorReading`
    / :class:`WorldState` — modality-specific schemas

All env knobs default OFF (advisory). No subprocess / requests / urllib /
socket / http imports at module top.
"""

# __R33_SLOT__

from __future__ import annotations

from .task import (
    ENV_MULTIMODAL_ENABLED,
    ENV_DISPATCHER_ENABLED,
    ENV_REMOTE_PAYLOAD_REFS_ENABLED,
    ModalityType,
    MultimodalTask,
    PayloadRefKind,
    classify_payload_ref,
    multimodal_enabled,
    dispatcher_enabled,
    payload_ref_boundary,
    remote_payload_refs_enabled,
    wrap_task_packet,
)
from .dispatcher import (
    MultimodalDispatcher,
    LaneAssignment,
    DEFAULT_MODALITY_LANE_MAP,
)
from .evidence import MultimodalEvidenceCard, wrap_evidence_card
from .schema import (
    AudioClip,
    SensorReading,
    VisionFrame,
    WorldState,
    schema_for_modality,
)

__all__ = [
    "ENV_DISPATCHER_ENABLED",
    "ENV_MULTIMODAL_ENABLED",
    "ENV_REMOTE_PAYLOAD_REFS_ENABLED",
    "AudioClip",
    "DEFAULT_MODALITY_LANE_MAP",
    "LaneAssignment",
    "ModalityType",
    "MultimodalDispatcher",
    "MultimodalEvidenceCard",
    "MultimodalTask",
    "PayloadRefKind",
    "SensorReading",
    "VisionFrame",
    "WorldState",
    "classify_payload_ref",
    "dispatcher_enabled",
    "multimodal_enabled",
    "payload_ref_boundary",
    "remote_payload_refs_enabled",
    "schema_for_modality",
    "wrap_evidence_card",
    "wrap_task_packet",
]
