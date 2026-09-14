"""Public, transport-free validation contracts for distilled vector data."""
from __future__ import annotations

import math
import re
import sqlite3
import struct
from typing import Optional

_VECTOR_NORM_TOLERANCE = 5e-3
_VEC_DDL_DIM = re.compile(r"float\s*\[\s*(\d+)\s*\]", re.IGNORECASE)


def stored_vec_dim(conn: sqlite3.Connection) -> Optional[int]:
    """Vector width the index actually stores, parsed from its own DDL."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='vec_entries'"
    ).fetchone()
    if not row or not row[0]:
        return None
    m = _VEC_DDL_DIM.search(row[0])
    return int(m.group(1)) if m else None


def _validated_vector_batch(
    raw: bytes, *, dim: int, count: int, transport: str
) -> bytes:
    """Validate one transport response before any caller can persist/use it.

    The wire metadata is untrusted even on the local socket.  A declared
    dimension is not evidence that the byte payload has that shape, and
    sqlite-vec cannot make NaN/Inf or all-zero embeddings meaningful.  Keep
    this check shared by passage and query callers so neither path can accept
    a truncated/different contract.
    """
    expected = count * dim * 4
    if len(raw) != expected:
        raise RuntimeError(
            f"embed {transport} returned an invalid vector payload size"
        )
    for vector_index in range(count):
        start = vector_index * dim * 4
        stop = start + dim * 4
        norm_sq = 0.0
        for (value,) in struct.iter_unpack("<f", raw[start:stop]):
            if not math.isfinite(value):
                raise RuntimeError(
                    f"embed {transport} returned a non-finite vector"
                )
            norm_sq += float(value) * float(value)
        if (
            not math.isfinite(norm_sq)
            or norm_sq <= 0.0
            or abs(norm_sq - 1.0) > _VECTOR_NORM_TOLERANCE
        ):
            raise RuntimeError(
                f"embed {transport} returned a non-normalized/invalid vector"
            )
    return raw
