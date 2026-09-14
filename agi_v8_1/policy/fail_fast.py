# __SLOT_FAIL_FAST_2026_07_25__ Strict mode: make swallowed failures loud.
"""One choke point for every "we caught it and carried on" site.

Why this exists: every defect the 2026-07-25 audit found was hidden by a silent
swallow, not by complicated logic —

  * the sandbox grader scored every candidate 0 passed / 0 failed and said nothing;
  * the continuation ring never rehydrated, so ``force_replan`` was unreachable;
  * the cross-model verifier echo-agreed with itself and reported ``agree: true``;
  * every non-GIS bundle's weights were dropped to ``{}`` on the way in.

None of those raised. An AST census of production code counts **626 non-raising
exception handlers** (78 bare ``except: pass``, 37 ``except: return None``, 185
``except: return <default>``, 231 other silent, 95 log-only). Converting all of
them to ``raise`` outright would be wrong: a meaningful share are deliberately
non-fatal by contract — ring persistence, telemetry, ``executor_log`` — and
making an observability fault kill a production cycle inverts the safety model.

So this module makes the *strategy* available as a tool instead:

  ``swallowed(exc, site=..., category=...)`` replaces a silent handler body.
    - ALWAYS records the swallow (counter + WARNING log naming the site). Even
      with strict mode off, nothing is silent any more — it is *recorded*, and
      ``census()`` shows exactly where a run swallowed what.
    - With ``AGI_V8_STRICT_FAIL_FAST=true`` it RE-RAISES. Flip it on, run the
      cycle or the suite, and the remaining bugs surface by themselves rather
      than being hunted one at a time.

Default OFF ⇒ byte-identical behaviour; the only change with the gate off is that
a swallow is now counted and logged instead of vanishing.
"""
from __future__ import annotations

import logging
import os
import re
import threading
from collections import Counter
from enum import Enum
from typing import Any, Iterator

logger = logging.getLogger(__name__)

STRICT_ENV = "AGI_V8_STRICT_FAIL_FAST"

# __SLOT_PANC_CENSUS_MASK_GATE_2026_08_10__ P0 #5.5 — the census this module
# keeps (``_last`` / ``census()``) stores raw ``str(exc)`` text, and
# ``runtime/cli.py`` republishes it verbatim in the orchestrator digest
# (stdout + any disk report built from that payload). An exception that
# happens to echo a secret (a provider error quoting its own key, a config
# error embedding a PEM block) has been reaching those sinks unmasked since
# HEAD. Default OFF ⇒ byte-identical to the pre-gate behaviour; ON, the text
# is passed through the canonical secret masker (``policy.secret_masker``)
# BEFORE it is ever truncated or stored — see ``swallowed()`` below for why
# the ordering (mask-then-truncate, never the reverse) is load-bearing.
CENSUS_MASK_ENV = "AGI_V8_FAILFAST_CENSUS_MASK_ENABLED"

# Categories are advisory labels for the census — they do NOT exempt a site from
# strict mode. Strict means strict: the point is to surface everything.
CATEGORY_VERIFY = "verify"          # correctness / verification path
CATEGORY_APPLY = "apply"            # apply + rollback path
CATEGORY_TELEMETRY = "telemetry"    # logging, counters, spans — non-fatal by contract
CATEGORY_PERSIST = "persist"        # durable sidecars / ledgers
CATEGORY_PROVIDER = "provider"      # provider construction / dispatch
CATEGORY_CONFIG = "config"          # env / path / model resolution

_lock = threading.Lock()
_counts: Counter = Counter()
_last: dict[str, str] = {}

_SAFE_EXCEPTION_TYPE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_UNKNOWN_EXCEPTION_TYPE = "Exception"


def safe_exception_type_name(exc: BaseException) -> str:
    """Return a bounded, non-secret-shaped exception type label.

    ``type(exc).__name__`` is normally inert, but an adversarial metaclass can
    override ``__getattribute__`` for ``__name__`` and either raise or return a
    credential-shaped string.  Error formatting and critical-record sentinels
    must not depend on that protocol.  Only ordinary ASCII identifiers are
    admitted; every other shape gets a fixed label.
    """
    try:
        name = type(exc).__name__
    except BaseException:  # noqa: BLE001 — hostile metaclass boundary
        return _UNKNOWN_EXCEPTION_TYPE
    if type(name) is not str or _SAFE_EXCEPTION_TYPE_RE.fullmatch(name) is None:
        return _UNKNOWN_EXCEPTION_TYPE
    return name


def strict_enabled(env: "dict[str, str] | None" = None) -> bool:
    """True iff ``AGI_V8_STRICT_FAIL_FAST`` is exactly ``"true"``/``"1"``."""
    source = env if env is not None else os.environ
    return source.get(STRICT_ENV, "") in ("true", "1")


def census_mask_enabled(env: "dict[str, str] | None" = None) -> bool:
    """True iff ``AGI_V8_FAILFAST_CENSUS_MASK_ENABLED`` is exactly ``"true"``/``"1"``.

    __SLOT_PANC_CENSUS_MASK_GATE_2026_08_10__ — see the constant's docstring
    for what this gates. ``runtime/cli.py`` reads this SAME helper (imported,
    not re-implemented) for its own publish-side masking pass so the two
    choke points can never drift on what "enabled" means.
    """
    source = env if env is not None else os.environ
    return source.get(CENSUS_MASK_ENV, "") in ("true", "1")


# __SLOT_PANC_CENSUS_MASK_SENTINEL_2026_08_10__ Fail-closed stand-in for
# ``_mask_census_text`` when the canonical masker cannot be reached at all
# (import failure — module missing/broken, not just a masking-call error).
# Mirrors the C1 precedent (``policy.secret_masker._strict_masker_unavailable``
# / ``mask_secrets_strict``'s own lazy ``providers.base`` import guard): NEVER
# fall back to storing the raw text. Name the failure, withhold the content.
_CENSUS_MASK_UNAVAILABLE_SENTINEL = (
    "<content withheld: census masker unavailable ({}) — fail-closed, "
    "__SLOT_PANC_CENSUS_MASK_SENTINEL_2026_08_10__>"
)

# __SLOT_PANC_MASK_REENTRY_GUARD_2026_08_10__ R2 verifier finding (CONFIRMED,
# reproduced): ``policy.secret_masker.mask_secrets_strict`` / ``_guarded``
# call ``swallowed()`` (THIS module's own choke point) from their own lazy
# ``providers.base`` import-failure branch. With the gate on, that nested
# ``swallowed()`` call re-enters ``_mask_census_text`` for the NEW exception
# it is recording, which calls back into the same masker, which — while
# ``providers.base`` stays unimportable — fails and calls ``swallowed()``
# again: unbounded recursion (measured: 248 nested frames for a single real
# swallow, before this guard existed). A thread-local re-entry flag breaks
# the cycle at exactly one nested level: the inner call fails closed
# immediately (named sentinel, no further masker call), so the outer,
# real swallow still gets counted and masked correctly, and the one nested
# swallow triggered by the masker's own telemetry gets counted once — not
# 248 times.
_mask_reentry = threading.local()

# __SLOT_PANC_UNCLOSED_BLOCK_GUARD_2026_08_10__ R2 verifier CRITICAL #2/#3
# (CONFIRMED, reproduced live): every call site this module cannot edit
# (``orchestrator_v8.py``, ``multimodal/si_cycle_wire.py``,
# ``multimodal/si_evidence_source.py``) pre-truncates its ``str(exc)`` to
# ~300 chars BEFORE handing the text to ``swallowed()``/``detail=``. A
# block-shaped secret (a PEM private key) whose closing
# ``-----END ... PRIVATE KEY-----`` marker falls past that cut arrives here
# already beheaded: ``policy.secret_masker``'s header+footer regex requires
# BOTH markers to match at all, so a headless block sails through
# ``mask_secrets_strict_guarded`` completely untouched — the header plus the
# raw key body ships in the clear, right next to a census line that looks
# masked. Verifier repro: this exact shape shipped 8 raw key-body segments
# to the WARNING log.
#
# This is NOT a second copy of ``policy.secret_masker``'s masking patterns
# (no substitution logic here, canonical patterns untouched) — it is a
# presence check run on the ALREADY-MASKED output: if a recognizable block
# secret opener is still there, the canonical masker could not have matched
# a complete block, so whatever survived cannot be trusted and the WHOLE
# string is withheld (never a partial pass-through — a partial pass-through
# is exactly the beheaded-PEM leak this guard exists to close). The opener
# literals mirror ``policy.secret_masker._SECRET_PATTERNS``' own header
# tokens (PEM RSA/EC/DSA/OPENSSH/PGP/ENCRYPTED, SSH2 4-dash, PuTTY) so this
# stays in lock-step with the canonical pattern set without importing its
# private table.
_UNCLOSED_BLOCK_OPENER_RE = re.compile(
    r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY-----"
    r"|---- BEGIN SSH2 (?:ENCRYPTED )?PRIVATE KEY ----"
    r"|PuTTY-User-Key-File-\d+:"
)
_UNCLOSED_BLOCK_SENTINEL = (
    "<content withheld: unclosed secret-block header detected post-mask — "
    "fail-closed, __SLOT_PANC_UNCLOSED_BLOCK_GUARD_2026_08_10__>"
)


# __SLOT_PANC_BASE64_SCRUB_2026_08_10__ R3 verifier MAJOR #2 (CONFIRMED,
# reproduced live): the canonical masker has NO bare-base64 pattern. Its
# broadest net is ``providers/base.py``'s generic ``\b[A-Za-z0-9_\-]{40,}\b``
# — and ``+``/``/`` are not in that class, so a REAL key body (base64 of
# random bytes contains ``+`` and ``/`` roughly every 32 chars) is chopped by
# its own alphabet into sub-40 runs that every canonical pattern lets
# through. The verifier planted ``b64encode(bytes(range(256))*3)`` in an
# exception and watched three raw body segments land in the stdout digest.
# The previous round's canary (``"QWxhZGRpbjpvcGVuIHNlc2FtZQ" * 6``) was 170
# chars of unbroken alnum, so it hit the generic 40+ rule and could not fail
# — a canary that cannot die.
#
# The real fix belongs in ``policy/secret_masker.py`` (canonical pattern set)
# — NOT editable by this pan, declared to the tower. What IS in scope: a
# thin, narrow scrub applied ONLY to text this pan knows structurally is
# EXCEPTION-DERIVED free text (census ``last_error``, digest
# ``error_detail``/``wire_error_detail``/``detail``). That surface carries no
# structural audit values, so a false positive costs one line of a debug
# message, not a join axis.
#
# Deliberately narrow so it cannot eat diagnostics (the single most useful
# thing in an exception is a PATH, and ``/`` is in base64's alphabet):
#   * >= 32 chars — below that a "run" is far more likely a path or an id;
#   * must contain ``+`` or ``/`` — pure-alnum runs are already covered by
#     the canonical generic 40+ rule, and re-covering them here would start
#     eating hex digests and identifiers;
#   * must mix upper + lower + digit — random base64 always does, a path
#     segment rarely does;
#   * >= 20% of chars uppercase-or-digit — random base64 sits near 56%;
#     ``/Users/Doha/Projects/App2Test/src`` (the nastiest path shape: mixed
#     case, digit, many slashes) sits at 18% and survives untouched.
# Runs ONLY on ALREADY-MASKED output, so canonical replacements
# (``***MASKED***``, ``<MASKED-SK>``) are what it sees for anything the
# canonical patterns did catch — it never pre-empts them.
_BASE64_BODY_RE = re.compile(r"[A-Za-z0-9+/]{32,}={0,2}")
_BASE64_BODY_MASK = "<MASKED-BASE64-BODY>"


def _looks_like_base64_body(run: str) -> bool:
    """True iff *run* looks like a base64-encoded key/credential BODY rather
    than a path, an identifier, or a hex digest. See the module comment above
    ``_BASE64_BODY_RE`` for why each condition is here.
    """
    if "+" not in run and "/" not in run:
        return False
    if not (
        any(c.isupper() for c in run)
        and any(c.islower() for c in run)
        and any(c.isdigit() for c in run)
    ):
        return False
    dense = sum(1 for c in run if c.isupper() or c.isdigit())
    return dense * 5 >= len(run)


def _scrub_base64_bodies(text: str) -> str:
    """Thin scrub for bare base64 key bodies the canonical masker has no
    pattern for. Runs BOTH BEFORE and AFTER ``mask_secrets_strict_guarded``
    — never *instead of* it.

    Why both, measured (2026-08-10, gate ON, real
    ``b64encode(bytes(range(256))*3)`` body in an exception):
      * AFTER only: the canonical generic ``{40,}`` rule fires first and
        replaces the alnum stretches, SHREDDING the one long run into
        fragments like ``+wsbKztLW2t7i5uru8vb6/`` (22 chars) glued between
        ``<MASKED-TOKEN>`` markers. No contiguous 32+ run is left for this
        scrub to see, and those fragments ARE raw key material. Measured: 1
        surviving raw run in stdout, 6 in the census row.
      * BEFORE: the run is still whole, so it collapses to one marker and
        the canonical patterns then see nothing left to shred. Measured: 0.
      * AFTER is still kept as the second pass — a canonical substitution can
        JOIN previously separated text into a new run, and re-running is a
        no-op on the common path.

    Pure regex substitution: cannot raise, so it never disturbs the
    fail-closed contract of its caller. Never replaces anything the
    canonical patterns would have caught in a *narrower* form — a PEM block
    whose body lines collapse to markers still matches its own
    header-to-footer pattern (verified in the owned test file), and
    JWT/``sk-``/GitHub/Slack shapes use base64URL (``-``/``_``), which this
    rule's mandatory ``+``/``/`` condition excludes by construction.
    """
    return _BASE64_BODY_RE.sub(
        lambda m: _BASE64_BODY_MASK if _looks_like_base64_body(m.group(0)) else m.group(0),
        text,
    )


# __SLOT_PANC_MASK_INPUT_CAP_2026_08_10__ R3 verifier MINOR (CONFIRMED,
# measured 60.05s): ``policy.secret_masker``'s PEM pattern is
# ``-----BEGIN ...-----.*?-----END ...-----`` with ``re.DOTALL``. Every
# unterminated header rescans to end-of-string, so 1.18MB of repeated
# headers costs O(n·m). HEAD never called the masker from a publish path, so
# arming these choke points is what made that cost reachable — this pan owns
# the mitigation even though it cannot edit the pattern.
#
# ⛔ The mitigation is NOT "truncate the input" — cutting BEFORE masking is
# the exact ordering this whole task exists to forbid (a beheaded PEM ships
# its body). It is WITHHOLD: an exception message larger than this cap is
# not diagnosable free text anyway, so the whole string is replaced by a
# named sentinel and the masker is never called on it. Fail-closed, no
# partial pass-through, and the pathological input costs one ``len()``.
SINK_MASK_INPUT_MAX_CHARS = 32768
# Backwards-compatible internal spelling retained for the existing census
# helpers/tests; external sink adapters should use the public contract above.
_MASK_INPUT_MAX_CHARS = SINK_MASK_INPUT_MAX_CHARS
_MASK_INPUT_OVERSIZE_SENTINEL = (
    "<content withheld: {} chars exceeds the {}-char mask input cap "
    "(unbounded-scan guard) — fail-closed, "
    "__SLOT_PANC_MASK_INPUT_CAP_2026_08_10__>"
)

_SINK_FORMAT_INVALID_KEEP_SENTINEL = (
    "<content withheld: invalid sink truncation mode — fail-closed, "
    "__SLOT_EXCEPTION_TEXT_SINK_2026_08_11__>"
)
_SINK_MASK_UNAVAILABLE_SENTINEL = (
    "<content withheld: sink masker unavailable ({}) — fail-closed, "
    "__SLOT_EXCEPTION_TEXT_SINK_2026_08_11__>"
)


class _SinkStringifyFailure(RuntimeError):
    """Safe synthetic census event for a value whose ``str`` implementation failed.

    The caught exception object is intentionally not retained or passed to
    :func:`swallowed`: its own ``__str__`` may be the second failing level and
    may carry sensitive text.  The stable message below preserves the census
    and strict-mode event without touching that secondary value.
    """


class _SinkMaskFailure(RuntimeError):
    """Detached, message-safe strict event for an external masker failure."""


def _oversize_for_masking(text: str) -> bool:
    return len(text) > _MASK_INPUT_MAX_CHARS


def _oversize_withhold_sentinel(text: str) -> str:
    return _MASK_INPUT_OVERSIZE_SENTINEL.format(len(text), _MASK_INPUT_MAX_CHARS)


def _withhold_unclosed_secret_block(masked_text: str) -> str:
    """Second, independent safety net over TEXT THE CANONICAL MASKER ALREADY
    RAN ON. See the module-level comment above ``_UNCLOSED_BLOCK_OPENER_RE``
    for why this is needed and why it is not a masking-pattern duplicate.

    Runs AFTER ``mask_secrets_strict_guarded`` — never before, never instead
    of. Pure string containment check: cannot itself raise, so it never
    disturbs the fail-closed contract of its caller.
    """
    if _UNCLOSED_BLOCK_OPENER_RE.search(masked_text):
        return _UNCLOSED_BLOCK_SENTINEL
    return masked_text


def _attempt_mask_pipeline(
    text: str,
    *,
    scrub_bare_base64: bool = True,
) -> tuple[bool, str]:
    """Run the shared mask composition and retain only type on failure.

    This low-level attempt intentionally does not call :func:`swallowed`:
    census capture uses it while already inside that choke point.  The caller
    decides whether failure is a nonthrowing census sentinel or an external
    sink event subject to strict fail-fast.
    """
    try:
        from agi_v8_1.policy.secret_masker import mask_secrets_strict_guarded

        pre_scanned = _scrub_base64_bodies(text) if scrub_bare_base64 else text
        masked = _withhold_unclosed_secret_block(
            mask_secrets_strict_guarded(pre_scanned)
        )
        return True, (
            _scrub_base64_bodies(masked)
            if scrub_bare_base64
            else masked
        )
    except BaseException as exc:  # noqa: BLE001 — masker/plugin boundary
        return False, safe_exception_type_name(exc)


# Generated source is not diagnostic text.  Replacing a credential-shaped
# span with a mask can turn otherwise reviewable Python/C#/Lean into a
# different (and often invalid) program.  Publication therefore needs a
# scan-only contract: a clean string crosses byte-identically; every other
# verdict refuses the whole artifact before a sink opens.  FileArtifact's
# schema already caps content at 64 KiB, so the full canonical scan remains
# bounded without the 32 KiB diagnostic-text withhold policy.
ARTIFACT_PUBLICATION_MAX_BYTES = 64 * 1024


class ArtifactPublicationVerdict(str, Enum):
    """Closed verdict vocabulary for executable/generated artifact text."""

    CLEAN = "clean"
    SECRET_SHAPED = "secret_shaped"
    OVERSIZE = "oversize"
    UNSCANNABLE = "unscannable"
    SCANNER_FAILURE = "scanner_failure"
    UNSAFE_PATH = "unsafe_path"
    DUPLICATE_TARGET = "duplicate_target"


class ArtifactPublicationRefused(RuntimeError):
    """Typed, raw-free refusal raised before an artifact publication sink.

    Only fixed vocabulary and a numeric position are rendered.  In
    particular, the provider-controlled path/content and scanner exception
    message are never retained as exception args or context.
    """

    def __init__(
        self,
        *,
        verdict: ArtifactPublicationVerdict,
        artifact_index: int,
        field: str,
    ) -> None:
        safe_verdict = (
            verdict
            if isinstance(verdict, ArtifactPublicationVerdict)
            else ArtifactPublicationVerdict.UNSCANNABLE
        )
        safe_index = (
            artifact_index
            if type(artifact_index) is int and artifact_index >= 0
            else 0
        )
        safe_field = field if field in {"path", "content", "target"} else "content"
        self.verdict = safe_verdict
        self.artifact_index = safe_index
        self.field = safe_field
        super().__init__(
            "artifact publication refused: "
            f"artifact[{safe_index}].{safe_field} ({safe_verdict.value})"
        )


def scan_artifact_text_for_publication(
    value: Any,
    *,
    field: str = "content",
) -> ArtifactPublicationVerdict:
    """Classify one generated-artifact string without returning a rewrite.

    The bound is measured in UTF-8 bytes to match ``FileArtifact``.  No
    ``str``/``repr`` fallback is allowed: a non-plain string or an encoding
    failure is unscannable.  The canonical masking composition is used only as
    a detector; equality means the original object may be published exactly,
    while any change is treated as secret-shaped and must be refused by the
    caller.  Scanner exceptions become a closed verdict, never fail-open.
    """
    if type(value) is not str:
        return ArtifactPublicationVerdict.UNSCANNABLE
    if len(value) > ARTIFACT_PUBLICATION_MAX_BYTES:
        return ArtifactPublicationVerdict.OVERSIZE
    # UTF-8 can encode every ordinary Python code point.  Lone surrogate code
    # points are the sole failure class under strict UTF-8; detect them without
    # adding another swallowed exception handler to this central choke point.
    if any(0xD800 <= ord(ch) <= 0xDFFF for ch in value):
        return ArtifactPublicationVerdict.UNSCANNABLE
    encoded_size = len(value.encode("utf-8"))
    if encoded_size > ARTIFACT_PUBLICATION_MAX_BYTES:
        return ArtifactPublicationVerdict.OVERSIZE
    # For a verdict (unlike a redacted diagnostic), any recognized private-key
    # opener is already sufficient to refuse.  Detect it before the canonical
    # opener-to-footer substitution so a 64-KiB string of repeated unterminated
    # headers stays linear instead of exercising that regex's worst case.
    if _UNCLOSED_BLOCK_OPENER_RE.search(value):
        return ArtifactPublicationVerdict.SECRET_SHAPED
    # The bare-base64 heuristic is intentionally for free text/code bodies.
    # A normal repository path contains ``/`` plus mixed letters/digits and can
    # satisfy that heuristic accidentally (e.g. the production W66 Unity path).
    # Paths still receive the canonical token/credential/PEM composition and
    # separate containment validation, just not the free-text base64-body pass.
    scan_base64 = field != "path"
    ok, scanned = _attempt_mask_pipeline(
        value,
        scrub_bare_base64=scan_base64,
    )
    if not ok or type(scanned) is not str:
        return ArtifactPublicationVerdict.SCANNER_FAILURE
    if scanned != value:
        return ArtifactPublicationVerdict.SECRET_SHAPED
    return ArtifactPublicationVerdict.CLEAN


def require_artifact_text_safe(
    value: Any,
    *,
    artifact_index: int,
    field: str,
) -> str:
    """Return a clean artifact string unchanged or raise a raw-free refusal."""
    verdict = scan_artifact_text_for_publication(value, field=field)
    if verdict is not ArtifactPublicationVerdict.CLEAN:
        raise ArtifactPublicationRefused(
            verdict=verdict,
            artifact_index=artifact_index,
            field=field,
        )
    # ``CLEAN`` is possible only for ``type(value) is str``.
    return value


def _mask_census_text(text: str) -> str:
    """Mask *text* through the canonical secret masker before it can ever
    reach ``_last`` / ``census()``.

    Lazily imported: ``policy.secret_masker`` itself imports ``swallowed``
    from THIS module (``from agi_v8_1.policy.fail_fast import swallowed``), so
    a module-level import here would be circular. Called from ``swallowed()``
    BEFORE that function acquires ``_lock`` (a plain, non-reentrant
    ``threading.Lock``) — this function must never call ``swallowed()``
    itself, or a masking failure during a held lock would deadlock the very
    choke point 573 call sites depend on. Telemetry for a masking failure
    therefore goes through the plain module ``logger`` instead, not through
    the census it would otherwise be reporting into.

    ONE handler covers both failure modes (import unavailable OR the call
    itself failing) rather than two — ``mask_secrets_strict_guarded`` already
    fail-closes its own internal masking failure in non-strict mode (its own
    documented contract), so a second try/except here would only ever fire
    for a bug in that contract; keeping a single handler avoids adding a
    second untestable branch to this already-573-site chokepoint. Fail-closed
    on ANY failure: the raw text is never returned as a fallback, only the
    named sentinel.

    # __SLOT_PANC_SILENT_RATCHET_NOTE_2026_08_10__ this except body is
    # necessarily NOT routed through ``swallowed()`` (see docstring above —
    # would deadlock/could recurse) and is therefore counted "silent" by
    # ``tests/v8_1/test_fail_fast_and_preflight_2026_07_25.py``'s AST census
    # (which only recognises import-bound choke-point calls, and this module
    # cannot import-bind its own locally-defined ``swallowed``). This is the
    # ONE handler in this file's whole diff that cannot be routed — reported
    # to the tower rather than silently bumping that ceiling.
    """
    # __SLOT_PANC_MASK_REENTRY_GUARD_2026_08_10__ — see module-level comment
    # on ``_mask_reentry`` above for why this check exists.
    if getattr(_mask_reentry, "active", False):
        logger.warning(
            "fail_fast census mask re-entered (nested swallow raised while "
            "masking) — breaking recursion, fail-closed"
        )
        return _CENSUS_MASK_UNAVAILABLE_SENTINEL.format("ReentrantMaskCall")
    # __SLOT_PANC_MASK_INPUT_CAP_2026_08_10__ — withhold (never truncate)
    # before the masker is ever called; see that constant's comment above.
    if _oversize_for_masking(text):
        logger.warning(
            "fail_fast census mask input oversize (%d chars) — withheld, fail-closed",
            len(text),
        )
        # (reached only when the re-entry guard above did NOT fire, i.e.
        # ``_mask_reentry.active`` is already False — nothing to restore.)
        return _oversize_withhold_sentinel(text)
    _mask_reentry.active = True
    try:
        ok, result = _attempt_mask_pipeline(text)
    finally:
        _mask_reentry.active = False
    if ok:
        return result
    logger.warning("fail_fast census mask unavailable: %s", result)
    return _CENSUS_MASK_UNAVAILABLE_SENTINEL.format(result)


def _mask_external_sink_text(text: str) -> str:
    """Full mask pipeline for externally observable/durable text sinks.

    Unlike census capture, this path participates in strict fail-fast.  A
    failed canonical mask is counted using a detached synthetic event;
    non-strict mode returns a named fail-closed sentinel, while strict mode
    re-raises that safe event.  No caught masker exception object survives
    the low-level attempt.
    """
    if getattr(_mask_reentry, "active", False):
        return _SINK_MASK_UNAVAILABLE_SENTINEL.format("ReentrantMaskCall")
    _mask_reentry.active = True
    try:
        ok, result = _attempt_mask_pipeline(text)
    finally:
        _mask_reentry.active = False
    if ok:
        return result
    synthetic = _SinkMaskFailure("external sink masking failed")
    swallowed(
        synthetic,
        site="policy.fail_fast._mask_external_sink_text",
        category=CATEGORY_TELEMETRY,
    )
    return _SINK_MASK_UNAVAILABLE_SENTINEL.format(result)


def mask_exception_text(
    text: str,
    env: "dict[str, str] | None" = None,
) -> str:
    """Gate-aware sanitizer for exception-derived text before durable sinks.

    With the census-mask gate OFF this returns *text* byte-for-byte.  With the
    gate ON it applies the same fail-closed masking path used by
    :func:`swallowed`.  Callers must pass the complete text and truncate only
    after this function returns; otherwise a cut PEM block can evade the
    canonical block matcher.
    """
    return _mask_census_text(text) if census_mask_enabled(env) else text


def _attempt_stringify(value: Any) -> tuple[bool, str]:
    """Return a string or a failure bit without retaining the thrown object.

    🔴 2026-08-11 (Codex 감사 이어받음) — ``except Exception`` 만으로는 좁다.
    ``str(value)`` 자체가 ``SystemExit``/``KeyboardInterrupt``/``GeneratorExit``
    (``BaseException`` 이되 ``Exception`` 서브클래스가 아닌 것)를 던지는
    병리적 ``__str__`` 을 가진 값이 오면 이 관문을 그대로 뚫고 나간다 — 실측:
    ``format_exception_for_sink(Poison())`` 이 ``SystemExit`` 을 그대로
    올린다. 이 함수는 573개 sink 초크포인트가 공유하는 자리라, 이 한 줄이
    executor_log/si_spend_ledger/goal_campaign 등 돈·감사 원장 행 전체의
    증발 경로였다. ⚠️ 이건 **실제 인터럽트 신호를 삼키는 게 아니다** — 이
    ``try`` 는 ``str(value)`` 호출 **한 줄**만 감싼다. 프로그램 다른 곳에서
    발생한 진짜 Ctrl-C/``sys.exit()`` 는 이 함수를 안 거치므로 여전히
    정상 전파된다. 여기서 잡히는 건 오직 "이 값을 문자열화하는 그 순간"
    던져지는 것뿐 — 정상적인 ``SystemExit`` 인스턴스를 로깅하는 흔한 경우
    (``str(a_normal_systemexit)``)는 애초에 아무것도 안 던지므로 동작 불변.
    """
    try:
        return True, str(value)
    except BaseException:  # noqa: BLE001 — hostile ``__str__`` is untrusted input,
        # BaseException(위 설명) — Exception 만으로는 SystemExit 류가 샌다
        return False, ""


def _stringify_text_for_sink(value: Any) -> str:
    """Safely stringify a sink value without running a masking regex.

    The separation is load-bearing: :func:`format_text_for_sink` must measure
    the complete raw string against ``_MASK_INPUT_MAX_CHARS`` *before* any
    regular-expression masker runs.  ``repr`` is deliberately not a fallback;
    a broken ``__str__`` may expose the same sensitive value through ``repr``.
    """
    ok, text = _attempt_stringify(value)
    if not ok:
        synthetic = _SinkStringifyFailure("sink value stringification failed")
        swallowed(
            synthetic,
            site="policy.fail_fast._stringify_text_for_sink",
            category=CATEGORY_TELEMETRY,
        )
        return "<unserializable>"
    return text


def format_text_for_sink(
    value: Any,
    *,
    max_chars: int = 300,
    one_line: bool = True,
    keep: str = "head",
) -> str:
    """Return always-on, fail-closed text for a non-logger sink.

    Ordering is fixed: safe raw stringify → oversize whole-content withhold →
    canonical/bare-base64/unclosed-block masking → optional one-line
    normalization → head/tail truncation.  In particular, no truncation or
    masking regex is allowed to run before the 32 KiB scan guard.

    ``keep`` accepts only ``"head"`` or ``"tail"``.  An invalid mode returns
    a named sentinel that contains none of *value* rather than guessing a
    potentially unsafe caller intent.
    """
    if keep not in {"head", "tail"}:
        return _SINK_FORMAT_INVALID_KEEP_SENTINEL
    raw = _stringify_text_for_sink(value)
    if _oversize_for_masking(raw):
        masked = _oversize_withhold_sentinel(raw)
    else:
        masked = _mask_external_sink_text(raw)
    if one_line:
        masked = " ".join(masked.splitlines())
    limit = max_chars if type(max_chars) is int and max_chars > 0 else 300
    return masked[:limit] if keep == "head" else masked[-limit:]


def format_exception_for_sink(
    exc: BaseException,
    *,
    max_chars: int = 300,
    one_line: bool = True,
    keep: str = "head",
) -> str:
    """Return full-pipeline ``TypeName: message`` text for an exception sink.

    Safe stringification occurs before the type prefix is joined, then the
    complete joined value crosses :func:`format_text_for_sink` exactly once.
    This preserves multiline diagnostics for callers that request them while
    keeping the cap-before-regex and strict failure contracts centralized.
    """
    exc_type = safe_exception_type_name(exc)
    raw = f"{exc_type}: {_stringify_text_for_sink(exc)}"
    return format_text_for_sink(
        raw,
        max_chars=max_chars,
        one_line=one_line,
        keep=keep,
    )


def format_exception_for_log(
    exc: BaseException,
    *,
    max_chars: int = 300,
) -> str:
    """Return one safe, bounded line for an exception-bearing logger field.

    This is the canonical adapter for ``logger.*(..., exc)`` call sites.  It
    is deliberately **always on**: ordinary stderr/logging sinks do not share
    the census compatibility gate, and the repository's log/trail contract
    requires exception-derived free text to pass through the canonical
    composed masker.  The complete message is masked before whitespace
    normalisation and truncation, so a length cap can never behead a PEM block
    before the block matcher sees its footer.

    A hostile/broken ``__str__`` produces ``<unserializable>`` rather than
    falling back to ``repr(exc)`` (which can repeat the same secret-bearing
    failure).  The raw string is size-checked before any masking regex runs.
    ``max_chars`` accepts a positive built-in int; anything else uses the safe
    default.
    """
    return format_exception_for_sink(
        exc,
        max_chars=max_chars,
        one_line=True,
        keep="head",
    )


def format_exception_for_critical_record(
    exc: BaseException,
    *,
    max_chars: int = 300,
    one_line: bool = True,
    keep: str = "head",
) -> str:
    """Text for a transaction-boundary sink (spend/audit/executor row) that
    must **never raise**, in any mode — writing the row and letting the
    caller re-raise *exc*'s own identity always wins over surfacing a
    secondary stringification failure.

    🔴 2026-08-11 (Codex 감사 이어받음, 타워가 완성) — 왜 이 함수가 별도로
    필요한가: ``format_exception_for_sink``/``swallowed`` 는 stringify 실패를
    ``swallowed()`` 로 세는데, strict 모드에서 그 호출이 **합성** 예외
    (``_SinkStringifyFailure``)를 재던진다. 라이브 재현: ``executor_log_span``
    의 except 블록이 ``span._finish_failure(real_exc)`` → ``_format_failure_
    error(real_exc)`` → (poison ``__str__``) → strict 재던짐 — 이때 (1) 원장
    행 자체가 안 쓰인다(``_emit`` 호출까지 못 감) (2) 호출자에게 전파되는
    예외가 진짜 실패(``real_exc``)가 아니라 무관한 포맷팅 실패로 **바뀐다**.
    돈/감사 원장의 "행은 항상 남는다"는 계약과 "원래 실패 정체성 보존"
    계약을 둘 다 어긴다.

    이 어댑터는 그 자리에서만 쓴다: stringify 가 실패하면 census 는 그대로
    세되(관측성 유지) strict 재던짐은 **국소 흡수**하고, 타입명만 담은
    이름 붙은 sentinel 을 반환한다 — 호출자는 항상 문자열을 받아 행을 쓸 수
    있고, 그 다음 자기 코드에서 *exc* 를 그대로 재던지면 원래 정체성이
    보존된다(이 함수는 그 재던짐에 관여하지 않는다 — 호출자 책임).
    """
    exc_type = safe_exception_type_name(exc)
    ok, text = _attempt_stringify(exc)
    if not ok:
        synthetic = _SinkStringifyFailure(
            "critical-record stringification failed"
        )
        try:
            swallowed(
                synthetic,
                site="policy.fail_fast.format_exception_for_critical_record",
                category=CATEGORY_TELEMETRY,
                detail=exc_type,
            )
        except BaseException:
            pass  # preserve the primary exception and its durable row
        return f"{exc_type}: <unserializable>"
    try:
        return format_text_for_sink(
            f"{exc_type}: {text}",
            max_chars=max_chars,
            one_line=one_line,
            keep=keep,
        )
    except BaseException as formatter_failure:  # noqa: BLE001 — primary owns control flow
        # ``format_*_for_sink`` already counted/named ordinary formatter
        # failures before strict mode re-raised its safe synthetic.  This
        # transaction boundary must not let that secondary replace *exc* or
        # erase the row, so only its type is retained in a raw-free sentinel.
        return (
            f"{exc_type}: <content withheld: critical-record formatter "
            f"failure ({safe_exception_type_name(formatter_failure)})>"
        )


def format_text_for_critical_record(
    value: Any,
    *,
    max_chars: int = 300,
    one_line: bool = True,
    keep: str = "head",
) -> str:
    """Non-throwing full-pipeline text for a spend/audit record field.

    This is deliberately narrower than :func:`format_text_for_sink`: only a
    transaction boundary that must preserve a durable row may absorb strict
    formatter failures.  It never falls back to ``str``/``repr`` of *value*.
    """
    try:
        return format_text_for_sink(
            value,
            max_chars=max_chars,
            one_line=one_line,
            keep=keep,
        )
    except BaseException as formatter_failure:  # noqa: BLE001 — row must survive
        return (
            "<content withheld: critical-record formatter failure "
            f"({safe_exception_type_name(formatter_failure)})>"
        )


def record_critical_failure(
    exc: BaseException,
    *,
    site: str,
    category: str = CATEGORY_TELEMETRY,
) -> None:
    """Count a secondary transaction-boundary failure without rethrowing it.

    Spend/audit recorders must finish their row even when strict fail-fast is
    enabled.  This narrow choke point records a fixed, type-only surrogate and
    locally absorbs strict's surrogate rethrow; it never stringifies *exc* and
    therefore cannot copy its message into census/log output.  It is not for
    ordinary recovery paths, which must keep calling :func:`swallowed`.
    """
    surrogate = RuntimeError(
        "critical-record secondary failure withheld "
        f"({safe_exception_type_name(exc)})"
    )
    try:
        swallowed(surrogate, site=site, category=category)
    except BaseException:
        pass


def swallowed(
    exc: BaseException,
    *,
    site: str,
    category: str = CATEGORY_TELEMETRY,
    detail: str = "",
) -> None:
    """Record a swallowed failure — and re-raise it when strict mode is on.

    Call this INSTEAD of an empty ``except: pass`` body::

        try:
            ring.append(...)
        except (OSError, ValueError) as exc:
            swallowed(exc, site="si.ring.append", category=CATEGORY_PERSIST)

    ``site`` is a stable dotted id so the census is greppable and a counter can
    be attributed without a traceback. Never returns a value — the caller keeps
    whatever fallback it had, so behaviour with the gate off is unchanged.
    """
    key = f"{category}:{site}"
    exc_type = safe_exception_type_name(exc)
    mask_on = census_mask_enabled()
    # __SLOT_PANC_COUNT_BEFORE_FORMAT_2026_08_10__ R2 verifier finding
    # (CONFIRMED, differential HEAD-vs-worktree): the previous shape of this
    # function formatted ``f"{type(exc).__name__}: {exc}"`` BEFORE the
    # counter increment. If ``str(exc)`` itself raises (a pathological
    # ``__str__``), that formatting throws before ``_counts[key] += 1`` ever
    # runs — the swallow vanishes from the census entirely, which is exactly
    # the defect class this module exists to eliminate. HEAD incremented the
    # counter FIRST (inside the same lock as the old unmasked formatting), so
    # a raising ``__str__`` still left a real count with ``last_error`` simply
    # unset for that key. Restored here: the counter always lands first, and
    # the OFF branch below reproduces that exact HEAD shape (format+store
    # inside the same lock, in the same order) so OFF stays byte-identical
    # including this edge case.
    with _lock:
        _counts[key] += 1

    # Strict mode is a debugger, not a formatter.  Re-raise the exact original
    # object after the count lands, without touching its potentially hostile
    # ``__str__``.  ``from None`` prevents a caller's active handler from
    # becoming a newly displayed context chain.
    if strict_enabled():
        logger.warning(
            "swallowed[%s] %s: %s (strict re-raise; message not stringified)",
            category,
            site,
            exc_type,
        )
        raise exc from None

    stringify_ok, message = _attempt_stringify(exc)
    raw_last = (
        f"{exc_type}: {message}"
        if stringify_ok
        else f"{exc_type}: <unserializable>"
    )
    if mask_on:
        # __SLOT_PANC_CENSUS_CAPTURE_MASK_2026_08_10__ P0 #5.5 CAPTURE choke
        # point. Mask BEFORE truncating, never the reverse: truncating a raw
        # PEM block first can behead its closing "-----END ... KEY-----"
        # marker, so the masker's header+footer pattern no longer matches the
        # truncated fragment and the still-raw key body — the very thing
        # truncation looked like it was making safer — ships instead. Masking
        # the full text first means the block is already replaced by the
        # time truncation runs. Formatting + masking happen OUTSIDE the lock
        # (masking can be slow, and must never run while holding this
        # non-reentrant lock — see ``_mask_census_text``'s own docstring); the
        # counter above is already recorded, so a raising ``__str__`` here
        # still leaves a real count and simply skips ``_last`` for this key,
        # mirroring the OFF branch's contract exactly.
        stored_last = _mask_census_text(raw_last)[:300]
    else:
        stored_last = raw_last[:300]
    with _lock:
        _last[key] = stored_last
    # __SLOT_PANC_DETAIL_MASK_2026_08_10__ P0 #5.5 — ``detail`` is free-form
    # caller text that never enters ``_last``/``census()`` (its only sink is
    # the WARNING line below), but several call sites pass
    # ``f"{type(exc).__name__}: {exc}"`` into it — the exact same
    # secret-shaped risk as ``raw_last`` above. This is the ONE choke point
    # all 573 call sites share for ``detail``, so it is masked here (not at
    # each site) when the gate is on. OFF path: ``detail`` is used exactly as
    # received, completely untouched — byte-identical to HEAD regardless of
    # what any caller passes.
    log_detail = _mask_census_text(detail) if (mask_on and detail) else detail
    logger.warning(
        "swallowed[%s] %s: %s%s",
        category, site, exc_type, f" ({log_detail})" if log_detail else "",
    )


def census() -> dict[str, dict[str, Any]]:
    """Everything swallowed so far, per site: count + the most recent error.

    This is what turns "silent" into "recorded" even with strict mode off — run
    a cycle, read the census, and every place that quietly absorbed a failure is
    named with a count.
    """
    with _lock:
        return {
            key: {"count": count, "last_error": _last.get(key, "")}
            for key, count in sorted(_counts.items(), key=lambda kv: -kv[1])
        }


def total_swallowed() -> int:
    with _lock:
        return sum(_counts.values())


def reset_census() -> None:
    """Clear the counters (tests, or between cycles)."""
    with _lock:
        _counts.clear()
        _last.clear()


class strict_scope:
    """Context manager that forces strict mode on/off for a block.

    Useful for arming a single risky stage without flipping the process-wide env
    (and for tests). Restores the previous value on exit, even on exception.
    """

    def __init__(self, enabled: bool = True) -> None:
        self._enabled = bool(enabled)
        self._prev: str | None = None

    def __enter__(self) -> "strict_scope":
        self._prev = os.environ.get(STRICT_ENV)
        os.environ[STRICT_ENV] = "true" if self._enabled else "false"
        return self

    def __exit__(self, *exc_info: Any) -> None:
        if self._prev is None:
            os.environ.pop(STRICT_ENV, None)
        else:
            os.environ[STRICT_ENV] = self._prev


__all__ = [
    "STRICT_ENV",
    "SINK_MASK_INPUT_MAX_CHARS",
    "ARTIFACT_PUBLICATION_MAX_BYTES",
    "ArtifactPublicationVerdict",
    "ArtifactPublicationRefused",
    "CATEGORY_VERIFY",
    "CATEGORY_APPLY",
    "CATEGORY_TELEMETRY",
    "CATEGORY_PERSIST",
    "CATEGORY_PROVIDER",
    "CATEGORY_CONFIG",
    "strict_enabled",
    "CENSUS_MASK_ENV",
    "census_mask_enabled",
    "mask_exception_text",
    "format_text_for_sink",
    "format_text_for_critical_record",
    "format_exception_for_sink",
    "format_exception_for_log",
    "format_exception_for_critical_record",
    "record_critical_failure",
    "scan_artifact_text_for_publication",
    "require_artifact_text_safe",
    "safe_exception_type_name",
    # __SLOT_PANC_BASE64_SCRUB_2026_08_10__ / __SLOT_PANC_MASK_INPUT_CAP_2026_08_10__
    # — shared with ``runtime/cli.py``'s publish-side pass so the two layers
    # cannot drift on what they consider a key body / an oversize input.
    "_scrub_base64_bodies",
    "_oversize_for_masking",
    "_oversize_withhold_sentinel",
    "_withhold_unclosed_secret_block",
    "swallowed",
    "census",
    "total_swallowed",
    "reset_census",
    "strict_scope",
]
