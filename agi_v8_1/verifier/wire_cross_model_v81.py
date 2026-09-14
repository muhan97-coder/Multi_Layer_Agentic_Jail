"""R8 — cross_model_verifier production adapter (audit claim C14 반증).

Audit claim C14 CONFIRMED: ``verifier/cross_model_verifier.py`` (the A-29
bridge addendum) had ZERO production import — the only importer was
``tests/v8/test_cross_model_verifier.py``. It was dead code: a second-opinion
verifier that nothing ever consulted.

R8 wires it as an *optional* second opinion in the SI loop's S3 verify stage
(``self_improvement_v8._run_verify_stage``). The wiring is a thin gated
adapter so that:

  * gate-OFF (default) == byte-equivalent no-op. ``run_cross_model_verify``
    returns ``None`` *before constructing the verifier or calling any
    adversary* — no provider cost, no network, no behavioural change. The
    verify verdict is exactly what it was without cross-model.
  * gate-ON (operator opt-in, cost) == ``CrossModelVerifier.evaluate`` runs a
    2nd-opinion adversary over the proposed-change spec. A *refuted* result
    BLOCKS the apply (verdict ok=False).

The grep call SITE lives in ``_run_verify_stage`` even when the gate is OFF
(anti-amnesia: the wiring must be visible in production source so a future
refactor that severs it is caught by ``tests/v8_1/test_anti_amnesia_wiring.py``,
not silently re-dead like C14).

Cost note (PART1 §5.3): ON routes a DeepSeek thinking lane as the adversary →
token cost. Hence ``AGI_V81_CROSS_MODEL_VERIFY_ENABLED`` is default-OFF
operator-approve, NOT part of the default-ON learning read loop. A real provider
adversary is injected by the caller (``adversary=...``); this module never spins
up a provider itself, honouring the no-network default and avoiding a silent
provider fallback (feedback_deepseek_flash_silent_fallback).

FAIL-CLOSED no-adversary contract (2026-07-25 audit A1 fix): the earlier design
used a deterministic *echo* adversary as the gate-ON default. That echo agrees
with any non-empty spec (agreement_score 1.0 → refuted False), so flipping the
gate ON *without* wiring a real second model produced silent verification
theatre — a verdict indistinguishable from a genuine agreement, on every apply.
That is exactly the feedback_deepseek_flash_silent_fallback failure class. Per
the v8.1 verify contract ("provider OFF / no real check == REJECT, never
approve"), gate-ON with ``adversary is None`` now BLOCKS (refuted=True,
reason=``no_independent_adversary_wired``) instead of echo-passing. Operators arm
a genuine second opinion by injecting ``adversary=...``; until they do, the gate
being ON is a loud block, not a quiet pass. Gate-OFF (default) is unchanged: a
byte-identical no-op that returns ``None`` before touching anything.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Callable, Mapping

# __SLOT_FAIL_FAST_2026_07_25__ Swallowed failures route through one choke
# point: counted + named always, re-raised under AGI_V8_STRICT_FAIL_FAST.
from agi_v8_1.policy.fail_fast import swallowed as _swallowed, record_critical_failure
from agi_v8_1.runtime import halt_sentinel

# Default-OFF operator-approve gate. Strict match (exact "true"/"1") mirrors the
# rest of the v8.1 gate convention. Unset == OFF == no-op (no provider cost).
CROSS_MODEL_VERIFY_ENV_ENABLED = "AGI_V81_CROSS_MODEL_VERIFY_ENABLED"


def cross_model_verify_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Default-OFF cross-model second-opinion gate.

    True iff ``AGI_V81_CROSS_MODEL_VERIFY_ENABLED`` is exactly ``"true"`` or
    ``"1"``. Any other value (including unset) leaves the second opinion
    dormant → the S3 verify verdict is byte-identical to the no-cross-model
    path (no verifier constructed, no adversary called).
    """
    source = env if env is not None else os.environ
    return source.get(CROSS_MODEL_VERIFY_ENV_ENABLED, "") in ("true", "1")  # tier: T6


def run_cross_model_verify(
    spec: Mapping[str, Any],
    *,
    adversary: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    env: Mapping[str, str] | None = None,
    agreement_threshold: float = 0.5,
) -> dict[str, Any] | None:
    """Optional 2nd-opinion verify hook for the SI S3 stage (C14 wire).

    Returns ``None`` when the gate is OFF (default) — the caller treats a
    ``None`` result as "no second opinion ran" and leaves its verdict
    unchanged. When ON, returns a JSON-serialisable summary::

        {"agree": bool, "agreement_score": float, "refuted": bool,
         "spec_hash": str, "evidence": list[str]}

    ``refuted=True`` (agreement below threshold) is the BLOCK signal the
    caller folds into its verify verdict. The summary also carries
    ``"adversary"`` (``"injected"`` | ``"none"``) and ``"reason"`` so the caller
    can tell a genuine refutation apart from a no-adversary block.

    Gate-OFF returns BEFORE constructing :class:`CrossModelVerifier` or
    calling any adversary, so there is zero provider cost / network when the
    operator has not opted in.

    FAIL-CLOSED (audit A1): gate-ON with ``adversary is None`` means no real
    second opinion is wired. Rather than echo-pass (silent theatre), it returns
    a refuted verdict (``reason="no_independent_adversary_wired"``) so the apply
    is BLOCKED and the misconfiguration is visible, not silently green.
    """
    if not cross_model_verify_enabled(env):
        return None

    # __SLOT_HALT_K0_PRE_SPEND_2026_08_07__ 교차모델 검증은 **매 사이클 실호출**되고
    # (실측 $0.0724/일) 별도 provider 를 쓴다 — `ProviderManager` 의 K0 를 안 탄다.
    # 그래서 여기가 이 경로의 pre-spend 초크포인트다. 게이트 검사 **뒤**, adversary
    # 구성/호출 **앞**이라 아직 한 푼도 안 썼다.
    #
    # ⛔ 하류의 `cross_model_verifier.py` 는 adversary 호출을 `except BaseException`
    #    으로 감싸 **default-refute** 로 접는다(안티패턴 #9 방어). 그 안에서 던지면
    #    사람의 정지가 "적대검증이 반박했다"로 기록된다 — 그래서 그보다 **위**다.
    halt_sentinel.raise_if_halted()

    if adversary is None:
        # No genuine independent adversary → no real check. Fail-closed: BLOCK
        # (loud) instead of echo-passing (silent theatre). No verifier is built
        # and no network/provider is touched.
        try:
            canonical = json.dumps(
                dict(spec), sort_keys=True, separators=(",", ":"), ensure_ascii=False
            )
            spec_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        except BaseException as _ff_exc:  # noqa: BLE001 — hashing must never leak a traceback
            _swallowed(_ff_exc, site="verifier.wire_cross_model_v81.run_cross_model_verify:112", category="verify")
            spec_hash = ""
        return {
            "agree": False,
            "agreement_score": 0.0,
            "refuted": True,
            "spec_hash": spec_hash,
            "evidence": ["no_independent_adversary_wired"],
            "adversary": "none",
            "reason": "no_independent_adversary_wired",
        }

    # Lazy import: the dead-until-now verifier is only loaded on the opt-in
    # path, so the default cycle never even imports it.
    from agi_v8_1.capabilities import PayloadPort, PayloadUnavailable, resolve_payload

    try:
        CrossModelVerifier = resolve_payload(PayloadPort(
            6, "agi_v8_1.verifier.cross_model_verifier", "CrossModelVerifier",
        ))
    except PayloadUnavailable as exc:
        record_critical_failure(exc, site="verifier.wire_cross_model_v81.payload", category="verify")
        return {
            "agree": False, "agreement_score": 0.0, "refuted": True,
            "spec_hash": "", "evidence": ["T6_payload_unavailable"],
            "adversary": "none", "reason": "T6_payload_unavailable",
            "doc_pointer": "Plz_ReadMe.md §T6",
        }

    verifier = CrossModelVerifier(agreement_threshold=agreement_threshold)
    result = verifier.evaluate(spec, adversary)
    return {
        "agree": not result.refuted,
        "agreement_score": float(result.agreement_score),
        "refuted": bool(result.refuted),
        "spec_hash": result.spec_hash,
        "evidence": list(result.evidence),
        "adversary": "injected",
        "reason": "refuted" if result.refuted else "",
    }


__all__ = [
    "CROSS_MODEL_VERIFY_ENV_ENABLED",
    "cross_model_verify_enabled",
    "run_cross_model_verify",
]
