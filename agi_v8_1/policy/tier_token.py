"""Offline public-tier token verification; no issuer or key provisioning here.

The release trust map deliberately starts empty. Never obtain a verification
key, expected subject, or expected release from the submitted token itself.
Signatures cover canonical ASCII JSON of the exact claims object, not a JSON
serialization chosen by a client. Cryptography is imported only after bounded
structure, trust, identity, and time checks pass.
"""
from __future__ import annotations

import base64
import json
import re
from types import MappingProxyType
from typing import Mapping

SCHEMA = "agi-v8-public-tier-token/v1"
TRUSTED_KEYS: Mapping[tuple[str, str], bytes] = MappingProxyType({})
_CLAIMS = frozenset({"issuer", "key_id", "tier", "user_id", "repo_commit",
                     "evidence_digest", "issued_at", "expiry"})
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}\Z")


class TokenRefused(ValueError):
    """Fixed, non-secret reason; never includes submitted data or crypto errors."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__("public_tier_token_refused:" + reason)


def canonical_claims(claims: dict) -> bytes:
    return json.dumps(claims, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise TokenRefused("duplicate_key")
        result[key] = value
    return result


def parse_token(raw: bytes) -> dict:
    if type(raw) is not bytes or not 0 < len(raw) <= 8192:
        raise TokenRefused("token_size")
    try:
        result = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeError, ValueError, RecursionError):
        raise TokenRefused("token_encoding") from None
    if type(result) is not dict:
        raise TokenRefused("token_shape")
    return result


def verify_token(token: dict, *, issuer: str, user_id: str, repo_commit: str,
                 evidence_digest: str, now: int,
                 trusted_keys: Mapping[tuple[str, str], bytes] | None = None) -> dict:
    """Verify exact caller-owned bindings and return a fresh claims object.

    ``trusted_keys`` is an explicit trusted-code/test seam, not a request field.
    Production callers omit it and use the reviewed release trust map.
    """
    if type(token) is not dict or set(token) != {"schema", "claims", "signature"}:
        raise TokenRefused("token_shape")
    if token["schema"] != SCHEMA:
        raise TokenRefused("token_schema")
    claims = token["claims"]
    if type(claims) is not dict or set(claims) != _CLAIMS:
        raise TokenRefused("claims_shape")
    for field in ("issuer", "key_id", "user_id"):
        if type(claims[field]) is not str or _IDENTIFIER.fullmatch(claims[field]) is None:
            raise TokenRefused("claims_identity")
    for field, count in (("repo_commit", 40), ("evidence_digest", 64)):
        if type(claims[field]) is not str or re.fullmatch(r"[0-9a-f]{%d}" % count, claims[field]) is None:
            raise TokenRefused("claims_digest")
    tier = claims["tier"]
    if type(tier) is not int or not 3 <= tier <= 9:
        raise TokenRefused("claims_tier")
    if any(type(claims[k]) is not int or not 0 <= claims[k] <= 253402300799
           for k in ("issued_at", "expiry")) or type(now) is not int or now < 0:
        raise TokenRefused("claims_time")
    lifetime = claims["expiry"] - claims["issued_at"]
    if not 0 < lifetime <= (30 if tier == 9 else 90) * 86400:
        raise TokenRefused("claims_lifetime")
    if not claims["issued_at"] <= now < claims["expiry"]:
        raise TokenRefused("token_expired_or_future")
    expected = {"issuer": issuer, "user_id": user_id, "repo_commit": repo_commit,
                "evidence_digest": evidence_digest}
    if any(type(value) is not str or not value for value in expected.values()):
        raise TokenRefused("expected_identity_missing")
    if any(claims[field] != value for field, value in expected.items()):
        raise TokenRefused("identity_mismatch")
    keys = TRUSTED_KEYS if trusted_keys is None else trusted_keys
    public_key = keys.get((issuer, claims["key_id"]))
    if type(public_key) is not bytes or len(public_key) != 32:
        raise TokenRefused("issuer_unarmed")
    signature = token["signature"]
    if type(signature) is not str or re.fullmatch(r"[A-Za-z0-9_-]{86}", signature) is None:
        raise TokenRefused("signature_encoding")
    try:
        decoded = base64.b64decode(signature + "==", altchars=b"-_", validate=True)
    except ValueError:
        raise TokenRefused("signature_encoding") from None
    if base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=") != signature:
        raise TokenRefused("signature_encoding")
    try:
        from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        raise TokenRefused("verifier_unavailable") from None
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(decoded, canonical_claims(claims))
    except (InvalidSignature, UnsupportedAlgorithm, ValueError):
        raise TokenRefused("signature_invalid") from None
    return dict(claims)
