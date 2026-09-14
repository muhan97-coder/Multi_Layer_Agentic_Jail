"""R33 — :class:`MultimodalDispatcher` — modality -> lane mapping (additive).

The dispatcher is a *thin advisory layer*: it inspects a
:class:`MultimodalTask` and returns a :class:`LaneAssignment` describing
which V8 pod/lane should handle the work.  It does **not** call the
swarm router, does **not** mutate the underlying ``TaskPacket``, and is
fully OFF by default (gated by ``AGI_V8_MULTIMODAL_DISPATCHER_ENABLED``).

The mapping below is intentionally conservative — modality lanes that
have no concrete V8 pod (audio / sensor / world today) map to the
``"meta"`` lane, which advisory consumers treat as "needs further
review".  Vision routes to ``pod_a`` so the existing pod_a/pod_b
acceptance gating sees it.
"""

# __R33_SLOT__

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .task import ModalityType, MultimodalTask, dispatcher_enabled


# ---------------------------------------------------------------------------
# Default modality -> lane map (advisory; configurable per dispatcher)
# ---------------------------------------------------------------------------

DEFAULT_MODALITY_LANE_MAP: Mapping[ModalityType, str] = {
    ModalityType.TEXT: "pod_a",
    ModalityType.VISION: "pod_a_vision",
    ModalityType.AUDIO: "meta",
    ModalityType.SENSOR: "meta",
    ModalityType.WORLD: "world",
}


@dataclass(frozen=True, slots=True)
class LaneAssignment:
    """Advisory routing record produced by :class:`MultimodalDispatcher`.

    Fields:
      * ``task_id`` — copy of the source task id.
      * ``primary_lane`` — lane chosen for the primary modality.
      * ``secondary_lanes`` — lanes for each secondary modality, in order.
      * ``advisory`` — always ``True``; this dispatcher never mutates the
        downstream router.
    """

    task_id: str
    primary_lane: str
    secondary_lanes: tuple[str, ...]
    advisory: bool = True


class MultimodalDispatcher:
    """Pure-Python advisory dispatcher.

    Constructor accepts an optional ``lane_map`` override (handy for tests
    and for downstream pods that want a custom routing table).
    """

    __slots__ = ("_lane_map",)

    def __init__(self, lane_map: Mapping[ModalityType, str] | None = None) -> None:
        # Defensive copy so callers can't mutate our table after the fact.
        src = lane_map if lane_map is not None else DEFAULT_MODALITY_LANE_MAP
        self._lane_map: Mapping[ModalityType, str] = dict(src)
        # Validate keys are real ModalityType members; values are non-empty.
        for k, v in self._lane_map.items():
            if not isinstance(k, ModalityType):
                raise TypeError(
                    f"lane_map key must be ModalityType, got {type(k).__name__}"
                )
            if not isinstance(v, str) or not v:
                raise ValueError(f"lane_map[{k!r}] must be non-empty str")

    @property
    def lane_map(self) -> Mapping[ModalityType, str]:
        """Read-only view of the active mapping."""
        return dict(self._lane_map)

    def lane_for(self, modality: ModalityType) -> str:
        """Return the lane label for a single modality."""
        return self._lane_map[modality]

    def dispatch(self, task: MultimodalTask) -> LaneAssignment:
        """Produce an advisory :class:`LaneAssignment` for ``task``.

        Always returns a record (never raises on disabled). When the
        dispatcher env knob is OFF, ``primary_lane`` is ``"disabled"``
        and ``secondary_lanes`` is empty so downstream consumers can
        detect the no-op state.
        """
        if not dispatcher_enabled():
            return LaneAssignment(
                task_id=task.task_id,
                primary_lane="disabled",
                secondary_lanes=(),
                advisory=True,
            )
        primary = self.lane_for(task.primary_modality)
        secondary = tuple(self.lane_for(m) for m in task.secondary_modalities)
        return LaneAssignment(
            task_id=task.task_id,
            primary_lane=primary,
            secondary_lanes=secondary,
            advisory=True,
        )


__all__ = [
    "DEFAULT_MODALITY_LANE_MAP",
    "LaneAssignment",
    "MultimodalDispatcher",
]
