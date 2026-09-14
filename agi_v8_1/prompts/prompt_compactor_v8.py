"""V8 prompt-history compactor — read_since-driven cycle prompt compression.

Slot: __SLOT_W8_PROMPT_COMPACTOR_V8_2026_05_31__
Write-path slot: __SLOT_W8_COMPACTOR_WRITE_PATH_2026_05_31__

Partial revival of the v7.1 PromptCompactor (orchestrator prompt-evolution
mechanism that was lost in the v8 swarm-merge — see
[[project_v71_prompt_evolution_drop_2026_05_30]]).

Reference: ``agi_v7.1/agent_system/core/prompt_compactor.py``:1-456
  - compact_prompts() drives prompt-file compaction
  - ArchiveEntry preserves raw + summary + dedupe metadata
  - Managed-summary writer collapses categories

V8 reshapes that idea around the **cycle-event stream**:
  - Read cycle-prompt history via :class:`CycleLogger.read_since` (feat 5).
  - Score events with importance = quality_signal × type_weight × recency.
  - Compact under a token budget (semantic-chunk-preserving truncation).
  - Emit a :class:`RolePromptPacket` (feat 6 schema — defined locally with a
    minimal contract until feat 6 wires the canonical class).

Env gate: ``AGI_V8_PROMPT_COMPACTOR_ENABLED`` (default OFF). Compaction is a
read-only operation; the gate controls whether `compact_prompts()` returns the
fully-compacted packet (ON) or an empty stub packet (OFF). This keeps the
import-time surface stable for the swarm topology and lets V8 enable
compaction per-role without touching call sites.

Step 2 write-path: ``write_compacted(target_file, packet, safe_auto_apply)``
renders the packet to markdown and routes it through SafeAutoApply.apply_block
under the unique proposal_id ``compactor_<role>``. The block-marker contract
gives us REPLACE semantics for free — one named block per role, swapped
atomically each compaction round. Archive (JSON) fires first; on archive
failure no SafeAutoApply call is made (compactor must not write a prompt block
it cannot trace back to a snapshot). Both archive and write_compacted are
controlled by ``AGI_V8_COMPACTOR_WRITE_PATH_ENABLED`` (default OFF) — when
OFF the function archives nothing and short-circuits, so the existing read-
only call sites keep working unchanged.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Protocol

# __SLOT_FAIL_FAST_2026_07_25__ Swallowed failures route through one choke
# point: counted + named always, re-raised under AGI_V8_STRICT_FAIL_FAST.
from agi_v8_1.policy.fail_fast import (
    format_exception_for_critical_record,
    format_text_for_critical_record,
    swallowed as _swallowed,
)


_ENV_GATE = "AGI_V8_PROMPT_COMPACTOR_ENABLED"
_WRITE_GATE = "AGI_V8_COMPACTOR_WRITE_PATH_ENABLED"

# Where archive JSON snapshots land. Honours AGI_V8_STATE_DIR (mirrors
# SafeAutoApply's audit-dir contract) so tests redirect both into tmp_path.
_ARCHIVE_SUBDIR = "prompt_compaction_archive"

# Importance weighting: event_type -> base weight (higher = retain first).
# Mirrors v7.1's category-summary preference: contracts > evidence > steps.
_TYPE_WEIGHTS: Mapping[str, float] = {
    "cycle_start": 0.4,
    "agent_step": 0.5,
    "evidence_emit": 0.9,
    "decision_record": 1.0,
    "handoff": 0.7,
    "si_policy": 0.8,
    "cycle_end": 0.6,
}
_DEFAULT_TYPE_WEIGHT: float = 0.3

# Default token budget for one RolePromptPacket. Callers override per role.
# __SLOT_COMPACTOR_BUDGET_2026_07_24__ Raised 2048 -> 62_000. The old value was a
# small-context-era bound: combined with a non-advancing cursor it forced EVERY
# compaction round to squeeze the whole (growing) cycle history into 2048 tokens,
# so durable-learning fidelity degraded as history grew. With the cursor now
# advancing (each round compacts only NEW events) this is a per-slice CEILING,
# not a per-history one — it stops events being dropped, it does not target 62k.
# Override with AGI_V81_PROMPT_COMPACTOR_TOKEN_BUDGET (same V81 prefix as the
# cadence knob).
_ENV_TOKEN_BUDGET = "AGI_V81_PROMPT_COMPACTOR_TOKEN_BUDGET"
DEFAULT_TOKEN_BUDGET: int = 62_000


def resolve_token_budget(explicit: int | None = None) -> int:
    """Resolve the compaction token budget: explicit > env > default.

    A non-positive / unparseable value falls back to the default so a misconfig
    can never silently collapse the budget to zero (which would drop everything).
    """
    if explicit is not None and explicit > 0:
        return explicit
    raw = os.getenv(_ENV_TOKEN_BUDGET, "").strip()  # tier: T2
    if raw:
        try:
            n = int(raw)
        except (TypeError, ValueError) as _ff_exc:
            _swallowed(_ff_exc, site="prompts.prompt_compactor_v8.resolve_token_budget:95", category="telemetry")
            return DEFAULT_TOKEN_BUDGET
        if n > 0:
            return n
    return DEFAULT_TOKEN_BUDGET

# Approx char/token ratio (cl100k-ish heuristic; we are budget-aware, not
# tokenizer-exact). Real tokenization is provider-specific and out-of-scope
# for a compactor that has to run before the provider is chosen.
_CHARS_PER_TOKEN: int = 4


def is_enabled() -> bool:
    """Return True only when the env gate is explicitly set (default OFF)."""
    return os.getenv(_ENV_GATE, "false").strip().lower() in {"1", "true", "yes", "on"}  # tier: T2


def write_path_enabled() -> bool:
    """Return True only when the write-path gate is explicitly set (default OFF).

    Independent of :func:`is_enabled` — the read-path (compaction) and the
    write-path (prompt-file rewrite) are gated separately so we can dogfood
    compaction in dry-run before any disk write becomes live.
    """
    return os.getenv(_WRITE_GATE, "false").strip().lower() in {"1", "true", "yes", "on"}  # tier: T9


# ---------------------------------------------------------------------------
# RolePromptPacket — minimal local schema (feat 6 will canonicalise).
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class RolePromptPacket:
    """Output of one compaction pass for one lane/role.

    A frozen dataclass so callers cannot mutate compacted state mid-cycle.

    Fields:
      role: lane role string (e.g. ``"verifier"``).
      base_prompt: the role's static base prompt (untouched by compaction).
      compacted_history: ordered list of *compacted* event summaries.
      retained_event_ids: ordered tuple of source event ids kept verbatim
        (high-quality outcomes).
      dropped_event_count: how many low-importance events were discarded.
      token_budget: budget used for this compaction (echoed for audit).
      estimated_tokens: heuristic token count of the rendered packet.
    """

    role: str = ""
    base_prompt: str = ""
    compacted_history: tuple[str, ...] = field(default_factory=tuple)
    retained_event_ids: tuple[str, ...] = field(default_factory=tuple)
    dropped_event_count: int = 0
    token_budget: int = DEFAULT_TOKEN_BUDGET
    estimated_tokens: int = 0
    # __SLOT_COMPACTOR_CURSOR_2026_07_24__ The cursor a caller should persist and
    # pass back on the NEXT round, so each compaction reads only NEW events.
    # Equals the newest event timestamp seen this round; when nothing was read
    # (gate OFF / no new events) it echoes the incoming cursor so persisting it
    # is always safe and never rewinds.
    next_cursor: float = 0.0

    def is_empty(self) -> bool:
        """True when no history survived compaction (e.g. cold start)."""
        return not self.compacted_history and not self.retained_event_ids

    def render(self) -> str:
        """Render the packet to a single composed prompt string."""
        if not self.compacted_history:
            return self.base_prompt
        bullets = "\n".join(f"- {line}" for line in self.compacted_history if line.strip())
        if not bullets:
            return self.base_prompt
        if not self.base_prompt:
            return f"# Cycle history (compacted)\n{bullets}"
        return f"{self.base_prompt}\n\n# Cycle history (compacted)\n{bullets}"


# ---------------------------------------------------------------------------
# CycleLogger protocol — only the read_since surface we depend on.
# ---------------------------------------------------------------------------

class _CycleLogReader(Protocol):
    """Structural protocol: any logger exposing read_since works as input."""

    def read_since(
        self,
        cursor: float,
        *,
        event_type: str | None = ...,
        cycle_id: str | None = ...,
        limit: int | None = ...,
    ) -> list[dict[str, Any]]: ...


# ---------------------------------------------------------------------------
# Scoring + summarisation primitives.
# ---------------------------------------------------------------------------

def _quality_signal(payload: Mapping[str, Any]) -> float:
    """Extract a 0..1 quality score from cycle payload metadata.

    v7.1 carried this in proposal metadata (`status`, `outcome`); v8 follows
    the cycle-event payload contract used by orchestrator_v8 / SI:

      - explicit ``quality`` key (float 0..1) wins.
      - ``outcome in {"pass","success","verified","integrated"}`` -> 1.0
      - ``outcome in {"fail","rejected","error"}`` -> 0.0
      - ``severity in {"critical","high"}`` lifts low-quality to 0.6 so
        adversarial findings are not dropped.
      - otherwise 0.5 (neutral retention bias).
    """
    if not isinstance(payload, Mapping):
        return 0.5
    raw_quality = payload.get("quality")
    if isinstance(raw_quality, (int, float)):
        return max(0.0, min(1.0, float(raw_quality)))

    outcome = str(payload.get("outcome", "")).strip().lower()
    if outcome in {"pass", "success", "verified", "integrated", "ok"}:
        return 1.0
    if outcome in {"fail", "failure", "rejected", "error", "blocked"}:
        return 0.0

    severity = str(payload.get("severity", "")).strip().lower()
    if severity in {"critical", "high"}:
        return 0.6

    return 0.5


def _importance(event: Mapping[str, Any], *, now_cursor: float) -> float:
    """Importance = type_weight × quality × recency_factor.

    recency_factor decays linearly from 1.0 (event at now_cursor) to 0.5
    (event arbitrarily old). This keeps very recent low-quality events above
    very old neutral ones — important for the v7.1 "evolving prompt" effect.
    """
    type_w = _TYPE_WEIGHTS.get(str(event.get("event_type", "")), _DEFAULT_TYPE_WEIGHT)
    payload = event.get("payload") or {}
    if not isinstance(payload, Mapping):
        payload = {}
    quality = _quality_signal(payload)

    ts = event.get("timestamp_unix")
    try:
        ts_val = float(ts)  # type: ignore[arg-type]
    except (TypeError, ValueError) as _ff_exc:
        _swallowed(_ff_exc, site="prompts.prompt_compactor_v8._importance:242", category="telemetry")
        ts_val = 0.0
    if now_cursor <= 0.0 or ts_val <= 0.0:
        recency = 1.0
    else:
        age = max(0.0, now_cursor - ts_val)
        # Half-life ~ now_cursor itself; works for both small synthetic and
        # large epoch-second cursors.
        denom = max(1.0, now_cursor)
        recency = max(0.5, 1.0 - 0.5 * (age / denom))

    return type_w * (0.25 + 0.75 * quality) * recency


def _summarise(event: Mapping[str, Any]) -> str:
    """Produce a one-line summary for an event (semantic-chunk-preserving).

    Preserves the event_type label and the most-load-bearing payload keys
    (summary / outcome / message / decision). Falls back to JSON-ish key=val
    truncation if no canonical key is present.
    """
    event_type = str(event.get("event_type", "event"))
    payload = event.get("payload") or {}
    if not isinstance(payload, Mapping):
        payload = {}

    for key in ("summary", "decision", "message", "outcome", "note"):
        if key in payload and payload[key]:
            text = str(payload[key]).strip().replace("\n", " ")
            return f"[{event_type}] {text}"

    # Fallback: short k=v rendering. __SLOT_COMPACTOR_FALLBACK_KEYS_2026_07_24__
    # This was a hard `[:3]` with NO marker and no counter, and it runs BEFORE the
    # token budget — so raising DEFAULT_TOKEN_BUDGET could not recover what it
    # dropped. It is also the COMMON path: only summary/decision/message/outcome/
    # note short-circuit above, so keys 4..N of every ordinary agent_step /
    # evidence_emit payload were lost silently. Now bounded much higher and the
    # drop is MARKED, matching the per-value "..." cap just below.
    _max_keys = 12
    items = list(payload.items())
    parts: list[str] = []
    for k, v in items[:_max_keys]:
        sv = str(v).strip().replace("\n", " ")
        if len(sv) > 60:
            sv = sv[:57] + "..."
        parts.append(f"{k}={sv}")
    if len(items) > _max_keys:
        parts.append(f"(+{len(items) - _max_keys} more keys)")
    body = " ".join(parts) if parts else "(no payload)"
    return f"[{event_type}] {body}"


def _estimate_tokens(text: str) -> int:
    """Heuristic token estimate (char-count / _CHARS_PER_TOKEN, ceil)."""
    n = len(text)
    if n <= 0:
        return 0
    return (n + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN


# ---------------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------------

def compact_prompts(
    *,
    logger: _CycleLogReader,
    role: str,
    base_prompt: str = "",
    cursor: float = 0.0,
    cycle_id: str | None = None,
    token_budget: int | None = None,
) -> RolePromptPacket:
    """Read cycle history since *cursor* and emit a compacted RolePromptPacket.

    Args:
      logger: any object exposing :meth:`read_since` (CycleLogger or stub).
      role: lane/role identifier — echoed into the packet.
      base_prompt: the role's static base prompt; prepended verbatim.
      cursor: ``timestamp_unix`` lower bound (strictly-after semantics).
      cycle_id: optional cycle filter passed straight to read_since.
      token_budget: heuristic char/_CHARS_PER_TOKEN ceiling for the packet.

    Returns:
      :class:`RolePromptPacket` with ``compacted_history`` ordered
      most-important-first. When the env gate is OFF the packet keeps an
      empty history (the base prompt still rides through verbatim) so the
      surface is wired but inert.
    """
    # __SLOT_COMPACTOR_BUDGET_2026_07_24__ ALWAYS route through the resolver.
    # This was `if token_budget <= 0: token_budget = resolve_token_budget()`,
    # which never fired for a caller that omitted the argument (the signature
    # default was the constant itself) — so the env knob was dead for every such
    # caller, e.g. compact_from_events. Precedence: explicit > env > default.
    token_budget = resolve_token_budget(token_budget)

    # When the gate is off return a stub: base prompt only, no events read.
    # next_cursor echoes the incoming cursor — persisting it must never rewind.
    if not is_enabled():
        return RolePromptPacket(
            role=role,
            base_prompt=base_prompt,
            token_budget=token_budget,
            estimated_tokens=_estimate_tokens(base_prompt),
            next_cursor=float(cursor),
        )

    events: list[dict[str, Any]] = list(
        logger.read_since(float(cursor), cycle_id=cycle_id)
    )
    if not events:
        # Nothing new since the cursor — echo it so the caller's persisted
        # cursor stays put instead of rewinding to 0.
        return RolePromptPacket(
            role=role,
            base_prompt=base_prompt,
            token_budget=token_budget,
            estimated_tokens=_estimate_tokens(base_prompt),
            next_cursor=float(cursor),
        )

    # Use the newest event timestamp as the recency anchor.
    anchor = max((float(e.get("timestamp_unix", 0.0) or 0.0) for e in events), default=0.0)

    scored: list[tuple[float, Mapping[str, Any]]] = [
        (_importance(e, now_cursor=anchor), e) for e in events
    ]
    # Most-important first; ties broken by ascending timestamp for stability.
    scored.sort(key=lambda kv: (-kv[0], float(kv[1].get("timestamp_unix", 0.0))))

    base_token_cost = _estimate_tokens(base_prompt)
    remaining = max(0, token_budget - base_token_cost)
    kept_lines: list[str] = []
    kept_ids: list[str] = []
    dropped = 0

    for score, event in scored:
        # Hard-drop quality==0 (explicit failure) events when we have anything
        # at all kept already: they retain their slot only if budget allows
        # AND no higher-importance event is starved.
        line = _summarise(event)
        line_cost = _estimate_tokens(line) + 1  # +1 for newline/bullet
        if line_cost > remaining:
            dropped += 1
            continue
        kept_lines.append(line)
        evid = str(
            event.get("event_id")
            or event.get("payload", {}).get("event_id")
            or f"{event.get('event_type', 'event')}@{event.get('timestamp_unix', 0.0)}"
        )
        kept_ids.append(evid)
        remaining -= line_cost

    estimated = base_token_cost + sum(_estimate_tokens(l) + 1 for l in kept_lines)
    return RolePromptPacket(
        role=role,
        base_prompt=base_prompt,
        compacted_history=tuple(kept_lines),
        retained_event_ids=tuple(kept_ids),
        dropped_event_count=dropped,
        token_budget=token_budget,
        estimated_tokens=estimated,
        # Newest timestamp read this round. Never regress below the incoming
        # cursor (a clock skew / out-of-order write must not rewind progress).
        next_cursor=max(float(cursor), anchor),
    )


def compact_from_events(
    *,
    events: Iterable[Mapping[str, Any]],
    role: str,
    base_prompt: str = "",
    # None (not the constant) so an omitted argument still reaches
    # resolve_token_budget and the env knob applies here too.
    token_budget: int | None = None,
) -> RolePromptPacket:
    """Pure-data variant: compact a pre-fetched event list.

    Bypasses the env gate; intended for offline batch use and unit tests that
    don't want to spin up a CycleLogger. Behaviour is otherwise identical to
    :func:`compact_prompts`.
    """
    event_list = list(events)

    class _Stub:
        def read_since(self, _c: float, **_k: Any) -> list[dict[str, Any]]:
            return [dict(e) for e in event_list]

    # Locally force-enable the compactor for this synchronous call.
    prev = os.environ.get(_ENV_GATE)
    os.environ[_ENV_GATE] = "true"
    try:
        return compact_prompts(
            logger=_Stub(),
            role=role,
            base_prompt=base_prompt,
            cursor=0.0,
            token_budget=token_budget,
        )
    finally:
        if prev is None:
            os.environ.pop(_ENV_GATE, None)
        else:
            os.environ[_ENV_GATE] = prev


# ---------------------------------------------------------------------------
# Write path — archive + SafeAutoApply.apply_block (Step 2).
# __SLOT_W8_COMPACTOR_WRITE_PATH_2026_05_31__
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WriteCompactedResult:
    """Outcome of :func:`write_compacted`.

    Fields:
      written: True iff SafeAutoApply.apply_block returned ``success=True``.
      archived: True iff a JSON snapshot was written to the archive dir.
      target: absolute path the compactor *attempted* to write.
      proposal_id: the block-marker proposal id used (``compactor_<role>``).
      archive_path: absolute path to the archive JSON file (when archived).
      reason: short tag describing the dominant outcome
              (``ok`` / ``write_path_disabled`` / ``not_in_allowlist`` /
              ``empty_packet`` / ``archive_failed`` / ``apply_failed`` /
              ``rate_limited`` / ``safe_auto_apply_unavailable``).
      errors: optional underlying error strings (from SafeAutoApply or I/O).
    """

    written: bool = False
    archived: bool = False
    target: str = ""
    proposal_id: str = ""
    archive_path: str = ""
    reason: str = ""
    errors: tuple[str, ...] = field(default_factory=tuple)


def _archive_dir() -> Path:
    """Resolve archive directory honouring AGI_V8_STATE_DIR override."""
    state_dir = os.environ.get("AGI_V8_STATE_DIR", "").strip()
    if state_dir:
        base = Path(state_dir)
    else:
        base = (Path(__file__).resolve().parents[1] / "state")
    return base / _ARCHIVE_SUBDIR


def _archive_packet(target_file: str, proposal_id: str,
                    packet: RolePromptPacket) -> Path:
    """Persist a JSON snapshot of the packet under the archive dir.

    Returns the archive path on success; raises OSError on disk failure.
    The archive is append-style: each call writes a *new* timestamped file
    so the JSON history is preserved across replacements (matches v7.1's
    ArchiveEntry list semantics without the dedupe — dedupe happens at the
    SafeAutoApply layer via block-marker REPLACE).
    """
    adir = _archive_dir()
    adir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    safe_role = "".join(c if c.isalnum() or c in "-_" else "_" for c in packet.role)
    archive_path = adir / f"{Path(target_file).name}.{safe_role}.{ts}.json"
    payload: dict[str, Any] = {
        "schema": "prompt_compactor_v8.archive.v1",
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        "target_file": str(target_file),
        "proposal_id": proposal_id,
        "role": packet.role,
        "base_prompt": packet.base_prompt,
        "compacted_history": list(packet.compacted_history),
        "retained_event_ids": list(packet.retained_event_ids),
        "dropped_event_count": packet.dropped_event_count,
        "token_budget": packet.token_budget,
        "estimated_tokens": packet.estimated_tokens,
    }
    archive_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return archive_path


def render_compacted_markdown(packet: RolePromptPacket) -> str:
    """Render a compacted packet to the markdown payload SafeAutoApply expects.

    The block-marker wrapper (`# [Auto-applied: ...] start/end`) is added by
    SafeAutoApply._build_block — we only contribute the inner body. We keep
    the body small and well-structured so a future audit/diff can scan it.
    """
    lines: list[str] = [f"# Compacted role prompt — {packet.role or '(unknown)'}"]
    if packet.base_prompt and packet.base_prompt.strip():
        lines.append("")
        lines.append("## Base prompt")
        lines.append(packet.base_prompt.rstrip())
    if packet.compacted_history:
        lines.append("")
        lines.append("## Cycle history (compacted, importance-ordered)")
        for entry in packet.compacted_history:
            if entry.strip():
                lines.append(f"- {entry}")
    lines.append("")
    lines.append(
        f"<!-- estimated_tokens={packet.estimated_tokens} "
        f"dropped={packet.dropped_event_count} "
        f"retained={len(packet.retained_event_ids)} -->"
    )
    return "\n".join(lines)


class _SafeAutoApplyLike(Protocol):
    """Structural protocol matching SafeAutoApply.apply_block we depend on."""

    def apply_block(
        self,
        target_abs_path: str,
        proposal_id: str,
        payload: str,
        *,
        verifier_id: str = ...,
        decision_source: str = ...,
    ) -> Any: ...


def write_compacted(
    target_file: str,
    compacted_packet: RolePromptPacket,
    safe_auto_apply: Optional[_SafeAutoApplyLike] = None,
    *,
    allowlist: Optional[Iterable[str]] = None,
    archive_fn: Optional[Callable[[str, str, RolePromptPacket], Path]] = None,
    verifier_id: str = "prompt_compactor_v8",
    decision_source: str = "prompt_compactor_v8",
) -> WriteCompactedResult:
    """Write a compacted RolePromptPacket to *target_file* via SafeAutoApply.

    Pipeline (Step 2, mirrors v7.1 contract end-to-end):

      1. Gate check (`AGI_V8_COMPACTOR_WRITE_PATH_ENABLED`). Default OFF →
         archive only is skipped too; caller keeps the old read-only flow.
      2. Allowlist check. Target must appear in *allowlist* or, when
         *safe_auto_apply* is provided, in its ``prompt_allowlist``.
      3. Empty-packet guard. A packet with no base prompt AND no compacted
         history is a no-op (would write a banner-only block).
      4. Archive write. JSON snapshot lands under
         ``{AGI_V8_STATE_DIR or installed-package/state}/prompt_compaction_archive``.
         If archive raises, NO SafeAutoApply call is made (provenance gap).
      5. Render markdown payload (block-marker REPLACE — proposal_id is
         deterministic: ``compactor_<role>``, so each role owns exactly one
         block per file).
      6. SafeAutoApply.apply_block. Surfaces success/rate-limit/error via
         the returned WriteCompactedResult.

    Args:
      target_file: absolute path of a prompt file in the allowlist.
      compacted_packet: packet returned by :func:`compact_prompts`.
      safe_auto_apply: a SafeAutoApply (or .apply_block-compatible) object.
        When None and gate is ON → reason='safe_auto_apply_unavailable'.
      allowlist: optional explicit allowlist; falls back to
        ``safe_auto_apply.prompt_allowlist`` if missing.
      archive_fn: injection point for tests; defaults to :func:`_archive_packet`.
      verifier_id / decision_source: forwarded to SafeAutoApply for audit.

    Returns:
      :class:`WriteCompactedResult` with full provenance.
    """
    role = compacted_packet.role or "unknown"
    safe_role = "".join(c if c.isalnum() or c in "-_" else "_" for c in role)
    proposal_id = f"compactor_{safe_role}"

    # 1. Gate check.
    if not write_path_enabled():
        return WriteCompactedResult(
            target=str(target_file),
            proposal_id=proposal_id,
            reason="write_path_disabled",
        )

    # 2. Allowlist check (explicit > safe_auto_apply.prompt_allowlist).
    allow: Optional[frozenset[str]] = None
    if allowlist is not None:
        allow = frozenset(str(a) for a in allowlist)
    elif safe_auto_apply is not None:
        saa_allow = getattr(safe_auto_apply, "prompt_allowlist", None)
        if saa_allow is not None:
            allow = frozenset(str(a) for a in saa_allow)
    # __SLOT_T3_PATH_CONTAINMENT_2026_08_08__ Second copy of the literal-membership
    # test (the first is safe_auto_apply._is_allowlisted_abs). Same gate folds both
    # sides here so the two copies cannot drift apart. Containment itself is still
    # enforced downstream by SafeAutoApply — this only stops a spelling difference
    # from short-circuiting to `not_in_allowlist` before it gets there.
    from agi_v8_1.enforcement.safe_auto_apply import _path_containment_enabled, _safe_resolve

    if allow is not None and _path_containment_enabled():
        # ⛔ Normalization must not RAISE out of the pre-filter — `~unknownuser`
        # spellings throw RuntimeError, and round 1 let that escape (measured
        # 2026-08-08). `_safe_resolve` returns None instead; None = deny, and a
        # single unnormalizable allowlist entry only removes itself.
        _tf = _safe_resolve(target_file)
        _allow_res = {r for r in (_safe_resolve(a) for a in allow) if r is not None}
        if _tf is None or _tf not in _allow_res:
            return WriteCompactedResult(
                target=str(target_file),
                proposal_id=proposal_id,
                reason="not_in_allowlist",
                errors=(f"target_not_in_allowlist: {target_file}",),
            )
    elif allow is not None and str(target_file) not in allow:
        return WriteCompactedResult(
            target=str(target_file),
            proposal_id=proposal_id,
            reason="not_in_allowlist",
            errors=(f"target_not_in_allowlist: {target_file}",),
        )

    # 3. Empty-packet guard.
    if compacted_packet.is_empty() and not compacted_packet.base_prompt.strip():
        return WriteCompactedResult(
            target=str(target_file),
            proposal_id=proposal_id,
            reason="empty_packet",
        )

    # 4. Archive first — provenance precondition for any write.
    archive_writer = archive_fn or _archive_packet
    try:
        archive_path = archive_writer(target_file, proposal_id, compacted_packet)
    except Exception as exc:  # noqa: BLE001
        safe_exc = format_exception_for_critical_record(
            exc, max_chars=2000, one_line=True
        )
        _swallowed(exc, site="prompts.prompt_compactor_v8.write_compacted:650", category="telemetry")
        return WriteCompactedResult(
            target=str(target_file),
            proposal_id=proposal_id,
            reason="archive_failed",
            errors=(f"archive_failed: {safe_exc}",),
        )

    # 5. SafeAutoApply unavailability check.
    if safe_auto_apply is None:
        return WriteCompactedResult(
            archived=True,
            target=str(target_file),
            proposal_id=proposal_id,
            archive_path=str(archive_path),
            reason="safe_auto_apply_unavailable",
        )

    # 6. Render + apply_block (REPLACE semantics via deterministic proposal_id).
    payload_md = render_compacted_markdown(compacted_packet)
    try:
        apply_res = safe_auto_apply.apply_block(
            str(target_file),
            proposal_id,
            payload_md,
            verifier_id=verifier_id,
            decision_source=decision_source,
        )
    except Exception as exc:  # noqa: BLE001
        safe_exc = format_exception_for_critical_record(
            exc, max_chars=2000, one_line=True
        )
        _swallowed(exc, site="prompts.prompt_compactor_v8.write_compacted:678", category="telemetry")
        return WriteCompactedResult(
            archived=True,
            target=str(target_file),
            proposal_id=proposal_id,
            archive_path=str(archive_path),
            reason="apply_failed",
            errors=(f"apply_block_raised: {safe_exc}",),
        )

    success = bool(getattr(apply_res, "success", False))
    err_iter = getattr(apply_res, "errors", ()) or ()
    err_tuple = tuple(
        format_text_for_critical_record(e, max_chars=2000, one_line=True)
        for e in err_iter
    )
    if success:
        reason = "ok"
    else:
        joined = " ".join(err_tuple)
        if "rate_limited" in joined:
            reason = "rate_limited"
        elif "not_in_allowlist" in joined:
            reason = "not_in_allowlist"
        else:
            reason = "apply_failed"

    return WriteCompactedResult(
        written=success,
        archived=True,
        target=str(target_file),
        proposal_id=proposal_id,
        archive_path=str(archive_path),
        reason=reason,
        errors=err_tuple,
    )


__all__ = [
    "DEFAULT_TOKEN_BUDGET",
    "RolePromptPacket",
    "WriteCompactedResult",
    "compact_from_events",
    "compact_prompts",
    "is_enabled",
    "render_compacted_markdown",
    "write_compacted",
    "write_path_enabled",
    # __SLOT_W8_PROMPT_COMPACTOR_V8_2026_05_31__
    # __SLOT_W8_COMPACTOR_WRITE_PATH_2026_05_31__
]
