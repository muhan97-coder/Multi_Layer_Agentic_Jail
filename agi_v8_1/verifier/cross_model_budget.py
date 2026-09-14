# __SLOT_CROSS_MODEL_BUDGET_2026_07_25__ Durable USD cap for the S3 reviewer.
"""Cross-process spend ledger + hard USD cap for the cross-model adversary.

The S3 second opinion now runs on a real paid model (Claude Sonnet 5 by
default), once per verified apply. That needs a ceiling, and the ceiling has to
survive process boundaries — which is exactly where a naive implementation
fails here:

    ``scripts/tick.sh`` is a cron entry that runs ONE python process per tick,
    and each tick runs ONE cycle. An in-memory counter would therefore reset on
    every single cycle and never accumulate — the same defect that made the
    continuation ring's escalation streak unreachable (see
    ``AGI_V8_CONTINUATION_RING_REHYDRATE_ENABLED``). So spend is appended to a
    JSONL ledger on disk and re-summed from disk on every check.

FAIL-CLOSED POSTURE (each property is load-bearing):
  * **A missing cap is not an unlimited budget.** ``DEFAULT_CAP_USD`` applies
    when the env var is unset or unparseable, so a config slip cannot authorise
    unbounded spend.
  * **An unreadable/corrupt ledger reads as MAX spend, not zero.** If we cannot
    prove how much has been spent, we must assume the cap is gone — the
    opposite (treating an unreadable ledger as $0 spent) would turn a disk
    fault into an unlimited budget.
  * **Reserve-then-reconcile.** The caller reserves a conservative estimate
    BEFORE dispatching and reconciles to the measured cost after, so a crash
    mid-call leaves the budget over-charged rather than un-charged.
  * Cap exhausted → the caller builds no adversary → the verify stage
    fail-closed BLOCKS. Running out of budget can never mean "skip the check
    and apply anyway".
"""
from __future__ import annotations

import json
import hashlib
import logging
import math
import os
import time
import uuid
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from agi_v8_1.state.path_guard import resolve_sink

# __SLOT_FAIL_FAST_2026_07_25__ Swallowed failures route through one choke
# point: counted + named always, re-raised under AGI_V8_STRICT_FAIL_FAST.
from agi_v8_1.policy.fail_fast import (
    format_exception_for_log,
    record_critical_failure,
    swallowed as _swallowed,
)

logger = logging.getLogger(__name__)

# Operator knobs.
CAP_ENV = "AGI_V81_CROSS_MODEL_VERIFY_USD_CAP"
LEDGER_ENV = "AGI_V81_CROSS_MODEL_VERIFY_LEDGER"
# Issued by goal_campaign from validated episode_budget_usd, not an overlay.
EPISODE_BUDGET_ENV = "AGI_V8_EPISODE_BUDGET_USD"

# __SLOT_P0B_USAGE_UNMEASURED_2026_08_17__ Defect fix: `usage_to_usd` prices
# BOTH "genuinely free" and "no token evidence at all" as $0.0, so the old
# `_review_fn` reconcile (`actual - reserve`) silently refunded the ENTIRE
# reservation whenever `provider.last_usage` was missing/malformed — the
# exact "measured absent collapses into cost $0" defect this track was asked
# to close (see module docstring FAIL-CLOSED POSTURE + audit directive #4).
#
# Default ON: this is a bug fix, not a new feature. V2 retires the unsafe
# rollback direction: OFF remains observable for configuration compatibility,
# but cannot erase measurement evidence or refund malformed usage.
USAGE_MEASURED_STRICT_ENV = "AGI_V81_CROSS_MODEL_USAGE_MEASURED_STRICT"

# Hard default so an unset env var still bounds spend (see module docstring).
DEFAULT_CAP_USD = 19.24

# Below this remaining balance we refuse to start another review rather than
# risk a call whose reconciled cost overshoots the cap.
MIN_HEADROOM_USD = 0.05

SCHEMA_VERSION = "cross_model_verify_spend_v2"
LEGACY_SCHEMA_VERSION = "cross_model_verify_spend_v1"
_KINDS = frozenset({"reserve", "reconcile", "actual"})
_TX_PREFIX = "xmv2-"
_USD_QUANTUM = Decimal("0.00000001")
_ATTRIBUTION_KEY = "_cross_model_attribution"
ATTRIBUTION_EXACT = "exact_transaction"
ATTRIBUTION_LEGACY_UPPER_BOUND = "legacy_upper_bound"


def resolve_ledger_path(state_dir: Path | str | None = None) -> Path:
    """Resolve the one canonical cross-model ledger path.

    An explicit ledger override always wins.  A consumer that already owns a
    jail/state root may pass it here; otherwise the writer's environment and
    package-state precedence is used.  Keeping this resolver public prevents
    admission, cap, cycle, and briefing readers from silently observing a
    different ledger than the writer.
    """
    override = os.environ.get(LEDGER_ENV, "").strip()
    if override:
        # __SLOT_LEDGER_SINK_2026_08_08__ 정규화(+AGI_V8_LEDGER_SINK_STRICT 시 containment).
        return resolve_sink(override, what="cross_model_verify ledger")
    if state_dir is not None:
        root = Path(state_dir)
    else:
        env_dir = os.environ.get("AGI_V8_STATE_DIR") or os.environ.get("AGI_STATE_DIR")
        root = (
            Path(env_dir)
            if env_dir
            else Path(__file__).resolve().parent.parent / "state"
        )
    return root / "runtime_logs" / "cross_model_verify_spend.jsonl"


def _resolve_ledger_path() -> Path:
    """Backward-compatible private alias for the canonical writer resolver."""
    return resolve_ledger_path()


def _new_transaction_id() -> str:
    """Collision-resistant correlation ID; not authorization or authenticity."""
    return f"{_TX_PREFIX}{uuid.uuid4().hex}"


def _valid_transaction_id(value: Any) -> bool:
    if type(value) is not str or not value.startswith(_TX_PREFIX):
        return False
    suffix = value[len(_TX_PREFIX):]
    return len(suffix) == 32 and all(ch in "0123456789abcdef" for ch in suffix)


def _finite_number(value: Any) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, TypeError, ValueError):
        return False


def _usd_has_at_most_8dp(value: Any) -> bool:
    """True only for finite, non-bool JSON numbers representable at 8dp.

    V2 writers quantize durable money to eight decimal places.  Readers must
    enforce the same contract rather than accepting a near-equal float under
    the reconcile tolerance: otherwise a hand-written 9dp detail can validate
    against an 8dp principal and give independent watchers a different view.
    ``Decimal(str(...))`` mirrors the durable JSON number's semantic decimal
    value and also handles scientific notation without binary-float noise.
    """
    if not _finite_number(value):
        return False
    try:
        return Decimal(str(value)).as_tuple().exponent >= -8
    except (InvalidOperation, TypeError, ValueError):
        return False


def quantize_usd_ceiling(value: Any) -> float:
    """Round a non-negative USD principal upward to the ledger quantum."""
    if not _finite_number(value) or float(value) < 0:
        raise ValueError("invalid non-negative USD principal")
    try:
        return float(Decimal(str(value)).quantize(_USD_QUANTUM, rounding=ROUND_CEILING))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("invalid non-negative USD principal") from exc


def _json_object_no_duplicates(raw: str) -> dict[str, Any]:
    def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in pairs:
            if key in out:
                raise ValueError("duplicate JSON object key")
            out[key] = value
        return out

    def _reject_constant(_value: str) -> None:
        # Python's decoder otherwise accepts NaN/Infinity even though they are
        # not JSON numbers.  Reject them at any nesting depth before semantic
        # accounting validation sees a partially decoded row.
        raise ValueError("non-standard JSON numeric constant")

    value = json.loads(
        raw,
        object_pairs_hook=_pairs,
        parse_constant=_reject_constant,
    )
    if not isinstance(value, dict):
        raise ValueError("ledger row is not an object")
    # ``parse_constant`` catches literal NaN/Infinity, but a standards-valid
    # numeric lexeme such as ``1e309`` still decodes to ``inf`` in CPython.
    # Apply the same recursive finite/plain-tree contract as the writer before
    # any reader accepts the row.
    return _plain_json_tree(value)


def _plain_json_tree(value: Any, *, depth: int = 0) -> Any:
    """Copy a value into a finite, subclass-free JSON tree.

    ``json.dumps`` accepts NaN by default and can retain hostile ``str``
    subclasses as mapping keys.  Either can make an acknowledged append poison
    the strict reader on the very next pass, so accounting detail is validated
    before it reaches the durable transaction.
    """
    if depth > 32:
        raise ValueError("accounting detail nesting is too deep")
    if value is None or type(value) in (bool, str, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("accounting detail contains a non-finite number")
        return value
    if type(value) is list:
        return [_plain_json_tree(item, depth=depth + 1) for item in value]
    if type(value) is dict:
        out: dict[str, Any] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError("accounting detail key is not a plain string")
            if key in out:
                raise ValueError("accounting detail contains a duplicate key")
            out[key] = _plain_json_tree(item, depth=depth + 1)
        return out
    raise ValueError("accounting detail is not a plain JSON tree")


def _read_ledger_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(_json_object_no_duplicates(line))
    return rows


def read_spend_rows(path: Path | str) -> list[dict[str, Any]]:
    """Strict JSON reader shared by every cross-model spend consumer."""
    return _read_ledger_rows(Path(path))


def _transaction_defect(rows: list[Mapping[str, Any]]) -> str | None:
    """Validate v2 accounting state.  Open reserves are deliberately valid."""
    states: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        if row.get("schema_version") != SCHEMA_VERSION:
            continue
        txid = row.get("transaction_id")
        model = row.get("model")
        kind = row.get("kind")
        usd = row.get("usd")
        measured = row.get("usage_measured")
        ts = row.get("ts")
        if not _valid_transaction_id(txid):
            return "transaction_id_invalid"
        if not isinstance(model, str) or not model.strip():
            return "transaction_model_invalid"
        if kind not in _KINDS:
            return "transaction_kind_invalid"
        if not _finite_number(usd) or not _finite_number(ts) or float(ts) <= 0:
            return "transaction_number_invalid"
        if not _usd_has_at_most_8dp(usd):
            return "transaction_amount_precision_invalid"
        if type(measured) is not bool:
            return "transaction_measurement_invalid"
        amount = float(usd)
        if kind in {"reserve", "actual"} and amount < 0:
            return "transaction_negative_principal"
        if kind == "reserve" and measured is not False:
            return "transaction_reserve_measured"
        if kind == "actual" and measured is not True:
            return "transaction_actual_unmeasured"
        if kind == "reconcile" and measured is False and amount != 0.0:
            return "transaction_unmeasured_reconcile_nonzero"
        states.setdefault(str(txid), []).append(row)

    for tx_rows in states.values():
        kinds = [str(row["kind"]) for row in tx_rows]
        if kinds == ["actual"]:
            continue
        if kinds not in (["reserve"], ["reserve", "reconcile"]):
            return "transaction_sequence_invalid"
        if len(tx_rows) == 1:
            continue
        reserve, reconcile = tx_rows
        if reserve.get("model") != reconcile.get("model"):
            return "transaction_model_mismatch"
        reserved = Decimal(str(reserve["usd"]))
        delta = Decimal(str(reconcile["usd"]))
        if float(reconcile["ts"]) < float(reserve["ts"]):
            return "transaction_time_reversed"
        actual = reserved + delta
        if actual < 0:
            return "transaction_negative_net"
        detail = reconcile.get("detail")
        if not isinstance(detail, Mapping):
            return "transaction_reconcile_detail_invalid"
        declared_reserved = detail.get("reserved_usd")
        if not _usd_has_at_most_8dp(declared_reserved):
            return "transaction_reserved_detail_precision_invalid"
        if Decimal(str(declared_reserved)) != reserved:
            return "transaction_reserved_detail_mismatch"
        if reconcile.get("usage_measured") is True:
            declared_actual = detail.get("actual_usd")
            if not _usd_has_at_most_8dp(declared_actual):
                return "transaction_actual_detail_precision_invalid"
            if Decimal(str(declared_actual)) != actual:
                return "transaction_actual_detail_mismatch"
        elif detail.get("usage_unmeasured") is not True:
            return "transaction_unmeasured_detail_invalid"
    return None


def _accounting_admission_blocker(rows: list[Mapping[str, Any]]) -> str | None:
    """Return a durable reason why another paid review may not be admitted.

    A measured positive reconcile proves that the supposedly worst-case wire
    envelope was not actually an upper bound.  Likewise, a closed transaction
    explicitly marked with unknown provider outcome/model identity cannot be
    used as evidence that the requested model's catalog price bounded the
    charge.  Both facts live in the immutable ledger, so a fresh process must
    remain blocked until an operator migrates/clears the affected authority.
    """
    for row in rows:
        if row.get("schema_version") != SCHEMA_VERSION:
            continue
        if row.get("kind") != "reconcile":
            continue
        if Decimal(str(row["usd"])) > 0:
            return "cost_envelope_breached"
        detail = row.get("detail")
        if isinstance(detail, Mapping) and (
            detail.get("provider_outcome_unverified") is True
        ):
            return "provider_outcome_unverified"
        if isinstance(detail, Mapping) and (
            detail.get("model_identity_unverified") is True
        ):
            return "provider_identity_unverified"
    return None


def _ledger_total_decimal(rows: list[Mapping[str, Any]]) -> Decimal:
    """Exact sum of mixed immutable-v1/current-v2 ledger principals."""
    seen_v2 = False
    for row in rows:
        schema = row.get("schema_version")
        if schema not in {LEGACY_SCHEMA_VERSION, SCHEMA_VERSION}:
            raise ValueError("unknown cross-model ledger schema")
        if schema == SCHEMA_VERSION:
            seen_v2 = True
        elif seen_v2:
            # Migration is a one-way cutover.  Accepting a legacy row after a
            # v2 transaction would let a stale writer append an unbound,
            # possibly negative reconcile behind the transaction validator.
            raise ValueError("schema_downgrade_after_v2")
        if row.get("kind") not in _KINDS:
            raise ValueError("unknown cross-model accounting kind")
        if not isinstance(row.get("model"), str) or not str(row["model"]).strip():
            raise ValueError("missing cross-model model")
        if not _finite_number(row.get("usd")):
            raise ValueError("invalid cross-model amount")
        if schema == LEGACY_SCHEMA_VERSION:
            ts = row.get("ts")
            # Immutable V1 writers historically omitted ``ts``.  Grandfather
            # that exact absence so the existing authoritative prefix can
            # migrate to V2; a present-but-invalid timestamp remains poison.
            if ts is not None and (not _finite_number(ts) or float(ts) <= 0):
                raise ValueError("invalid legacy cross-model timestamp")
        if row.get("kind") in {"reserve", "actual"} and float(row["usd"]) < 0:
            raise ValueError("negative cross-model principal")
    defect = _transaction_defect(rows)
    if defect is not None:
        raise ValueError(defect)
    total = sum((Decimal(str(row["usd"])) for row in rows), Decimal("0"))
    if total < Decimal("-0.000000005"):
        raise ValueError("invalid cross-model ledger total")
    return total


def _ledger_total(rows: list[Mapping[str, Any]]) -> float:
    """Public/internal float view after exact monetary validation."""
    total = float(_ledger_total_decimal(rows))
    if not math.isfinite(total):
        raise ValueError("cross-model ledger total is outside the float domain")
    return total


def transaction_charge_rows(
    rows: list[Mapping[str, Any]],
    *,
    allow_preschema_fixtures: bool = False,
) -> list[dict[str, Any]]:
    """Return window-attributable spend rows without mutating *rows*.

    V2 reserve/reconcile rows describe one accounting transaction, not two
    independently windowable charges.  A non-positive reconcile is collapsed
    to the non-negative net and anchored at the durable reserve timestamp, so
    a refund after midnight cannot create new-window credit.  A positive
    reconcile is different: the reserved principal stays at reserve time and
    the proven excess is charged at reconcile time.  This both preserves exact
    totals and prevents an envelope breach after midnight from being hidden in
    the preceding window.  An open reserve remains charged in full; a
    standalone ``actual`` row keeps its own timestamp.

    Ledgers containing V2 are validated with the same one-way V1-prefix and
    transaction rules as :func:`spent_usd`.  A ledger with no V2 rows is
    compatible with immutable V1.  Pre-schema rows are accepted only through
    the explicit non-authoritative fixture seam; production readers keep the
    default exact-schema contract.
    """
    materialized = [dict(row) for row in rows]
    if not materialized:
        return []
    if not any(row.get("schema_version") == SCHEMA_VERSION for row in materialized):
        schemas = {row.get("schema_version") for row in materialized}
        if schemas <= {None}:
            if not allow_preschema_fixtures:
                raise ValueError("unknown cross-model ledger schema")
            # Pre-schema fixtures have no transaction identity.  Preserve their
            # physical rows but never allow a negative historical correction
            # to enter a later window as a credit.
            charges: list[dict[str, Any]] = []
            for row in materialized:
                if not _finite_number(row.get("usd")):
                    raise ValueError("invalid pre-schema cross-model amount")
                charge = dict(row)
                charge["usd"] = max(0.0, float(row["usd"]))
                charge[_ATTRIBUTION_KEY] = ATTRIBUTION_LEGACY_UPPER_BOUND
                charges.append(charge)
            return charges
        # Declared V1 is still an authoritative schema: validate the complete
        # immutable ledger, then conservatively ignore unbound negative
        # corrections for window attribution.
        _ledger_total(materialized)
        return [
            {
                **dict(row),
                "usd": max(0.0, float(row["usd"])),
                _ATTRIBUTION_KEY: ATTRIBUTION_LEGACY_UPPER_BOUND,
            }
            for row in materialized
        ]

    # Validate the complete ledger before deriving a view.  In particular,
    # never let an unknown/downgraded prefix disappear merely because its
    # timestamp falls outside the requested consumer window.
    _ledger_total(materialized)

    by_transaction: dict[str, list[dict[str, Any]]] = {}
    for row in materialized:
        if row.get("schema_version") == SCHEMA_VERSION:
            by_transaction.setdefault(str(row["transaction_id"]), []).append(row)

    charges: list[dict[str, Any]] = []
    for row in materialized:
        if row.get("schema_version") != SCHEMA_VERSION:
            charge = dict(row)
            charge["usd"] = max(0.0, float(row["usd"]))
            charge[_ATTRIBUTION_KEY] = ATTRIBUTION_LEGACY_UPPER_BOUND
            charges.append(charge)
            continue
        if row["kind"] == "reconcile":
            continue

        charge = dict(row)
        charge[_ATTRIBUTION_KEY] = ATTRIBUTION_EXACT
        if row["kind"] == "reserve":
            tx_rows = by_transaction[str(row["transaction_id"])]
            # Stored amounts are already 8dp principals.  Re-quantize their
            # net once and erase a possible negative zero; validation above
            # has already rejected a materially negative transaction.
            reserve_amount = Decimal(str(row["usd"]))
            net = sum(
                (Decimal(str(part["usd"])) for part in tx_rows), Decimal("0")
            )
            if net <= reserve_amount:
                charge["usd"] = float(max(Decimal("0"), net))
            else:
                # The first charge is the amount legitimately admitted at
                # reserve time.  The excess is a distinct exact charge at the
                # reconcile timestamp and is also a durable admission poison.
                charge["usd"] = float(reserve_amount)
                reconcile = tx_rows[1]
                excess = dict(reconcile)
                excess["usd"] = float(net - reserve_amount)
                excess[_ATTRIBUTION_KEY] = ATTRIBUTION_EXACT
                charges.append(charge)
                charges.append(excess)
                continue
        charges.append(charge)
    return charges


def charge_attribution(rows: list[Mapping[str, Any]]) -> str:
    """Describe whether a derived charge set is exact or a legacy upper bound."""
    if any(
        row.get(_ATTRIBUTION_KEY) == ATTRIBUTION_LEGACY_UPPER_BOUND
        for row in rows
    ):
        return ATTRIBUTION_LEGACY_UPPER_BOUND
    return ATTRIBUTION_EXACT


def cap_usd() -> float:
    """Configured ceiling. Unset/garbage/negative → ``DEFAULT_CAP_USD``."""
    raw = os.environ.get(CAP_ENV, "").strip()  # tier: T6
    if not raw:
        return DEFAULT_CAP_USD
    try:
        value = float(raw)
    except (TypeError, ValueError) as _ff_exc:
        _swallowed(_ff_exc, site="verifier.cross_model_budget.cap_usd:76", category="verify")
        logger.warning(
            "cross_model budget: %s=%r is not a number — falling back to $%.2f",
            CAP_ENV, raw, DEFAULT_CAP_USD,
        )
        return DEFAULT_CAP_USD
    if not math.isfinite(value) or value < 0:
        logger.warning(
            "cross_model budget: %s=%r is non-finite/negative — falling back to $%.2f",
            CAP_ENV, raw, DEFAULT_CAP_USD,
        )
        return DEFAULT_CAP_USD
    return value


def spent_usd() -> float:
    """Total recorded spend, re-read from disk (survives process boundaries).

    A missing ledger is genuinely $0 (nothing has been spent yet). An existing
    ledger we cannot read or parse returns ``cap_usd()`` — i.e. "assume
    exhausted" — so a corrupt file blocks rather than unlocks spending.
    """
    path = _resolve_ledger_path()
    if not path.exists():
        return 0.0
    try:
        total = _ledger_total(_read_ledger_rows(path))
    except Exception as exc:  # noqa: BLE001 — unreadable ledger must fail CLOSED
        _swallowed(exc, site="verifier.cross_model_budget.spent_usd:110", category="verify")
        logger.error(
            "cross_model budget: ledger %s unreadable (%s) — treating as EXHAUSTED",
            path, format_exception_for_log(exc),
        )
        return cap_usd()
    return total


def usage_measured_strict_enabled() -> bool:
    """Gate for the reserve/reconcile "don't refund unmeasured usage" fix.

    __SLOT_P0B_USAGE_UNMEASURED_2026_08_17__ See ``USAGE_MEASURED_STRICT_ENV``
    docstring above. Default ON. V2 callers may observe OFF but must not
    restore the unsafe refund behavior; explicit measurement is structural.
    """
    return os.environ.get(USAGE_MEASURED_STRICT_ENV, "true").strip().lower() in (
        "1", "true", "yes", "on",
    )


def usage_is_measured(usage: Mapping[str, Any] | None) -> bool:
    """True iff *usage* carries real token evidence (not merely $0-shaped).

    __SLOT_P0B_USAGE_UNMEASURED_2026_08_17__ ``usage_to_usd`` cannot itself
    distinguish "0 tokens, genuinely free" from "no usage reported at all" —
    both come back ``0.0``. This predicate lets a caller keep those apart
    BEFORE deciding whether a reconcile may refund a reservation.
    """
    if not isinstance(usage, Mapping):
        return False
    # Both canonical sides are required.  Presence-only, alias-only, bool,
    # string, negative and non-finite values used to certify a measured call
    # while ``usage_to_usd`` priced the missing/malformed side as zero.  That
    # could refund the whole conservative reservation.
    for key in ("input_tokens", "output_tokens"):
        value = usage.get(key)
        if type(value) is not int or value < 0:
            return False
    # A synthetic default mapping of 0/0 is indistinguishable from a genuinely
    # free call unless the provider explicitly says it parsed raw usage.  All
    # production providers now emit this marker only for an exact raw pair.
    if usage["input_tokens"] == 0 and usage["output_tokens"] == 0:
        return usage.get("usage_measured") is True
    return True


def accounting_admission_blocker() -> str | None:
    """Durable admission poison, or ``None`` when another reserve is safe.

    An unreadable ledger is itself a blocker.  This helper is intentionally
    separate from the numeric balance: a cheap envelope breach must not become
    launchable merely because the global dollar cap still has headroom.
    """
    path = _resolve_ledger_path()
    if not path.exists():
        return None
    try:
        rows = _read_ledger_rows(path)
        _ledger_total_decimal(rows)
        return _accounting_admission_blocker(rows)
    except Exception as exc:  # noqa: BLE001 — unknown authority blocks admission
        _swallowed(
            exc,
            site="verifier.cross_model_budget.accounting_admission_blocker",
            category="verify",
        )
        return "ledger_unreadable"


def remaining_usd() -> float:
    """Budget left, never negative."""
    return max(0.0, cap_usd() - spent_usd())


def exhausted() -> bool:
    """True when there is not enough headroom left to safely run a review."""
    return (
        remaining_usd() < MIN_HEADROOM_USD
        or accounting_admission_blocker() is not None
    )


# __SLOT_CROSS_MODEL_ATOMIC_RESERVE_2026_08_25__ R21 P1 fix.
#
# WHY: ``exhausted()`` (a resum-from-disk check) and ``record_spend(kind=
# "reserve")`` (a resum-from-disk append) were always two SEPARATE disk
# round-trips with no lock spanning both — the caller in
# ``self_improvement_v8._build_cross_model_adversary``/``_review_fn`` checked
# once, then appended later. Two processes racing that gap both read the same
# "not exhausted yet" snapshot and both append their reserve — the ledger ends
# up over cap even though every individual append succeeded and was itself
# correctly durable. Reproduced with two real subprocesses sharing one ledger
# (cap=$1.0, two $0.6 reserves, both ack=True, final spent=$1.2).
#
# FIX: one lock spans "resum the ledger" + "capacity check" + "append the
# OPEN reserve row" as a single critical section, so only one racer can ever
# claim the last slice of headroom; every other racer's check runs AFTER that
# append has landed and correctly sees less room.
@contextmanager
def _reserve_lock(
    ledger_path: Path,
) -> Iterator[Callable[[Mapping[str, Any]], None]]:
    """Serializes the whole check→append critical section for *ledger_path*.

    This MUST be the same ``<ledger>.lock`` used by historical V1 writers.
    A V2-only sidecar lets a stale writer append after the capacity read but
    before the V2 reserve, so both can spend the same headroom.  The store
    transaction yields an append callback that does not recursively reacquire
    the lock; every V2 read→validate→append uses only that callback.
    """
    from agi_v8_1.state.store import atomic_append_jsonl_transaction

    with atomic_append_jsonl_transaction(
        ledger_path,
        require_directory_fsync=True,
    ) as append_locked:
        yield append_locked


def _build_spend_row(
    usd: Any,
    *,
    model: Any,
    kind: Any,
    detail: Mapping[str, Any] | None,
    usage_measured: Any,
    transaction_id: Any,
) -> dict[str, Any]:
    if type(kind) is not str or kind not in _KINDS:
        raise ValueError("invalid accounting kind")
    if type(model) is not str or not model.strip():
        raise ValueError("invalid accounting model")
    if not _finite_number(usd):
        raise ValueError("invalid accounting amount")
    amount = float(usd)
    if kind in {"reserve", "actual"} and amount < 0:
        raise ValueError("negative accounting principal")
    if type(usage_measured) is not bool:
        raise ValueError("usage measurement must be explicit")
    if kind == "reserve" and usage_measured is not False:
        raise ValueError("reserve cannot be measured")
    if kind == "actual" and usage_measured is not True:
        raise ValueError("actual must be measured")
    if kind == "reconcile" and usage_measured is False and amount != 0.0:
        raise ValueError("unmeasured reconcile must be zero")
    if transaction_id is None and kind == "actual":
        transaction_id = _new_transaction_id()
    if not _valid_transaction_id(transaction_id):
        raise ValueError("invalid transaction identity")
    # The strict reader validates the complete row at depth 0, so ``detail``
    # begins at depth 1 there.  Use the same offset before acknowledging a
    # write; otherwise a detail tree at the boundary can be writer-valid but
    # make the very next authoritative read fail.
    safe_detail = (
        None if detail is None else _plain_json_tree(detail, depth=1)
    )

    durable_amount = (
        quantize_usd_ceiling(amount)
        if kind in {"reserve", "actual"}
        else round(amount, 8)
    )
    row: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "transaction_id": transaction_id,
        "ts": time.time(),
        "usd": durable_amount,
        "model": model.strip(),
        "kind": kind,
        "usage_measured": usage_measured,
    }
    if safe_detail:
        row["detail"] = safe_detail

    return row


def _append_primary(
    append_locked: Callable[[Mapping[str, Any]], None],
    row: Mapping[str, Any],
) -> None:
    append_locked(dict(row))


def _validated_join_evidence(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError("join evidence is not a mapping")
    materialized = dict(value)
    safe: dict[str, str] = {}
    for key, item in materialized.items():
        if type(key) is not str or key not in {"cycle_id", "call_id"}:
            raise ValueError("join evidence contains non-additive keys")
        if type(item) is not str or not item.strip():
            raise ValueError("join evidence contains invalid identity")
        # Reconstruct onto a plain literal key; never retain hostile key/value
        # subclasses in the durable JSON object.
        safe["cycle_id" if key == "cycle_id" else "call_id"] = str(item)
    return safe


def _episode_capacity_snapshot(
    ledger_path: Path, cross_model_spent: Decimal,
) -> dict[str, Decimal] | None:
    """All-time episode exposure at the reviewer's locked admission boundary.

    Cross-model rows already include open reserves and measured reconciles;
    use the caller's locked sum, never reacquire that lock. Executor spend is
    read from the same parent-issued episode root without a calendar filter.
    Other executor writers do not share this lock: this serializes competing
    reviewers, not admission by every spender in the system.
    """
    raw = os.environ.get(EPISODE_BUDGET_ENV)
    if raw is None:
        return None
    cap = Decimal(raw)
    if not cap.is_finite() or cap <= 0 or not math.isfinite(float(cap)):
        raise ValueError("invalid episode budget transport")
    state_dir = os.environ.get("AGI_V8_STATE_DIR") or os.environ.get("AGI_STATE_DIR")
    if not state_dir or not Path(state_dir).is_absolute():
        raise ValueError("episode budget requires absolute state root")
    if os.environ.get(LEDGER_ENV, "").strip() or os.environ.get("AGI_V8_EXECUTOR_LOG_PATH", "").strip():
        raise ValueError("episode budget ledger override is forbidden")
    root = Path(state_dir)
    from agi_v8_1.runtime.episode_ledger import LEDGERS
    from agi_v8_1.state.store import read_jsonl

    (executor_rel, executor_key), (cross_rel, cross_key) = LEDGERS
    if cross_key != "usd" or ledger_path.resolve() != (root / cross_rel).resolve():
        raise ValueError("episode budget ledger root mismatch")
    executor_spent = Decimal("0")
    for row in read_jsonl(root / executor_rel):
        value = row.get(executor_key)
        if value is None:
            continue
        if not _finite_number(value) or value < 0:
            raise ValueError("invalid episode executor spend")
        executor_spent += Decimal(str(value))
    spent = executor_spent + cross_model_spent
    if not math.isfinite(float(spent)):
        raise ValueError("non-finite episode spend")
    return {"cap_usd": cap, "spent_before_usd": spent,
            "remaining_before_usd": cap - spent}


def reserve_if_capacity(
    usd: float,
    *,
    model: str,
    kind: str = "reserve",
    detail: Mapping[str, Any] | None = None,
    usage_measured: bool | None = None,
) -> dict[str, Any]:
    """Atomically check capacity and append the reserve row, or refuse.

    This is the ONLY capacity-safe way to reserve spend on this ledger — it
    replaces the ``exhausted()`` (check) + ``record_spend(kind="reserve")``
    (append) two-step, which had no lock spanning both and could be raced by
    two concurrent processes (see the module-level SLOT comment above for the
    reproduced failure). Callers MUST treat any receipt whose
    ``"acknowledged"`` key is not ``True`` as "no capacity — make zero
    provider calls", exactly like a failed ``record_spend`` today.

    Returns a receipt dict, always with at least:
      * ``"acknowledged"`` (bool) — ``True`` only when the OPEN reserve row is
        durably on disk AND fit under the cap at the instant it was appended.
      * ``"reason"`` (str | None) — why refused, when not acknowledged:
        ``"invalid_kind"`` / ``"invalid_model"`` /
        ``"invalid_amount"`` / ``"invalid_measurement"`` /
        ``"non_finite_ledger_state"`` / ``"capacity_exhausted"`` /
        ``"ledger_append_failed"`` / ``"internal_error"``.
      * ``"reserved_usd"`` — the amount actually reserved (``0.0`` if refused).
      * ``"transaction_id"`` — the additive v2 correlation identity, present
        only after the reserve append is durably acknowledged.
      * ``"cap_usd"`` / ``"spent_before_usd"`` / ``"remaining_before_usd"`` —
        the snapshot the decision was made against (present once the lock is
        acquired; absent if refused before that, e.g. bad ``usd``).
      * Optional ``episode_*`` fields bind admission to all-time executor and
        cross-model exposure when the campaign supplied EPISODE_BUDGET_ENV.
        At that boundary exact exhaustion is refused, as the pair watcher
        stops at exposure >= its cap. Missing transport preserves legacy use.

    FAIL-CLOSED: a malformed/non-finite ``usd``, or a cap/spent read that
    comes back non-finite (NaN/inf — e.g. a garbled ``AGI_V81_..._USD_CAP``
    combined with a poisoned ledger row) refuses rather than risking an
    unbounded reservation; it never falls back to "assume it fits".
    """
    receipt: dict[str, Any] = {
        "acknowledged": False,
        "reason": None,
        "reserved_usd": 0.0,
        "model": model if isinstance(model, str) else "",
        "kind": kind if isinstance(kind, str) else "",
        "transaction_id": None,
    }
    if type(kind) is not str or kind != "reserve":
        receipt["reason"] = "invalid_kind"
        return receipt
    if type(model) is not str or not model.strip():
        receipt["reason"] = "invalid_model"
        return receipt
    if not _finite_number(usd):
        receipt["reason"] = "invalid_amount"
        return receipt
    if usage_measured is not None and usage_measured is not False:
        receipt["reason"] = "invalid_measurement"
        return receipt
    amount = float(usd)
    if not math.isfinite(amount) or amount < 0:
        receipt["reason"] = "invalid_amount"
        return receipt

    try:
        amount = quantize_usd_ceiling(amount)
    except ValueError:
        receipt["reason"] = "invalid_amount"
        return receipt
    ledger_path: Path
    row: dict[str, Any]
    appended = False
    try:
        ledger_path = _resolve_ledger_path()
        transaction_id = _new_transaction_id()
        with _reserve_lock(ledger_path) as append_locked:
            cap = cap_usd()
            existing = _read_ledger_rows(ledger_path)
            spent_decimal = _ledger_total_decimal(existing)
            admission_blocker = _accounting_admission_blocker(existing)
            if admission_blocker is not None:
                receipt["reason"] = admission_blocker
                logger.error(
                    "cross_model budget: refusing atomic reserve — durable "
                    "accounting blocker=%s (model=%s)",
                    admission_blocker,
                    model,
                )
                return receipt
            if not math.isfinite(cap):
                receipt["reason"] = "non_finite_ledger_state"
                logger.error(
                    "cross_model budget: refusing atomic reserve — non-finite "
                    "cap=%r (model=%s)", cap, model,
                )
                return receipt
            cap_decimal = Decimal(str(cap))
            amount_decimal = Decimal(str(amount))
            remaining_before = cap_decimal - spent_decimal
            receipt["cap_usd"] = round(cap, 6)
            receipt["spent_before_usd"] = round(float(spent_decimal), 6)
            receipt["remaining_before_usd"] = round(
                float(max(Decimal("0"), remaining_before)), 6
            )
            if (
                remaining_before < Decimal(str(MIN_HEADROOM_USD))
                or amount_decimal > remaining_before
            ):
                receipt["reason"] = "capacity_exhausted"
                logger.error(
                    "cross_model budget: refusing atomic reserve $%.6f for "
                    "model=%s (remaining $%.6f of $%.2f cap)",
                    amount,
                    model,
                    float(max(Decimal("0"), remaining_before)),
                    cap,
                )
                return receipt
            try:
                episode = _episode_capacity_snapshot(ledger_path, spent_decimal)
            except (OSError, ValueError, InvalidOperation) as exc:
                record_critical_failure(
                    exc,
                    site="verifier.cross_model_budget.reserve_if_capacity:episode_budget",
                    category="verify",
                )
                receipt["reason"] = "episode_budget_unverifiable"
                return receipt
            if episode is not None:
                receipt.update({f"episode_{key}": round(float(value), 8)
                                for key, value in episode.items()})
                remaining = episode["remaining_before_usd"]
                if remaining < Decimal(str(MIN_HEADROOM_USD)) or amount_decimal >= remaining:
                    receipt["reason"] = "episode_capacity_exhausted"
                    return receipt
            row = _build_spend_row(
                amount,
                model=model,
                kind="reserve",
                detail=detail,
                usage_measured=False,
                transaction_id=transaction_id,
            )
            try:
                from agi_v8_1.runtime.ledger_join import join_keys

                joined = join_keys()
                validated_joined = _validated_join_evidence(joined)
                row.update(validated_joined)
            except Exception as join_exc:  # noqa: BLE001
                record_critical_failure(
                    join_exc,
                    site="verifier.cross_model_budget.reserve_if_capacity:join_keys",
                    category="telemetry",
                )
            # Validate the complete mixed-schema ledger, not only the new v2
            # row.  This also enforces the one-way v1-prefix -> v2 cutover.
            _ledger_total_decimal([*existing, row])
            try:
                _append_primary(append_locked, row)
                appended = True
            except Exception as exc:  # noqa: BLE001 — preserve receipt contract
                record_critical_failure(
                    exc,
                    site="verifier.cross_model_budget.reserve_if_capacity:append",
                    category="verify",
                )
                receipt["reason"] = "ledger_append_failed"
                logger.error(
                    "cross_model budget: atomic reserve capacity check passed "
                    "but the durable append failed (model=%s, $%.6f)",
                    model, amount,
                )
                return receipt
            receipt["acknowledged"] = True
            receipt["reserved_usd"] = round(amount, 8)
            receipt["transaction_id"] = transaction_id
    except Exception as exc:  # noqa: BLE001 — a broken lock must refuse, not raise
        record_critical_failure(
            exc, site="verifier.cross_model_budget.reserve_if_capacity",
            category="verify",
        )
        logger.error(
            "cross_model budget: atomic reserve raised for model=%s: %s",
            model, format_exception_for_log(exc),
        )
        receipt["reason"] = "internal_error"
        return receipt
    if appended:
        try:
            _tee_episode(row, ledger_path)
        except Exception as exc:  # noqa: BLE001 — primary reserve is durable
            record_critical_failure(
                exc,
                site="verifier.cross_model_budget.reserve_if_capacity:episode_tee",
                category="telemetry",
            )
    return receipt


def _tee_episode(row: Mapping[str, Any], ledger_path: "Any") -> None:
    """교차모델 검증 지출을 계약 원장(``episode.jsonl`` COST)에도 한 벌.

    __SLOT_XMODEL_EPISODE_TEE_2026_08_07__ 🔴 **젤 전체 지출의 53% 가 계약 원장에
    없었다** (2026-08-07 실측) ::

        episode_spend       0.58473938   = executor_log 0.27416438 + cross_model 0.31057500
        episode COST 콜합    0.25902898
                                          ⇒ cross_model 0.310575 이 통째로 0행

    swarm 간극($0.015)보다 **20배 크다.** 계약이 말하는 *"모든 과금 콜은 행을
    받는다"* 를 제일 크게 어기고 있던 곳이 여기다.

    ⚠️ ``kind`` 는 ``reserve``/``reconcile``(음수 가능)/``actual`` 이라 **한 콜이 두
    행**일 수 있다. 합은 맞지만 **콜 수는 아니다** ⇒ ``kind`` 를 ``purpose`` 에
    실어 소비자가 거를 수 있게 한다. ⛔ 여기서 행을 합쳐 "한 콜"인 척하지 않는다 —
    reserve 만 있고 reconcile 이 안 온 콜을 잃게 된다.

    ⛔ 예외를 밖으로 내지 않는다 — 원장 tee 실패가 SI 사이클을 죽이면 안 된다.
    """
    try:
        from pathlib import Path

        from agi_v8_1.runtime import episode_log as _el

        if not _el.enabled():
            return
        p = Path(ledger_path)
        if p.parent.name != "runtime_logs":
            return                      # 젤 구조를 모르면 안 쓴다(젤 밖 쓰기 금지)
        # __SLOT_P0B_USAGE_UNMEASURED_2026_08_17__ 예전엔 여기서 무조건 True 를
        # 하드코딩했다 — reserve(추정치)도, usage 증거가 없는 reconcile 도 전부
        # "실측"이라고 적었다는 뜻이다. V2는 모든 writer가 명시 bool을
        # 구조적으로 강제하므로 primary row의 값을 그대로 투영한다.
        _el.emit_cost(
            p.parent.parent,
            cycle_id=None,              # episode_ctx 문맥이 폴백으로 채운다
            usd=float(row.get("usd") or 0.0),
            purpose=f"cross_model_verify:{row.get('kind') or 'actual'}",
            model=str(row.get("model") or "") or None,
            scope=_el.COST_SCOPE_CALL,
            usage_measured=bool(row["usage_measured"]),
            # This is an accounting transaction identity, not proof of a paid
            # provider call.  The exact cross_model_verify:* purpose keeps the
            # two domains disjoint for successor consumers.
            call_id=str(row.get("transaction_id") or "") or None,
        )
    except Exception as exc:  # noqa: BLE001 — 원장 tee 가 사이클을 죽이면 안 된다
        _swallowed(exc, site="verifier.cross_model_budget._tee_episode",
                   category="telemetry")


def record_spend(
    usd: float,
    *,
    model: str,
    kind: str = "actual",
    detail: Mapping[str, Any] | None = None,
    usage_measured: bool | None = None,
    transaction_id: str | None = None,
) -> bool:
    """Append one spend row and report whether the durable append succeeded.

    Returns ``True`` only after the primary cross-model spend ledger has
    durably accepted the row.  Returns ``False`` on any failure and never
    raises into the SI cycle.  Callers must supply explicit measurement
    evidence and must inspect the acknowledgement whenever subsequent
    behaviour depends on the durable accounting result.

    ``kind`` is ``"reserve"`` for the pre-dispatch estimate and ``"reconcile"``
    for the correction applied afterwards (which may be negative when the real
    cost came in under the estimate). Both are summed, so reserve+reconcile
    nets to the measured cost.

    V2 requires explicit measurement state and transaction identity.  Reserve
    rows may only be created by :func:`reserve_if_capacity`; reconcile rows
    must reuse the acknowledged reserve transaction ID.  The same sidecar lock
    serializes state validation and append, so concurrent duplicate reconciles
    cannot both pass a stale check.
    """
    if type(kind) is not str or kind == "reserve":
        return False
    _path: Path
    row: dict[str, Any]
    try:
        _path = _resolve_ledger_path()
        row = _build_spend_row(
            usd,
            model=model,
            kind=kind,
            detail=detail,
            usage_measured=usage_measured,
            transaction_id=transaction_id,
        )

        # Keep this direct call in the canonical writer: generated contracts
        # bind ``record_spend→join_keys`` and the episode tee must receive the
        # same already-joined row.  Join failure remains non-fatal.
        try:
            from agi_v8_1.runtime.ledger_join import join_keys

            joined = join_keys()
            validated_joined = _validated_join_evidence(joined)
            row.update(validated_joined)
        except Exception as join_exc:  # noqa: BLE001
            # Join evidence is optional; even strict mode may not erase an
            # already-authorized spend transaction.  Record the telemetry
            # fault and continue with the protected core row unchanged.
            record_critical_failure(
                join_exc,
                site="verifier.cross_model_budget.record_spend:join_keys",
                category="telemetry",
            )

        def _commit(
            append_locked: Callable[[Mapping[str, Any]], None],
        ) -> None:
            existing = _read_ledger_rows(_path)
            # A reconcile may not append behind a malformed/downgraded prefix
            # just because its own transaction shape is valid.
            _ledger_total([*existing, row])
            _append_primary(append_locked, row)

        with _reserve_lock(_path) as append_locked:
            _commit(append_locked)
    except Exception as exc:  # noqa: BLE001 — a ledger fault must not abort a cycle
        # Spend recorders are transaction boundaries: even strict fail-fast
        # must not replace the promised boolean acknowledgement with a raise.
        record_critical_failure(
            exc,
            site="verifier.cross_model_budget.record_spend:151",
            category="verify",
        )
        logger.error(
            "cross_model budget: failed to record transaction: %s",
            format_exception_for_log(exc),
        )
        return False

    # The primary ledger is the budget authority.  Its append has committed at
    # this point, so an optional episode-log tee failure must not turn the
    # acknowledgement back into ``False`` and cause a caller to mistake a
    # durable reservation for an absent one.  ``_tee_episode`` owns its own
    # non-fatal error boundary; this guard preserves that contract if it is
    # replaced by a faulty test/plugin implementation.
    try:
        _tee_episode(row, _path)
    except Exception as exc:  # noqa: BLE001 — primary spend row is already durable
        record_critical_failure(
            exc,
            site="verifier.cross_model_budget.record_spend:episode_tee",
            category="telemetry",
        )
    return True


def estimate_call_usd(*, model: str, input_tokens: int, output_tokens: int) -> float:
    """Price a call from the canonical pricing catalog.

    Raises ``KeyError`` for an unpriced model — a cap cannot be enforced against
    an unknown price, so callers must treat that as "do not dispatch".
    """
    if "/" in model:
        # OpenRouter 관례(provider/model). ⚠️ 미등재 폴백은 단가 inf 인데
        # `0 * inf == nan` 이고 `nan > cap` 은 항상 False 라 fail-closed 가
        # 뒤집힌다(08-22 적대검증) — inf 산술에 기대지 않고 명시 KeyError.
        from agi_v8_1.providers.pricing import (
            OPENROUTER_KNOWN_MODELS,
            get_openrouter_pricing,
        )

        _key = model.strip().lower()
        if _key not in OPENROUTER_KNOWN_MODELS:
            raise KeyError(f"unpriced openrouter model: {model!r}")
        entry = get_openrouter_pricing(_key)
    elif model.startswith("claude-"):
        from agi_v8_1.providers.pricing import get_anthropic_pricing

        entry = get_anthropic_pricing(model)
    elif model.startswith("deepseek-"):
        from agi_v8_1.providers.pricing import get_deepseek_ceiling_pricing

        # A process env tier (especially manually selected off-peak) is not a
        # worst-case billing authority.  Actual and reserve both use the
        # per-axis catalog ceiling so a stale cheap tier can only overcharge
        # locally, never open headroom that the provider may exceed.
        entry = get_deepseek_ceiling_pricing()
    else:
        raise KeyError(f"unpriced cross-model provider model: {model!r}")
    return (
        max(0, int(input_tokens)) * float(entry["input_per_token_usd"])
        + max(0, int(output_tokens)) * float(entry["output_per_token_usd"])
    )


def call_cost_envelope(
    *,
    model: str,
    max_output_tokens: int | None = None,
    paid_attempts: int = 1,
) -> dict[str, Any]:
    """Build the immutable worst-case cost capability for one dispatch.

    The input side deliberately reserves the whole catalog context window.
    This is conservative but tokenizer-independent and includes system/schema
    framing that a prompt-only estimate misses.  Cross-model verification uses
    one paid attempt and no fallback; callers must pin their provider request
    to the returned output/attempt values before consuming the reservation.
    """
    if type(model) is not str or not model.strip():
        raise ValueError("invalid envelope model")
    if type(paid_attempts) is not int or paid_attempts != 1:
        raise ValueError("cross-model envelope requires exactly one paid attempt")

    normalized = model.strip()
    if "/" in normalized:
        from agi_v8_1.providers.pricing import (
            OPENROUTER_KNOWN_MODELS,
            get_openrouter_pricing,
        )

        if normalized.lower() not in OPENROUTER_KNOWN_MODELS:
            raise KeyError(f"unpriced openrouter model: {normalized!r}")
        entry = get_openrouter_pricing(normalized.lower())
        family = "openrouter"
    elif normalized.startswith("claude-"):
        from agi_v8_1.providers.pricing import get_anthropic_pricing

        entry = get_anthropic_pricing(normalized)
        family = "anthropic"
    elif normalized.startswith("deepseek-"):
        from agi_v8_1.providers.pricing import (
            get_deepseek_ceiling_pricing,
            get_deepseek_pricing,
        )

        shape = get_deepseek_pricing(model=normalized, tier="tou_peak")
        ceiling = get_deepseek_ceiling_pricing()
        entry = {
            **shape,
            "input_per_token_usd": ceiling["input_per_token_usd"],
            "output_per_token_usd": ceiling["output_per_token_usd"],
            "_last_verified_iso": (
                f"{shape.get('_last_verified_iso')}:per-axis-catalog-ceiling"
            ),
        }
        family = "deepseek"
    else:
        raise KeyError(f"unpriced cross-model provider model: {normalized!r}")

    context_tokens = entry.get("context_window_tokens")
    catalog_output = entry.get("max_output_tokens")
    if type(context_tokens) is not int or context_tokens <= 0:
        raise ValueError("pricing entry lacks a finite context window")
    if type(catalog_output) is not int or catalog_output <= 0:
        raise ValueError("pricing entry lacks a finite output ceiling")
    output_tokens = catalog_output if max_output_tokens is None else max_output_tokens
    if (
        type(output_tokens) is not int
        or output_tokens <= 0
        or output_tokens > catalog_output
    ):
        raise ValueError("wire output ceiling is outside the priced catalog envelope")
    revision = entry.get("_last_verified_iso")
    if type(revision) is not str or not revision.strip():
        raise ValueError("pricing entry has no verified revision")

    reserve_usd = quantize_usd_ceiling(
        estimate_call_usd(
            model=normalized,
            input_tokens=context_tokens,
            output_tokens=output_tokens,
        )
    )
    core: dict[str, Any] = {
        "provider": family,
        "model": normalized,
        "pricing_revision": revision,
        "max_input_tokens": context_tokens,
        "max_output_tokens": output_tokens,
        "paid_attempts": 1,
        "fallbacks": 0,
        "reserve_usd": reserve_usd,
    }
    encoded = json.dumps(
        core, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return {**core, "envelope_sha256": hashlib.sha256(encoded).hexdigest()}


def usage_to_usd(usage: Mapping[str, Any] | None, *, model: str) -> float:
    """Price a provider ``last_usage`` mapping.

    Absent or malformed *usage* → ``0.0`` (the caller's ``usage_is_measured``
    check is what distinguishes "genuinely $0" from "no evidence").
    An **unpriced model** raises :class:`KeyError` — see the slot below.

    __SLOT_UNPRICED_MODEL_NOT_ZERO_2026_08_29__ 종전에는 ``KeyError`` 도 함께
    삼키고 ``0.0`` 을 돌려줬다. 그래서 두 개의 전혀 다른 사실이 같은 값으로
    접혔다 — **"토큰 증거가 없다"** 와 **"이 모델의 단가를 모른다"**.
    앞엣것은 뒤의 ``usage_is_measured`` 가 잡아주지만, 뒤엣것은 아무도 안 잡고
    **정산이 $0 으로 기록**된다(과소계상 ⇒ 캡이 늦게 물어 잔액을 넘길 수 있다).

    ``estimate_call_usd`` 는 같은 상황에서 이미 ``KeyError`` 를 던지고
    (``tests/v8_1/test_cross_model_adversary_2026_07_25.py`` 가 그 의도를
    *"An unpriced model must fail loudly rather than price a call at $0"* 로
    고정한다), 이 함수만 그 계약에서 이탈해 있었다.

    ⚠️ 현재 유일한 프로덕션 소비자(``self_improvement_v8`` 의 reserve→reconcile)
    는 **앞단 reserve 가 같은 모델로 ``estimate_call_usd`` 를 먼저 부르고 실패 시
    콜 자체를 거부**하므로 이 ``KeyError`` 에 도달하지 않는다 — 즉 라이브 회귀는
    없다. 문제는 그 방어가 **호출자에만 있고 함수 자신에는 없었다**는 것이다.
    reserve 를 거치지 않는 새 소비자가 생기면 그 즉시 조용한 $0 이 된다.
    """
    if not isinstance(usage, Mapping):
        return 0.0
    try:
        return estimate_call_usd(
            model=model,
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
        )
    except KeyError:
        # 단가를 모르면 값을 만들지 않는다. ⛔ 여기서 0.0 을 돌려주면 그 0 이
        # 원장에 **측정된 사실처럼** 앉는다.
        raise
    except (TypeError, ValueError) as _ff_exc:
        # usage 필드 자체가 malformed — 이건 "증거 없음"이고 호출자의
        # ``usage_is_measured`` 가 잡는 축이다. 종전대로 0.0.
        _swallowed(_ff_exc, site="verifier.cross_model_budget.usage_to_usd:180", category="verify")
        return 0.0


def status() -> dict[str, Any]:
    """Snapshot for logs / the verify verdict."""
    cap = cap_usd()
    spent = spent_usd()
    blocker = accounting_admission_blocker()
    return {
        "cap_usd": round(cap, 6),
        "spent_usd": round(spent, 6),
        "remaining_usd": round(max(0.0, cap - spent), 6),
        "exhausted": (cap - spent) < MIN_HEADROOM_USD or blocker is not None,
        "accounting_admission_blocker": blocker,
        "ledger": str(_resolve_ledger_path()),
    }


__all__ = [
    "CAP_ENV",
    "LEDGER_ENV",
    "USAGE_MEASURED_STRICT_ENV",
    "DEFAULT_CAP_USD",
    "MIN_HEADROOM_USD",
    "cap_usd",
    "spent_usd",
    "remaining_usd",
    "exhausted",
    "reserve_if_capacity",
    "record_spend",
    "estimate_call_usd",
    "call_cost_envelope",
    "quantize_usd_ceiling",
    "usage_to_usd",
    "usage_is_measured",
    "usage_measured_strict_enabled",
    "resolve_ledger_path",
    "read_spend_rows",
    "transaction_charge_rows",
    "charge_attribution",
    "ATTRIBUTION_EXACT",
    "ATTRIBUTION_LEGACY_UPPER_BOUND",
    "accounting_admission_blocker",
    "status",
]
