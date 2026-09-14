"""R33 — per-modality payload schemas.

Each schema is a frozen, slot-backed dataclass describing the shape of a
modality payload reference *header* — never the raw bytes.  Lanes that
need to fetch the underlying media do so via their own (gated) routes.

Schemas:
  * :class:`VisionFrame` — image / scene caption header.
  * :class:`AudioClip` — audio header (duration / sample rate / format).
  * :class:`SensorReading` — IMU / GPS / accelerometer reading.
  * :class:`WorldState` — Minecraft mc / robot world-state snapshot ref.
"""

# __R33_SLOT__

from __future__ import annotations

from dataclasses import dataclass

from .task import ModalityType


@dataclass(frozen=True, slots=True)
class VisionFrame:
    """Header for a single vision frame (image / scene caption)."""

    payload_ref: str
    width_px: int
    height_px: int
    captured_at_unix: float
    caption: str = ""

    def __post_init__(self) -> None:  # pragma: no cover - exercised by tests
        if self.width_px <= 0 or self.height_px <= 0:
            raise ValueError("VisionFrame dimensions must be positive")


@dataclass(frozen=True, slots=True)
class AudioClip:
    """Header for an audio clip."""

    payload_ref: str
    duration_s: float
    sample_rate_hz: int
    channels: int
    captured_at_unix: float

    def __post_init__(self) -> None:  # pragma: no cover - exercised by tests
        if self.duration_s < 0:
            raise ValueError("AudioClip duration_s must be >= 0")
        if self.sample_rate_hz <= 0:
            raise ValueError("AudioClip sample_rate_hz must be positive")
        if self.channels <= 0:
            raise ValueError("AudioClip channels must be positive")


@dataclass(frozen=True, slots=True)
class SensorReading:
    """Header for a phone / robot sensor sample.

    ``sensor_kind`` is a free-form string (e.g. ``"imu"`` / ``"gps"`` /
    ``"accelerometer"``).  ``values`` is a tuple of floats so the card
    stays hashable + JSON-friendly.  ``units`` is parallel to ``values``.
    """

    payload_ref: str
    sensor_kind: str
    captured_at_unix: float
    values: tuple[float, ...]
    units: tuple[str, ...]

    def __post_init__(self) -> None:  # pragma: no cover - exercised by tests
        if not self.sensor_kind:
            raise ValueError("SensorReading.sensor_kind must be non-empty")
        if len(self.values) != len(self.units):
            raise ValueError(
                "SensorReading.values and .units must have equal length"
            )


@dataclass(frozen=True, slots=True)
class WorldState:
    """Header for a world-state snapshot (Minecraft mc / robot state)."""

    payload_ref: str
    world_kind: str  # e.g. "minecraft" | "reachy_mini" | "robot_arm"
    snapshot_at_unix: float
    entity_count: int

    def __post_init__(self) -> None:  # pragma: no cover - exercised by tests
        if not self.world_kind:
            raise ValueError("WorldState.world_kind must be non-empty")
        if self.entity_count < 0:
            raise ValueError("WorldState.entity_count must be >= 0")


# Modality -> schema class lookup (TEXT has no dedicated schema —
# the TaskPacket / EvidenceCard text fields are already sufficient.)
_MODALITY_SCHEMA: dict[ModalityType, type] = {
    ModalityType.VISION: VisionFrame,
    ModalityType.AUDIO: AudioClip,
    ModalityType.SENSOR: SensorReading,
    ModalityType.WORLD: WorldState,
}


def schema_for_modality(modality: ModalityType) -> type | None:
    """Return the schema class for ``modality`` (or ``None`` for TEXT).

    The mapping is read-only — callers receive the class object directly.
    """
    return _MODALITY_SCHEMA.get(modality)


__all__ = [
    "AudioClip",
    "SensorReading",
    "VisionFrame",
    "WorldState",
    "schema_for_modality",
]
