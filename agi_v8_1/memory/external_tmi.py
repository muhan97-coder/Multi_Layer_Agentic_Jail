"""Public validation and explicit adapter for an operator-owned TMI endpoint.

No personal-memory paths, daemon discovery, model download, automatic spawn or
fallback live here. Transport belongs to the T9 payload. Endpoint identity and
vector space must be supplied by the operator, never inferred from a response.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
from pathlib import PurePosixPath

from agi_v8_1.capabilities import PayloadPort, resolve_payload
from agi_v8_1.memory.distilled_contract import _validated_vector_batch


@dataclass(frozen=True)
class TMIEndpoint:
    socket_path: str
    model: str
    dimension: int
    timeout_s: float = 30.0


@dataclass(frozen=True)
class EmbeddingBatch:
    vectors: tuple[bytes, ...]
    model: str
    dimension: int
    transport: str
    response_sha256: str


def validate_endpoint(endpoint: TMIEndpoint) -> None:
    if (type(endpoint) is not TMIEndpoint or type(endpoint.socket_path) is not str
            or not endpoint.socket_path.startswith("/") or "\x00" in endpoint.socket_path
            or ".." in PurePosixPath(endpoint.socket_path).parts
            or len(endpoint.socket_path.encode("utf-8")) > 100
            or type(endpoint.model) is not str or not endpoint.model.strip()
            or len(endpoint.model) > 200 or any(ord(c) < 32 for c in endpoint.model)
            or type(endpoint.dimension) is not int or not 1 <= endpoint.dimension <= 8192
            or type(endpoint.timeout_s) not in (int, float)
            or not math.isfinite(endpoint.timeout_s) or not 0 < endpoint.timeout_s <= 120):
        raise ValueError("TMI endpoint contract invalid")


def validate_response(response: object, *, endpoint: TMIEndpoint, count: int) -> EmbeddingBatch:
    validate_endpoint(endpoint)
    if (type(count) is not int or not 1 <= count <= 128 or type(response) is not dict
            or "error" in response or type(response.get("dim")) is not int
            or response["dim"] != endpoint.dimension
            or type(response.get("count")) is not int or response["count"] != count
            or response.get("model_version") != endpoint.model
            or type(response.get("bytes_hex")) is not str
            or len(response["bytes_hex"]) != count * endpoint.dimension * 8):
        raise ValueError("TMI response contract mismatch")
    try:
        raw = bytes.fromhex(response["bytes_hex"])
    except ValueError:
        raise ValueError("TMI vector encoding invalid") from None
    checked = _validated_vector_batch(raw, dim=endpoint.dimension, count=count,
                                      transport="external-unix")
    width = endpoint.dimension * 4
    return EmbeddingBatch(tuple(checked[i:i + width] for i in range(0, len(checked), width)),
                          endpoint.model, endpoint.dimension, "external-unix",
                          hashlib.sha256(checked).hexdigest())


def encode(texts: tuple[str, ...], *, endpoint: TMIEndpoint, kind: str = "passage") -> EmbeddingBatch:
    """Use exactly one explicit endpoint; refusal/error never loads a model."""
    validate_endpoint(endpoint)
    if (type(texts) is not tuple or not 1 <= len(texts) <= 128
            or kind not in ("query", "passage")
            or any(type(text) is not str or not text.strip() for text in texts)
            or sum(len(text) + len(kind) + 2 for text in texts) > 16000):
        raise ValueError("TMI request contract invalid")
    exchange = resolve_payload(PayloadPort(9, "agi_v8_1.memory.external_tmi_payload", "exchange"))
    response = exchange(endpoint, {"texts": [kind + ": " + text for text in texts]})
    return validate_response(response, endpoint=endpoint, count=len(texts))
