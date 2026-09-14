# R25 W4 + Write-Step2 — safe_auto_apply (v8 revived from v7.1)
"""Bulletproof safety gate for v8 SI + compactor writes.
R25 W4 advisory (back-compat) + Write-Step2 (env-gated default OFF):
block-marker insert/replace, atomic write (.tmp+fsync+replace, O_NOFOLLOW),
.bak.<ts> w/ sha256 verify, audit JSONL, size cap, rate limit, rollback API.
Env: AGI_V8_SAFE_AUTO_APPLY_ENABLED / _WRITE_ENABLED ("true" only),
_MAX_WRITES_PER_CYCLE (int 5), AGI_V8_STATE_DIR."""

from __future__ import annotations

import ast
import errno
import hashlib
import logging
import os
import re
import stat as _stat
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

# __SLOT_FAIL_FAST_2026_07_25__ Swallowed failures route through one choke
# point: counted + named always, re-raised under AGI_V8_STRICT_FAIL_FAST.
from agi_v8_1.policy.fail_fast import (
    format_exception_for_log,
    format_text_for_sink,
    record_critical_failure as _record_critical_failure,
    swallowed as _swallowed,
)

# __SLOT_R10_RA_2026_08_17__ §12-b 배선: write-ahead 2단계 로깅 primitive.
# (트랙 RA) `begin_intent`/`commit`/`find_orphaned_intents` 는 apply_chain_full
# 모듈에 primitive 로만 있었고 이 모듈의 실제 mutation 지점(`_apply_change`,
# `apply_block`)에는 배선돼 있지 않았다 — 이 슬롯이 그 배선이다. 이 로그는
# `_audit_event`(si_audit, mutation **후** 기록)와는 **다른** 별도 원장이다:
# intent 는 mutation **직전**에 남고 commit 은 mutation 이 실제로 끝난 뒤에만
# 남는다. 자세한 한계는 `_two_phase_chain_enabled` 및 아래 wiring 지점의
# 주석 참조.
from agi_v8_1.enforcement.apply_chain_full import (  # __SLOT_R10_RA_2026_08_17__
    begin_intent as _chain_begin_intent,
    commit as _chain_commit,
    find_orphaned_intents as _chain_find_orphaned_intents,
)

logger = logging.getLogger(__name__)

MAX_BLOCK_BYTES = 8192
MAX_FILE_BYTES = 524288
DEFAULT_MAX_WRITES_PER_CYCLE = 5
BACKUP_RETENTION_DEFAULT = 10

_PROMPT_DIR = str(Path(__file__).resolve().parents[1] / "data" / "prompts")
PROMPT_ALLOWLIST: frozenset[str] = frozenset({f"{_PROMPT_DIR}/{n}" for n in (
    "architect.md", "continuation.md", "continuation.txt", "critic.md", "critic.txt",
    "digestive_agent.md", "digestive_agent.txt", "head_strategist.md", "head_strategist.txt",
    "prompt_engineer.md", "self_improvement.md", "self_improvement.txt", "support_advisor.md",
    "timesfm_skill.md", "worker_builder.md", "worker_builder.txt",
    "worker_tester.md", "worker_tester.txt",
)})

PROTECTED_FILES: frozenset[str] = frozenset({
    "agi_v8_1/enforcement/safe_auto_apply.py",
    "agi_v8_1/enforcement/destructive_command_policy.py",
    "agi_v8_1/enforcement/executor_command_policy.py",
    "agi_v8_1/enforcement/apply_chain_full.py",
    "agi_v8_1/core/apply_chain.py",
    ".env", "config.py",
    "state/goal_card.json", "state/memory_store.json", "state/state_store.json",
    ".gitignore",
})

PROTECTED_DIRS: frozenset[str] = frozenset({"runs/", "state/", "__pycache__/", ".git/"})

_MANAGED_SUMMARY = "# Managed by SafeAutoApply (v8 write-step2)"
_PID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")

# __SLOT_W_STAGE2_FUZZY_SYNTAX_2026_06_18__ Stage 2 (v7.1 richer-write lift).
# Two independent, default-OFF capabilities layered on the existing line-edit
# helpers + _apply_change. Both are byte-identical no-ops when their env gate is
# unset — the OFF path never imports difflib / never calls ast.parse.
#
#  (A) FUZZY_MATCH: a THIRD line-match tier (difflib similarity) below the
#      existing exact + whitespace-normalized tiers. v8.1 already had the
#      whitespace tier; v7.1's genuine extra was the difflib fallback
#      (utils.insert_line_flexible cutoff 0.6). Gated OFF by default because a
#      loose fuzzy match can silently edit the WRONG line — opt-in only.
#  (B) PY_SYNTAX_CHECK: pre-write ast.parse on create/edit of a *.py path; a
#      SyntaxError makes _apply_change return False so apply_session counts it a
#      failure (→ session revert of any sibling writes). Stops a malformed
#      proposal from landing as live code.
_FUZZY_MATCH_ENV = "AGI_V8_SAFE_AUTO_APPLY_FUZZY_MATCH"
_PY_SYNTAX_CHECK_ENV = "AGI_V8_SAFE_AUTO_APPLY_PY_SYNTAX_CHECK"
# Mirrors v7.1 utils.insert_line_flexible difflib cutoff; env-overridable.
_FUZZY_CUTOFF_DEFAULT = 0.6

# __SLOT_T3_PATH_CONTAINMENT_2026_08_08__ apply_block / revert_last picked the
# write target by LITERAL string membership only (`_is_allowlisted_abs`). Measured
# 2026-08-08: with an ANCESTOR dir symlinked, the literal matches, the final
# lstat is not a link, and the write lands OUTSIDE repo_root (revert_last then
# rewound that outside file). `apply_session`'s resolve-based checks exist but
# are never called on this path. Full writeup: docs/LESSONS.md L-E2.
#
# 🔴 ANCHOR PROVENANCE — re-measured 2026-08-08 after round 1:
#   `repo_root` is NOT a usable containment anchor. The one production
#   apply_block/revert_last caller (self_improvement_v8.py:~1952) passes
#   `repo_root=prompt_dir`, i.e. the target's OWN PARENT, so
#   `require_under(repo_root, prompt_dir/x)` is a TAUTOLOGY and an ancestor
#   symlink collapses root and target through the SAME link. Round 1 shipped
#   that check and the live-shape escape still measured success=True /
#   "WROTE OUTSIDE repo=True" with the gate ON — ON == OFF.
#   ⇒ The anchor must not be reachable from caller input: `containment_root`
#     defaults to `_package_root()` (this file's own location). Callers may
#     override it for embedding/tests, but the SI lane must never pass it.
#
# ON therefore layers FOUR checks (see `_containment_verdict`), all fail-closed,
# on top of ONE shared interpretation of the spelling:
#   (0) `_act_target` → `_expand_literal` (2026-08-08 round 3). Round 2 judged
#       the expanduser-ed path and acted on the verbatim one; the kernel does
#       not expand `~`, so the two surfaces named DIFFERENT files (measured:
#       verdict `''` for `~/agi_v8_1/data/prompts/critic.md`, write landed at
#       `<cwd>/~/agi_v8_1/...`). Judge the string you will actually open.
#   (1) normalization (expanduser+resolve) — on failure DENY with its own
#       reason; never mis-recorded as `not_in_allowlist` (the ledger must not
#       lie about why a write was refused);
#   (2) no symlink COMPONENT under the anchor, on the target *and* on every
#       allowlist entry — reuses `_path_has_symlink_component`, the part
#       `apply_session` already trusts. Without (2), resolved membership turns
#       the 18-file allowlist into a whole-root allowlist whenever an
#       allowlisted spelling is reached through a symlinked ancestor. That walk
#       is fail-CLOSED as of round 3: a non-existent component no longer ends
#       it, and `..` is followed lexically instead of being handed to the kernel;
#       ⚠️ B2: the clause that used to stand here ("`parent.mkdir(parents=True)`
#       creates exactly those components") no longer holds for `..` spellings —
#       `_safe_write_text` skips that mkdir (`__SLOT_B2_SAFE_AUTO_APPLY_2026_08_10__`).
#       The walk is fail-closed on its own; do not re-derive "the components
#       exist by the time we write" from here;
#   ⚠️ STILL OPEN (measured 2026-08-08, pre-existing, NOT closed here): (2) is
#       guarded by `target.is_relative_to(anchor)`, which is LEXICAL. A spelling
#       that sits outside the anchor but RESOLVES into it (`/tmp/gw -> <anchor>`,
#       target `/tmp/gw/prompts/p.md`) skips the symlink-component check
#       entirely and is admitted by (3)+(4). It lands on the allowlisted file
#       today, so it is not a data escape — but it reaches it through an
#       unvetted, repointable link, which is the TOCTOU surface (2) exists for.
#   (3) membership compared on RESOLVED paths — alone this only WIDENS the
#       allowlist, so it is never a fix by itself;
#   (4) `state.path_guard.require_under` against BOTH the independent anchor
#       and `repo_root`. The repo_root wall is kept (it is vacuous for the SI
#       lane but not for other callers) — ADD, never replace.
_PATH_CONTAINMENT_ENV = "AGI_V8_SAFE_AUTO_APPLY_PATH_CONTAINMENT"


def _path_containment_enabled() -> bool:
    """Default-OFF (strict ``true``/``1``). OFF → literal membership only,
    byte-identical to pre-gate behaviour (no path_guard import, same reasons)."""
    return os.environ.get(_PATH_CONTAINMENT_ENV, "").strip().lower() in ("true", "1")


def _package_root() -> Path:
    """The ``agi_v8_1`` package root — containment anchor of last resort.

    Derived from this module's own location (``enforcement/`` → package root),
    exactly like `sia.harness_safety._package_root`. Deliberately NOT from
    ``os.getcwd``, an env var or any caller argument: an anchor an attacker (or
    a careless call site) can move is not an anchor. Worktree-safe because the
    module actually executing is the one that names the root.
    """
    return Path(__file__).resolve().parent.parent


def _safe_resolve(path: Path | str) -> Optional[Path]:
    """``expanduser`` + ``resolve``, or ``None`` when the spelling cannot be
    normalized — callers MUST treat ``None`` as deny (fail-closed).

    Measured 2026-08-08: ``Path("~nosuchuser/x").expanduser()`` raises
    ``RuntimeError`` ("Could not determine home directory"), which an
    ``except OSError`` misses. Round 1 caught only ``OSError``, so with the
    gate ON a single poisoned allowlist entry turned a *deny* into an
    uncaught exception escaping `apply_block` — no si_audit row at all, and
    even legitimate writes died. Hence the widened tuple here, in ONE place.
    """
    try:
        return Path(path).expanduser().resolve()
    except (OSError, RuntimeError, ValueError) as _ff_exc:
        _swallowed(_ff_exc, site="enforcement.safe_auto_apply._safe_resolve", category="apply")
        return None


# __SLOT_T3_PATH_CONTAINMENT_2026_08_08__ residual ① — ONE interpretation point.
def _expand_literal(path: Path | str) -> Optional[Path]:
    """``expanduser`` ONLY — the spelling the FILESYSTEM will actually walk.

    Round 2 judged the ``expanduser``-ed path and then acted on the verbatim
    one. The kernel does not expand ``~``, so the two named different files.
    Measured 2026-08-08 with the gate ON:
    ``_containment_verdict(Path("~/agi_v8_1/data/prompts/critic.md"))`` → ``''``
    (ALLOW, because ``resolve()`` expanded it onto an allowlist entry) while
    `apply_block` wrote to ``<cwd>/~/agi_v8_1/data/prompts/critic.md`` — a path
    nobody allowlisted, outside both the anchor and ``repo_root``.

    Same disease as the tick_auth RED① of the same day (authorization surface ≠
    decision surface) and the same cure: put the interpretation in ONE function
    and make the verdict AND every FS op take their path from it.

    ⛔ Deliberately NOT ``resolve()``. Resolving collapses a FINAL-component
    symlink, which would kill `_safe_write_text`'s ``O_NOFOLLOW``/lstat refusal
    and `revert_last`'s preflight — the defenses `_containment_verdict`'s
    docstring already warns must not be displaced (pinned by
    ``test_on_still_refuses_final_component_symlink``). ``expanduser`` touches
    no symlink and reads no directory: it is pure spelling.

    Idempotent and byte-identical for a ``~``-free spelling: ``Path.expanduser``
    returns ``self`` when the first component does not start with ``~``.
    """
    try:
        return Path(path).expanduser()
    except (OSError, RuntimeError, ValueError) as _ff_exc:
        _swallowed(_ff_exc, site="enforcement.safe_auto_apply._expand_literal", category="apply")
        return None


def _fuzzy_match_enabled() -> bool:
    """Default-OFF (strict ``true``/``1``). OFF → no difflib tier, byte-identical."""
    return os.environ.get(_FUZZY_MATCH_ENV, "").strip().lower() in ("true", "1")


def _fuzzy_cutoff() -> float:
    raw = os.environ.get("AGI_V8_SAFE_AUTO_APPLY_FUZZY_CUTOFF", "").strip()
    try:
        v = float(raw) if raw else _FUZZY_CUTOFF_DEFAULT
    except ValueError as _ff_exc:
        _swallowed(_ff_exc, site="enforcement.safe_auto_apply._fuzzy_cutoff:85", category="apply")
        return _FUZZY_CUTOFF_DEFAULT
    # clamp to a sane band: never below 0.5 (too loose → wrong-line edits)
    return min(1.0, max(0.5, v))


def _py_syntax_check_enabled() -> bool:
    """Default-OFF (strict ``true``/``1``). OFF → no ast.parse, byte-identical."""
    return os.environ.get(_PY_SYNTAX_CHECK_ENV, "").strip().lower() in ("true", "1")


# __SLOT_SI_AUDIT_APPLY_SESSION_2026_08_10__ 실트리 승격 레인의 원장.
# 🔴 실측 2026-08-10(적대검증): `si_lanes/promote.py` 는 `SafeAutoApply.apply_session`
#    만 부르는데 `_audit_event` 호출부는 `apply_block`/`revert_last` 둘뿐이었다 ⇒
#    **실트리에 닿는 유일한 레인이 si_audit 에 한 줄도 안 났다**. 같은 파일의
#    `_audit_event` 주석이 이 원장을 "무엇이 실트리에 닿았나" 로 규정하는데도.
#    `tools/ledger_join_check.py` 는 `si_audit/*.jsonl` 의 writer 를 `_audit_event`
#    로 등재해 뒀지만 승격 레인은 그 writer 를 지나지 않았다.
# ⚠️ 라이브 쓰기 표면이라 default-OFF. OFF = `_audit_event` 호출 자체가 없다
#    (원장 파일도 디렉터리도 안 생긴다) = byte-identical.
_SI_AUDIT_APPLY_SESSION_ENV = "AGI_V8_SI_AUDIT_APPLY_SESSION"


def _si_audit_apply_session_enabled() -> bool:
    """Default-OFF (strict ``true``/``1``)."""
    return os.environ.get(_SI_AUDIT_APPLY_SESSION_ENV, "").strip().lower() in ("true", "1")


# __SLOT_SI_AUDIT_LOG_DIR_PIN_2026_08_10__ R3 적대검증 실측: 한 인스턴스가
# si_audit 를 **두 디렉터리로 쪼갰다** — `apply_session` 은 젤로,
# `apply_block`/`revert_last` 는 판독기가 안 보고 `.gitignore` 에도 없는
# `<repo_root>/state/si_audit/` 로. 원장 위치를 메서드가 정하면 조인은 메서드
# 축으로 깨진다. ⇒ 세 경로가 `_ledger_root()` **하나**를 탄다(위치 SSOT).
# ⚠️ 라이브 착지를 바꾸므로 default-OFF: OFF = `_audit_log_path` 종전 폴백
#    (`repo_root/state`) 그대로 = byte-identical. ON = 판독기 기본 젤
#    (`tools/ledger_join_check.py:DEFAULT_JAIL` = `<repo>/state/si_jail`).
_SI_AUDIT_LOG_DIR_PIN_ENV = "AGI_V8_SI_AUDIT_LOG_DIR_PIN"


def _si_audit_log_dir_pin_enabled() -> bool:
    """Default-OFF (strict ``true``/``1``)."""
    return os.environ.get(_SI_AUDIT_LOG_DIR_PIN_ENV, "").strip().lower() in ("true", "1")


# __SLOT_R5_A_SESSION_HARDENING_2026_08_17__ Round-5 track-A security fixes for
# apply_session: incomplete batch rollback (§8), protected-path namespace
# mismatch (§9), and unenforced max_writes_per_cycle in the session path
# (§11), plus mode-loss on rewrite (§13). Unlike this module's other gates,
# this is a SECURITY FIX, not a new capability, so it defaults ON per repo
# policy ("이 판은 보안 결함 수리라 기본 ON"). Flip OFF (env "false"/"0") to
# recover the exact pre-fix byte-identical behaviour if it regresses a live
# caller — every branch behind this gate has a documented pre-fix path taken
# when OFF. Does NOT touch apply_block/revert_last (already correct per the
# tower's §11 measurement) or PROTECTED_FILES membership for existing
# entries (only additive).
_SESSION_HARDENING_ENV = "AGI_V8_SAFE_AUTO_APPLY_SESSION_HARDENING"


def _session_hardening_enabled() -> bool:
    """Default-ON security fix (§8/§9/§11/§13). OFF only via explicit
    ``"false"``/``"0"`` — any other value (including unset) stays ON."""
    return os.environ.get(_SESSION_HARDENING_ENV, "").strip().lower() not in ("false", "0")


# __SLOT_R5_A_WRITE_RECHECK_2026_08_17__ (§10) `_apply_change` only ever reads
# `self.dry_run`; `write_enabled()` (the two-env capability gate) is never
# consulted inside apply_session, so `SafeAutoApply(dry_run=False)` built
# without the two envs set still writes to disk (the module's own R2 F3
# comment on `_audit_session` already documents this asymmetry). Default OFF
# — unlike the other §8/§9/§11/§13 fixes above, flipping this ON by default
# regresses a WIDE swath of existing tests that intentionally construct
# `SafeAutoApply(..., dry_run=False)` directly without setting either env
# (test_r3_apply_ladder.py, test_safe_auto_apply_r2/r3/r4_nails, etc. — see
# report). Every production caller (self_improvement_v8.py, si_lanes/promote.py)
# already translates `dry_run=not write_enabled()` correctly, so the live
# exploit surface is a caller BUG class, not a currently-exploited path; this
# is defense-in-depth an operator can opt into once those tests are updated.
_SESSION_WRITE_RECHECK_ENV = "AGI_V8_SAFE_AUTO_APPLY_SESSION_WRITE_RECHECK"


def _session_write_recheck_enabled() -> bool:
    """Default-OFF (strict ``true``/``1``) — see module comment above."""
    return os.environ.get(_SESSION_WRITE_RECHECK_ENV, "").strip().lower() in ("true", "1")


# __SLOT_R10_RA_2026_08_17__ (트랙 RA) apply_chain_full 의 write-ahead
# primitive(`begin_intent`/`commit`)를 이 모듈의 **실제** mutation 지점에
# 배선하는 게이트. 지금까지 `enforcement/apply_chain_full.py` 는 그 primitive
# 를 정의만 했고 production 호출자가 0건이었다(같은 파일 §12-b 주석, 테스트
# 만 호출) — 그래서 "mutation 후 · 기록 전 SIGKILL" 이면 그 mutation 은 어느
# 원장에도 흔적이 없었다. 이 게이트가 ON 이면:
#   1) 실제 쓰기(`_safe_write_text`/`unlink`/`flexible_*_line`) **직전**에
#      `begin_intent` 로 "이 mutation 을 시도한다"를 먼저 남긴다,
#   2) 쓰기가 예외 없이 끝난 뒤에만 `commit` 으로 짝을 맞춘다 — 쓰기 도중
#      예외가 나면 commit 은 절대 불리지 않으므로 그 intent 는 **고아로
#      남는다**(의도적: "완료했다"고 거짓으로 남기지 않는다).
# 이 원장은 `_audit_event`(si_audit)과 **별개**다 — si_audit 은 mutation
# 완료 후 결과를 기록하는 기존 관례(§12-b 가 고치려던 바로 그 gap)이고, 이
# 게이트가 붙이는 건 mutation 시도 자체의 write-ahead 흔적이다. 보안 결함
# 수리라 이 모듈의 다른 R5 하드닝 게이트와 같은 관례로 **기본 ON**
# ("false"/"0" 만 명시적으로 OFF; 그 외 미설정 포함 전부 ON). OFF ⇒
# `begin_intent`/`commit` 호출 자체가 없다 = byte-identical(체인 파일도 안
# 생긴다).
#
# 🔑 정직성 한계(§12-c, 안 고친 것): 이 두-단계 기록은 파일을 쓰는 주체가
# 정직하다는 전제 위에서만 "부분 비일관성"을 잡는다. 체인 파일 전체에 쓰기
# 권한이 있는 주체는 여전히 intent/commit 행을 포함해 전체 이력을
# 재계산해서 다시 쓸 수 있다 — 이 배선은 그 위협을 막지 않는다. 외부
# anchor(서명/notary)는 이 트랙 범위 밖이다.
_TWO_PHASE_CHAIN_ENV = "AGI_V8_APPLY_CHAIN_TWO_PHASE"


def _two_phase_chain_enabled() -> bool:
    """Default-ON security fix. OFF only via explicit ``"false"``/``"0"``."""
    return os.environ.get(_TWO_PHASE_CHAIN_ENV, "").strip().lower() not in ("false", "0")


# __SLOT_R5_A_PROTECTED_NAMESPACE_2026_08_17__ (§9) Additive-only: paths the
# tower's audit measured as reachable-and-unprotected via the ONE live
# apply_session caller (si_lanes/promote.py, repo_root=git_root, so the
# canonical rel path carries NO "agi_v8_1/" prefix). Never remove existing
# PROTECTED_FILES entries — some caller shapes (e.g. repo_root=data/prompts'
# grandparent) DO produce the "agi_v8_1/..." spelling, so both namespaces
# must keep matching. Only consulted when the hardening gate is ON (see
# _protected_reason); OFF keeps PROTECTED_FILES matching byte-identical.
_ADDITIONAL_PROTECTED_FILES: frozenset[str] = frozenset({
    "si_lanes/verify_gate.py",
    "si_lanes/verify_isolation.py",
    "si_lanes/promote.py",
    "enforcement/command_executor.py",
    "runtime/halt_sentinel.py",
    "runtime/daily_cost_cap.py",
})


@dataclass(slots=True)
class FileChange:
    path: str
    action: str
    content: str = ""
    old_content: str = ""
    target_line: str = ""
    replacement: str = ""
    proposal_id: str = ""
    # __SLOT_EXECUTOR_LINE_EDIT_2026_06_13__ insert_line direction relative to
    # the flex-matched anchor ("after"/"before"). Default keeps every existing
    # FileChange byte-identical. Ignored by all non-insert_line actions.
    position: str = "after"
    # Trusted verify→apply CAS. Empty preserves every historical caller.  When
    # set, only a full-file ``edit`` whose current regular-file bytes match this
    # SHA-256 may reach the mutation branches.
    expected_preimage_sha256: str = ""


@dataclass(slots=True)
class ApplyResult:
    success: bool = False
    reverted: bool = False
    changes_applied: int = 0
    test_passed: bool = False
    test_output: str = ""
    commit_sha: str = ""
    post_commit_sha: str = ""
    errors: list[str] = field(default_factory=list)
    decision_source: str = ""
    verifier_id: str = ""
    pre_apply_hash: str = ""
    post_apply_hash: str = ""
    rollback_latency_ms: float = 0.0
    # __SLOT_R5_A_SESSION_HARDENING_2026_08_17__ (§8) `reverted` only ever
    # meant "a revert loop ran" — it stays True even when the loop failed to
    # restore everything (partial revert) or when a `create` action left a
    # residual file the loop never even attempted to remove (no backup was
    # ever registered for creates pre-fix). These three fields let a caller
    # ask the three DIFFERENT questions "did we try", "did it fully work",
    # and "what's left dirty" instead of overloading one bool. Populated only
    # when `_session_hardening_enabled()` (gate OFF → stay at defaults,
    # `reverted` keeps its pre-fix meaning unchanged).
    rollback_attempted: bool = False
    rollback_complete: bool = False
    rollback_errors: list[str] = field(default_factory=list)
    residual_paths: list[str] = field(default_factory=list)
    # __SLOT_R10_RA_REPAIR_2026_08_17__ write-ahead two-phase ledger
    # (`_two_phase_txn`) failures land HERE, never in `errors` — `errors`
    # gates `result.success` (see `apply_session`'s
    # ``applied == len(changes) and not result.errors``) and a ledger-only
    # fault must never flip a REAL mutation to "failed" (that would be the
    # "mutation landed, ledger says refuse" split this repair closes, see
    # this module's `_two_phase_txn` docstring). A caller that wants to know
    # "did the write-ahead audit trail work" reads this field explicitly;
    # `success`/`changes_applied` answer "did the mutation happen" alone,
    # same separation of concerns as the three rollback fields above.
    ledger_errors: list[str] = field(default_factory=list)


# R2 잔여 ⑥(F6) — `.bak` 은 방어를 안 탔다. 읽는 쪽의 **집행**이 여기다.
def _read_bytes_nofollow(file_path: Path) -> bytes:
    """``O_NOFOLLOW`` read — 최종 컴포넌트가 심링크면 링크 목적지의 바이트를
    돌려주는 대신 ``OSError(ELOOP)`` 로 끊는다.

    ``is_symlink()`` preflight 는 TOCTOU 를 못 막는다 — preflight 는 원장에
    **이유를 적기 위한** 것이고, 집행은 커널 플래그다(이 파일의 `_safe_write_text`
    가 쓰기 쪽에서 이미 같은 형태를 취한다).
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(file_path), flags)
    try:
        chunks: list[bytes] = []
        while True:
            b = os.read(fd, 1 << 20)
            if not b:
                return b"".join(chunks)
            chunks.append(b)
    finally:
        os.close(fd)


def _safe_write_text(file_path: Path, text: str, encoding: str = "utf-8") -> None:
    """Atomic write: tmp + fsync + replace. O_NOFOLLOW + lstat symlink refusal."""
    file_path = Path(file_path); parent = file_path.parent
    # __SLOT_B2_SAFE_AUTO_APPLY_2026_08_10__ 유령 디렉터리 — mkdir **범위**를 좁힌다.
    # `parents=True` 는 철자의 어휘적 조상을 전부 만들어, `..` 가 든 철자에서 판정이
    # 한 번도 안 본 컴포넌트를 남겼다(`<a>/g1/g2/../../p/ghost/../x.md` → `g1`·`g2`·
    # `p/ghost` 생성 후에야 커널이 `..` 적용). ⛔ 처방으로 `os.path.normpath` 는 영구
    # 금지 — 어휘적 접기는 커널과 의미가 달라(심링크를 따라간 **뒤** `..` 적용) 판정을
    # deny→allow 로 뒤집는다: `__SLOT_T3_NORMPATH_RETRACTED_2026_08_10__`. 철자가 아니라
    # 부작용만 없앤다. 술어는 `parent` 가 아니라 **`file_path` 전체 철자**다 —
    # `parent.parts` 만 보면 후행 `..`(`<d>/ghost/..`)이 빠져나가 `ghost` 를 만들었다.
    # 🔑 false-deny 0(컴포넌트가 다 있으면 mkdir 자체가 불필요). ⚠️ 단 **새 거절 가족**
    # 하나 — `..` 철자인데 해석된 부모가 없으면(`<r>/a/../b/x.md`, `b` 부재) 종전엔
    # mkdir 이 만들어 줬지만 이제 `os.open` 이 ENOENT(fail-closed → write_failed);
    # 원장에는 정책 deny 가 아니라 `status='error'` 로 남는다(어휘 변경=타워 몫).
    # ⚠️ 경로 봉쇄 게이트를 같이 탄다 ⇒ **OFF 기본 형상에서는 유령이 그대로 생기고**
    # 그 계약이 `test_off_ghost_traversal_writes_through` 로 이미 박제돼 있다.
    # 🔑 다만 그 문장은 **재는 것보다 넓다**(R7): OFF 의 `_containment_verdict` 는 축자
    # 일치라 `..` 철자는 대개 먼저 `not_in_allowlist` 다(라이브 allowlist 18개에 `..` 0회).
    if not (_path_containment_enabled() and ".." in file_path.parts):
        parent.mkdir(parents=True, exist_ok=True)
    try: st = os.lstat(str(file_path))
    except FileNotFoundError:
        st = None
    if st is not None and _stat.S_ISLNK(st.st_mode):
        raise ValueError(f"refusing symlink write target: {file_path}")
    # __SLOT_R5_A_SESSION_HARDENING_2026_08_17__ (§13) `_open_tmp` below always
    # creates the tmp file `0o600` (deliberately restrictive — it may briefly
    # hold secret content before `os.replace`), then `os.replace` carries that
    # mode onto `file_path`. For a REWRITE of an existing file (edit/replace_line
    # /insert/delete_line), that silently drops the original mode — e.g. an
    # executable `100755` script becomes `100600` and stops being executable.
    # Captured here (not after `_open_tmp`) because `st` is already an lstat of
    # the pre-write file; `None`/symlink → nothing to preserve (new file keeps
    # the `0o600` default, matching prior behaviour exactly).
    _orig_mode = _stat.S_IMODE(st.st_mode) if st is not None else None
    data = text.encode(encoding)
    tmp = parent / f".{file_path.name}.{os.getpid()}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"): flags |= os.O_NOFOLLOW

    def _open_tmp(extra: int) -> int:
        try:
            return os.open(str(tmp), flags | extra, 0o600)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, getattr(errno, "EMLINK", -1)):
                raise OSError(exc.errno, f"refusing symlink: {tmp}") from exc
            raise

    # __SLOT_B2_SAFE_AUTO_APPLY_2026_08_10__ 회수 **자격**. `tmp` 철자는 결정적이다
    # (`.{name}.{pid}.tmp`) ⇒ 남이 그 자리에 심어 둘 수 있다. 그러니 "지울 권리"는
    # **이번 호출이 그 파일을 만들었다**는 증명에서만 나온다. 증명은 세 겹이다 —
    # ① `lexists` 로 "그 자리는 비어 있었다", ② `O_EXCL` 로 "비어 있었다는 판단과
    # 생성 사이가 원자적이었다"(①만이면 TOCTOU), ③ `fstat(fd)` 로 잡은 (dev, ino)를
    # 회수 직전에 재대조. 셋 중 하나라도 못 세우면 자격 없음이다.
    # ⛔ 없으면 안 지운다 — 삭제는 이 함수에 HEAD 에 없던 프리미티브이고, 판단
    #    불능일 때의 fail-closed 는 "지우지 않는다" 쪽이다(잔해 < 남의 파일 삭제).
    # 🔑 false-deny 0: 이미 뭔가 앉아 있으면(재사용된 pid 의 잔해·심링크·남의 파일)
    #    `lexists` 가 먼저 보고 **종전 열기로** 간다 — 쓰기는 그대로 성공하고, 잃는
    #    건 그 형상의 회수 자격뿐이다. 심링크도 `lexists` 가 True 라 종전 경로를 타고
    #    `O_NOFOLLOW` 가 `ELOOP` 로 잡으므로 거절 어휘가 안 갈린다.
    # ⛔ `try/except FileExistsError` 로 폴백하지 않는다 — 그 핸들러는 침묵 래칫
    #    (`test_silent_swallows_do_not_creep_back`)에서 **새 침묵 삼킴**으로 세어지고
    #    (실측: 천장 36 → 38), `_swallowed` 로 라우팅하면 STRICT 형상에서 되던져져
    #    폴백 자체가 깨진다. ⇒ 분기로 쓴다. `lexists`→`O_EXCL` 사이에 남이 끼어들면
    #    `EEXIST` 가 그대로 올라간다(fail-closed, HEAD 는 조용히 재사용했다).
    # ⚠️ 잔여(정직하게, 실측): (dev, ino) 재대조는 **아이노드 재사용을 못 이긴다** —
    #    우리 tmp 를 `unlink` 하고 곧바로 같은 철자를 만들면 커널이 방금 놓아준
    #    아이노드를 돌려주는 일이 흔하다(이 트리 tmpfs 에서 재현). 그러면 재대조가
    #    통과하고 그 새 파일이 지워진다. 파이썬에 fd 로 지우는 `unlinkat` 이 없어
    #    경로로 지우는 어떤 구현도 이 창을 못 닫는다. 🔴 R9 타워 재정정: 위 "회수"
    #    문장도 과장이었다 — 이 못은 오직 **삭제**에 대해서만 자격을 가린다.
    #    그 자리(결정적 tmp 철자)에 이미 남의 파일·하드링크가 **있으면**(`lexists`
    #    True) `_open_tmp` 는 `O_EXCL` 없이 열어 그 내용을 truncate+overwrite 하고
    #    `os.replace` 가 그 아이노드에 payload 를 얹는다 — 이건 HEAD 와 바이트
    #    동일한 동작(회귀 아님, `test_safe_auto_apply_b2_nails_2026_08_10.py:318-320`
    #    이 정직하게 "이 못은 '안 지운다'만 약속한다"고 적어 둔 그대로다). 참인
    #    문장은 "우리가 만들지 않은 철자는 안 건드린다"가 아니라 **"우리가 만들지
    #    않은 파일은 안 지운다(단, 그 철자에 쓰기 자체는 막지 않는다 — 그건 이
    #    함수가 아니라 상위 호출자의 allowlist/PROTECTED_FILES 책임이다)"**.
    #    그 경계가 `test_inode_reuse_is_the_residual_this_check_cannot_win` 에 박혀
    #    있다. 더 좁히려면 회수 제거 또는 tmp 무작위 철자 — 둘 다 타워 몫.
    _tmp_ident: tuple[int, int] | None = None
    _tmp_created = _path_containment_enabled() and not os.path.lexists(str(tmp))
    fd = _open_tmp(os.O_EXCL if _tmp_created else 0)
    # __SLOT_R5_A_SESSION_HARDENING_2026_08_17__ (§13) restore the ORIGINAL
    # file's mode on the fd BEFORE `os.replace` swaps it in, so a rewrite never
    # silently downgrades e.g. `0o755` to the tmp default `0o600`. Gated: OFF
    # keeps every rewrite at the pre-fix `0o600`, byte-identical. `fchmod` on
    # the fd (not a path-based `chmod` after replace) avoids a second TOCTOU
    # window between the swap and the permission fix.
    if _session_hardening_enabled() and _orig_mode is not None:
        try:
            os.fchmod(fd, _orig_mode)
        except OSError as _ff_exc:
            _swallowed(_ff_exc, site="enforcement.safe_auto_apply._safe_write_text:fchmod",
                       category="apply")
    # __SLOT_B2_SAFE_AUTO_APPLY_2026_08_10__ 유령의 나머지 절반 — **payload 잔해**.
    # 좁힌 mkdir 은 디렉터리를 안 만들 뿐, 판정이 안 본 자리에 tmp 가 남는 가족은
    # 그대로였다(실측: 후행 `..` + 그 디렉터리 존재 ⇒ `<d>/.…{pid}.tmp` 에 payload
    # 전문 착지). ⇒ 실패하면 tmp 를 회수하고 **같은 예외를 그대로 올린다**(fail-closed).
    # 같은 게이트라 OFF 는 종전 그대로.
    # 🔴 R7 적대검증: 종전 회수는 `os.replace` **한 가족만** 걸었다 — `os.write`/
    #   `os.fsync`/`os.close` 실패는 게이트 ON 에서도 payload 전문을 남겼다.
    # 🔴 R8 타워 정정: 종전 주석은 "tmp 는 방금 이 함수가 **열었으므로** 남의 것이
    #   아니다"라고 적었다. **연 것은 만든 것이 아니다** — `O_CREAT` 는 `O_EXCL`
    #   없이는 기존 파일을 열 뿐이고, 그래서 그 문장은 비파괴성의 증명이 아니었다.
    #   증명은 위 `_tmp_ident` 가 한다. 아래는 그 자격을 **쓰기만** 한다.
    # ⛔ 핸들러는 **하나**다(R7 과 같은 수). 신원 확인과 삭제를 한 `try` 에 두는 이유:
    #    갈라 놓으면 `except OSError: return` 이 침묵 삼킴으로 세어져 래칫이 붉어진다.
    #    한 자리로 모으면 "신원을 못 읽었다"와 "지우다 실패했다"가 같은 라우팅을 탄다 —
    #    둘 다 결과는 같다(안 지워졌다). `return` 은 예외 없는 정상 판정 경로다.
    def _reclaim_tmp() -> None:
        if not _path_containment_enabled():
            return
        if _tmp_ident is None:
            return                                  # 우리가 만든 게 아니다 → 안 지운다
        try:
            _now = os.lstat(str(tmp))
            if (_now.st_dev, _now.st_ino) != _tmp_ident:
                return                              # 우리가 만든 그 파일이 아니다
            os.unlink(str(tmp))
        except OSError as _rm_exc:
            _swallowed(_rm_exc, site="enforcement.safe_auto_apply._safe_write_text:tmp_reclaim",
                       category="apply")

    try:
        try:
            # 회수 자격의 증거는 **커널이 준 fd** 에서 뽑는다(철자 재stat 아님).
            # 여기 둔 이유: `os.fstat` 이 죽어도 아래 `finally` 가 fd 를 닫고,
            # `_tmp_ident` 는 None 인 채로 남아 회수가 스스로 기권한다 = 핸들러 0.
            if _tmp_created:
                _st_tmp = os.fstat(fd)
                _tmp_ident = (_st_tmp.st_dev, _st_tmp.st_ino)
            view = memoryview(data)
            while view:
                n = os.write(fd, view)
                if n == 0: raise OSError("short write")
                view = view[n:]
            os.fsync(fd)
        finally:
            os.close(fd)
    except BaseException:
        _reclaim_tmp()
        raise
    try:
        os.replace(str(tmp), str(file_path))
    except OSError:
        _reclaim_tmp()
        raise
    try:
        dfd = os.open(str(parent), os.O_RDONLY)
        try: os.fsync(dfd)
        finally: os.close(dfd)
    except OSError as _ff_exc:
        _swallowed(_ff_exc, site="enforcement.safe_auto_apply._safe_write_text:160", category="apply")
        pass


def flexible_replace_line(file_path: Path, target: str, replacement: str,
                          encoding: str = "utf-8") -> bool:
    # Shares the exact→whitespace(→fuzzy, gated) tiers with insert/delete via
    # _flex_line_index. Behaviour is byte-identical to the prior inline two-tier
    # match when the Stage-2 fuzzy gate is OFF (the default).
    lines = file_path.read_text(encoding=encoding).splitlines()
    idx = _flex_line_index(lines, target)
    if idx == -1:
        return False
    lines[idx] = replacement
    _safe_write_text(file_path, "\n".join(lines) + ("\n" if lines else ""), encoding)
    return True


# __SLOT_EXECUTOR_LINE_EDIT_2026_06_13__ Flexible insert/delete line edits.
# Ported from v7.1 executor_helpers.flexible_insert_line, but routed through
# _safe_write_text (atomic + symlink-refusing) instead of raw write_text, so
# the apply ladder's backup/rollback/2-env gate envelopes them exactly like
# replace_line. Pure text — no subprocess, no command_policy.
def _flex_line_index(lines: list[str], target: str) -> int:
    """First index in *lines* matching *target* — exact rstrip match first,
    then whitespace-normalized, then (gated) difflib similarity. -1 if none.
    Shared by replace/insert/delete."""
    for i, line in enumerate(lines):
        if line.rstrip("\n") == target.rstrip("\n"):
            return i
    tn = re.sub(r"\s+", " ", target.strip())
    for i, line in enumerate(lines):
        if re.sub(r"\s+", " ", line.strip()) == tn:
            return i
    # __SLOT_W_STAGE2_FUZZY_SYNTAX_2026_06_18__ (A) third tier (v7.1 lift):
    # difflib similarity fallback. Default-OFF → this block is skipped and the
    # function returns -1 exactly as the pre-Stage-2 two-tier matcher did
    # (byte-identical). ON → a single close (>= cutoff) line is accepted; this
    # can edit a near-but-not-equal line, hence the deliberate opt-in.
    if _fuzzy_match_enabled():
        import difflib

        stripped = [ln.strip() for ln in lines]
        close = difflib.get_close_matches(
            target.strip(), stripped, n=1, cutoff=_fuzzy_cutoff()
        )
        if close:
            for i, s in enumerate(stripped):
                if s == close[0]:
                    return i
    return -1


def flexible_insert_line(file_path: Path, anchor: str, new_line: str,
                         position: str = "after", encoding: str = "utf-8") -> bool:
    """Insert *new_line* relative to a flexibly-matched *anchor* line.

    ``position`` "after"/"before" inserts adjacent to the anchor. An empty
    anchor appends at EOF (subsumes append_block). Returns False (no write) if
    a non-empty anchor is not found.
    """
    lines = file_path.read_text(encoding=encoding).splitlines()
    if anchor.strip() == "":
        lines.append(new_line)
    else:
        idx = _flex_line_index(lines, anchor)
        if idx == -1:
            return False
        insert_at = idx if position == "before" else idx + 1
        lines.insert(insert_at, new_line)
    _safe_write_text(file_path, "\n".join(lines) + ("\n" if lines else ""), encoding)
    return True


def flexible_delete_line(file_path: Path, target: str, encoding: str = "utf-8") -> bool:
    """Delete the first line flexibly matching *target*. False (no write) if not found."""
    lines = file_path.read_text(encoding=encoding).splitlines()
    idx = _flex_line_index(lines, target)
    if idx == -1:
        return False
    del lines[idx]
    _safe_write_text(file_path, "\n".join(lines) + ("\n" if lines else ""), encoding)
    return True


def _hash_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _hash_path(path: Path) -> str:
    if not path.exists() or not path.is_file():
        return ""
    return _hash_bytes(path.read_bytes())


def _build_block(proposal_id: str, payload: str) -> str:
    if not _PID_RE.fullmatch(proposal_id or ""):
        raise ValueError(f"invalid proposal_id: {proposal_id!r}")
    body = (payload or "").rstrip("\n")
    return (f"# [Auto-applied: {proposal_id}] start\n"
            f"{_MANAGED_SUMMARY}\n{body}\n"
            f"# [Auto-applied: {proposal_id}] end\n")


def _find_block_span(text: str, proposal_id: str) -> Optional[tuple[int, int]]:
    pid_esc = re.escape(proposal_id)
    sp = re.compile(rf"^# \[Auto-applied: {pid_esc}\] start\s*$", re.MULTILINE)
    ep = re.compile(rf"^# \[Auto-applied: {pid_esc}\] end\s*$", re.MULTILINE)
    s = sp.search(text)
    if not s:
        return None
    e = ep.search(text, s.end())
    if not e:
        raise ValueError(f"malformed block marker: start without end for {proposal_id}")
    end_off = e.end()
    if end_off < len(text) and text[end_off] == "\n":
        end_off += 1
    return (s.start(), end_off)


# __SLOT_SI_AUDIT_JAIL_ANCHOR_2026_08_09__ 젤은 `repo_root` 가 **아니다**.
# 🔴 실측 08-09: 프롬프트 진화 레인이 `repo_root=prompt_dir` 이라 이 폴백이
#    `data/prompts/state/si_audit/` 를 낳았다 — 젤 밖·untracked·미조인 12행
#    (08-08 라이브). 읽는 쪽(`tools/ledger_join_check.py:DEFAULT_JAIL`)은 젤을
#    못 박아 보므로 쓰기 표면과 판독 표면이 갈렸다.
# 🔑 `repo_root` 는 **쓰기 대상 격리 앵커**지 원장 위치가 아니다 — 이 파일이
#    `containment_root` 를 따로 뽑아낸 것과 같은 형태. 젤을 아는 호출자가 말한다.
# ⛔ env 우선은 유지(쓰기/판독이 같은 파일을 봐야 대조 성립 — 08-08 반증 완료).
# ⛔ 최종 폴백도 `repo_root/state` 유지: `_package_root()` 로 바꾸면 env 를 안 박은
#    테스트가 라이브 젤 옆에 쓴다. ⇒ `audit_root` 미전달 = HEAD 와 byte-identical.
def _audit_log_path(repo_root: Path, audit_root: Path | None = None) -> Path:
    state_dir = os.environ.get("AGI_V8_STATE_DIR", "").strip()
    if state_dir:
        base: Path = Path(state_dir)
    elif audit_root is not None:
        base = Path(audit_root)
    else:
        base = repo_root / "state"
    return base / "si_audit" / (datetime.now(timezone.utc).strftime("%Y%m%d") + ".jsonl")


def _audit_event(repo_root: Path, event: Dict[str, Any],
                 audit_root: Path | None = None) -> None:
    try:
        from agi_v8_1.state.store import atomic_append_jsonl
    except Exception as exc:  # pragma: no cover
        _swallowed(exc, site="enforcement.safe_auto_apply._audit_event:store_import",
                   category="persist")
        logger.warning("audit fallback: %s", format_exception_for_log(exc)); return
    payload = dict(event)
    payload.setdefault("ts_utc", datetime.now(timezone.utc).isoformat())
    # __SLOT_LEDGER_JOIN_2026_08_08__ apply 는 사이클 안에서 일어난다. 이 원장은
    # **무엇이 실트리에 닿았나**를 담으므로 사이클 귀속이 특히 값을 한다.
    # ⚠️ ``ts_utc`` 는 ISO **문자열**이라 문자열 정렬이 TZ 로 밀린다 —
    # 시각 축은 ``runtime.ledger_join.row_ts`` 가 별칭으로 해석한다(기존 필드 보존).
    try:
        from agi_v8_1.runtime.ledger_join import join_keys

        payload.update(join_keys())
    except Exception as _join_exc:  # noqa: BLE001
        _swallowed(_join_exc, site="enforcement.safe_auto_apply._audit_event:join_keys",
                   category="telemetry")
    try:
        atomic_append_jsonl(_audit_log_path(repo_root, audit_root), payload)
    except Exception as exc:  # noqa: BLE001
        _swallowed(exc, site="enforcement.safe_auto_apply._audit_event:294", category="apply")
        logger.warning("audit append failed: %s", format_exception_for_log(exc))
        return

    # __SLOT_PROVENANCE_MIRROR_V0_2026_08_18__ Off-jail hash-chained mirror of
    # THIS si_audit row, only after the primary jail write above succeeded (a
    # failed primary write is never mirrored — the mirror describes what
    # actually landed in the jail, not what was merely attempted). Gated by
    # ``AGI_V8_PROVENANCE_MIRROR_ENABLED`` (default OFF); ``mirror_event``
    # itself re-checks that gate and is fully non-fatal, so OFF is a zero-I/O
    # no-op and this call is byte-identical to not existing.
    # ⚠️ `_swallowed` here (not `record_critical_failure`) would rethrow
    # under AGI_V8_STRICT_FAIL_FAST — same trap as the `_fchmod` fallback a
    # few hundred lines up in this same file ("STRICT 형상에서 되던져져
    # 폴백 자체가 깨진다"). `_audit_event`'s own primary write above already
    # succeeded by the time this runs; a secondary mirror-only failure must
    # never surface here as if the primary audit write itself had failed.
    try:
        from agi_v8_1.enforcement.provenance_mirror import mirror_event

        mirror_event("safe_auto_apply_audit", "si_audit_row", payload)
    except Exception as exc:  # noqa: BLE001 — mirroring must never affect the primary audit write
        _record_critical_failure(exc, site="enforcement.safe_auto_apply._audit_event:mirror",
                                  category="persist")


# __SLOT_EXECUTOR_LOG_2026_06_14__ Shared executor-log emitter. ADDS a record
# to the unified executor trail; the existing _audit_event / si_audit trail is
# untouched (both fire at every site). Lazy import keeps zero import cost when
# logging is off, and is fully non-fatal (helper swallows its own errors; this
# wrapper guards the import + call). No file CONTENT is passed — only the
# structured action/path/sha/bytes summary the caller already computed.
def _executor_log(
    *,
    phase: str,
    status: str,
    output_summary: Dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    try:
        from agi_v8_1.enforcement.executor_log import log_executor_event
        log_executor_event(
            executor="safe_auto_apply",
            phase=phase,
            status=status,
            output_summary=output_summary or {},
            error=error,
        )
    except Exception as exc:  # noqa: BLE001 — logging must never crash a caller
        _swallowed(exc, site="enforcement.safe_auto_apply._executor_log:320", category="apply")
        logger.warning("executor_log emit failed: %s", format_exception_for_log(exc))


class SafeAutoApply:
    """Advisory + Write-Step2 safety gate. See module docstring."""

    def __init__(self, repo_root: Path | None = None,
                 protected_files: frozenset[str] | None = None,
                 protected_dirs: frozenset[str] | None = None,
                 dry_run: bool = True,
                 prompt_allowlist: frozenset[str] | None = None,
                 backup_retention: int = BACKUP_RETENTION_DEFAULT,
                 max_writes_per_cycle: Optional[int] = None,
                 containment_root: Path | None = None,
                 audit_root: Path | None = None) -> None:
        self.repo_root = (Path(repo_root).resolve() if repo_root is not None
                          else Path(os.getcwd()).resolve())
        # __SLOT_T3_PATH_CONTAINMENT_2026_08_08__ Independent containment anchor.
        # ⛔ The SI lane must NEVER pass this — `repo_root` there is the target's
        # own parent, which is exactly what made round 1's containment vacuous
        # (see the module comment). Kept as an explicit opt-in so an embedder /
        # an isolated harness can jail writes into ITS OWN tree; the default is
        # unreachable from caller input. Appended last so no positional call
        # site shifts.
        self.containment_root = (Path(containment_root).resolve()
                                 if containment_root is not None else _package_root())
        # __SLOT_SI_AUDIT_JAIL_ANCHOR_2026_08_09__ si_audit 원장이 떨어질 젤.
        # 젤을 아는 건 호출자(SI 레인의 `state_dir`)뿐 — `repo_root` 는 프롬프트
        # 레인에서 타깃의 부모다. None = 종전 해석, byte-identical.
        self.audit_root = (Path(audit_root).resolve()
                           if audit_root is not None else None)
        self.protected_files = protected_files or PROTECTED_FILES
        self.protected_dirs = protected_dirs or PROTECTED_DIRS
        self.dry_run = dry_run
        self.prompt_allowlist = prompt_allowlist or PROMPT_ALLOWLIST
        self.backup_retention = max(1, int(backup_retention))
        if max_writes_per_cycle is None:
            env_cap = os.environ.get("AGI_V8_SAFE_AUTO_APPLY_MAX_WRITES_PER_CYCLE", "").strip()
            try:
                max_writes_per_cycle = int(env_cap) if env_cap else DEFAULT_MAX_WRITES_PER_CYCLE
            except ValueError as _ff_exc:
                _swallowed(_ff_exc, site="enforcement.safe_auto_apply.__init__:345", category="apply")
                max_writes_per_cycle = DEFAULT_MAX_WRITES_PER_CYCLE
        self.max_writes_per_cycle = max(1, int(max_writes_per_cycle))
        self._writes_this_cycle = 0
        # __SLOT_W8_STEP3_APPLY_SESSION_BACKUP_2026_05_31__ (R3 session atomicity)
        # Per-session (target, bak) accumulator. apply_session resets this at
        # entry and, on PARTIAL failure, replays it in reverse to revert every
        # file already written this session — closing the "applied += 1 mid-loop
        # crash leaves a half-applied tree" gap (PART1 §2.3). Per-file backups
        # already exist via _disk_backup; this adds SESSION atomicity only.
        self._session_backups: list[tuple[Path, Path]] = []
        # __SLOT_R5_A_SESSION_HARDENING_2026_08_17__ (§8) `create` actions have
        # no pre-existing content to back up (nothing to restore TO), so they
        # were entirely absent from the session-rollback ledger — a partial
        # session failure left every newly-created file on disk even though
        # `result.reverted` reported True. Tracks (this session's) full paths
        # created via `_apply_change`'s "create" branch; `apply_session`
        # resets it at entry exactly like `_session_backups`. Reversed
        # unlink on partial failure closes the gap. Gated (see
        # `_session_hardening_enabled`): OFF → never appended, never
        # consulted, byte-identical.
        self._session_creates: list[Path] = []
        # __SLOT_BAK_PROVENANCE_2026_08_10__ (R3 F6 재반증) `.bak` **출처 원장**.
        # 이 인스턴스가 실제로 쓴 백업의 `절대경로 -> sha256` 만 들어간다.
        # `revert_last` 는 이 표에 없는 후보를 복원 소스로 삼지 않는다 — 링크
        # 종류를 묻는 대신 "우리가 만든 그 백업인가"를 묻는다(아래 참조).
        self._backup_sha: Dict[str, str] = {}
        # __SLOT_R10_RA_REPAIR_2026_08_17__ 이 인스턴스가 이번 호출(session 1건
        # 또는 block 1건) 동안 `_two_phase_txn` 에서 겪은 원장(ledger) 실패의
        # 이름 붙은 기록. `apply_session`/`apply_block` 이 반환 직전 이 리스트를
        # `result.ledger_errors` 로 복사한다 — "mutation은 됐고 원장만 실패했다"
        # 를 호출자가 `result.errors`(=mutation 실패)와 섞이지 않게 구분해
        # 받도록. `apply_session`/`apply_block` 시작부에서 매번 새로 비운다
        # (== `self._session_backups` 와 같은 인스턴스-재사용 관례).
        self._two_phase_ledger_errors: List[str] = []
        # __SLOT_R10_RA_2026_08_17__ (트랙 RA §3) "기동 시 조회" — 이 인스턴스가
        # 뜰 때(= 이 mutation 게이트가 기동할 때) 이전 프로세스가 남긴 고아
        # intent(= 기록됐는데 commit 안 된 것 = 크래시 중 mutation 의심)를
        # **조용히 지나치지 않고** 여기서 드러낸다. 진짜 OS 프로세스 기동
        # 훅(orchestrator/cli 진입점)은 이번 판 동시 진행 중인 다른 7트랙과
        # 충돌 위험이 커서 범위 밖으로 남긴다(리포트에 명시) — 대신 이
        # mutation 경로 자체의 "기동"에 건다. 조회 자체는 read-only
        # (`find_orphaned_intents` 는 파일을 절대 안 건드림) 이므로 실패해도
        # 쓰기를 막을 이유가 없다: 실패는 swallowed 로 넘기고 로그만 남긴다.
        self.orphaned_intents_at_boot: List[Dict[str, Any]] = []
        _boot_chain_path = self._two_phase_chain_path() if _two_phase_chain_enabled() else None
        if _boot_chain_path is not None:
            try:
                _orphans = _chain_find_orphaned_intents(_boot_chain_path)
                self.orphaned_intents_at_boot = _orphans
                if _orphans:
                    logger.warning(
                        "apply_chain_two_phase: %d orphaned intent(s) found at boot "
                        "(possible crash-during-mutation): %s",
                        len(_orphans),
                        [o.get("txn_id") for o in _orphans],
                    )
            except Exception as exc:  # noqa: BLE001 — 조회 실패가 게이트를 못 죽인다
                _swallowed(exc, site="enforcement.safe_auto_apply.__init__:orphan_boot_check",
                           category="apply")

    @staticmethod
    def write_enabled() -> bool:
        g = lambda k: os.environ.get(k, "").strip().lower()  # noqa: E731
        return g("AGI_V8_SAFE_AUTO_APPLY_ENABLED") == "true" and g("AGI_V8_SAFE_AUTO_APPLY_WRITE_ENABLED") == "true"

    def _protected_reason(self, rel_path: str) -> str:
        rel = rel_path.replace("\\", "/")
        while rel.startswith("./"):
            rel = rel[2:]
        # __SLOT_R5_A_PROTECTED_NAMESPACE_2026_08_17__ (§9) OFF path below is
        # UNCHANGED (single literal `rel`, original `self.protected_files`
        # only) — byte-identical when the hardening gate is OFF. ON: match
        # BOTH the "agi_v8_1/"-prefixed and unprefixed spelling of `rel`
        # (never removes either namespace — different callers legitimately
        # produce different prefixes; see module comment on
        # `_ADDITIONAL_PROTECTED_FILES`), and widen the file set additively.
        candidates = {rel}
        protected_files = self.protected_files
        if _session_hardening_enabled():
            protected_files = protected_files | _ADDITIONAL_PROTECTED_FILES
            if rel.startswith("agi_v8_1/"):
                candidates.add(rel[len("agi_v8_1/"):])
            else:
                candidates.add(f"agi_v8_1/{rel}")
        if candidates & protected_files:
            return f"Protected file: {rel_path}"
        for prefix in self.protected_dirs:
            clean = prefix.rstrip("/")
            if any(c == clean or c.startswith(prefix) for c in candidates):
                return f"Protected dir: {rel_path}"
        return ""

    def _path_has_symlink_component(self, raw_path: Path, *, root: Path | None = None) -> bool:
        # __SLOT_T3_PATH_CONTAINMENT_2026_08_08__ *root* defaults to `repo_root`,
        # so `apply_session`'s existing call is byte-identical. The containment
        # gate passes the independent anchor instead of re-implementing the walk.
        base = self.repo_root if root is None else root
        # R2 잔여 ⑤(F5) — 널바이트는 **판정 불가**다. `Path.is_symlink()` 가
        # `ValueError: lstat: embedded null character in path` 를 던지는데 이
        # 함수에도 호출부(:`_validate_changes` 의 `except OSError`)에도 핸들러가
        # 없어서, 쓰기 표면이 미포획 예외로 죽었다(실측 2026-08-10, HEAD 에서도
        # 동일: `path='\x00x.py'` · `'a\x00/b.py'` 둘 다 RAISE).
        # ⛔ 순수 fail-closed 수리, 게이트 불요: 커널이 받을 수 없는 철자라
        #    정당한 타깃이 될 수 없다(실측: allowlist 18개에 널바이트 0).
        if "\x00" in str(raw_path):
            return True
        try:
            rel = raw_path.relative_to(base)
        except ValueError as _ff_exc:
            # __SLOT_T3_PATH_CONTAINMENT_2026_08_08__ residual ③ (2026-08-10):
            # this handler used to `return False` — a security predicate answering
            # "no symlink here" for an input it cannot even walk. `relative_to`
            # failing means *this base cannot describe this path*, i.e. UNKNOWN,
            # and for a defence component unknown is a refusal. Measured
            # 2026-08-10: the branch is unreachable today (every caller runs
            # `_resolve_change_path` — which returns None for an absolute or
            # escaping spelling — or an `is_relative_to` guard first), so the flip
            # is inert live; it is the ORDER dependence that made fail-open a
            # hazard: one reordered call site and the default becomes live.
            # Pinned by test_symlink_walk_fails_closed_when_the_path_is_outside_its_base
            # + test_live_callers_never_reach_the_fail_closed_branch.
            _swallowed(_ff_exc, site="enforcement.safe_auto_apply._path_has_symlink_component:377", category="apply")
            return True
        cursor = base
        for part in rel.parts:
            # __SLOT_T3_PATH_CONTAINMENT_2026_08_08__ residual ② — fail-CLOSED walk.
            # (a) `..` used to be walked as a literal component, so `is_symlink`/
            #     `exists` asked the KERNEL (which resolves `..` against the real
            #     tree) about a cursor the walk no longer described. Every part
            #     before this point has already been proved a non-symlink, so the
            #     lexical parent IS the real parent — take it explicitly. A `..`
            #     at the anchor itself climbs OUT of the anchor: deny.
            if part == "..":
                if cursor == base:
                    return True
                cursor = cursor.parent
                continue
            cursor = cursor / part
            if cursor.is_symlink():
                return True
            # (b) the old `if not cursor.exists(): break` was the fail-OPEN.
            #     `Path.is_symlink()` is False for a path that does not exist, so
            #     one absent component waved the WHOLE tail through — and
            #     `_safe_write_text`'s `parent.mkdir(parents=True)` then creates
            #     that component, after which the kernel walks the tail for real.
            #     Measured 2026-08-08: `<root>/ghost/../link/p.md` → False while
            #     `<root>/link/p.md` → True, i.e. one non-existent component
            #     bought a free pass through a symlinked ancestor. Walking on is
            #     safe for the routine "new file" case: without `..` every
            #     remaining component is absent too, so `is_symlink` stays False
            #     and the loop simply falls through to `return False`.
            # ⚠️ B2: the "`parent.mkdir(parents=True)` then creates that
            #     component" clause above is the PRE-FIX hazard only — with the
            #     gate ON `_safe_write_text` no longer mkdirs for `..` spellings
            #     (`__SLOT_B2_SAFE_AUTO_APPLY_2026_08_10__`). (b) stays
            #     fail-closed on its own either way: two defences, not one.
        return False

    def _resolve_change_path(self, path: str) -> tuple[Path, str] | None:
        # R2 잔여 ⑤(F5) — 널바이트 = 커널이 못 받는 철자 = 거부(`Path escape`).
        # `_path_has_symlink_component` 의 같은 가드와 짝: 이쪽은 `_apply_change`
        # 를 포함한 **모든** 호출부를 덮는다.
        if "\x00" in path:
            return None
        if Path(path).is_absolute():
            return None
        full = (self.repo_root / path).resolve()
        try:
            rel = full.relative_to(self.repo_root)
        except ValueError as _ff_exc:
            _swallowed(_ff_exc, site="enforcement.safe_auto_apply._resolve_change_path:394", category="apply")
            return None
        return full, rel.as_posix()

    def _validate_changes(self, changes: List[FileChange]) -> List[str]:
        rejected: List[str] = []
        for change in changes:
            path = change.path
            if not path: rejected.append("Empty path in FileChange"); continue
            r = self._protected_reason(path)
            if r: rejected.append(r); continue
            try: resolved = self._resolve_change_path(path)
            except OSError as _ff_exc:
                _swallowed(_ff_exc, site="enforcement.safe_auto_apply._validate_changes:406", category="apply")
                resolved = None
            if resolved is None: rejected.append(f"Path escape: {path}"); continue
            _full, canonical_rel = resolved
            if self._path_has_symlink_component(self.repo_root / path):
                rejected.append(f"Symlink path: {path}"); continue
            r = self._protected_reason(canonical_rel)
            if r: rejected.append(r); continue
            if change.action in ("create", "edit") and not str(change.content).strip():
                rejected.append(f"Empty content: {path}"); continue
            if change.expected_preimage_sha256:
                expected = str(change.expected_preimage_sha256)
                if (
                    change.action != "edit"
                    or re.fullmatch(r"[0-9a-f]{64}", expected) is None
                ):
                    rejected.append(f"Invalid expected preimage: {path}"); continue
            # __SLOT_EXECUTOR_LINE_EDIT_2026_06_13__ insert needs a new line;
            # delete needs an anchor to match (empty anchor on insert = EOF append).
            if change.action == "insert_line" and not str(change.content).strip():
                rejected.append(f"Empty insert content: {path}"); continue
            if change.action == "delete_line" and not str(change.target_line).strip():
                rejected.append(f"Empty delete target: {path}"); continue
        return rejected

    def _disk_backup(self, target: Path, old_content: str) -> Path:
        # __SLOT_W8_STEP3_APPLY_SESSION_BACKUP_2026_05_31__
        # Atomic disk backup before destructive _apply_change ops (edit/delete/replace_line).
        # Closes Step2 Lens 1 Gap 1: in-memory old_content is lost if process crashes mid-write.
        bak = self._backup_path(target)
        _safe_write_text(bak, old_content)
        # Verify backup readable + content matches before allowing destructive op
        readback = bak.read_text(encoding="utf-8")
        if readback != old_content:
            try:
                bak.unlink()
            except OSError as _ff_exc:
                _swallowed(_ff_exc, site="enforcement.safe_auto_apply._disk_backup:434", category="apply")
                pass
            raise RuntimeError(f"disk_backup_verify_failed: {bak}")
        # __SLOT_BAK_PROVENANCE_2026_08_10__ 검증된 백업만 출처 원장에 오른다.
        self._backup_sha[str(bak)] = _hash_bytes(old_content.encode("utf-8"))
        # R3: record (target, bak) for SESSION-level rollback. The verified
        # backup holds the pre-change content; apply_session replays these in
        # reverse on partial failure. Recorded BEFORE _prune_backups so a
        # this-session backup is never pruned away before the session ends
        # (retention only removes OLDER siblings, but recording first keeps the
        # invariant explicit).
        self._session_backups.append((target, bak))
        self._prune_backups(target)
        return bak

    def _apply_change(self, change: FileChange) -> bool:
        if self.dry_run: return True
        resolved = self._resolve_change_path(change.path)
        if resolved is None: return False
        full, canonical_rel = resolved
        if self._path_has_symlink_component(self.repo_root / change.path): return False
        if self._protected_reason(canonical_rel): return False
        # __SLOT_VERIFY_APPLY_PREIMAGE_CAS_2026_09_01__ The completion gate
        # tests committed raw-HEAD bytes, while this method mutates the live
        # episode workspace.  A trusted expected digest closes that deterministic
        # split: dirty/concurrently replaced/wrong-type targets are refused
        # immediately before backup/write.  Empty keeps legacy callers inert.
        if change.expected_preimage_sha256:
            if change.action != "edit":
                return False
            try:
                metadata = os.lstat(str(full))
                current = _read_bytes_nofollow(full)
            except (OSError, ValueError) as exc:
                _swallowed(
                    exc,
                    site="enforcement.safe_auto_apply._apply_change:preimage",
                    category="apply",
                )
                return False
            if (
                not _stat.S_ISREG(metadata.st_mode)
                or hashlib.sha256(current).hexdigest()
                != change.expected_preimage_sha256
            ):
                return False
        # __SLOT_W_STAGE2_FUZZY_SYNTAX_2026_06_18__ (B) pre-write .py syntax guard
        # (gated, default-OFF). A malformed *.py create/edit is rejected BEFORE any
        # write or backup → apply_session counts a failure → session revert of any
        # siblings. OFF (default) short-circuits before ast.parse → byte-identical.
        if (
            _py_syntax_check_enabled()
            and change.action in ("create", "edit")
            and full.suffix == ".py"
        ):
            try:
                ast.parse(change.content)
            except SyntaxError as _ff_exc:
                _swallowed(_ff_exc, site="enforcement.safe_auto_apply._apply_change:465", category="apply")
                return False
        # __SLOT_R10_RA_2026_08_17__ 아래 여섯 분기 전부 실제 디스크 mutation
        # 직전에 intent, 직후(예외 없이 끝난 경우만)에 commit — 트랙 RA §12-b
        # 배선. `change.path`(원문 상대경로)를 쓴다: `canonical_rel`/`full` 은
        # 이 함수 안에서만 살고, 두-단계 원장은 세션 밖 다른 프로세스가 읽을
        # 것이므로 호출자가 실제로 건넨 경로 문자열을 남긴다.
        if change.action == "create":
            if full.exists(): return False
            full.parent.mkdir(parents=True, exist_ok=True)
            with self._two_phase_txn("create", change.path):
                _safe_write_text(full, change.content)
            # __SLOT_R5_A_SESSION_HARDENING_2026_08_17__ (§8) register for
            # session-rollback consideration; see `_session_creates` docstring.
            if _session_hardening_enabled():
                self._session_creates.append(full)
            return True
        if change.action == "edit":
            if not full.exists(): return False
            change.old_content = full.read_text(encoding="utf-8")
            self._disk_backup(full, change.old_content)
            with self._two_phase_txn("edit", change.path):
                _safe_write_text(full, change.content)
            return True
        if change.action == "delete":
            if not full.exists(): return False
            change.old_content = full.read_text(encoding="utf-8")
            self._disk_backup(full, change.old_content)
            with self._two_phase_txn("delete", change.path):
                full.unlink()
            return True
        if change.action == "replace_line":
            if not full.exists(): return False
            change.old_content = full.read_text(encoding="utf-8")
            self._disk_backup(full, change.old_content)
            with self._two_phase_txn("replace_line", change.path):
                _ok = flexible_replace_line(full, change.target_line, change.replacement)
            return _ok
        # __SLOT_EXECUTOR_LINE_EDIT_2026_06_13__ insert/delete line — same
        # exists→backup→helper envelope as replace_line, so session rollback +
        # 2-env dry_run gate (checked at :310) cover them identically.
        if change.action == "insert_line":
            if not full.exists(): return False
            change.old_content = full.read_text(encoding="utf-8")
            self._disk_backup(full, change.old_content)
            with self._two_phase_txn("insert_line", change.path):
                _ok = flexible_insert_line(full, change.target_line, change.content, change.position)
            return _ok
        if change.action == "delete_line":
            if not full.exists(): return False
            change.old_content = full.read_text(encoding="utf-8")
            self._disk_backup(full, change.old_content)
            with self._two_phase_txn("delete_line", change.path):
                _ok = flexible_delete_line(full, change.target_line)
            return _ok
        return False

    def _build_pre_apply_snapshot(self, changes: List[FileChange]) -> Dict[str, str]:
        return {c.path: _hash_path((self.repo_root / c.path).resolve()) for c in changes}

    def _aggregate_hash(self, snapshot: Dict[str, str]) -> str:
        h = hashlib.sha256()
        for path in sorted(snapshot.keys()):
            h.update(path.encode("utf-8")); h.update(b":")
            h.update(snapshot[path].encode("utf-8")); h.update(b"\n")
        return h.hexdigest()

    # __SLOT_SI_AUDIT_APPLY_SESSION_2026_08_10__ R2 잔여 ①(F1) — **소비되는 자리**.
    # 🔴 R2 적대검증 실측: 이 증분은 행을 내되 판독기가 **안 보는 자리**에 냈다.
    #    당시 프로덕션 형상은 `si_lanes/promote.py` 의
    #    `SafeAutoApply(repo_root=git_root, dry_run=not write_on)` — `audit_root`
    #    **미전달**이었다(같은 함수 `promote_patches` 가 `state_dir` 을 인자로 이미
    #    쥐고 있는데도). 그 형상에서 `_audit_log_path` 최종 폴백은 `repo_root/state`
    #    ⇒ `<repo>/state/si_audit/*.jsonl`. 판독기
    #    `tools/ledger_join_check.py:DEFAULT_JAIL = <repo>/state/si_jail` 은 그
    #    자리를 안 본다. 선언✓·생산✓·**소비✗**.
    # ✅ 그 호출자는 2026-08-10 에 고쳐졌다 — 이제 젤을 아는 쪽이 말한다
    #    (`si_lanes/promote.py` 의 `__SLOT_B2_SAFE_AUTO_APPLY_2026_08_10__`).
    #    위 문단은 **이 PIN 계단이 왜 생겼는지의 기록**이지 오늘의 형상이 아니다.
    #    ⛔ 줄번호로 가리키지 않는다: 종전 판이 적어 둔 `promote.py:263` 은 하루
    #    만에 썩어 엉뚱한 줄을 가리켰다.
    # 🔑 라이브 관례가 정본이다(실측 2026-08-10): si_audit 행은 전부 **젤 밑**에
    #    산다 — `state/si_jail/si_audit/` 2행 · `data/prompts/state/si_audit/` 12행
    #    (후자가 08-09 에 `audit_root` 를 낳은 그 유출) · SI 레인 3곳
    #    (`self_improvement_v8.py` 의 `__SLOT_SI_AUDIT_JAIL_ANCHOR_2026_08_09__`)이
    #    `audit_root=state_dir`.
    #    ⇒ 미전달 형상은 `repo_root/state/si_jail` 로 귀속시킨다: 판독기의 기본
    #    젤과 **같은 자리**이고 `.gitignore:29 state/si_jail/` 로 이미 무시된다.
    #    ⚠️ "`state/si_audit/` 는 켜는 순간 소스트리가 더러워진다"는 종전 단정은
    #    **PIN 계단이 꺼진 형상에서만** 참이다 — PIN=true(커밋된 `.env` 형상)에서는
    #    미전달이어도 젤로 간다. 못: `tests/v8_1/test_promote_b2_audit_root_2026_08_10.py`.
    # ⛔ `_audit_log_path` 자체는 안 건드린다 — `apply_block` 과 공유라 폴백을
    #    바꾸면 **게이트 밖** 라이브 레인의 착지가 변한다. 이 해석은 게이트 안에만.
    #
    # __SLOT_SI_AUDIT_LOG_DIR_PIN_2026_08_10__ R3 잔여 — 위 문단이 `apply_session`
    # 에만 젤을 못 박아, 한 인스턴스가 원장을 **메서드 축으로** 쪼갰다(실측:
    # `state/si_jail/si_audit`=apply_session · `state/si_audit`=block/rollback).
    # ⇒ 세 경로가 이 함수 하나를 탄다. `None` = `_audit_log_path` 종전 폴백.
    def _ledger_root(self) -> Path | None:
        if self.audit_root is not None:
            return self.audit_root
        if _si_audit_log_dir_pin_enabled():
            return self.repo_root / "state" / "si_jail"
        return None

    # __SLOT_R10_RA_2026_08_17__ write-ahead 두-단계 원장의 경로. `_ledger_root`
    # 와 같은 계단을 탄다(명시 `audit_root` → `AGI_V8_SI_AUDIT_LOG_DIR_PIN` →
    # `AGI_V8_STATE_DIR`/`AGI_STATE_DIR`) — si_audit 이 이미 겪은 "원장 위치가
    # 레인마다 갈리면 조인이 깨진다"를 반복하지 않는다.
    # 🔴 `repo_root/state` 로 더는 폴백하지 않는다(실측 회귀, R10 자체 발견):
    #    `si_lanes/promote.py` 형상은 `repo_root=git_root`(**실 소스트리**)를
    #    `audit_root` 없이 넘긴다(`promote_shape` 픽스처가 그 형상을 그대로
    #    재현 — `tests/v8_1/test_safe_auto_apply_r3_nails_2026_08_10.py`). 이
    #    게이트는 기본 ON 인데, 알려진 jail 이 하나도 없을 때 `repo_root/state`
    #    를 새로 지어내면 **실 소스트리에 새 디렉터리를 쓰는** — 이 파일의
    #    si_audit 자매 게이트들이 정확히 그래서 default-OFF 로 남은 그 위험을
    #    재도입한다. ⇒ 알려진 jail 이 없으면 `None` (기록 스킵, 실제 mutation
    #    은 그대로 진행 — 이 원장은 부가 기능이지 mutation 을 막을 이유가
    #    아니다). 파일명은 si_audit 과 겹치지 않게 별도
    #    (`apply_chain_two_phase.jsonl`) — 이건 별개 원장이다.
    def _two_phase_chain_path(self) -> Path | None:
        base = self._ledger_root()
        if base is None:
            env_dir = os.environ.get("AGI_V8_STATE_DIR") or os.environ.get("AGI_STATE_DIR")
            if not env_dir:
                return None
            base = Path(env_dir)
        # 서브디렉터리 한 겹(si_audit 과 같은 계단) — jail 자리가 파일(디렉터리
        # 아님)이면 `mkdir` 이 `NotADirectoryError` 로 터진다(= `_audit_event`
        # 가 같은 불량 jail 에서 내는 예외와 같은 계열). jail 바로 밑에
        # `*.jsonl` 을 직접 두면 그 자리엔 대신 `FileExistsError` 가 나 어휘가
        # 갈린다 — `test_an_unusable_jail_is_loud_under_strict_fail_fast` 로 실측.
        return base / "apply_chain_two_phase" / "chain.jsonl"

    # __SLOT_R10_RA_REPAIR_2026_08_17__ Local, non-propagating record of a
    # `_two_phase_txn` ledger failure — counts + names it (`_swallowed`,
    # so `census()` sees it and STRICT still logs it) but the strict-mode
    # re-raise `_swallowed` itself performs is absorbed right here, one
    # frame in, and never allowed past this call. Mirrors the established
    # repo idiom for "this call is genuinely optional, the counter must
    # fire but the caller must not die" —
    # `policy.secret_masker._dict_insert_no_loss`'s masked-key-collision
    # branch (2026-08-10, "swallowed() 성공케이스 오용=strict 돈행증발" postmortem):
    # call `_swallowed` for the count+log, then swallow ITS OWN re-raise in
    # a second, immediately-adjacent try/except so the count still lands
    # but nothing escapes. `phase` is "begin" or "commit"; `rel_path` is the
    # write target the ledger record was FOR (not the ledger file itself) —
    # `result.ledger_errors` needs to say what write the audit trail missed.
    def _two_phase_ledger_fault(self, phase: str, action: str, rel_path: str,
                                 detail: str) -> None:
        # 2026-08-18: 이 메서드는 이제 원본 ``exc`` 를 받지 않고 이미
        # ``format_text_for_sink(exc)`` 로 마스킹된 ``detail`` 문자열만 받는다
        # (nonlogger_exception_audit raw 시정). 메서드 이름에 "ledger" 가
        # 들어 있어 그 스캐너가 `append`-형 durable sink 로 보는데, 예전처럼
        # 원본 exc 를 여기로 계속 들려보내면 masking 이 이 프레임 안에서
        # 일어나도(이전 시도) 호출부에서 넘겨준 raw 값 자체가 여전히
        # "durable-이름 콜에 노출됐다"로 잡혔다(실측). ``_swallowed`` 텔레메트리
        # 는 그래서 원본 exc 가 살아있는 호출부로 옮겼다(아래 두 곳) — 거기서는
        # ``_swallowed`` 가 choke point 로 직접 인식돼 안전하다.
        self._two_phase_ledger_errors.append(
            f"two_phase_{phase}_failed:{action}:{rel_path}:{detail}"
        )

    @contextmanager
    def _two_phase_txn(self, action: str, rel_path: str) -> Iterator[None]:
        """§12-b wiring: `begin_intent` before, `commit` after a real write.

        Gate OFF, or `self.dry_run` True (nothing will actually touch disk)
        ⇒ falls through to a plain ``yield`` — the wrapped write always
        still runs, this context manager only ever ADDS an audit trail
        around it, never blocks it.

        🔴 R10 repair (2026-08-17): the pre-write `begin_intent` call and the
        post-write `commit` call can BOTH fail (an unusable jail — a file
        sitting where the ledger expects a directory — is one live-reachable
        shape, not hypothetical). Neither failure may ever block or unwind
        the wrapped write:
          * a `begin_intent` failure happens BEFORE ``yield`` — if it were
            allowed to raise here (even just re-raising under STRICT), the
            generator would never reach ``yield`` at all and the wrapped
            write inside ``with self._two_phase_txn(...): ...`` would never
            run. That was the actual bug this repair closes (see the
            module's R10 CHAIN2PHASE report) — the ledger is meant to be a
            side-channel audit trail, not a precondition for the mutation it
            describes.
          * a `commit` failure happens AFTER ``yield`` — the write has
            already landed on disk by then. Letting `commit`'s failure
            escape here would surface as an exception from the `with` block
            AFTER the mutation succeeded, which every caller of this method
            (`_apply_change`, `apply_block`) currently has no way to tell
            apart from "the write itself failed" — exactly the
            ledger-says-refused/tree-says-mutated split this repair closes.
        Both failures are instead handed to `_two_phase_ledger_fault`, which
        counts + names them (`census()`, WARNING log) and appends a message
        to `self._two_phase_ledger_errors` — surfaced by the caller as
        `result.ledger_errors`, deliberately never `result.errors` (which
        gates `result.success`).

        If the wrapped write raises, the code after ``yield`` (the `commit`
        call) never runs — that is still the point: the intent stays
        orphaned so `find_orphaned_intents` can surface "started, never
        confirmed done" instead of silently recording a completion that
        didn't happen. This repair does not touch that path.
        """
        if not (_two_phase_chain_enabled() and not self.dry_run):
            yield
            return
        _chain_path = self._two_phase_chain_path()
        if _chain_path is None:  # 알려진 jail 없음 — 기록 스킵, 쓰기는 그대로
            yield
            return
        _txn_id: str | None = None
        try:
            _txn_id = _chain_begin_intent(
                {
                    "schema": "safe_auto_apply.two_phase.v1",
                    "action": action,
                    "target": rel_path,
                },
                chain_path=_chain_path,
            )
        except Exception as exc:  # noqa: BLE001 — 원장 쓰기 실패가 실 mutation 을 막지 않는다
            self._two_phase_ledger_fault(
                "begin", action, rel_path, format_text_for_sink(exc)
            )
            try:
                _swallowed(
                    exc,
                    site="enforcement.safe_auto_apply._two_phase_txn:begin",
                    category="apply",
                )
            except Exception:
                pass  # STRICT re-raise absorbed here — count/log above already ran.
            _txn_id = None
        yield
        if _txn_id is not None:
            try:
                _chain_commit(
                    _txn_id,
                    {
                        "schema": "safe_auto_apply.two_phase.v1",
                        "action": action,
                        "target": rel_path,
                    },
                    chain_path=_chain_path,
                )
            except Exception as exc:  # noqa: BLE001 — mutation 은 이미 끝났다. 그 사실이 없어지면 안 된다
                self._two_phase_ledger_fault(
                    "commit", action, rel_path, format_text_for_sink(exc)
                )
                try:
                    _swallowed(
                        exc,
                        site="enforcement.safe_auto_apply._two_phase_txn:commit",
                        category="apply",
                    )
                except Exception:
                    pass  # STRICT re-raise absorbed here — count/log above already ran.

    # 세션 한 건 = 원장 한 행. 젤 앵커는 `apply_block` 과 **같은 계단**을 탄다 —
    # 원장 위치가 레인마다 갈리면 조인이 또 깨진다(08-08 젤-밖 12행이 그 결과였다).
    def _audit_session(self, changes: List[FileChange], result: ApplyResult,
                       proposed: List[FileChange] | None = None) -> None:
        if not _si_audit_apply_session_enabled():
            return
        # R2 잔여 ②(F2) — **분모**. `apply_session` 은 거부된 변경을 걸러낸 뒤
        # `changes` 를 재바인딩하므로, 이 함수가 받는 리스트는 생존자뿐이다.
        # 그대로 세면 4건 중 2건 거부 시 행은 `total_changes=2`, 전부 거부 시
        # `total_changes=0`·`target="no_valid_changes"` 가 되어 원장이 **몇 건이
        # 제안됐나**를 말하지 못한다([[feedback_metric_denominator]] 와 같은 자리).
        # ⇒ 세션 진입 시점의 리스트를 분모로 따로 싣는다.
        _proposed = list(changes) if proposed is None else list(proposed)
        # R2 잔여 ④(F4) — dry-run 의 **성패를 접지 않는다**. SI 레인은
        # `do_real_write` 가 거짓일 때마다 dry-run 세션을 돌리므로(게이트 ON 시
        # 최빈 status) 옛 어휘는 실패한 dry-run 과 성공한 dry-run 을 같은 값으로
        # 남겼고, 성패는 `reason` 문자열로만 구분됐다.
        if self.dry_run:
            status = "dry_run" if result.success else "dry_run_failed"
        else:
            status = "applied" if result.success else "failed"
        _audit_event(self.repo_root, {
            "schema": "safe_auto_apply.v1", "proposal_id": "",
            "target": ";".join(c.path for c in changes) or "no_valid_changes",
            "proposed_targets": ";".join(c.path for c in _proposed),
            "action": "apply_session", "status": status,
            "success": bool(result.success),
            "reason": "; ".join(result.errors[:5]),
            "changes_applied": result.changes_applied,
            "total_changes": len(changes),
            "total_proposed": len(_proposed),
            "rejected_count": len(_proposed) - len(changes),
            "reverted": result.reverted,
            "sha256_before": result.pre_apply_hash,
            "sha256_after": result.post_apply_hash,
            "verifier_id": result.verifier_id,
            "decision_source": result.decision_source,
            # R2 잔여 ③(F3) — `write_enabled` 는 **이 쓰기**에 대한 사실이 아니다.
            # `_apply_change` 는 `self.dry_run` 만 보고 `write_enabled()` 를 조회
            # 하지 않는다(2-env 게이트는 호출자가 dry_run 으로 번역해 주는 관례일
            # 뿐). 그래서 `SafeAutoApply(dry_run=False)` 를 env 없이 만든 호출자는
            # 실제로 파일을 쓰면서 write_enabled=false 행을 남길 수 있다.
            # ⇒ 쓰기를 실제로 결정하는 술어(`dry_run`)를 나란히 싣는다.
            #   `write_enabled` 는 **env 두 개의 상태**로만 읽어야 한다.
            #   ⚠️ 소비자는 "이 세션이 썼나" 를 `dry_run is False` 로 물어야 한다.
            "dry_run": bool(self.dry_run),
            "write_enabled": self.write_enabled(),
        }, audit_root=self._ledger_root())

    def apply_session(self, changes: List[FileChange],
                      commit_message: str = "self-modification",
                      verifier_id: str = "", decision_source: str = "auto_apply") -> ApplyResult:
        result = ApplyResult(decision_source=decision_source, verifier_id=verifier_id)
        _hardening = _session_hardening_enabled()
        # __SLOT_R5_A_WRITE_RECHECK_2026_08_17__ (§10) `_apply_change` only
        # reads `self.dry_run`; a `dry_run=False` instance built without the
        # two write-enable envs set would otherwise write for real here. Gate
        # default-OFF (see module comment on `_session_write_recheck_enabled`)
        # — this whole block is a no-op unless explicitly opted in. When it
        # fires, `self.dry_run` is forced True for the DURATION of this call
        # only (restored in `finally`), so every downstream read of
        # `self.dry_run` (`_apply_change`, `_audit_session`, the tee below)
        # sees the forced value consistently instead of a half-translated one.
        _orig_dry_run = self.dry_run
        _write_recheck_forced = (
            _session_write_recheck_enabled()
            and not self.dry_run
            and not self.write_enabled()
        )
        if _write_recheck_forced:
            self.dry_run = True
            result.errors.append(
                "write_disabled: AGI_V8_SAFE_AUTO_APPLY_*_ENABLED not set (session_write_recheck)")
        try:
            # R3: fresh session-backup ledger per apply_session call. Each
            # _disk_backup (edit/delete/replace_line) appends its verified
            # (target, bak) here so a partial failure can revert the whole session.
            self._session_backups = []
            # __SLOT_R5_A_SESSION_HARDENING_2026_08_17__ (§8) fresh per-session
            # create-ledger, same lifecycle as `_session_backups` above.
            self._session_creates = []
            # __SLOT_R10_RA_REPAIR_2026_08_17__ fresh per-session write-ahead
            # ledger fault list, same lifecycle as `_session_backups` above —
            # copied into `result.ledger_errors` right before every return
            # below (success AND the pre-loop all-rejected early return).
            self._two_phase_ledger_errors = []
            # R2 잔여 ②(F2) — 분모는 **필터 전** 리스트다. 아래에서 `changes` 가
            # 생존자로 재바인딩되므로, 그 뒤에 세면 세션의 분모가 아니라 생존자 수를
            # 센다. 여기서 한 번 잡아 두고 원장까지 그대로 들고 간다.
            proposed = list(changes)
            rejected = self._validate_changes(changes)
            if rejected:
                result.errors = result.errors + rejected
                logger.warning("Rejected: %s", rejected)
                changes = [c for c in changes if not self._validate_changes([c])]
                if not changes:
                    # 전부 거부된 세션도 결정이다 — 원장에 남는다(게이트 ON 일 때).
                    self._audit_session(changes, result, proposed=proposed)
                    return result
            pre = self._build_pre_apply_snapshot(changes)
            result.pre_apply_hash = self._aggregate_hash(pre)
            t0 = time.monotonic()
            applied = 0
            for c in changes:
                # __SLOT_R5_A_SESSION_HARDENING_2026_08_17__ (§11) `apply_block`
                # already enforces `max_writes_per_cycle` (:~1358); this session
                # path never consulted the counter at all, so the ONE live
                # promotion lane (`si_lanes/promote.py` → `apply_session`) had
                # no rate limit. `not self.dry_run` mirrors `apply_block`'s own
                # implicit gating (it only reaches the check once `write_enabled`
                # is true) — a dry-run session never touches disk, so it must
                # not consume or be blocked by the real-write budget.
                if _hardening and not self.dry_run and self._writes_this_cycle >= self.max_writes_per_cycle:
                    result.errors.append(
                        f"rate_limited: {self._writes_this_cycle}/{self.max_writes_per_cycle}: {c.path}")
                    continue
                try:
                    if self._apply_change(c):
                        applied += 1
                        if _hardening and not self.dry_run:
                            self._writes_this_cycle += 1
                    else:
                        result.errors.append(f"Apply returned False: {c.path}")
                except Exception as exc:  # noqa: BLE001
                    _swallowed(exc, site="enforcement.safe_auto_apply.apply_session:536", category="apply")
                    result.errors.append(
                        f"Failed to apply {c.path}: {format_text_for_sink(exc)}"
                    )
                    logger.error(
                        "Apply failed for %s: %s",
                        c.path,
                        format_exception_for_log(exc),
                    )
            result.changes_applied = applied
            # __SLOT_R5_A_SESSION_HARDENING_2026_08_17__ (§8) True only when the
            # EXECUTION loop above actually failed to apply something it
            # attempted — as opposed to `result.errors` merely carrying
            # pre-loop VALIDATION rejections of OTHER, unrelated changes
            # (`_validate_changes` filters `changes` before the loop even
            # starts). Measured regression while building this fix:
            # `test_apply_session_skips_rejected` (tests/v8/test_r25_safe_auto_apply.py)
            # sends one rejected change (state/bad.json) alongside one
            # unrelated create (good.py) that fully succeeds — pre-fix this
            # never rolled back (creates were untracked), so gating the NEW
            # create-cleanup on raw `result.errors` would wrongly delete
            # good.py for a rejection that has nothing to do with it. Session
            # `changes` is already the survivor list by this point, so
            # `applied < len(changes)` is exactly "something we tried to
            # apply failed".
            _execution_had_errors = applied < len(changes)
            # R3 session atomicity (PART1 §2.3): on PARTIAL failure (some files
            # already written + at least one error), replay the session backups in
            # reverse to restore the pre-session content of every touched file.
            # dry_run sessions never record backups, so this block is a no-op there.
            # __SLOT_R5_A_SESSION_HARDENING_2026_08_17__ (§8) also attempt when
            # ONLY creates are pending (no edits/deletes this session) — pre-fix
            # the whole block was gated on `self._session_backups` alone, so a
            # session of pure `create` failures never even tried to clean up.
            # ⚠️ The `self._session_backups` disjunct is left EXACTLY as it was
            # pre-fix (still triggered by any `result.errors`, including a
            # pre-loop rejection of an unrelated change) — that is a
            # PRE-EXISTING latent bug (confirmed reproducible on the pre-fix
            # module too, see report), out of this fix's §8 scope. Only the
            # NEW `_session_creates` branch is scoped to genuine execution
            # failures, so this fix does not widen the pre-existing bug's
            # blast radius to a class of change (create) that was previously
            # immune to it.
            if result.errors and (self._session_backups or
                                  (_hardening and _execution_had_errors and self._session_creates)):
                if _hardening:
                    result.rollback_attempted = True
                for target, bak in reversed(self._session_backups):
                    try:
                        # __SLOT_F6_SESSION_ROLLBACK_2026_08_10__ (타워 R5)
                        # 🔴 R4 는 F6("철자가 아니라 출처로 자격을 판정")을
                        # `revert_last` 에만 적용했다. 그런데 `revert_last` 는 오늘
                        # 비테스트 호출자가 **0**이고, **라이브 레인이 쓰는 건 이
                        # 세션 롤백**이다(`si_lanes/promote.py` → `apply_session`).
                        # 적대검증이 실제 서브프로세스 공격자로 여기에 공격자
                        # 바이트를 착지시켰다: O_NOFOLLOW 는 심링크만 막고
                        # os.replace·하드링크 교체는 못 막는다.
                        # ⇒ 되감기 자격을 여기서도 **출처**로 판정한다:
                        #   (1) 이 인스턴스가 쓰고 검증한 백업인가(`_backup_sha`),
                        #   (2) O_NOFOLLOW 로 읽은 바이트의 sha 가 그대로인가.
                        # 둘 중 하나라도 어긋나면 되감지 않는다(fail-closed) —
                        # 되감기 실패는 시끄럽게 남기지만, 낯선 바이트를
                        # allowlist 파일에 앉히는 것보다 언제나 낫다.
                        _expected = self._backup_sha.get(str(bak))
                        if _expected is None:
                            _msg = f"session_revert_refused {target}: bak_unverified {bak}"
                            result.errors.append(_msg)
                            if _hardening:
                                result.rollback_errors.append(_msg)
                                result.residual_paths.append(str(target))
                            continue
                        _data = _read_bytes_nofollow(bak)
                        if _hash_bytes(_data) != _expected:
                            _msg = f"session_revert_refused {target}: bak_tampered {bak}"
                            result.errors.append(_msg)
                            if _hardening:
                                result.rollback_errors.append(_msg)
                                result.residual_paths.append(str(target))
                            continue
                        _safe_write_text(target, _data.decode("utf-8"))
                    except (OSError, ValueError) as exc:  # noqa: PERF203
                        _swallowed(exc, site="enforcement.safe_auto_apply.apply_session:548", category="apply")
                        _msg = f"session_revert_failed {target}: {format_text_for_sink(exc)}"
                        result.errors.append(_msg)
                        if _hardening:
                            result.rollback_errors.append(_msg)
                            result.residual_paths.append(str(target))
                # __SLOT_R5_A_SESSION_HARDENING_2026_08_17__ (§8) reverse-unlink
                # every `create` this session produced — no backup exists for a
                # create (nothing pre-existed), so restoring means REMOVING it,
                # not overwriting it. Source-of-truth check mirrors the backup
                # loop above (fail-closed on anything unexpected at that path).
                # __SLOT_R5_A_BLOCKER_FIX_2026_08_17__ this loop MUST see the
                # same fact the entry `if` above judged this block on. The
                # entry `if` reaches this block via TWO disjuncts —
                # `self._session_backups` (any `result.errors`, including an
                # unrelated pre-loop VALIDATION rejection that never touched
                # the execution loop) OR `_execution_had_errors` (a genuine
                # loop failure). Pre-fix this inner guard checked `_hardening`
                # ALONE, so entry via the FIRST disjunct (e.g. one successful
                # edit + one successful create + one unrelated protected-path
                # rejection) silently reverse-unlinked a fully successful
                # create for a failure that never happened (adversarial
                # verifier REFUTE, 2026-08-17; see
                # test_adv_r5_020_successful_create_survives_unrelated_pre_loop_rejection).
                # Re-checking `_execution_had_errors` here makes this loop's
                # condition identical to the intent of the entry `if`'s own
                # create-scoped disjunct — a create is reverse-unlinked only
                # when the execution loop itself actually failed to apply
                # something it attempted, never for an unrelated rejection.
                if _hardening and _execution_had_errors:
                    for created in reversed(self._session_creates):
                        try:
                            _cst = os.lstat(str(created))
                        except FileNotFoundError:
                            continue  # already gone — nothing residual
                        except OSError as exc:
                            _swallowed(exc, site="enforcement.safe_auto_apply.apply_session:create_lstat",
                                       category="apply")
                            _msg = f"session_revert_failed {created}: {format_text_for_sink(exc)}"
                            result.errors.append(_msg)
                            result.rollback_errors.append(_msg)
                            result.residual_paths.append(str(created))
                            continue
                        if _stat.S_ISLNK(_cst.st_mode):
                            # The create branch never follows an existing
                            # symlink (`full.exists()` denies it up front), so
                            # this means something replaced our file AFTER we
                            # wrote it — refuse to unlink an unknown target.
                            _msg = f"session_revert_refused {created}: symlink_at_create_target"
                            result.errors.append(_msg)
                            result.rollback_errors.append(_msg)
                            result.residual_paths.append(str(created))
                            continue
                        try:
                            os.unlink(str(created))
                        except OSError as exc:
                            _swallowed(exc, site="enforcement.safe_auto_apply.apply_session:create_unlink",
                                       category="apply")
                            _msg = f"session_revert_failed {created}: {format_text_for_sink(exc)}"
                            result.errors.append(_msg)
                            result.rollback_errors.append(_msg)
                            result.residual_paths.append(str(created))
                result.reverted = True
            result.rollback_latency_ms = (time.monotonic() - t0) * 1000.0
            post = self._build_pre_apply_snapshot(changes)
            result.post_apply_hash = self._aggregate_hash(post)
            # __SLOT_R5_A_SESSION_HARDENING_2026_08_17__ (§8) "fully restored"
            # is judged ONLY by aggregate hash equality post-rollback vs the
            # pre-transaction snapshot — never by "the loop finished without
            # raising" (that is what pre-fix `reverted=True` actually meant).
            if _hardening:
                result.rollback_complete = (
                    result.rollback_attempted
                    and not result.rollback_errors
                    and result.post_apply_hash == result.pre_apply_hash
                )
            # __SLOT_R10_RA_REPAIR_2026_08_17__ deliberately BEFORE
            # `result.success` and NOT folded into `result.errors` — a
            # ledger-only fault must never flip a real mutation to failed
            # (see `_two_phase_txn`'s docstring + `ApplyResult.ledger_errors`).
            result.ledger_errors = list(self._two_phase_ledger_errors)
            result.success = applied == len(changes) and not result.errors
            self._audit_session(changes, result, proposed=proposed)
            # __SLOT_EXECUTOR_LOG_2026_06_14__ One session-level executor record
            # (pure side-effect; ``result`` is returned unchanged).
            _executor_log(
                phase="success" if result.success else "failure",
                status="ok" if result.success else "error",
                output_summary={
                    "changes_applied": result.changes_applied,
                    "total_changes": len(changes),
                    "reverted": result.reverted,
                    "verdict": "ok" if result.success else "partial_or_fail",
                },
                error=("; ".join(result.errors[:5]) if result.errors else None),
            )
            # __SLOT_P0_LOG_UNIFICATION_VERIFY_TEE_2026_08_10__ 같은 판정을 계약
            # 원장(episode.jsonl)의 VERIFY 행으로 사영한다 — 라이브 실측(08-10)에서
            # VERIFY 는 0행이었고, apply 판정은 executor_log 에만 남아 판독기가 손으로
            # 꿰야 했다. ⛔ 어휘는 위 executor 행의 것을 **그대로** 쓴다(ok /
            # partial_or_fail — 번역하면 게이트마다 진리값 어휘가 갈라진다). 실패
            # 신원 = ``result.errors`` 문자열(경로가 박혀 있다). episode 게이트 OFF =
            # byte-identical, tee 실패는 명명 카운트(본작업 불사 — tee_verify 내부).
            try:
                from agi_v8_1.runtime.episode_tee import tee_verify
                tee_verify(
                    target=";".join(c.path for c in changes) or "no_valid_changes",
                    command=f"safe_auto_apply.apply_session(dry_run={self.dry_run})",
                    ran=True,
                    verdict="ok" if result.success else "partial_or_fail",
                    failed_ids=result.errors,
                )
            except Exception as exc:  # noqa: BLE001 — import 실패조차 apply 를 못 죽인다
                _swallowed(exc, site="enforcement.safe_auto_apply.apply_session:verify_tee",
                           category="telemetry")
            return result
        finally:
            # __SLOT_R5_A_WRITE_RECHECK_2026_08_17__ (§10) always restore the
            # caller's own `dry_run`, on every exit path (normal return or an
            # exception escaping the try body).
            if _write_recheck_forced:
                self.dry_run = _orig_dry_run

    def replace_line(self, path: str, old_line: str, new_line: str) -> ApplyResult:
        return self.apply_session([FileChange(
            path=path, action="replace_line", target_line=old_line, replacement=new_line)])

    # __SLOT_EXECUTOR_LINE_EDIT_2026_06_13__ convenience wrappers mirroring replace_line.
    def insert_line(self, path: str, anchor: str, new_line: str,
                    position: str = "after") -> ApplyResult:
        """Insert *new_line* after/before a flex-matched *anchor* (empty anchor → EOF)."""
        return self.apply_session([FileChange(
            path=path, action="insert_line", target_line=anchor,
            content=new_line, position=position)])

    def delete_line(self, path: str, target_line: str) -> ApplyResult:
        """Delete the first line flexibly matching *target_line*."""
        return self.apply_session([FileChange(
            path=path, action="delete_line", target_line=target_line)])

    def _is_allowlisted_abs(self, abs_path: str) -> bool:
        return abs_path in self.prompt_allowlist

    # __SLOT_T3_PATH_CONTAINMENT_2026_08_08__
    def _containment_verdict(self, target: Path) -> str:
        """Return ``""`` when *target* may be written, else the deny reason.

        OFF (default) → delegates to `_is_allowlisted_abs`, so the only verdict
        it can produce is the pre-existing ``"not_in_allowlist"`` and both the
        error string and the si_audit ``reason`` stay byte-identical.

        ⛔ Callers must NOT rebind ``target`` to the RESOLVED value. Measured
        2026-08-08: ``lstat`` on the RESOLVED path is not a symlink, so rebinding
        kills the final-component O_NOFOLLOW refusal below and makes the write
        follow the link — the fix would open a new hole. The resolved path is a
        verdict input only.
        ✅ Callers MUST, however, rebind to the EXPANDED value (`_act_target`):
        expanduser is pure spelling — it touches no symlink — and without it the
        verdict and the FS ops named different files (round-3 residual ①).
        So: every FS op (lstat/read/.bak/write) uses the EXPANDED target, never
        the resolved one.

        ON verdicts (all deny): ``path_normalize_failed`` / ``symlink_path`` /
        ``not_in_allowlist`` / ``path_containment``.
        """
        if not _path_containment_enabled():
            return "" if self._is_allowlisted_abs(str(target)) else "not_in_allowlist"
        from agi_v8_1.state.path_guard import require_under

        anchor = self.containment_root
        # (1) normalization — its own reason, so the ledger never books a
        #     malformed spelling as "not_in_allowlist".
        # __SLOT_T3_PATH_CONTAINMENT_2026_08_08__ residual ① — from here on the
        # verdict reads the EXPANDED spelling, the same object `_act_target`
        # hands to every FS op. Before this, a `~` spelling was judged expanded
        # and written verbatim; it also slipped past check (2) entirely, because
        # the literal `~/...` is not `is_relative_to(anchor)`.
        expanded = _expand_literal(target)
        resolved = _safe_resolve(target)
        if expanded is None or resolved is None:
            return "path_normalize_failed"
        target = expanded
        # (2) no symlink COMPONENT under the anchor. `is_relative_to` first so
        #     the out-of-anchor case (routine: the SI lane's targets live outside
        #     a test anchor) never trips `_path_has_symlink_component`'s swallow
        #     handler, which re-raises under AGI_V8_STRICT_FAIL_FAST.
        if target.is_relative_to(anchor) and self._path_has_symlink_component(target, root=anchor):
            # The FINAL component keeps the pre-gate vocabulary ("symlink") —
            # apply_block's lstat deny and revert_last's preflight already name
            # that event, and a ledger consumer must not see it renamed. The new
            # reason is reserved for the genuinely new case: an ANCESTOR link.
            return "symlink" if target.is_symlink() else "symlink_path"
        # (3) membership on RESOLVED paths. An allowlist entry reached through a
        #     symlinked ancestor is DROPPED, not resolved: resolving it would
        #     admit the link's destination under a spelling nobody allowlisted
        #     (measured: allowlist <root>/prompts/p.md with prompts -> secret_area
        #     let <root>/secret_area/p.md be written with the gate ON). One bad
        #     entry only removes ITSELF — it can no longer kill the whole call.
        allow: set[Path] = set()
        for _raw in self.prompt_allowlist:
            _entry = _safe_resolve(_raw)
            if _entry is None:
                continue
            # The literal check runs on the EXPANDED entry for the same reason
            # the target does: a `~`-spelled allowlist entry is not
            # `is_relative_to(anchor)`, so its symlink walk was skipped.
            _lit = _expand_literal(_raw)
            if _lit is None:
                continue
            if _lit.is_relative_to(anchor) and self._path_has_symlink_component(_lit, root=anchor):
                continue
            allow.add(_entry)
        if resolved not in allow:
            return "not_in_allowlist"
        # (4) containment against the INDEPENDENT anchor, then against repo_root.
        #     Both walls, never one instead of the other.
        #     ⚠️ Strict surface: these denies route through `_swallowed` like every
        #     other handler in this file, so `AGI_V8_STRICT_FAIL_FAST=true` turns
        #     the refusal into a raise. Still fail-closed (no write happens) and
        #     census-neutral (measured: silent handlers 3 → 3, routed 22 → 25).
        #     Pinned by test_strict_fail_fast_surfaces_the_deny_as_a_raise.
        for _root, _what in ((anchor, "safe_auto_apply target (package root)"),
                             (self.repo_root, "safe_auto_apply target")):
            try:
                require_under(_root, resolved, what=_what)
            except RuntimeError as _ff_exc:
                _swallowed(_ff_exc, site="enforcement.safe_auto_apply._containment_verdict:require_under",
                           category="apply")
                return "path_containment"
            except (OSError, ValueError) as _ff_exc:
                _swallowed(_ff_exc, site="enforcement.safe_auto_apply._containment_verdict:require_under_normalize",
                           category="apply")
                return "path_normalize_failed"
        return ""

    # __SLOT_T3_PATH_CONTAINMENT_2026_08_08__ residual ①
    def _act_target(self, target: Path) -> Path:
        """The ONE spelling every FS op must use — the one the verdict judges.

        Called once at the top of `apply_block` / `revert_last`, BEFORE the
        `_audit` closure captures ``target``, so the ledger, the lstat preflight,
        the ``.bak`` sibling, the read and the write all name the same file.

        OFF (default) → returns the argument object unchanged: byte-identical.
        ON → `expanduser` only (never `resolve`; see `_expand_literal`). When
        the spelling cannot even be expanded (``~nosuchuser/...``) the argument
        is returned unchanged and `_containment_verdict` denies it with
        ``path_normalize_failed`` before anything touches the filesystem, so the
        audit row keeps the caller's own literal spelling.
        """
        if not _path_containment_enabled():
            return target
        expanded = _expand_literal(target)
        if expanded is None:
            return target
        # ⛔ __SLOT_T3_NORMPATH_RETRACTED_2026_08_10__ — R4 가 유령 디렉터리
        # (`<anchor>/data/prompts/ghost/../critic.md` 가 판정에 안 쓰인 `ghost` 를
        # mkdir 하던 부작용)를 없애려고 여기서 `os.path.normpath` 로 `..`/`.` 를
        # 어휘적으로 접었다. **타워가 같은 날 철회한다.**
        #
        # 철회 사유(R4 적대검증 실측): 어휘적 접기는 **커널과 의미가 다르다**.
        # `<anchor>/symlinkdir/../x` 를 normpath 는 `<anchor>/x` 로 접지만 커널은
        # symlinkdir 를 따라간 **뒤** `..` 를 적용해 다른 곳에 착지한다 ⇒ 심링크
        # 디렉터리를 `..` 가 상쇄하는 철자에서 판정이 deny→allow 로 뒤집혔다.
        # 🔑 이는 이 게이트가 애초에 고치려던 결함(**판정과 FS 연산이 다른 경로를
        # 본다**)의 정확한 재발이다. R4 주석이 "같은 철자를 쓰므로 갈라질 여지가
        # 없다"고 적었는데, 같은 철자여도 **그 철자 자체가 커널 의미와 다르게
        # 접히면** 갈라진다.
        # ⇒ 유령 디렉터리(순수 부작용, 앵커 밖 착지 아님)를 감수하고 축자 철자를
        #   유지한다. 부작용은 쓰기 직전 mkdir 범위를 좁히는 쪽으로 따로 풀 것.
        # ✅ 2026-08-10 그 별건이 닫혔다 — `_safe_write_text` 의
        #   `__SLOT_B2_SAFE_AUTO_APPLY_2026_08_10__`(철자에 `..` 가 있으면 mkdir
        #   안 함). 철자는 여전히 축자다: 여기서 접지 않는다. ⚠️ 같은 게이트를
        #   타므로 OFF 기본 형상에서는 유령이 그대로 생긴다(그 계약도 못으로 박힘).
        return expanded

    # __SLOT_T3_PATH_CONTAINMENT_2026_08_08__ ON-only audit field naming WHERE the
    # write actually landed. The pre-gate ledger only carried the LITERAL target,
    # so an escaped write would have been recorded under its innocent spelling.
    # OFF → {} keeps the si_audit row's key set byte-identical.
    def _resolved_audit_extra(self, target: Path,
                              requested: Path | str | None = None) -> Dict[str, Any]:
        if not _path_containment_enabled():
            return {}
        # ⛔ Must never raise: this runs INSIDE `_audit`, i.e. on the deny path.
        # An exception here would delete the very ledger row that records the
        # refusal (measured 2026-08-08 with a `~unknownuser` spelling).
        _res = _safe_resolve(target)
        extra: Dict[str, Any] = {} if _res is None else {"target_resolved": str(_res)}
        # `target` 은 **실제로 연 철자**다(`_act_target` 은 expanduser 만 한다 —
        # 어휘적 접기는 철회됐다: `__SLOT_T3_NORMPATH_RETRACTED_2026_08_10__`).
        # 호출자가 무엇을 요구했는지는 다를 때만 따로 남긴다 — 셋(요구/행위/해석)이
        # 갈릴 수 있으면 원장은 셋 다 말해야 한다.
        if requested is not None and str(requested) != str(target):
            extra["target_requested"] = str(requested)
        return extra

    def _backup_path(self, target: Path) -> Path:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        return target.with_name(f"{target.name}.bak.{ts}")

    def _prune_backups(self, target: Path) -> None:
        siblings = sorted(target.parent.glob(f"{target.name}.bak.*"), key=lambda p: p.name)
        excess = len(siblings) - self.backup_retention
        for p in siblings[: max(0, excess)]:
            try:
                p.unlink()
            except OSError as _ff_exc:
                _swallowed(_ff_exc, site="enforcement.safe_auto_apply._prune_backups:600", category="apply")
                pass

    def apply_block(self, target_abs_path: str, proposal_id: str, payload: str,
                    *, verifier_id: str = "", decision_source: str = "auto_apply") -> ApplyResult:
        """Inject/replace block-marker payload in an allowlisted prompt file."""
        result = ApplyResult(decision_source=decision_source, verifier_id=verifier_id)
        # __SLOT_R10_RA_REPAIR_2026_08_17__ fresh per-call write-ahead ledger
        # fault list — same lifecycle/purpose as `apply_session`'s reset,
        # see `ApplyResult.ledger_errors`.
        self._two_phase_ledger_errors = []
        # __SLOT_T3_PATH_CONTAINMENT_2026_08_08__ residual ① — interpret ONCE,
        # before the audit closure and before any FS op binds a different name.
        target = self._act_target(Path(target_abs_path))
        state = {"action": "block_inject", "sha_before": "", "sha_after": "",
                 "bytes_before": 0, "bytes_after": 0}
        t0 = time.monotonic()

        def _audit(status: str, reason: str = "") -> None:
            _audit_event(self.repo_root, {
                "schema": "safe_auto_apply.v1", "proposal_id": proposal_id,
                "target": str(target), "action": state["action"], "status": status, "reason": reason,
                "bytes_before": state["bytes_before"], "bytes_after": state["bytes_after"],
                "sha256_before": state["sha_before"], "sha256_after": state["sha_after"],
                "verifier_id": verifier_id, "decision_source": decision_source,
                "write_enabled": self.write_enabled(),
                **self._resolved_audit_extra(target, target_abs_path),
            }, audit_root=self._ledger_root())
            # __SLOT_EXECUTOR_LOG_2026_06_14__ Mirror into the executor trail
            # (ADD, not replace — _audit_event above still fired). success iff
            # status=="applied"; everything else is a failure with reason.
            _ok = status == "applied"
            _executor_log(
                phase="success" if _ok else "failure",
                status="ok" if _ok else "error",
                output_summary={
                    "action": state["action"], "path": str(target),
                    "bytes_before": state["bytes_before"],
                    "bytes_after": state["bytes_after"],
                    "applied": result.success,
                },
                error=None if _ok else (reason or status),
            )

        def _fail(err: str, status: str, reason: str) -> ApplyResult:
            result.errors.append(err); _audit(status, reason); return result

        if not self.write_enabled():
            return _fail("write_disabled: AGI_V8_SAFE_AUTO_APPLY_*_ENABLED not set",
                         "dry_run", "write_disabled")
        _verdict = self._containment_verdict(target)
        if _verdict:
            return _fail(f"{_verdict}: {target}", "denied", _verdict)
        if self._writes_this_cycle >= self.max_writes_per_cycle:
            return _fail(f"rate_limited: {self._writes_this_cycle}/{self.max_writes_per_cycle}",
                         "denied", "rate_limited")
        payload_bytes = (payload or "").encode("utf-8")
        if len(payload_bytes) > MAX_BLOCK_BYTES:
            return _fail(f"block_too_large: {len(payload_bytes)} > {MAX_BLOCK_BYTES}",
                         "denied", "block_too_large")
        try:
            st = os.lstat(str(target))
            if _stat.S_ISLNK(st.st_mode):
                return _fail(f"symlink_refused: {target}", "denied", "symlink")
        except FileNotFoundError:
            pass
        try:
            new_block = _build_block(proposal_id, payload)
        except ValueError as exc:
            _swallowed(exc, site="enforcement.safe_auto_apply.apply_block:660", category="apply")
            detail = format_text_for_sink(exc)
            return _fail(
                f"invalid_proposal_id: {detail}",
                "denied",
                f"invalid_proposal_id:{detail}",
            )
        try:
            existing = target.read_text(encoding="utf-8") if target.exists() else ""
        except OSError as exc:
            _swallowed(exc, site="enforcement.safe_auto_apply.apply_block:664", category="apply")
            detail = format_text_for_sink(exc)
            return _fail(f"read_failed: {detail}", "error", f"read_failed:{detail}")
        state["bytes_before"] = len(existing.encode("utf-8"))
        state["sha_before"] = _hash_bytes(existing.encode("utf-8")) if existing else ""
        try:
            span = _find_block_span(existing, proposal_id)
        except ValueError as exc:
            _swallowed(exc, site="enforcement.safe_auto_apply.apply_block:670", category="apply")
            detail = format_text_for_sink(exc)
            return _fail(detail, "error", detail)
        if span is None:
            state["action"] = "block_insert"
            # 🔴 R3 잔여(선행 false-deny, 실측 2026-08-10 HEAD 동일): 여기서
            # `existing` 을 **덮어쓰면** 아래 `.bak` 이 원본이 아닌 개행 보정본을
            # 담아 `sha_before` 와 구조적으로 어긋난다 ⇒ 끝에 개행이 없는 프롬프트는
            # `bak_sha_mismatch` 로 **항상** 실패했다(라이브 apply 를 막는 실버그).
            # ⇒ 개행 보정은 새 본문에만 하고 `existing` 은 파일 바이트 그대로 둔다.
            new_content = (existing + "\n" if existing and not existing.endswith("\n")
                           else existing) + new_block
        else:
            state["action"] = "block_replace"
            new_content = existing[: span[0]] + new_block + existing[span[1]:]
        new_bytes = new_content.encode("utf-8")
        if len(new_bytes) > MAX_FILE_BYTES:
            return _fail(f"file_too_large_after_write: {len(new_bytes)} > {MAX_FILE_BYTES}",
                         "denied", "file_too_large")
        bak_path: Optional[Path] = None
        if target.exists():
            bak_path = self._backup_path(target)
            # R2 잔여 ⑥(F6) — 여기는 `_safe_write_text` 가 아니라 raw
            # `write_bytes` 였다: O_NOFOLLOW/lstat 거부가 **안 걸린다**. 프롬프트
            # 디렉터리에 파일을 만들 수 있는 주체가 `<target>.bak.<ts>` 심링크를
            # 미리 심으면 백업 쓰기가 링크 목적지로 나간다. 최종-컴포넌트 심링크
            # preflight 는 `target` 만 보고 `.bak` 은 안 봤다.
            # ⛔ 순수 fail-closed, 게이트 불요: 실측 2026-08-10 라이브 `.bak.*`
            #    16개 전부 정규 파일(심링크 0) ⇒ false-deny 0.
            if bak_path.is_symlink():
                return _fail(f"bak_symlink_refused: {bak_path}", "denied", "bak_symlink")
            try:
                _safe_write_text(bak_path, existing)
                bak_sha = _hash_bytes(_read_bytes_nofollow(bak_path))
                if bak_sha != state["sha_before"]:
                    try: bak_path.unlink()
                    except OSError as _ff_exc:
                        _swallowed(_ff_exc, site="enforcement.safe_auto_apply.apply_block:691", category="apply")
                        pass
                    return _fail(f"bak_sha_mismatch: expected={state['sha_before']} got={bak_sha}",
                                 "error", "bak_sha_mismatch")
                # __SLOT_BAK_PROVENANCE_2026_08_10__ 검증을 통과한 **이** 백업만
                # 되감기 소스 자격을 얻는다(경로+내용 둘 다 기록).
                self._backup_sha[str(bak_path)] = bak_sha
            # F6: `_safe_write_text` 는 심링크 타깃에 `ValueError` 를 던진다
            # (preflight 와 os.open 사이의 TOCTOU). 잡지 않으면 방어가 크래시로
            # 새어 나간다 — 거부는 원장에 남아야 한다.
            except (OSError, ValueError) as exc:
                _swallowed(exc, site="enforcement.safe_auto_apply.apply_block:694", category="apply")
                detail = format_text_for_sink(exc)
                return _fail(
                    f"bak_write_failed: {detail}",
                    "error",
                    f"bak_write_failed:{detail}",
                )
        try:
            # __SLOT_R10_RA_2026_08_17__ 실제 mutation 직전 intent → 쓰기 →
            # 성공시에만 commit(§12-b 배선). 게이트 OFF/dry_run 이면
            # `_two_phase_txn` 이 순수 pass-through(아래 `_safe_write_text` 는
            # 이전과 동일하게 실행)라 byte-identical.
            with self._two_phase_txn(state["action"], str(target)):
                _safe_write_text(target, new_content)
        except Exception as exc:  # noqa: BLE001
            _swallowed(exc, site="enforcement.safe_auto_apply.apply_block:698", category="apply")
            detail = format_text_for_sink(exc)
            return _fail(f"write_failed: {detail}", "error", f"write_failed:{detail}")
        state["bytes_after"] = len(new_bytes); state["sha_after"] = _hash_bytes(new_bytes)
        self._writes_this_cycle += 1
        if bak_path is not None: self._prune_backups(target)
        # __SLOT_R10_RA_REPAIR_2026_08_17__ same split as `apply_session`:
        # a `_two_phase_txn` ledger fault (already absorbed, never raised
        # past the `with` above) never touches `result.errors`/`success`.
        result.ledger_errors = list(self._two_phase_ledger_errors)
        result.changes_applied = 1; result.success = True
        result.pre_apply_hash = state["sha_before"]; result.post_apply_hash = state["sha_after"]
        result.rollback_latency_ms = (time.monotonic() - t0) * 1000.0
        _audit("applied")
        return result

    def revert_last(self, target_abs_path: str) -> ApplyResult:
        """Restore *target_abs_path* from the most recent .bak.<ts> sibling."""
        result = ApplyResult(decision_source="rollback")
        # __SLOT_T3_PATH_CONTAINMENT_2026_08_08__ residual ① — same single
        # interpretation as apply_block, ahead of the first FS read (_hash_path).
        target = self._act_target(Path(target_abs_path)); t0 = time.monotonic()
        sha_before = _hash_path(target)
        bytes_before = target.stat().st_size if target.exists() else 0

        def _audit(status: str, reason: str = "", sha_after: str = "", bytes_after: int = 0) -> None:
            _audit_event(self.repo_root, {
                "schema": "safe_auto_apply.v1", "proposal_id": "",
                "target": str(target), "action": "rollback", "status": status, "reason": reason,
                "bytes_before": bytes_before, "bytes_after": bytes_after,
                "sha256_before": sha_before, "sha256_after": sha_after,
                **self._resolved_audit_extra(target, target_abs_path)},
                audit_root=self._ledger_root())
            # __SLOT_EXECUTOR_LOG_2026_06_14__ Mirror into the executor trail
            # (ADD, not replace). success iff status=="reverted".
            _ok = status == "reverted"
            _executor_log(
                phase="success" if _ok else "failure",
                status="ok" if _ok else "error",
                output_summary={
                    "action": "rollback", "path": str(target),
                    "reverted": result.reverted,
                },
                error=None if _ok else (reason or status),
            )

        def _fail(err: str, status: str, reason: str) -> ApplyResult:
            result.errors.append(err); _audit(status, reason); return result

        _verdict = self._containment_verdict(target)
        if _verdict:
            return _fail(f"{_verdict}: {target}", "denied", _verdict)
        # __SLOT_T3_PATH_CONTAINMENT_2026_08_08__ ON-only symlink preflight. The
        # pre-gate path had NO lstat here at all — a final-component symlink was
        # stopped only by `_safe_write_text`'s O_NOFOLLOW, which surfaces as
        # ``error/restore_failed``, so the ledger never counted it as a symlink
        # attempt. apply_block already had this preflight; revert_last did not.
        # ``is_symlink`` does not follow the link and is False for a missing path,
        # so it needs no handler — the census ceiling counts silent ``except``
        # blocks, and a preflight is not worth another one.
        if _path_containment_enabled() and target.is_symlink():
            return _fail(f"symlink_refused: {target}", "denied", "symlink")
        baks = sorted(target.parent.glob(f"{target.name}.bak.*"), key=lambda p: p.name)
        if not baks:
            return _fail("no_backup_available", "denied", "no_backup")
        # R2 잔여 ⑥(F6) — 복원은 심링크 방어를 **안 탔다**: `.bak` 을 raw
        # `read_bytes` 로 읽어 그 바이트를 allowlist 타깃에 그대로 썼다. 프롬프트
        # 디렉터리에 파일을 만들 수 있는 주체가 `<target>.bak.<큰타임스탬프>`
        # 심링크를 심으면 **임의 파일 내용이 allowlist 프롬프트로 들어온다**.
        # ⛔ 조용히 더 오래된 백업으로 내려가지 않는다 — 그건 관측 손실이다.
        #    가장 최신 후보가 심링크면 그 사실을 원장에 이름으로 남기고 끊는다.
        # ⛔ 순수 fail-closed(라이브 `.bak.*` 16개 전부 정규 파일, 심링크 0).
        if baks[-1].is_symlink():
            return _fail(f"bak_symlink_refused: {baks[-1]}", "denied", "bak_symlink")
        # __SLOT_BAK_PROVENANCE_2026_08_10__ R3 F6 재반증 — 위 줄은 **철자 방어**다.
        # 링크성만 물으면 같은 공격이 정규 파일/하드링크로 그대로 성립한다(실측
        # 2026-08-10: 두 형태 모두 success=True, allowlist 프롬프트에 낯선 내용
        # 착지). 진짜 전제는 "공격자가 이름을 고른 `.bak` 형제를 신뢰한다"
        # (`glob` 이름순 최대 선택)이므로 물어야 할 것은 종류가 아니라 **출처**다:
        #   (1) 이 인스턴스가 쓰고 검증한 백업인가 — `_backup_sha` 대조,
        #   (2) 그 뒤 내용이 바뀌지 않았나 — O_NOFOLLOW 로 읽어 sha 재대조
        #       (하드링크·os.replace 교체는 경로 검사로는 안 잡힌다).
        # ⛔ 조용히 더 오래된 백업으로 내려가지 않는다(관측 손실). 거부 사유를
        #    원장에 이름으로 남기고 끊는다.
        # ⛔ 순수 fail-closed, 게이트 불요: 실측 2026-08-10 `revert_last` 의
        #    비테스트 호출자 0 ⇒ 라이브 false-deny 표면 없음. 교차 프로세스 되감기가
        #    필요해지면 출처 원장을 젤에 영속화해야 한다(설계 부채, 타워 통보).
        _expected = self._backup_sha.get(str(baks[-1]))
        if _expected is None:
            return _fail(f"bak_unverified: {baks[-1]}", "denied", "bak_unverified")
        try:
            data = _read_bytes_nofollow(baks[-1])
        except OSError as exc:
            _swallowed(exc, site="enforcement.safe_auto_apply.revert_last:745", category="apply")
            detail = format_text_for_sink(exc)
            return _fail(
                f"bak_read_failed: {detail}", "error", f"bak_read_failed:{detail}"
            )
        if not data:
            return _fail("bak_empty", "error", "bak_empty")
        if _hash_bytes(data) != _expected:
            return _fail(f"bak_tampered: {baks[-1]}", "denied", "bak_tampered")
        try:
            _safe_write_text(target, data.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            _swallowed(exc, site="enforcement.safe_auto_apply.revert_last:751", category="apply")
            detail = format_text_for_sink(exc)
            return _fail(
                f"restore_failed: {detail}", "error", f"restore_failed:{detail}"
            )
        sha_after = _hash_bytes(data)
        result.reverted = True; result.success = True; result.changes_applied = 1
        result.rollback_latency_ms = (time.monotonic() - t0) * 1000.0
        result.pre_apply_hash = sha_before; result.post_apply_hash = sha_after
        _audit("reverted", "", sha_after, len(data))
        return result


__all__ = [
    "FileChange", "ApplyResult", "SafeAutoApply",
    "PROTECTED_FILES", "PROTECTED_DIRS", "PROMPT_ALLOWLIST",
    "MAX_BLOCK_BYTES", "MAX_FILE_BYTES", "DEFAULT_MAX_WRITES_PER_CYCLE",
    "flexible_replace_line",
]
