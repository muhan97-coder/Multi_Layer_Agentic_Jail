# __SLOT_SI_EXECUTION_AUTHORITY_2026_08_17__ Round 5 Track D security fix.
"""Single-source-of-truth authorization for the SI apply ladder's channels.

Round 5 audit §3 (CONFIRMED): the apply ladder computed
``do_real_write = consensus and verify_pass and write_on`` for the FILE-write
channel, but the command-exec channel a few lines below only checked
``seam_on and armed and plan.allowed`` — never ``consensus``, never
``verify_pass``. A cycle whose file changes were correctly REJECTED (split
verdict, or a RED verify) could still have its proposed shell commands
EXECUTED. That is a permission-monotonicity violation: the healthy invariant
is ``proposed ⊇ verified ⊇ approved ⊇ executed`` — a channel can never be MORE
permissive than the file-write channel it rides alongside.

This module computes ONE :class:`Authorization` per apply-ladder call and
every channel (file write, command exec, artifact exec) is meant to consult
only that value, never re-derive its own boolean from a subset of the inputs.

``allow_command_exec`` deliberately checks ``consensus and verify_pass`` but
NOT ``write_on``: an existing, sanctioned dual-gate contract
(``tests/v8_1/test_stage3_arming_stage4_2026_06_18.py::test_command_executed_when_double_gated``,
the command-exec seam's own module docstring — "Stage 3 deliberately ships
the SEAM, not a live executing action", observation-first by design) already
lets an operator arm command execution independently of the file-write
operator gate. Folding ``write_on`` into ``allow_command_exec`` would silently
break that live contract and would not be fixing anything §3 CONFIRMED — the
confirmed hole is specifically "consensus/verify_pass are never consulted",
not "write_on is never consulted". ``allow_file_write`` keeps ``write_on``
(it is exactly the pre-existing ``do_real_write`` formula).

``approved_command_digests`` closes the second half of the audit finding: the
apply ladder re-extracts commands from THREE independent sources
(``pod_a_block``, ``pod_b_block``, ``proposer_block``), but the F1 inc5
verify-gate upstream only ever filtered ``proposer_commands``. A pod block
that (today, no production caller does this) carried its own
``proposed_commands`` rode straight through, un-reviewed, on the SAME arm/seam
gate as inc5-verified proposer commands.

This binding is OPT IN, threaded by the caller (``run_one_si_cycle`` passes
``_run_apply_ladder(..., approved_command_digests=...)`` only when the F1
inc5 verify gate, ``AGI_V8_SI_VERIFY_GATE_ENABLED``, is itself ON) rather than
computed unconditionally inside ``_run_apply_ladder`` from whatever
``extra_commands`` it was handed. Two independent, ALREADY-SHIPPED test
contracts exercise ``_run_apply_ladder`` directly with commands riding in on
``pod_a_block`` and no ``extra_commands`` at all, and expect them to execute
once armed — ``tests/v8_1/test_stage3_arming_stage4_2026_06_18.py``'s Stage-3
suite, predating the F1 inc5 gate entirely. Restricting every source to a
digest set unconditionally would turn that sanctioned pod-block command path
into a silent no-op, which is not a monotonicity fix, it is a behaviour
regression with no CONFIRMED finding behind it. Opt-in-when-the-reviewing-gate
-is-armed matches what the audit literally asked for ("게이트 ON이면…") and
closes the gap for the one caller (the real SI cycle, when inc5 is armed)
where an un-reviewed pod-block command could otherwise ride the SAME
authorized-command digest set as an inc5-verified proposer command.

Gate: ``AGI_V8_SI_EXECUTION_AUTHORITY_ENABLED``, default ON — this is a
security fix, not a new feature, so (per repo convention for such fixes) the
safe behaviour ships live. Setting it to an explicit ``"false"``/``"0"``
restores the pre-fix behaviour byte-for-byte (unset / any other value stays
ON — only an explicit opt-out disables it).

Unwired-inputs axis (t3_repro_authority, 2026-08-19): this module's ONE
production ``compute_authorization`` caller that matters for authority
integrity is ``self_improvement_v8.py``'s goal-card work-product block
(``__SLOT_SI_ARTIFACT_EXEC_AUTHORITY_2026_08_18__``, ~L2081), which forces
``consensus=True, verify_pass=True`` — NOT because those are real verified
values, but because that channel is deliberately gated on the operator
write-gate alone (see the ``allow_artifact_exec`` field docstring below for
why that is a documented, sanctioned design, not itself a bug). The CONFIRMED
gap this axis closes is narrower: this module previously offered NO way for
ANY caller to distinguish "I verified this and it's really True" from "I
have nothing so I typed True" — both looked identical to every consumer.
``consensus``/``verify_pass`` are now ``bool | None``; passing ``None``
(instead of guessing a literal) is how a caller admits it has no real value,
and :class:`Authorization` gains ``authority_inputs_unwired`` — a NAMED,
inspectable field that is ``True`` exactly when either was ``None``. Python
truthiness already makes ``None`` fail ``core_ok`` today with ZERO code
change here (``bool(None and X)`` is ``False``) — so this axis's blocking
value is insurance against a FUTURE refactor of ``core_ok`` accidentally
stopping treating ``None`` as falsy, not a live gap today. That insurance is
what ``AGI_V8_EXECUTION_AUTHORITY_UNWIRED_REJECTS_ENABLED`` (new,
default-OFF) arms: ON forces every ``allow_*``/``readonly_diagnostic_ok``
field to ``False`` whenever ``authority_inputs_unwired`` is ``True``,
unconditionally, regardless of what ``core_ok`` computed. Every EXISTING
production caller passes real ``bool`` literals, never ``None`` — so this
axis has zero live callers and changes NOTHING observable this round either
way; editing ``self_improvement_v8.py``'s L2081-2083 to pass ``None`` instead
of a hardcoded ``True`` (the only way to actually retire that hardcode) is
out of this round's file-exclusion scope — see ``REPORT.md`` (tower request).
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from typing import Any, FrozenSet, Iterable, Sequence

_EXECUTION_AUTHORITY_ENV = "AGI_V8_SI_EXECUTION_AUTHORITY_ENABLED"
# New, SEPARATE default-OFF gate (t3_repro_authority) — see module docstring's
# "Unwired-inputs axis" section. Unlike ``_EXECUTION_AUTHORITY_ENV`` above
# (an already-shipped fix, default ON), this is a genuinely NEW capability
# with zero live callers, so it follows this file's sibling default-OFF
# convention instead.
_UNWIRED_REJECTS_ENV = "AGI_V8_EXECUTION_AUTHORITY_UNWIRED_REJECTS_ENABLED"


def execution_authority_enabled() -> bool:
    """Default ON. Explicit ``"false"``/``"0"`` is the only opt-out.

    Mirrors the ``core/rubric_emergency.py`` opt-out convention (default-ON
    security guards read an explicit disable, not an explicit enable) rather
    than this file's sibling default-OFF feature gates — this module ships a
    fix for an already-CONFIRMED hole, not a new capability.
    """
    val = os.environ.get(_EXECUTION_AUTHORITY_ENV, "true").strip().lower()  # tier: T1
    return val not in ("false", "0")


def execution_authority_unwired_rejects_enabled() -> bool:
    """Default-OFF (strict ``true``/``1``). See module docstring's
    "Unwired-inputs axis" section — ON forces every ``allow_*`` field (and
    ``readonly_diagnostic_ok``) to ``False`` whenever a caller admitted its
    ``consensus``/``verify_pass`` inputs were unwired (passed ``None``)."""
    return os.environ.get(_UNWIRED_REJECTS_ENV, "").strip().lower() in ("true", "1")  # tier: T9


def command_digest(command: object) -> str:
    """Stable content digest for a single command string.

    Whitespace-normalised (``str(...).strip()``) so a command surviving
    ``command_channel.normalize_commands`` unchanged still matches the digest
    computed before normalisation — the digest binds *meaning*, not byte
    layout, matching what ADV-R5-001 exercises (same path/action, different
    content ⇒ different digest; same content ⇒ same digest).
    """
    normalized = str(command).strip()
    return hashlib.sha256(normalized.encode("utf-8", errors="replace")).hexdigest()


def changes_digest(changes: Sequence[Any]) -> str:
    """Content digest over an ordered FileChange-like sequence (§2 partial).

    Binds the ACTUAL BYTES a verdict was computed over: two change sets that
    agree on every ``path``/``action`` but differ in ``content`` (or
    ``old_content``/``target_line``/``replacement``) produce different
    digests. Order-sensitive by design — the apply ladder applies changes in
    list order inside one atomic session, so a reorder is a different write
    just as much as a content edit is.

    This is the MINIMAL binding the Round 5 brief asked for (a verify-stage
    digest the apply step can compare against right before it writes), not
    the full sealed-bundle redesign (bundle_id / signature / nonce / expiry)
    that brief explicitly scoped OUT of this round.
    """
    h = hashlib.sha256()
    for c in changes:
        parts = (
            str(getattr(c, "path", "")),
            str(getattr(c, "action", "")),
            str(getattr(c, "content", "") or ""),
            str(getattr(c, "old_content", "") or ""),
            str(getattr(c, "target_line", "") or ""),
            str(getattr(c, "replacement", "") or ""),
        )
        h.update("\x1f".join(parts).encode("utf-8", errors="replace"))
        h.update(b"\x1e")
    return h.hexdigest()


@dataclass(frozen=True)
class Authorization:
    """One verdict, consulted (not re-derived) by every execution channel."""

    consensus: bool
    verify_pass: bool
    write_on: bool
    # File-change writes (SafeAutoApply.apply_session with dry_run=False).
    allow_file_write: bool
    # Shell-command execution (enforcement.command_executor.execute_planned).
    allow_command_exec: bool
    # Work-product .py artifact execution (enforcement.command_executor
    # .execute_artifact_run). __SLOT_SI_ARTIFACT_EXEC_AUTHORITY_2026_08_18__
    # (ADV-R10 §5): wired as of this round, but NOT via the cycle-wide
    # ``authorization`` object this class's own consumer constructs from
    # real ``consensus``/``verify_pass`` — the sole production call site
    # (self_improvement_v8.py, the goal-card work-product path) instead
    # builds a SEPARATE ``Authorization`` with ``consensus=True,
    # verify_pass=True`` forced, so ``allow_artifact_exec`` there reduces to
    # exactly ``write_on`` — preserving the pre-existing, documented design
    # (__SLOT_WORK_PRODUCT_OWN_SESSION_2026_08_02__: operator write-gate
    # only, no dependency on an unrelated self-mod cycle's consensus) while
    # still giving this field a real consumer via
    # ``enforcement/command_executor.execute_artifact_run``'s new
    # ``authorization`` parameter (gated by its own default-OFF
    # ``AGI_V8_SI_ARTIFACT_EXEC_AUTHORITY_ENABLED``).
    allow_artifact_exec: bool
    # Commands whose content-digest was actually reviewed (the inc5-verified,
    # or gate-off-passthrough, ``extra_commands`` list) for THIS cycle. Empty
    # when ``allow_command_exec`` is False.
    approved_command_digests: FrozenSet[str] = field(default_factory=frozenset)
    # Escape hatch for a future read-only-diagnostic allowance that should
    # survive even a denied cycle. Nothing sets this today — no caller
    # constructs it True — so it changes no current behaviour; it exists so a
    # future carve-out has a named field instead of a weakened boolean.
    readonly_diagnostic_ok: bool = False
    # --- unwired-inputs axis (t3_repro_authority, 2026-08-19) — appended
    # LAST, defaulted False, so every existing keyword-based ``Authorization
    # (...)`` construction in this repo (this module's own ``compute_
    # authorization``, plus two direct test constructions) stays valid
    # unchanged. ``True`` exactly when ``compute_authorization`` was called
    # with ``consensus=None`` or ``verify_pass=None`` — see module
    # docstring's "Unwired-inputs axis" section. A NAMED signal that the
    # caller admitted it had no real verified value, distinct from a REAL
    # verified ``False`` (a rejected/unverified cycle) — both used to look
    # identical (``consensus=False``) to every consumer of this class.
    authority_inputs_unwired: bool = False


def compute_authorization(
    *,
    consensus: bool | None,
    verify_pass: bool | None,
    write_on: bool,
    approved_commands: Iterable[object] = (),
    readonly_diagnostic_ok: bool = False,
) -> Authorization:
    """Compute the ONE authorization value for an apply-ladder cycle.

    ``allow_file_write = consensus and verify_pass and write_on`` (the
    pre-existing ``do_real_write`` formula, unchanged). ``allow_command_exec
    = consensus and verify_pass`` — the §3 fix: a REJECTED cycle (consensus
    False, e.g. a split verdict) or an UNVERIFIED one (verify_pass False, a
    RED gate/rubric-emergency/cross-model-refuted verdict) can no longer
    execute commands regardless of ``write_on``, closing the exact gap §3
    confirmed. ``allow_artifact_exec`` mirrors ``allow_file_write`` on THIS
    computed value; whether that is the right authorization for a given
    artifact-exec call site is the caller's decision — see the field's
    docstring on :class:`Authorization` for why the one production call site
    does NOT pass this exact object through unmodified.

    ``consensus``/``verify_pass`` accept ``None`` — a caller passing ``None``
    is EXPLICITLY admitting it has no real verified value for that input
    (as opposed to guessing a hardcoded literal), which sets
    ``Authorization.authority_inputs_unwired`` True. Python truthiness
    already makes ``bool(None and X)`` False, so ``core_ok`` (and therefore
    every ``allow_*`` field) is False for an unwired call with ZERO extra
    code here — the ``AGI_V8_EXECUTION_AUTHORITY_UNWIRED_REJECTS_ENABLED``
    gate below is pure INSURANCE against a future ``core_ok`` refactor no
    longer treating ``None`` as falsy, not a live gap today (see module
    docstring's "Unwired-inputs axis" section — same insurance-only shape as
    ``self_improvement_v8.py``'s own ``__SLOT_SI_VERIFY_APPLY_DIGEST_BIND_
    2026_08_17__``, which "never fires today" by its own docstring either).
    Every existing production caller passes real ``bool`` literals, never
    ``None`` — so this parameter widening changes nothing for them.
    """
    unwired = consensus is None or verify_pass is None
    core_ok = bool(consensus and verify_pass)
    allow_file_write = bool(core_ok and write_on)
    allow_command_exec = core_ok
    allow_artifact_exec = allow_file_write
    _readonly = bool(readonly_diagnostic_ok)
    if unwired and execution_authority_unwired_rejects_enabled():
        # Insurance layer: force every allow_* field (and the read-only
        # escape hatch) False regardless of what core_ok/write_on computed —
        # never MORE permissive than the natural-truthiness result above,
        # only ever a redundant confirmation of it.
        allow_file_write = False
        allow_command_exec = False
        allow_artifact_exec = False
        _readonly = False
    digests: FrozenSet[str] = (
        frozenset(command_digest(c) for c in approved_commands)
        if allow_command_exec
        else frozenset()
    )
    return Authorization(
        consensus=bool(consensus),
        verify_pass=bool(verify_pass),
        write_on=bool(write_on),
        allow_file_write=allow_file_write,
        allow_command_exec=allow_command_exec,
        allow_artifact_exec=allow_artifact_exec,
        approved_command_digests=digests,
        readonly_diagnostic_ok=_readonly,
        authority_inputs_unwired=unwired,
    )


def command_authorized(authorization: Authorization, command: object) -> bool:
    """Is ``command`` allowed to execute under ``authorization``?

    True only when the whole cycle is authorized AND this exact command's
    digest is in the reviewed set — closing the pod_a/pod_b/proposer 3-source
    gap: a pod-block command that never rode through the same review as the
    cycle's ``extra_commands`` list simply is not a member, regardless of
    which of the three ``_extract_commands`` sources it came from.
    """
    if not authorization.allow_command_exec:
        return False
    return command_digest(command) in authorization.approved_command_digests
