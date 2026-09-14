"""agi_v8_1 R25 W4 enforcement layer.

Ported from v7.1/agent_system R12 enforcement (destructive_command_policy +
executor_command_policy + executor_integrity + safe_auto_apply +
auto_apply_audit_chain).

R18 의 축약 ``agi_v8_1.core.apply_chain`` 은 그대로 유지 (backward compat).
이 패키지의 ``apply_chain_full`` 은 그 superset (entry_hash + ts + file_lock
critical section).

R12 invariants preserved:
  - DENY_CLASSES tier (class-1-irreversible / class-2-network-exec /
    class-3-confirm)
  - NFKC casefold normalization (Unicode bypass resistance)
  - process-local single-use carve-out tokens (pid-keyed, no env serialization)
  - DENY_FLAGS: 17 entries (force, force-with-lease, force-if-includes,
    --no-verify, --no-gpg-sign, --hard, --mirror, --delete, etc.)
  - prev_hash chain + verify_chain Merkle walk

All env knobs default OFF (advisory layer; modules do not execute commands).
No subprocess / requests / urllib / socket / http at module top.
"""

from .destructive_command_policy import (
    DENY_CLASSES,
    DENY_FLAGS,
    check_deny,
    issue_carve_out_token,
)
from .executor_command_policy import (
    COMMAND_ALLOWLIST,
    COMMAND_TIMEOUT,
    COMMAND_MAX_OUTPUT,
    describe_allowed_commands,
    is_command_allowed,
    is_read_only_command,
)
from .executor_integrity import validate_builder_output
from .safe_auto_apply import (
    FileChange,
    ApplyResult,
    SafeAutoApply,
    PROTECTED_FILES,
    PROTECTED_DIRS,
)
from .apply_chain_full import (
    append as audit_append,
    verify_chain as audit_verify_chain,
    compute_entry_hash,
    GENESIS_PREV_HASH,
    SCHEMA_VERSION as APPLY_CHAIN_FULL_SCHEMA,
)
from .cost_limiter import (
    estimate_tokens,
    estimate_cost,
    CostBudgetExceeded,
    enforce_budget,
)

# __SLOT_FAIL_FAST_2026_07_25__ Swallowed failures route through one choke
# point: counted + named always, re-raised under AGI_V8_STRICT_FAIL_FAST.
from agi_v8_1.policy.fail_fast import swallowed as _swallowed

# __R25_5_SLOT__ — full acceptance / judge ports (superset of R20 W1 short
# versions in agi_v8_1/core/acceptance_gate.py + judge.py). Suffix `_full.py`
# avoids name collision with W25's enforcement files.
# Imports wrapped in try/except so R25 (this slot) is independently importable
# even before R25.5 lands its files; once they exist the symbols re-export
# automatically.
try:
    from .acceptance_full import (
        AcceptanceJudge as AcceptanceJudgeFull,
        AcceptanceJudgement as AcceptanceJudgementFull,
        RecoveryAction,
        RecoveryDecision,
        RecoveryPolicy,
        judge_acceptance_full,
    )
    from .judge_full import (
        JUDGE_DECISIONS_FULL,
        JudgeOutcome as JudgeOutcomeFull,
        arbitrate,
        judge_cycle_full,
    )
except ImportError:  # pragma: no cover — R25.5 not yet shipped
    AcceptanceJudgeFull = None  # type: ignore[assignment,misc]
    AcceptanceJudgementFull = None  # type: ignore[assignment,misc]
    RecoveryAction = None  # type: ignore[assignment,misc]
    RecoveryDecision = None  # type: ignore[assignment,misc]
    RecoveryPolicy = None  # type: ignore[assignment,misc]
    judge_acceptance_full = None  # type: ignore[assignment]
    JUDGE_DECISIONS_FULL = None  # type: ignore[assignment]
    JudgeOutcomeFull = None  # type: ignore[assignment,misc]
    arbitrate = None  # type: ignore[assignment]
    judge_cycle_full = None  # type: ignore[assignment]

__all__ = [
    # destructive policy
    "DENY_CLASSES",
    "DENY_FLAGS",
    "check_deny",
    "issue_carve_out_token",
    # command policy
    "COMMAND_ALLOWLIST",
    "COMMAND_TIMEOUT",
    "COMMAND_MAX_OUTPUT",
    "describe_allowed_commands",
    "is_command_allowed",
    "is_read_only_command",
    # integrity
    "validate_builder_output",
    # safe auto apply
    "FileChange",
    "ApplyResult",
    "SafeAutoApply",
    "PROTECTED_FILES",
    "PROTECTED_DIRS",
    # apply chain full
    "audit_append",
    "audit_verify_chain",
    "compute_entry_hash",
    "GENESIS_PREV_HASH",
    "APPLY_CHAIN_FULL_SCHEMA",
    # cost
    "estimate_tokens",
    "estimate_cost",
    "CostBudgetExceeded",
    "enforce_budget",
    # __R25_5_SLOT__ full acceptance + judge
    "AcceptanceJudgeFull",
    "AcceptanceJudgementFull",
    "RecoveryAction",
    "RecoveryDecision",
    "RecoveryPolicy",
    "judge_acceptance_full",
    "JUDGE_DECISIONS_FULL",
    "JudgeOutcomeFull",
    "arbitrate",
    "judge_cycle_full",
]
