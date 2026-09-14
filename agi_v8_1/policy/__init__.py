"""agi_v8_1 R25 W4 policy layer.

Ported from v7.1/agent_system/v7/policy (4 files, all preserving R9.S7 +
R9.1 invariants — bounded recursion, cycle guard, fail-closed defaults).

  - secret_masker: bounded-recursion regex masking + ``contains_secret``
  - raw_media_policy: suffix + MIME + magic-byte sniffing
  - retention_decider: data-class default retention with secret/legal_hold
    precedence rules
  - privacy_policy: composes the three above
"""

from .secret_masker import (
    mask_text,
    mask_obj,
    mask_payload,
    contains_secret,
)
from .raw_media_policy import (
    is_raw_media,
    decide_raw_media,
)
from .retention_decider import (
    decide_retention,
    DEFAULT_RETENTION_DAYS,
)
from .privacy_policy import (
    classify_payload,
    apply_privacy_policy,
    evaluate_privacy_policy,
)

__all__ = [
    "mask_text",
    "mask_obj",
    "mask_payload",
    "contains_secret",
    "is_raw_media",
    "decide_raw_media",
    "decide_retention",
    "DEFAULT_RETENTION_DAYS",
    "classify_payload",
    "apply_privacy_policy",
    "evaluate_privacy_policy",
]
