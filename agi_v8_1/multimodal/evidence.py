"""R33 — :class:`MultimodalEvidenceCard` — EvidenceCard enrichment wrapper.

Mirrors the :class:`MultimodalTask` pattern: wraps an existing R18.5
:class:`EvidenceCard` with modality metadata + payload refs, without
modifying the wrapped card.  R17-R26 lanes that consume ``EvidenceCard``
continue to work — they read ``.evidence_card``.
"""

# __R33_SLOT__

from __future__ import annotations

from dataclasses import dataclass

from agi_v8_1.core.messages import EvidenceCard

from .task import PayloadRefKind, classify_payload_ref, payload_ref_boundary, ModalityType


@dataclass(frozen=True, slots=True)
class MultimodalEvidenceCard:
    """A modality-tagged enrichment of an :class:`EvidenceCard`.

    Fields:
      * ``evidence_id`` — copy of the wrapped card's ``evidence_id``
        (asserted equal in ``__post_init__``).
      * ``modality`` — the modality that produced this evidence.
      * ``payload_refs`` — opaque string refs (URIs / paths) describing
        the modality payload.  Not fetched here.
      * ``evidence_card`` — the wrapped R18.5 :class:`EvidenceCard`.
    """

    evidence_id: str
    modality: ModalityType
    payload_refs: tuple[str, ...]
    evidence_card: EvidenceCard

    def __post_init__(self) -> None:  # pragma: no cover - exercised by tests
        if self.evidence_id != self.evidence_card.evidence_id:
            raise ValueError(
                f"MultimodalEvidenceCard.evidence_id ({self.evidence_id!r}) "
                f"must match evidence_card.evidence_id "
                f"({self.evidence_card.evidence_id!r})"
            )
        if not isinstance(self.modality, ModalityType):
            raise TypeError(
                f"modality must be ModalityType, got "
                f"{type(self.modality).__name__}"
            )

    def payload_ref_kinds(self) -> tuple[PayloadRefKind, ...]:
        """Classify payload references without fetching them."""

        return tuple(classify_payload_ref(ref) for ref in self.payload_refs)

    def payload_boundary(self) -> dict[str, object]:
        """Return explicit payload-ref boundary metadata."""

        return payload_ref_boundary(self.payload_refs)


def wrap_evidence_card(
    card: EvidenceCard,
    modality: ModalityType = ModalityType.TEXT,
    payload_refs: tuple[str, ...] = (),
) -> MultimodalEvidenceCard:
    """Build a :class:`MultimodalEvidenceCard` around an ``EvidenceCard``.

    Defaults to ``TEXT`` so existing V8 consumers can opt-in incrementally.
    """
    return MultimodalEvidenceCard(
        evidence_id=card.evidence_id,
        modality=modality,
        payload_refs=tuple(payload_refs),
        evidence_card=card,
    )


__all__ = [
    "MultimodalEvidenceCard",
    "wrap_evidence_card",
]
