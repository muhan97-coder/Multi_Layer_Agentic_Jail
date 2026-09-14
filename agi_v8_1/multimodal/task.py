"""R33 — :class:`MultimodalTask` (TaskPacket enrichment wrapper).

A ``MultimodalTask`` *wraps* an existing R18.5 :class:`TaskPacket` with
modality metadata. It never alters the wrapped packet — backward compat
with all R17-R26 consumers is preserved by exposing ``.task_packet`` to
downstream lanes.
"""

# __R33_SLOT__

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum

from agi_v8_1.core.messages import TaskPacket, make_message_id


# ---------------------------------------------------------------------------
# Env knobs (default OFF — advisory)
# ---------------------------------------------------------------------------

ENV_MULTIMODAL_ENABLED = "AGI_V8_MULTIMODAL_ENABLED"
ENV_DISPATCHER_ENABLED = "AGI_V8_MULTIMODAL_DISPATCHER_ENABLED"
ENV_REMOTE_PAYLOAD_REFS_ENABLED = "AGI_V8_MULTIMODAL_REMOTE_REFS_ENABLED"


def _truthy(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "on", "yes")


def multimodal_enabled() -> bool:
    return _truthy(os.environ.get(ENV_MULTIMODAL_ENABLED, ""))  # tier: T4


def dispatcher_enabled() -> bool:
    return _truthy(os.environ.get(ENV_DISPATCHER_ENABLED, ""))  # tier: T4


def remote_payload_refs_enabled() -> bool:
    return _truthy(os.environ.get(ENV_REMOTE_PAYLOAD_REFS_ENABLED, ""))  # tier: T4


# ---------------------------------------------------------------------------
# Modality enum (5 modalities)
# ---------------------------------------------------------------------------


class ModalityType(StrEnum):
    """Five supported modality kinds."""

    TEXT = "text"
    VISION = "vision"
    AUDIO = "audio"
    SENSOR = "sensor"
    WORLD = "world"


class PayloadRefKind(StrEnum):
    """Boundary classification for multimodal payload references."""

    EMPTY = "empty"
    LOCAL = "local"
    REMOTE = "remote"
    OPAQUE = "opaque"


# Public count constant — invariants test pins this to 5.
MODALITY_COUNT: int = len(ModalityType)

_REMOTE_REF_SCHEMES = frozenset({"http", "https", "ws", "wss"})
_LOCAL_REF_SCHEMES = frozenset({"file"})


def classify_payload_ref(payload_ref: str) -> PayloadRefKind:
    """Classify a payload ref without fetching or opening it."""

    ref = (payload_ref or "").strip()
    if not ref:
        return PayloadRefKind.EMPTY
    scheme, sep, _rest = ref.partition(":")
    if sep and scheme:
        normalized_scheme = scheme.lower()
        if normalized_scheme in _REMOTE_REF_SCHEMES:
            return PayloadRefKind.REMOTE
        if normalized_scheme in _LOCAL_REF_SCHEMES:
            return PayloadRefKind.LOCAL
        return PayloadRefKind.OPAQUE
    if ref.startswith("/") or ref.startswith("./") or ref.startswith("../"):
        return PayloadRefKind.LOCAL
    return PayloadRefKind.OPAQUE


def payload_ref_boundary(payload_refs: tuple[str, ...]) -> dict[str, object]:
    """Summarize payload-ref boundary mode without resolving refs."""

    kinds = tuple(classify_payload_ref(ref) for ref in payload_refs)
    has_remote = any(kind is PayloadRefKind.REMOTE for kind in kinds)
    remote_enabled = remote_payload_refs_enabled()
    return {
        "kinds": tuple(kind.value for kind in kinds),
        "has_remote": has_remote,
        "remote_allowed": bool(has_remote and remote_enabled),
        "mode": "remote_ref" if has_remote else "local_or_opaque",
        "remote_fetch_attempted": False,
    }


# ---------------------------------------------------------------------------
# MultimodalTask — frozen, slot-backed
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MultimodalTask:
    """A modality-tagged enrichment of a :class:`TaskPacket`.

    Fields:
      * ``task_id`` — copy of the wrapped packet's ``task_id`` (denorm for
        fast lookup; we assert equality in ``__post_init__``).
      * ``primary_modality`` — the dominant modality kind.
      * ``secondary_modalities`` — additional modalities present
        (e.g., a vision task with audio narration).
      * ``payload_refs`` — opaque string handles (paths / blob ids / URIs)
        that *describe* the multimodal payload without embedding it.
        We never fetch them.
      * ``task_packet`` — the wrapped R18.5 ``TaskPacket``. Untouched.
    """

    task_id: str
    primary_modality: ModalityType
    secondary_modalities: tuple[ModalityType, ...]
    payload_refs: tuple[str, ...]
    task_packet: TaskPacket

    def __post_init__(self) -> None:  # pragma: no cover - exercised by tests
        if self.task_id != self.task_packet.task_id:
            raise ValueError(
                f"MultimodalTask.task_id ({self.task_id!r}) must match "
                f"task_packet.task_id ({self.task_packet.task_id!r})"
            )
        if not isinstance(self.primary_modality, ModalityType):
            raise TypeError(
                f"primary_modality must be ModalityType, got "
                f"{type(self.primary_modality).__name__}"
            )
        for m in self.secondary_modalities:
            if not isinstance(m, ModalityType):
                raise TypeError(
                    "secondary_modalities entries must be ModalityType, "
                    f"got {type(m).__name__}"
                )
        if self.primary_modality in self.secondary_modalities:
            raise ValueError(
                "primary_modality must not also appear in secondary_modalities"
            )

    def all_modalities(self) -> tuple[ModalityType, ...]:
        """Return primary + secondary, primary first, in insertion order."""
        return (self.primary_modality,) + tuple(self.secondary_modalities)

    def has_modality(self, modality: ModalityType) -> bool:
        return modality is self.primary_modality or modality in self.secondary_modalities

    def payload_ref_kinds(self) -> tuple[PayloadRefKind, ...]:
        """Classify payload references without fetching them."""

        return tuple(classify_payload_ref(ref) for ref in self.payload_refs)

    def payload_boundary(self) -> dict[str, object]:
        """Return explicit payload-ref boundary metadata."""

        return payload_ref_boundary(self.payload_refs)


def wrap_task_packet(
    packet: TaskPacket,
    primary_modality: ModalityType = ModalityType.TEXT,
    secondary_modalities: tuple[ModalityType, ...] = (),
    payload_refs: tuple[str, ...] = (),
) -> MultimodalTask:
    """Build a :class:`MultimodalTask` around an existing ``TaskPacket``.

    Backward-compatible default — when called with no extra args, returns a
    text-only wrapper, which is the V8 status-quo behavior.
    """
    return MultimodalTask(
        task_id=packet.task_id,
        primary_modality=primary_modality,
        secondary_modalities=tuple(secondary_modalities),
        payload_refs=tuple(payload_refs),
        task_packet=packet,
    )


__all__ = [
    "ENV_DISPATCHER_ENABLED",
    "ENV_MULTIMODAL_ENABLED",
    "ENV_REMOTE_PAYLOAD_REFS_ENABLED",
    "MODALITY_COUNT",
    "ModalityType",
    "MultimodalTask",
    "PayloadRefKind",
    "classify_payload_ref",
    "dispatcher_enabled",
    "make_message_id",
    "multimodal_enabled",
    "payload_ref_boundary",
    "remote_payload_refs_enabled",
    "wrap_task_packet",
]
