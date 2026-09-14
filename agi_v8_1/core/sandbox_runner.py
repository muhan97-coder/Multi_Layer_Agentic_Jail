"""R16 Phase 3b — Docker sandbox runner.

__R16_W_B_SLOT__

Executes Pod A/B code in an isolated Docker container and returns
structured telemetry. Used by axis_scorer (Worker C) and
final_integrator (Worker D) to ground 6-axis vote in real execution
signals (exit_code, wall_clock, peak_rss, type errors, lint warnings).

Isolation contract enforced at every call site:
  --network=none --read-only --tmpfs /tmp:size=64m
  --memory={env knob} --cpus=1 --pids-limit={env knob}
  --user 65532:65532
  --rm (cleanup on exit)
  -v {input dir}:/sandbox/input:ro

Gated behind ``AGI_V8_SWARM_SANDBOX_ENABLED``. When disabled, docker
binary missing, Docker backend unavailable, image missing, timeout, invalid
config/input, malformed harness output, or any other failure path, this
module returns ``SandboxTelemetry(stub=True, stub_reason=...)`` — callers
must inspect ``.stub`` and handle accordingly. An enabled call with missing
optional T5 code raises ``PayloadUnavailable`` before backend work; failures
of an installed operation retain the existing telemetry contract.

Image identity: ``_image_exists`` only proves the configured tag is present,
not that it still points at a reviewed image (``docker tag`` can silently
repoint any local tag). Set ``AGI_V8_SWARM_SANDBOX_IMAGE_ID`` to the trusted
``docker image inspect --format {{.Id}}`` output to arm digest pinning —
hardening recommended for any deployment that treats sandbox results as a
security boundary. Unset (the default) preserves pre-2026-08-19 behaviour
byte-for-byte and logs one warning recommending the pin be armed. Armed +
mismatch fails closed with ``stub_reason="image_untrusted"``.

Schema: ``agi_v8_sandbox_telemetry_v1``.
"""

from __future__ import annotations

import asyncio
import atexit
import hashlib
import json
import logging
import math
import os
import hmac
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

# __SLOT_FAIL_FAST_2026_07_25__ Swallowed failures route through one choke
# point: counted + named always, re-raised under AGI_V8_STRICT_FAIL_FAST.
from agi_v8_1.capabilities import PayloadPort, resolve_payload

_SANDBOX_PORT = PayloadPort(5, "agi_v8_1.core.sandbox_payload", "execute_sandbox")

from agi_v8_1.policy.fail_fast import (
    SINK_MASK_INPUT_MAX_CHARS,
    format_exception_for_log,
    format_text_for_sink,
    record_critical_failure,
    swallowed as _swallowed,
)


logger = logging.getLogger(__name__)


SCHEMA_VERSION = "agi_v8_sandbox_telemetry_v1"

# Canonical set of stub_reason tokens. Documented so callers/tests can rely on
# the exact strings.
STUB_REASONS: frozenset[str] = frozenset(
    {
        "disabled",
        "payload_unavailable",
        "docker_missing",
        "docker_unavailable",
        # __SLOT_SANDBOX_PRESEAL_DOCKER_ENDPOINT_2026_09_04__ An armed
        # pre-seal identity boundary may pass Docker authority only through
        # the exact host-captured rootless Unix socket.  Missing, stale, or
        # mismatched capture is a named refusal; it must never be collapsed
        # into docker_unavailable and silently routed to bwrap.
        "docker_endpoint_untrusted",
        "image_missing",
        "invalid_config",
        "input_rejected",
        "container_failed",
        "invalid_envelope",
        "timeout",
        "exception",
        # __SLOT_SANDBOX_BWRAP_2026_08_16__ fail-closed refusal: the bwrap
        # backend gate is ON but neither docker nor a working bwrap binary
        # is available. The module MUST NOT fall through to unisolated
        # execution — this reason marks that explicit refusal.
        "no_isolation_available",
        # __SLOT_SANDBOX_WORKTREE_2026_08_16__ fail-closed refusal: the
        # repo-context gate is ON but a read-only snapshot of the live repo
        # could not be built (git/tar missing, `git archive` failed, no
        # `.git` ancestor found, …). Execution NEVER silently falls back to
        # the standalone (no-repo-imports) harness when this gate is armed —
        # that would just reproduce the discriminative-power collapse this
        # slot exists to fix.
        "worktree_unavailable",
        # __SLOT_SANDBOX_IMAGE_PIN_2026_08_19__ fail-closed refusal: the
        # image-identity pin (``AGI_V8_SWARM_SANDBOX_IMAGE_ID``) is armed but
        # the resolved image's actual `docker image inspect --format
        # {{.Id}}` does not match the pinned value — e.g. `docker tag` was
        # used to point the trusted tag at a different (possibly malicious)
        # image. ``_image_exists`` only checks tag *presence*, not identity,
        # so this is a distinct refusal from ``image_missing``.
        "image_untrusted",
        # __SLOT_SANDBOX_GRADE_RECEIPT_2026_08_24__ fail-closed refusal: the
        # grade-receipt gate (``AGI_V8_SANDBOX_GRADE_RECEIPT_ENABLED``) is
        # armed but the harness's result row could not be authenticated —
        # no receipt at all (old image), unknown key id, MAC mismatch, a
        # nonce/input digest that does not belong to THIS call (replay or
        # splice), an envelope whose counts disagree with the signed ones,
        # or a harness digest outside the operator allowlist. Distinct from
        # ``image_untrusted``: that one says "the image is not the reviewed
        # one", this one says "the RESULT is not provably from a reviewed
        # harness observing this exact call". The specific cause is always
        # named in ``stderr_tail`` (``receipt_*:`` prefix) — a silent skip
        # would make "안 했다" and "못 했다" indistinguishable.
        "receipt_untrusted",
    }
)

# __SLOT_SANDBOX_STATUS_2026_08_17__ P0 #4 (Round 6 track SB): the single
# ``stub`` boolean collapses 10 distinct STUB_REASONS into one bit, so a
# candidate that deliberately hangs (timeout) reads identically to "docker
# isn't installed" — both no-fire the S3 fail-closed gate in
# ``self_improvement_v8.py``. ``status`` disaggregates without touching the
# existing ``stub``/``stub_reason`` fields (kept for compatibility). Gated by
# ``AGI_V8_SANDBOX_STATUS_ENABLED`` (default ON — this is an eval-integrity
# repair, not a new feature); OFF collapses ``status`` back to ``""`` at
# every call site, matching the pre-patch dataclass shape's implicit value.
SANDBOX_STATUS_NOT_CONFIGURED = "NOT_CONFIGURED"
SANDBOX_STATUS_INFRA_UNAVAILABLE = "INFRA_UNAVAILABLE"
SANDBOX_STATUS_EVALUATOR_ERROR = "EVALUATOR_ERROR"
SANDBOX_STATUS_CANDIDATE_TIMEOUT = "CANDIDATE_TIMEOUT"
SANDBOX_STATUS_PASS = "PASS"
SANDBOX_STATUS_CANDIDATE_FAIL = "CANDIDATE_FAIL"

SANDBOX_STATUSES: frozenset[str] = frozenset(
    {
        SANDBOX_STATUS_NOT_CONFIGURED,
        SANDBOX_STATUS_INFRA_UNAVAILABLE,
        SANDBOX_STATUS_EVALUATOR_ERROR,
        SANDBOX_STATUS_CANDIDATE_TIMEOUT,
        SANDBOX_STATUS_PASS,
        SANDBOX_STATUS_CANDIDATE_FAIL,
    }
)

# Every token in STUB_REASONS must map to exactly one non-PASS/CANDIDATE_FAIL
# status. Checked by an assertion right after the dict so a future reason
# added to STUB_REASONS without a status mapping fails loudly (import time)
# instead of silently falling through to "" (the P0 this slot exists to fix).
_STUB_REASON_TO_STATUS: dict[str, str] = {
    "disabled": SANDBOX_STATUS_NOT_CONFIGURED,
    "payload_unavailable": SANDBOX_STATUS_INFRA_UNAVAILABLE,
    "docker_missing": SANDBOX_STATUS_INFRA_UNAVAILABLE,
    "docker_unavailable": SANDBOX_STATUS_INFRA_UNAVAILABLE,
    "docker_endpoint_untrusted": SANDBOX_STATUS_INFRA_UNAVAILABLE,
    "image_missing": SANDBOX_STATUS_INFRA_UNAVAILABLE,
    "no_isolation_available": SANDBOX_STATUS_INFRA_UNAVAILABLE,
    "image_untrusted": SANDBOX_STATUS_INFRA_UNAVAILABLE,
    "invalid_config": SANDBOX_STATUS_EVALUATOR_ERROR,
    "input_rejected": SANDBOX_STATUS_EVALUATOR_ERROR,
    "container_failed": SANDBOX_STATUS_EVALUATOR_ERROR,
    "invalid_envelope": SANDBOX_STATUS_EVALUATOR_ERROR,
    "exception": SANDBOX_STATUS_EVALUATOR_ERROR,
    "worktree_unavailable": SANDBOX_STATUS_EVALUATOR_ERROR,
    # __SLOT_SANDBOX_GRADE_RECEIPT_2026_08_24__ the evaluator could not be
    # proven to have produced this row — an evaluator-integrity error, never
    # a candidate verdict. MUST NOT map to CANDIDATE_FAIL: that would let a
    # forged/unauthenticated run read as "the candidate legitimately failed".
    "receipt_untrusted": SANDBOX_STATUS_EVALUATOR_ERROR,
    "timeout": SANDBOX_STATUS_CANDIDATE_TIMEOUT,
}


def _sandbox_status_enabled() -> bool:
    """True unless ``AGI_V8_SANDBOX_STATUS_ENABLED`` is explicitly disabled.

    Default ON (eval-integrity repair, not a new capability — brief for
    Round 6 track SB). Set to ``"false"``/``"0"`` to fall back to the
    pre-patch behaviour where ``status`` is always ``""``.
    """

    return (
        os.getenv("AGI_V8_SANDBOX_STATUS_ENABLED", "true").strip().lower()  # tier: T5
        not in ("false", "0")
    )


def _status_for_stub_reason(reason: str) -> str:
    """Map a canonical ``STUB_REASONS`` token to its ``status`` value.

    Raises on an unmapped reason rather than defaulting to ``""`` — an
    unmapped reason silently reading as "unmeasured" is exactly the P0 this
    slot exists to close.
    """

    return _STUB_REASON_TO_STATUS[reason]


assert STUB_REASONS <= set(_STUB_REASON_TO_STATUS), (
    "STUB_REASONS has a token with no status mapping: "
    f"{STUB_REASONS - set(_STUB_REASON_TO_STATUS)!r}"
)


_DEFAULT_IMAGE = "agi_v8_sandbox:latest"
_DEFAULT_TIMEOUT_SEC = 30.0
_MIN_TIMEOUT_SEC = 1.0
_MAX_TIMEOUT_SEC = 300.0
_DEFAULT_MEMORY_MB = 512
_MIN_MEMORY_MB = 64
_MAX_MEMORY_MB = 4096
_DEFAULT_PIDS_LIMIT = 64
_MIN_PIDS_LIMIT = 8
_MAX_PIDS_LIMIT = 1024
_MAX_SOURCE_BYTES = 1_000_000

_SAFE_IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,254}$")
_CONTAINER_ID_RE = re.compile(r"^[a-fA-F0-9]{12,64}$")

# __SLOT_SANDBOX_IMAGE_PIN_2026_08_19__ P1-f: transplant of the image-identity
# verification pattern in si_lanes/verify_isolation.py's
# ``_trusted_verify_image`` — a docker *tag* only names a mutable pointer
# (``docker tag`` can repoint ``agi_v8_sandbox:latest`` at any local image),
# so ``_image_exists`` (presence-only) cannot detect a substitution attack.
# ``AGI_V8_SWARM_SANDBOX_IMAGE_ID`` pins the immutable image ID the tag must
# resolve to. Unlike verify_isolation's hardcoded constant, this stays an
# env override — sandbox_runner's other knobs (image tag, memory, pids,
# timeout) are all env-configurable, and there is no single correct hardcoded
# ID here (the sandbox image is locally built, not shipped at a fixed
# digest). Default UNSET = pin not armed = current tag-presence-only
# behaviour, unchanged, plus a one-line warning recommending the pin be
# armed (see ``_image_id_trusted`` docstring). This mirrors this file's own
# precedent for tightening an *existing* check (``AGI_V8_SANDBOX_STATUS_ENABLED``,
# default ON) rather than gating a brand-new capability default-OFF — but the
# scout could not confirm this precise env name against a real docker image
# ID at write time, so it ships default-OFF-by-absence (unset = inert) and
# ARMED the moment an operator sets a value, rather than defaulting to a
# baked-in guess that could brick sandbox execution on image rebuild.
_IMAGE_ID_ENV = "AGI_V8_SWARM_SANDBOX_IMAGE_ID"
_IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_image_id_pin_warned = False

# __SLOT_SANDBOX_ENTRY_DIGEST_R17_2026_08_21__ R17 §6. The docker backend does
# NOT read ``docker/sandbox/entry.py`` from this checkout — the Dockerfile
# ``COPY``s it, so a docker run executes whatever harness was baked when the
# image was built. That makes evaluator repairs invisible on the docker path
# until a rebuild, and the image-ID pin does not help: a stale image keeps
# matching its own (stale) pin. 2026-08-21's non-interference repair is exactly
# such a change, so "source is fixed" stopped implying "runtime is fixed".
# This gate closes the inference: images built after that date carry a
# ``agi_v8.entry_sha256`` label, and an armed check refuses any image whose
# label does not equal this checkout's harness digest — a stale image becomes
# a named ``image_untrusted`` refusal instead of a silent old-harness run.
# Default-OFF because every image built before the label exists would fail it;
# arm it in the same round as the rebuild + pin refresh.
_ENTRY_DIGEST_ENV = "AGI_V8_SWARM_SANDBOX_ENTRY_DIGEST_ENABLED"
_ENTRY_DIGEST_LABEL = "agi_v8.entry_sha256"


def is_entry_digest_check_enabled() -> bool:
    """True iff the harness-digest check is armed (strict true/1)."""
    return os.environ.get(_ENTRY_DIGEST_ENV, "") in ("true", "1")  # tier: T9


# ---------------------------------------------------------------------------
# __SLOT_SANDBOX_GRADE_RECEIPT_2026_08_24__ 결과 행 **출처 인증** (T9, default-OFF)
#
# 🔴 이 슬롯이 닫는 것 (R20 적대검증 실측):
#   후보가 stdout 에 ``__GRADE__{"completed":true,"rc":0,...}`` 를 쓰면,
#   컨테이너 안 감독자는 그 **진술의 형태만** 검사하고 자기 token 으로 정식
#   receipt 를 써준다. 즉 오늘의 receipt 가 증명하는 것은 "신뢰 평가기가 독립
#   관측했다"가 아니라 "감독자가 후보 진술을 읽었다"이고, 호스트에는 둘을
#   구분할 근거가 **하나도 없다**.
#   이 사슬이 지금 안 도는 유일한 이유는 배포 이미지 라벨과 체크아웃 entry.py
#   해시가 **우연히** 불일치해서 ``_entry_digest_trusted`` 가 매 호출을
#   ``image_untrusted`` 로 거부하기 때문이다. 이미지를 재빌드하는 순간 그 벽은
#   사라진다 — 벽이 우연이면 벽이 아니다.
#
# 🔑 스킴(설계 정본: ox_reports/sandbox_receipt_design_20260824.md §3):
#   - 호스트가 호출마다 ``secrets.token_hex(16)`` nonce 를 발급(프로세스 내 보유,
#     원장 저장 불요 — 대조는 이 호출 스코프 안에서 끝난다).
#   - 키는 **젤 밖**(레포도 state/ 도 아닌 호스트 설정 디렉터리, 0600)에 산다.
#     젤 안에 두면 젤이 자기 채점 결과에 서명할 수 있으므로 방어가 0 이 된다.
#   - nonce+키는 argv/env 가 아니라 **stdin 첫 줄**로만 컨테이너에 내려간다
#     (docker 는 게이트 ON 일 때만 ``-i`` 를 붙인다). 하네스는 읽자마자 fd 0 을
#     ``/dev/null`` 로 덮어 어떤 자식도 잔여 바이트에 못 닿게 한다.
#   - 신뢰 앵커는 **연산자 allowlist**(역시 레포 밖): 검토를 마친 entry.py
#     sha256 목록. 하네스는 실행 중인 자기 파일 해시를 서명 대상에 넣으므로,
#     "소스 변조 → 정상 재빌드 → 라벨 일치" 라는 digest 게이트의 공백이 여기서
#     메워진다. digest 게이트는 폐기하지 않고 **병행**한다(라벨↔체크아웃 drift
#     포착이라는 별개 위생 기능).
#
# ⛔ OFF = byte-identical: 게이트가 꺼져 있으면 argv·subprocess kwargs·봉투
#    스키마·``SandboxTelemetry.as_dict()`` 키 집합이 이 슬롯 이전과 완전히 같다.
#
# ⚠️ 정직한 한계(§3.6): 잘 만든 위조 **진술**은 감독자가 그대로 서명해준다.
#    receipt 는 출처(어떤 하네스가·어떤 입력으로·어떤 호출에서)를 증명할 뿐
#    counts 의 진위를 보장하지 않는다. 진위는 후보를 별도 uid/ns 로 내리는
#    ``AGI_V8_SANDBOX_CANDIDATE_ISOLATION_ENABLED`` 축의 몫이고 직교한다.
#    젤이 jail 밖 임의 코드를 실행할 수 있다는 가정 아래서는 같은 uid 의 키
#    파일이 읽힌다 — 오늘의 신뢰경계 규율과 같은 선에서의 방어다.
# ---------------------------------------------------------------------------

# ⛔ 리터럴 표(계산 조합 금지 — 08-22 실측: 식별자를 계산으로 만들면 그걸 세는
# 게이트 감사가 눈을 잃는다). 아래 4개는 ``docker/sandbox/entry.py`` 에도
# **똑같이** 하드코딩돼 있고(그 파일은 레포를 import 하지 않는 standalone
# 이미지 엔트리다), 신규 테스트가 두 정의의 동일성을 기계로 고정한다.
_GRADE_RECEIPT_ENV = "AGI_V8_SANDBOX_GRADE_RECEIPT_ENABLED"
_RECEIPT_KEY_FILE_ENV = "AGI_V8_SANDBOX_RECEIPT_KEY_FILE"
_RECEIPT_ALLOWLIST_FILE_ENV = "AGI_V8_SANDBOX_RECEIPT_HARNESS_ALLOWLIST_FILE"
_RECEIPT_PROTOCOL = "AGI_V8_GRADE_RECEIPT_V1"
_RECEIPT_MAC_DOMAIN = "agi_v8/grade_receipt/v1"

_RECEIPT_DEFAULT_KEY_FILE = "~/.config/agi_v8/sandbox_receipt_keys"
_RECEIPT_DEFAULT_ALLOWLIST_FILE = "~/.config/agi_v8/sandbox_harness_allowlist"

# Signed field list AND ORDER. Mirrored verbatim by
# ``entry.GRADE_RECEIPT_SIGNED_FIELDS``. Every value is a string (ints as
# decimal ``str``, unmeasured as ``""``) — floats (wall_clock/rss) stay OUT of
# the MAC because their serialisation is not stable across interpreters.
_RECEIPT_SIGNED_FIELDS: tuple[str, ...] = (
    "protocol",
    "key_id",
    "nonce",
    "code_sha256",
    "test_sha256",
    "cand_stdout_sha256",
    "exit_code",
    "cand_rc",
    "sup_child_rc",
    "runner_rc",
    "pytest_passed",
    "pytest_failed",
    "collect_errors",
    "pytest_status",
    "harness_self_sha256",
)

_RECEIPT_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_RECEIPT_KEY_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_RECEIPT_NONCE_RE = re.compile(r"^[0-9a-f]{32}$")

# Canonical refusal tokens. All of them surface as ``stub_reason
# ="receipt_untrusted"``; the token itself is the ``stderr_tail`` prefix so
# "안 했다"(gate off) / "못 했다"(named refusal) never collapse into one bit.
RECEIPT_REFUSALS: frozenset[str] = frozenset(
    {
        "receipt_key_unavailable",     # 키 파일 부재·판독 불가·빈 파일
        "receipt_key_in_jail",         # 키/allowlist 가 레포(=젤 쓰기 영역) 안
        "receipt_key_permissive",      # 0600 이 아님 (그룹/타인 판독 가능)
        "receipt_allowlist_unavailable",
        "receipt_missing",             # 봉투에 grade_receipt 없음 (구 하네스)
        "receipt_malformed",
        "receipt_protocol_mismatch",
        "receipt_key_unknown",
        "receipt_mac_mismatch",
        "receipt_context_mismatch",    # nonce/입력 해시가 이 호출의 것이 아님
        "receipt_envelope_mismatch",   # 서명값과 봉투 숫자가 불일치
        "receipt_harness_not_allowlisted",
    }
)


def is_grade_receipt_enabled() -> bool:
    """True iff ``AGI_V8_SANDBOX_GRADE_RECEIPT_ENABLED`` is armed.

    Strict ``"true"``/``"1"`` (T9 house rule — no case folding, no
    whitespace tolerance): a security gate must not be armed by accident,
    and must not read as armed when it is not.
    """
    return os.environ.get(_GRADE_RECEIPT_ENV, "") in ("true", "1")  # tier: T9


def _repo_root_dir() -> Path:
    """This checkout's root — the region a compromised gel can write to."""
    return Path(__file__).resolve().parents[1]


def _receipt_path_from_env(env_name: str, default: str) -> Path:
    raw = os.environ.get(env_name, "").strip()  # tier: T5
    return Path(raw or default).expanduser()


def _receipt_path_outside_jail(path: Path) -> bool:
    """False when *path* lives inside this checkout.

    The whole point of the out-of-jail anchor: if the signing key or the
    harness allowlist sat in the repo, "source tamper + honest rebuild"
    would simply update them too and the anchor would prove nothing.
    """
    try:
        resolved = path.resolve()
    except OSError as _ff_exc:
        _swallowed(_ff_exc, site="core.sandbox_runner._receipt_path_outside_jail",
                   category="verify")
        return False
    try:
        resolved.relative_to(_repo_root_dir())
    except ValueError:
        return True
    return False


def _load_receipt_keys() -> tuple[dict[str, str], str]:
    """Read the out-of-jail key file. Returns ``(keys, error)``.

    Format (one key per line, first line = the ACTIVE signing key, every
    line accepted for verification so a rotation has an overlap window)::

        # comment
        k1<TAB>0123...64hex

    Every failure is a refusal, never a lenient pass — "cannot prove" must
    not be cheaper than proving.
    """
    path = _receipt_path_from_env(_RECEIPT_KEY_FILE_ENV, _RECEIPT_DEFAULT_KEY_FILE)
    if not _receipt_path_outside_jail(path):
        return {}, (
            f"receipt_key_in_jail: {_RECEIPT_KEY_FILE_ENV} points inside this "
            "checkout; the signing key must live outside the gel's write region"
        )
    try:
        stat = path.stat()
        raw = path.read_text(encoding="utf-8")
    except OSError as _ff_exc:
        _swallowed(_ff_exc, site="core.sandbox_runner._load_receipt_keys:read",
                   category="verify")
        return {}, (
            "receipt_key_unavailable: sandbox receipt key file is missing or "
            f"unreadable (set {_RECEIPT_KEY_FILE_ENV}, mode 0600)"
        )
    if stat.st_mode & 0o077:
        return {}, (
            "receipt_key_permissive: sandbox receipt key file is readable "
            "beyond its owner; chmod 600 it"
        )
    keys: dict[str, str] = {}
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split()
        if len(parts) != 2:
            continue
        key_id, key_hex = parts[0], parts[1].lower()
        if not _RECEIPT_KEY_ID_RE.fullmatch(key_id):
            continue
        if not _RECEIPT_HEX64_RE.fullmatch(key_hex):
            continue
        keys.setdefault(key_id, key_hex)
    if not keys:
        return {}, (
            "receipt_key_unavailable: sandbox receipt key file carries no "
            "usable '<key_id> <64 hex>' line"
        )
    return keys, ""


def _load_harness_allowlist() -> tuple[frozenset[str], str]:
    """Read the out-of-jail list of reviewed ``entry.py`` digests."""
    path = _receipt_path_from_env(
        _RECEIPT_ALLOWLIST_FILE_ENV, _RECEIPT_DEFAULT_ALLOWLIST_FILE
    )
    if not _receipt_path_outside_jail(path):
        return frozenset(), (
            f"receipt_key_in_jail: {_RECEIPT_ALLOWLIST_FILE_ENV} points inside "
            "this checkout; the harness allowlist must live outside it"
        )
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as _ff_exc:
        _swallowed(_ff_exc, site="core.sandbox_runner._load_harness_allowlist:read",
                   category="verify")
        return frozenset(), (
            "receipt_allowlist_unavailable: sandbox harness allowlist is "
            f"missing or unreadable (set {_RECEIPT_ALLOWLIST_FILE_ENV})"
        )
    digests = set()
    for line in raw.splitlines():
        stripped = line.split("#", 1)[0].strip().lower()
        if _RECEIPT_HEX64_RE.fullmatch(stripped):
            digests.add(stripped)
    if not digests:
        return frozenset(), (
            "receipt_allowlist_unavailable: sandbox harness allowlist carries "
            "no sha256 line"
        )
    return frozenset(digests), ""


def _receipt_mac(key_hex: str, values: dict[str, str]) -> str:
    payload = _RECEIPT_MAC_DOMAIN + "\n" + "\n".join(
        values[name] for name in _RECEIPT_SIGNED_FIELDS
    )
    return hmac.new(
        bytes.fromhex(key_hex), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _build_receipt_context(
    *, code: str, test: str | None
) -> tuple[dict[str, Any] | None, str]:
    """Provision one call's receipt material, or refuse with a named reason.

    Called BEFORE any container is started, so a missing key never even
    probes docker: an unprovable run must cost nothing and produce nothing.
    """
    keys, key_error = _load_receipt_keys()
    if key_error:
        return None, key_error
    allowlist, allow_error = _load_harness_allowlist()
    if allow_error:
        return None, allow_error
    active_id = next(iter(keys))
    nonce = secrets.token_hex(16)
    return (
        {
            "nonce": nonce,
            "key_id": active_id,
            "keys": keys,
            "allowlist": allowlist,
            "code_sha256": hashlib.sha256(code.encode("utf-8")).hexdigest(),
            "test_sha256": (
                "" if test is None
                else hashlib.sha256(test.encode("utf-8")).hexdigest()
            ),
            # stdin 첫 줄. ⛔ 로그·telemetry·원장에 절대 싣지 않는다.
            "stdin_line": (
                f"{_RECEIPT_PROTOCOL} {nonce} {active_id} {keys[active_id]}\n"
            ),
        },
        "",
    )


def _verify_grade_receipt(
    envelope: dict[str, Any], ctx: dict[str, Any]
) -> str:
    """Return ``""`` when the row is authenticated, else a named refusal.

    Check order is deliberate: MAC before nonce, because a REPLAYED receipt
    from an earlier honest run carries a valid MAC — it is the nonce/input
    binding that kills it, and reporting "mac_mismatch" for a replay would
    misname the attack.
    """
    receipt = envelope.get("grade_receipt")
    if not isinstance(receipt, dict):
        return (
            "receipt_missing: harness produced no signed grade receipt "
            "(pre-receipt image, or the stdin key channel never arrived)"
        )
    values: dict[str, str] = {}
    for name in _RECEIPT_SIGNED_FIELDS:
        value = receipt.get(name)
        if not isinstance(value, str):
            return f"receipt_malformed: signed field {name!r} is missing or not a string"
        values[name] = value
    mac = receipt.get("mac")
    if not isinstance(mac, str) or not _RECEIPT_HEX64_RE.fullmatch(mac):
        return "receipt_malformed: receipt carries no well-formed mac"
    if values["protocol"] != _RECEIPT_PROTOCOL:
        return "receipt_protocol_mismatch: unknown grade-receipt protocol"
    key_hex = ctx["keys"].get(values["key_id"])
    if key_hex is None:
        return "receipt_key_unknown: receipt key_id is not in the key file"
    if not hmac.compare_digest(_receipt_mac(key_hex, values), mac):
        return "receipt_mac_mismatch: receipt signature does not verify"
    if not _RECEIPT_NONCE_RE.fullmatch(values["nonce"]):
        # compare_digest 는 비-ASCII str 에 TypeError 를 낸다 — 후보가 만질 수
        # 있는 값이므로 형태를 먼저 고정한다.
        return "receipt_malformed: receipt nonce is not 32 hex chars"
    if not hmac.compare_digest(values["nonce"], ctx["nonce"]):
        return (
            "receipt_context_mismatch: receipt nonce was not issued for this "
            "call (replay or splice)"
        )
    if values["code_sha256"] != ctx["code_sha256"]:
        return "receipt_context_mismatch: receipt code digest is not the mounted code"
    if values["test_sha256"] != ctx["test_sha256"]:
        return "receipt_context_mismatch: receipt test digest is not the mounted test"
    # 봉투 숫자가 서명 밖에서 바뀌었는지 — 서명은 문자열이므로 문자열로 댄다
    # (봉투가 bool 을 실어 보내면 ``str(True)`` 가 되어 여기서 걸린다).
    for envelope_key, signed_key in (
        ("exit_code", "exit_code"),
        ("pytest_passed", "pytest_passed"),
        ("pytest_failed", "pytest_failed"),
        ("pytest_status", "pytest_status"),
    ):
        if str(envelope.get(envelope_key)) != values[signed_key]:
            return (
                f"receipt_envelope_mismatch: envelope {envelope_key!r} disagrees "
                "with the signed receipt"
            )
    if values["harness_self_sha256"].lower() not in ctx["allowlist"]:
        return (
            "receipt_harness_not_allowlisted: the harness that produced this "
            "row is not an operator-reviewed entry.py"
        )
    return ""


@dataclass(frozen=True)
class SandboxTelemetry:
    """Structured execution telemetry returned by :func:`run`.

    Frozen dataclass — callers should treat instances as immutable. When
    ``stub`` is True the numeric fields may be zero / -1 placeholders;
    inspect ``stub_reason`` for the cause.
    """

    schema_version: str = SCHEMA_VERSION
    stub: bool = False
    # One of: "" plus the canonical tokens in STUB_REASONS.
    stub_reason: str = ""
    # __SLOT_SANDBOX_STATUS_2026_08_17__ additive, compatibility-preserving:
    # one of "" (status computation gated OFF) plus SANDBOX_STATUSES. Unlike
    # ``stub`` (a single bit) this disaggregates "not configured" from
    # "infra unavailable" from "evaluator errored" from "candidate timed
    # out" from "candidate ran and passed/failed" — see the gate slot above
    # for why. Never read ``stub``/``stub_reason`` alone to mean "no
    # problem"; consult ``status`` when the gate is ON.
    status: str = ""
    exit_code: int = -1
    wall_clock_sec: float = 0.0
    peak_rss_mb: float = 0.0
    ruff_warnings: int = 0
    ruff_errors: int = 0
    mypy_errors: int = 0
    pytest_passed: int = 0
    pytest_failed: int = 0
    stderr_tail: str = ""
    image: str = ""
    lane_id: str = ""
    # __SLOT_SANDBOX_BACKEND_PROVENANCE_2026_09_04__ The old generic
    # ``container_failed`` token erased whether Docker or bwrap emitted the
    # outer rc.  Keep one closed structural token; never persist backend
    # stderr or candidate bytes.  Empty means no backend was selected.
    backend: str = ""
    # __SLOT_SANDBOX_GRADE_RECEIPT_2026_08_24__ True ONLY when the
    # grade-receipt gate was armed AND this row's HMAC receipt verified
    # against this call's nonce, the mounted input digests and the operator
    # harness allowlist. Never set by ``_stub`` — an unauthenticated run is
    # a refusal (``stub_reason="receipt_untrusted"``), not a False flag on a
    # real result. See ``as_dict`` for why the key is absent when False.
    grade_receipt_verified: bool = False

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        # ⛔ 게이트 OFF = 소비자 스키마에 키가 **추가되지도 않는다**. 이 아크에서
        # 네 번 밟은 계약이라 필드를 늘리는 대신 값이 참일 때만 노출한다 —
        # OFF 이거나 stub 이면 이 슬롯 이전과 키 집합이 완전히 같다.
        if not data.get("grade_receipt_verified"):
            data.pop("grade_receipt_verified", None)
        if not data.get("backend"):
            data.pop("backend", None)
        return data


# ---------------------------------------------------------------------------
# Env-knob helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _SandboxRunConfig:
    image: str
    timeout_sec: float
    memory_mb: int
    pids_limit: int


def is_sandbox_enabled() -> bool:
    """True iff ``AGI_V8_SWARM_SANDBOX_ENABLED`` is set to the literal
    string ``true`` (case-insensitive, surrounding whitespace ignored)."""

    return (
        os.getenv("AGI_V8_SWARM_SANDBOX_ENABLED", "false").strip().lower()  # tier: T5
        == "true"
    )


def _subprocess_env() -> dict[str, str]:
    """Minimal environment for non-Docker helper subprocesses.

    The sandboxed workload receives no host environment through this runner.
    Docker daemon authority is deliberately *not* present here: git/tar and a
    bwrap child must never inherit the rootless daemon socket merely because
    the same runner also has a Docker backend.  Docker-only callers use
    :func:`_docker_subprocess_env` below.
    """

    return {
        "PATH": os.defpath,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }


_PRESEAL_IDENTITY_ENV = "AGI_V8_VERIFY_GATE_PRESEAL_GIT_IDENT_ENABLED"


class _DockerEndpointAuthorityUnavailable(ValueError):
    """The armed host-captured Docker endpoint cannot be proven here."""


def _preseal_identity_enabled() -> bool:
    return os.environ.get(_PRESEAL_IDENTITY_ENV, "") in ("true", "1")


def _docker_subprocess_env() -> dict[str, str]:
    """Minimal Docker CLI environment with optional pre-seal authority.

    Gate OFF preserves the historical PATH/LANG-only client environment and
    ignores every ambient Docker variable.  Gate ON reuses the canonical
    pre-seal consumer from ``verify_isolation``: that consumer accepts only
    ``unix:///run/user/<captured-host-uid>/docker.sock`` and rechecks its
    path/inode/type/owner/mode against the host-side capture.  HOME, XDG,
    Docker contexts, TLS variables, and arbitrary caller environment never
    cross this boundary.
    """

    clean = _subprocess_env()
    if not _preseal_identity_enabled():
        return clean
    try:
        # Lazy import is intentional. verify_gate imports verify_isolation only
        # at its execution seam, so importing it here after the pre-seal gate
        # is armed is cycle-safe and keeps the gate-OFF path byte-equivalent.
        from agi_v8_1.si_lanes import verify_isolation

        trusted = verify_isolation._docker_cli_env()
    except Exception as exc:  # noqa: BLE001 - identity uncertainty rejects
        raise _DockerEndpointAuthorityUnavailable(
            "pre-seal Docker endpoint authority unavailable"
        ) from exc
    if not isinstance(trusted, dict) or set(trusted) - {"PATH", "DOCKER_HOST"}:
        raise _DockerEndpointAuthorityUnavailable(
            "pre-seal Docker endpoint authority shape invalid"
        )
    endpoint = trusted.get("DOCKER_HOST")
    if endpoint is not None:
        if not isinstance(endpoint, str) or not endpoint:
            raise _DockerEndpointAuthorityUnavailable(
                "pre-seal Docker endpoint authority value invalid"
            )
        clean["DOCKER_HOST"] = endpoint
    return clean


def _preseal_identity_crossed() -> bool:
    """Whether the validated capture came from a different user namespace."""

    if not _preseal_identity_enabled():
        return False
    try:
        from agi_v8_1.si_lanes import verify_isolation

        context = verify_isolation._preseal_identity_context()
    except Exception as exc:  # noqa: BLE001 - identity uncertainty rejects
        raise _DockerEndpointAuthorityUnavailable(
            "pre-seal namespace authority unavailable"
        ) from exc
    if context is None or type(getattr(context, "crossed", None)) is not bool:
        raise _DockerEndpointAuthorityUnavailable(
            "pre-seal namespace authority unavailable"
        )
    return context.crossed


def _tail(text: object, limit: int = 1024) -> str:
    return format_text_for_sink(
        text,
        max_chars=limit,
        one_line=False,
        keep="tail",
    )


def _docker_executable() -> str | None:
    found = shutil.which("docker")
    if found is None:
        return None
    try:
        path = Path(found).resolve(strict=True)
    except OSError as _ff_exc:
        _swallowed(_ff_exc, site="core.sandbox_runner._docker_executable:157", category="verify")
        return None
    if not path.is_file():
        return None
    return str(path)


def _docker_executable_for_run() -> str | None:
    """Resolve Docker for this runner's current trust posture.

    The shared ``_docker_executable`` helper keeps its historical non-raising
    PATH lookup contract for other isolation consumers.  An armed pre-seal
    run is stronger: use the canonical root-owned binary consumer bound to the
    same host capture as the rootless endpoint, and reject uncertainty.
    """

    if not _preseal_identity_enabled():
        return _docker_executable()
    try:
        from agi_v8_1.si_lanes import verify_isolation

        trusted = verify_isolation._trusted_docker_path()
    except Exception as exc:  # noqa: BLE001 - identity uncertainty rejects
        raise _DockerEndpointAuthorityUnavailable(
            "pre-seal Docker executable authority unavailable"
        ) from exc
    if not isinstance(trusted, str) or not trusted:
        raise _DockerEndpointAuthorityUnavailable(
            "pre-seal Docker executable authority unavailable"
        )
    return trusted


def _docker_available() -> bool:
    return _docker_executable() is not None


def _docker_backend_available(docker_path: str) -> bool:
    """Return True when the Docker client can reach a server backend."""

    try:
        result = subprocess.run(
            [docker_path, "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            timeout=5,
            text=True,
            env=_subprocess_env(),
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as _ff_exc:
        _swallowed(_ff_exc, site="core.sandbox_runner._docker_backend_available:179", category="verify")
        return False
    except Exception as _ff_exc:  # pragma: no cover - defensive: never propagate
        _swallowed(_ff_exc, site="core.sandbox_runner._docker_backend_available:181", category="verify")
        return False
    return result.returncode == 0


def _docker_backend_available_for_run(docker_path: str) -> bool:
    """Runner-private daemon probe using the captured Docker authority.

    The public-ish helper above is reused by isolation consumers whose actual
    Docker spawn retains its historical environment.  Changing only that
    shared probe would split probe and execution across two daemons.  This
    wrapper is therefore used solely by :func:`run`, whose eventual Docker
    spawn uses the same :func:`_docker_subprocess_env` authority.
    """

    try:
        result = subprocess.run(
            [docker_path, "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            timeout=5,
            text=True,
            env=_docker_subprocess_env(),
        )
    except _DockerEndpointAuthorityUnavailable:
        raise
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        _swallowed(
            exc,
            site="core.sandbox_runner._docker_backend_available_for_run",
            category="verify",
        )
        return False
    except Exception as exc:  # pragma: no cover - defensive: never propagate
        _swallowed(
            exc,
            site="core.sandbox_runner._docker_backend_available_for_run",
            category="verify",
        )
        return False
    return result.returncode == 0


# ---------------------------------------------------------------------------
# __SLOT_SANDBOX_BWRAP_2026_08_16__ bwrap isolation backend (default-OFF).
#
# Priority: docker available → the docker path above runs UNCHANGED, this
# backend is never consulted. Only when docker itself is unavailable
# (executable missing or its backend unreachable) AND this gate is ON does
# ``run()`` attempt bwrap. If bwrap is also unavailable, ``run()`` refuses to
# execute unisolated (stub_reason="no_isolation_available") — it never falls
# through to a bare subprocess. Gate OFF (default) → none of this code is
# evaluated; the pre-existing docker_missing/docker_unavailable/image_missing
# stub sequence is byte-identical to before this slot existed.
# ---------------------------------------------------------------------------

_BWRAP_GATE_ENV = "AGI_V8_SANDBOX_BWRAP_ENABLED"

# The standalone harness (Worker A contract) that already ships for the
# docker image — self-contained stdlib-only script, no repo imports, safe to
# run directly under bwrap without a container image. Path is computed once
# from this file's own location, never from caller/env input.
_BWRAP_ENTRY_PATH = (
    Path(__file__).resolve().parents[1] / "docker" / "sandbox" / "entry.py"
)


def is_bwrap_isolation_enabled() -> bool:
    """True iff ``AGI_V8_SANDBOX_BWRAP_ENABLED`` is the literal string
    ``true``/``1`` (case-insensitive, surrounding whitespace ignored).

    Default-OFF. When OFF, :func:`run` never calls :func:`_bwrap_executable`
    or builds a bwrap argv — the docker-unavailable path is byte-identical
    to the pre-2026-08-16 behaviour."""

    return (
        os.getenv(_BWRAP_GATE_ENV, "false").strip().lower() == "true"  # tier: T9
    )


def _bwrap_executable() -> str | None:
    """Resolve the ``bwrap`` binary the same defensive way ``docker`` is
    resolved above: PATH lookup + symlink resolution + is_file check. Never
    raises; absence/any OSError maps to ``None``."""

    found = shutil.which("bwrap")
    if found is None:
        return None
    try:
        path = Path(found).resolve(strict=True)
    except OSError as _ff_exc:
        _swallowed(_ff_exc, site="core.sandbox_runner._bwrap_executable:bwrap_resolve", category="verify")
        return None
    if not path.is_file():
        return None
    return str(path)


# __SLOT_R9_T2_2026_08_17__ — see _build_bwrap_argv's ``--tmpfs /run``
# docstring entry above for the full attack this closes. Default-ON
# (house-rule exception: this is a defense closing a live-verified escape,
# not a new capability — same posture as ``AGI_V8_SANDBOX_STATUS_ENABLED``
# above). Setting it to "false"/"0" reproduces the pre-patch argv exactly.
_MASK_RUNTIME_SOCKETS_ENV = "AGI_V8_SANDBOX_MASK_RUNTIME_SOCKETS"


def is_mask_runtime_sockets_enabled() -> bool:
    """True unless ``AGI_V8_SANDBOX_MASK_RUNTIME_SOCKETS`` is explicitly
    disabled (``"false"``/``"0"``, case-insensitive, whitespace-trimmed).

    Default ON. When OFF, :func:`_build_bwrap_argv` omits the ``--tmpfs
    /run`` mask and its argv is byte-identical to before this slot existed
    — including the docker-socket exposure this slot exists to close."""

    return (
        os.getenv(_MASK_RUNTIME_SOCKETS_ENV, "true").strip().lower()  # tier: T9
        not in ("false", "0")
    )


def _build_bwrap_argv(
    *,
    bwrap_path: str,
    entry_path: str,
    input_dir: Path,
    work_dir: Path,
    python_executable: str | None = None,
    repo_dir: Path | None = None,
) -> list[str]:
    """Assemble the ``bwrap`` argv that runs the harness in isolation.

    Every flag here is hardcoded by this module — the caller supplies only
    filesystem paths this module itself created (``input_dir``/``work_dir``
    live inside ``run()``'s own ``tempfile.TemporaryDirectory``); no LLM or
    candidate-code output ever reaches this argv.

    Isolation contract:
      - ``--unshare-net``/``--unshare-pid``/``--unshare-ipc``/``--unshare-uts``/
        ``--unshare-cgroup-try``: no outbound network, no visibility into (or
        signaling of) host processes.
      - ``--die-with-parent``: the sandboxed process is killed if this
        subprocess (or ITS parent) dies — no orphaned survivors.
      - ``--new-session``: detach from the controlling terminal/session.
      - ``--ro-bind / /``: the host root is visible so the interpreter and
        its stdlib resolve, but READ-ONLY — nothing outside ``work_dir`` can
        be written or deleted. A bind mounted directly at ``/`` shadows any
        NEW top-level directory created afterward (empirically verified:
        ``bwrap --ro-bind / / --dir /sandbox`` fails ``EROFS`` even though
        ``--dir`` runs after the bind, because the mkdir lands on the real,
        read-only host filesystem) — that is WHY the input/entry binds below
        live under ``/tmp`` (a mountpoint that already exists on the host,
        so mounting a *fresh* tmpfs there needs no directory creation)
        rather than at a synthetic top-level ``/sandbox``.
      - ``--tmpfs /tmp``: fresh, writable, and (per the point above) mounts
        cleanly even under the read-only root bind. ``--dir /tmp/sandbox``
        then succeeds because it is creating a directory INSIDE that fresh
        tmpfs, not on the read-only host filesystem.
      - ``--bind work_dir /tmp/work``: the ONLY writable *host* location, and
        it is a path this module created inside its own ``run()`` tempdir —
        never a path outside the jail.
      - ``--ro-bind input_dir /tmp/sandbox/input`` +
        ``--ro-bind entry_path /tmp/sandbox/entry.py``: candidate code/test
        and the harness script, both read-only. The harness
        (``docker/sandbox/entry.py``) reads its input directory from
        ``AGI_V8_SANDBOX_INPUT_DIR`` (default ``/sandbox/input``, the
        docker image's baked-in mount point — untouched here, so the
        docker backend stays byte-identical); the caller sets that env var
        to this nested path for the bwrap-launched process only.
      - ``repo_dir`` (optional, ``__SLOT_SANDBOX_WORKTREE_2026_08_16__``):
        when given, an additional ``--ro-bind repo_dir /tmp/sandbox/repo`` —
        a read-only snapshot of the live repo at HEAD, nested under ``/tmp``
        for the same reason ``input_dir``/``entry_path`` are (see above).
        The caller pairs this with ``PYTHONPATH=/tmp/sandbox/repo`` in the
        launched process's env so candidate code that does
        ``from agi_v8_1.x import y`` (sibling-package imports, unavoidable
        for any whole-file replacement of a real repo module) resolves
        against the real package instead of crashing with
        ``ModuleNotFoundError`` in the standalone harness's empty
        namespace. Omitted (``None``, the default) → argv is byte-identical
        to before this parameter existed.
      - ``--tmpfs /run`` (``__SLOT_R9_T2_2026_08_17__``, gated behind
        :func:`is_mask_runtime_sockets_enabled`, default ON): masks the
        host's ``/run`` directory — where the Docker daemon's UNIX control
        socket lives (``/run/docker.sock``; ``/var/run`` is a symlink to
        ``/run`` on Linux) — with a fresh, empty tmpfs. ``--unshare-net``
        only isolates the IP stack; a network namespace does NOT isolate
        filesystem-path UNIX sockets, and ``--ro-bind / /`` above brings the
        host's ``/run`` (and therefore the socket) into the jail verbatim.
        Empirically verified (Round 9 T2 audit, 2026-08-17): without this
        mask, code running under this exact argv can ``connect()`` to
        ``/run/docker.sock`` and get ``HTTP/1.1 200 OK`` from the Docker
        API — on a host where the invoking user is in the ``docker`` group
        and the daemon is rootful, that is root-equivalent host access from
        inside a jail whose entire purpose is to deny it, including a path
        to the killswitch sentinel directory the docker daemon can write as
        root. MUST be placed after ``--ro-bind / /`` (bwrap argv order is
        significant — a later mount shadows an earlier one at the same
        path, never the reverse) and before nothing in particular otherwise;
        placed here, directly after the root bind, so it reads as "root
        comes in, then /run is immediately re-sealed" ahead of the
        unrelated ``--proc``/``--dev``/``--tmpfs /tmp`` mounts below. Does
        NOT touch ``/tmp`` (a distinct mountpoint) so the harness/work/input
        binds under ``/tmp/sandbox`` and ``/tmp/work`` are unaffected, and
        does not break interpreter/stdlib resolution (verified: ``python3
        -c "import json"`` still succeeds with this mask in place). OFF
        (``AGI_V8_SANDBOX_MASK_RUNTIME_SOCKETS=false``) → this flag pair is
        omitted and argv is byte-identical to before this slot existed.
    """

    python_bin = python_executable or sys.executable
    argv = [
        bwrap_path,
        "--unshare-net",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--unshare-cgroup-try",
        "--die-with-parent",
        "--new-session",
        "--ro-bind", "/", "/",
    ]
    if is_mask_runtime_sockets_enabled():
        argv += ["--tmpfs", "/run"]
    argv += [
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--dir", "/tmp/sandbox",
        "--bind", str(work_dir), "/tmp/work",
        "--ro-bind", str(input_dir), "/tmp/sandbox/input",
        "--ro-bind", entry_path, "/tmp/sandbox/entry.py",
    ]
    if repo_dir is not None:
        argv += ["--ro-bind", str(repo_dir), "/tmp/sandbox/repo"]
    argv += [
        "--chdir", "/tmp/work",
        python_bin,
        "/tmp/sandbox/entry.py",
    ]
    return argv


# Nested (not top-level) so a fresh, already-existing ``/tmp`` mountpoint can
# host it under the read-only root bind — see :func:`_build_bwrap_argv`'s
# docstring for why a synthetic top-level ``/sandbox`` cannot be created.
_BWRAP_INPUT_DIR_ENV = "AGI_V8_SANDBOX_INPUT_DIR"
_BWRAP_INPUT_DIR_VALUE = "/tmp/sandbox/input"


# ---------------------------------------------------------------------------
# __SLOT_SANDBOX_WORKTREE_2026_08_16__ repo-context execution (default-OFF).
#
# Root cause this fixes (agi_v8_1 process-bench triage, 2026-08-16): the
# standalone harness above (Worker A contract, ``docker/sandbox/entry.py``)
# deliberately mounts ONLY the candidate's ``code.py`` — no repo imports, by
# design, so the image needs no repo checkout baked in. But every candidate
# breadth_select/F2 actually scores is a whole-file replacement of a REAL
# repo module (e.g. ``core/judge.py``), and every such module imports sibling
# packages at its own top level (``from agi_v8_1.policy.fail_fast import
# swallowed`` etc.) — imports the candidate text carries verbatim. Inside the
# standalone harness those imports have nothing to resolve against, so EVERY
# such candidate dies with ``ModuleNotFoundError`` on the first line executed
# (wall_clock ~0.03s — an import crash, not a measurement), collapsing good
# and broken candidates onto the identical -750.0 score. See
# ``exec_select_log.jsonl``: 24/24 score events, all exactly -750.0.
#
# Fix: when armed, take a READ-ONLY, ephemeral snapshot of the live repo at
# HEAD via ``git archive`` (never ``git worktree add`` — that registers an
# entry in the live repo's own ``.git/worktrees/`` metadata; ``git archive``
# reads committed objects only and touches no repo state at all) and bind it
# read-only into the sandbox alongside the candidate, with ``PYTHONPATH`` set
# so ``import agi_v8_1.*`` resolves against the REAL (untouched) sibling
# modules. The candidate's OWN logic still executes in the harness's minimal,
# resource-capped, network-off jail — only its imports now have somewhere to
# land. ``.env`` and any other untracked/gitignored file are never included
# (``git archive`` only ever emits tracked blobs at the given commit).
#
# Gate OFF (default) → ``_ensure_repo_snapshot`` is never called, ``run()``'s
# docker/bwrap argv assembly is byte-identical to before this slot existed.
# ---------------------------------------------------------------------------

_WORKTREE_GATE_ENV = "AGI_V8_SANDBOX_WORKTREE_ENABLED"

_snapshot_lock = threading.Lock()
_snapshot_dir: str | None = None  # lazily built once per process, then reused


def is_sandbox_worktree_enabled() -> bool:
    """True iff ``AGI_V8_SANDBOX_WORKTREE_ENABLED`` is the literal string
    ``true``/``1`` (case-insensitive, surrounding whitespace ignored).

    Default-OFF. When OFF, :func:`run` never calls :func:`_ensure_repo_snapshot`
    and the docker/bwrap argv it assembles is byte-identical to before this
    gate existed."""

    return (
        os.getenv(_WORKTREE_GATE_ENV, "false").strip().lower() == "true"  # tier: T9
    )


def _repo_root() -> Path | None:
    """Find the repo root by walking up from this file looking for a
    ``.git`` entry (a directory in a normal checkout, a file in a linked
    worktree — either way ``.exists()`` is true for both). Never raises —
    including under ``AGI_V8_STRICT_FAIL_FAST``: routed through
    :func:`policy.fail_fast.record_critical_failure` rather than plain
    ``swallowed``, since (unlike an ordinary recovery path) this function's
    caller (:func:`_ensure_repo_snapshot`) makes the same hard 'never raise,
    fail-closed to a stub instead' promise this one does — a strict-mode
    escape here would silently defeat that downstream guarantee too."""

    here = Path(__file__).resolve()
    for candidate in (here.parent, *here.parents):
        try:
            if (candidate / ".git").exists():
                return candidate
        except OSError as _ff_exc:  # pragma: no cover - defensive
            record_critical_failure(_ff_exc, site="core.sandbox_runner._repo_root:git_probe", category="verify")
            return None
    return None


def _cleanup_repo_snapshot(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)


def _cleanup_repo_snapshot_from_seal(path: str) -> None:
    """Release a snapshot owned by a normally completing sealed child."""

    global _snapshot_dir
    try:
        shutil.rmtree(path)
    except FileNotFoundError as exc:
        record_critical_failure(
            exc,
            site="core.sandbox_runner._cleanup_repo_snapshot_from_seal:absent",
            category="persist",
        )
    except OSError as exc:
        record_critical_failure(
            exc,
            site="core.sandbox_runner._cleanup_repo_snapshot_from_seal",
            category="persist",
        )
        return
    if Path(path).exists():
        record_critical_failure(
            RuntimeError("sealed repo snapshot still exists after cleanup"),
            site="core.sandbox_runner._cleanup_repo_snapshot_from_seal:residue",
            category="persist",
        )
        return
    if _snapshot_dir == path:
        _snapshot_dir = None


def _ensure_repo_snapshot() -> Path | None:
    """Build (once per process; cached) a read-only ``git archive`` snapshot
    of the live repo at HEAD, laid out as ``<tmp>/agi_v8_1/...`` so
    ``PYTHONPATH=<tmp>`` makes ``import agi_v8_1`` resolve against it.

    Never raises — including under ``AGI_V8_STRICT_FAIL_FAST`` — every
    failure (no ``.git`` ancestor, missing ``git``/``tar``, archive/extract
    failure, timeout) returns ``None`` and the caller maps that to the
    ``worktree_unavailable`` stub (fail-closed: this gate never silently
    falls back to the standalone, import-broken path). Every recorded
    failure below routes through :func:`policy.fail_fast.record_critical_failure`
    rather than plain ``swallowed`` precisely so that strict mode's re-raise
    (a deliberate debugging aid elsewhere in this module) cannot defeat this
    function's own stronger promise to its caller."""

    global _snapshot_dir
    with _snapshot_lock:
        if _snapshot_dir is not None:
            cached = Path(_snapshot_dir)
            if (cached / "agi_v8_1" / "__init__.py").is_file():
                return cached
            _snapshot_dir = None  # went missing under us; rebuild once below

        repo_root = _repo_root()
        if repo_root is None:
            return None
        git_path = shutil.which("git")
        tar_path = shutil.which("tar")
        if git_path is None or tar_path is None:
            return None

        try:
            dest_parent = Path(tempfile.mkdtemp(prefix="r16_sandbox_repo_"))
            # mkdtemp defaults to 0700 (owner-only). The docker backend runs
            # the sandboxed process as a fixed, unrelated uid (65532:65532,
            # never this process's own uid), so without world read+traverse
            # the bind mount resolves to an unreadable/empty directory
            # inside the container. Widening this temp directory's own mode
            # is safe (the archived tree is public repo source — never
            # `.env`, since ``git archive`` only ever emits tracked blobs)
            # — but ``tar``-restored files/dirs underneath it are NOT
            # guaranteed world-readable: they inherit this PROCESS's umask,
            # not the repo's on-disk mode (confirmed by reproduction: a
            # 100644-mode tracked file extracts as 0600 under umask 077).
            # So every file/dir under here gets an explicit, unconditional
            # widen pass below, after extraction — see there.
            os.chmod(dest_parent, 0o755)
        except OSError as exc:
            record_critical_failure(exc, site="core.sandbox_runner._ensure_repo_snapshot:mkdtemp", category="verify")
            return None
        dest_pkg = dest_parent / "agi_v8_1"
        try:
            dest_pkg.mkdir()
            archive = subprocess.run(
                [git_path, "-C", str(repo_root), "archive", "--format=tar", "HEAD"],
                capture_output=True,
                timeout=30,
                env=_subprocess_env(),
            )
            if archive.returncode != 0:
                record_critical_failure(
                    RuntimeError(f"git archive exit={archive.returncode}"),
                    site="core.sandbox_runner._ensure_repo_snapshot:git_archive",
                    category="verify",
                )
                shutil.rmtree(dest_parent, ignore_errors=True)
                return None
            extract = subprocess.run(
                [tar_path, "-x", "-C", str(dest_pkg)],
                input=archive.stdout,
                capture_output=True,
                timeout=30,
                env=_subprocess_env(),
            )
            if extract.returncode != 0:
                record_critical_failure(
                    RuntimeError(f"tar extract exit={extract.returncode}"),
                    site="core.sandbox_runner._ensure_repo_snapshot:tar_extract",
                    category="verify",
                )
                shutil.rmtree(dest_parent, ignore_errors=True)
                return None
            # Explicit, unconditional permission widen — do NOT rely on
            # whatever mode ``tar`` happened to restore (that mode is this
            # process's umask applied to the archived st_mode, not a
            # guarantee of world-readability; see the comment above
            # ``mkdtemp``). ``dest_pkg`` itself is included since
            # ``Path.mkdir()`` above used no explicit ``mode=`` and is
            # equally subject to umask. Read-only content, so widening is
            # safe; any failure here folds into the same fail-closed
            # ``except`` below (whatever the pass built stays in a
            # temp dir removed by that handler, never returned).
            os.chmod(dest_pkg, os.stat(dest_pkg).st_mode | 0o755)
            for walk_root, dirnames, filenames in os.walk(dest_pkg):
                for name in dirnames:
                    p = os.path.join(walk_root, name)
                    os.chmod(p, os.stat(p).st_mode | 0o755)
                for name in filenames:
                    p = os.path.join(walk_root, name)
                    os.chmod(p, os.stat(p).st_mode | 0o444)
        except (OSError, subprocess.SubprocessError) as exc:
            record_critical_failure(exc, site="core.sandbox_runner._ensure_repo_snapshot:build", category="verify")
            shutil.rmtree(dest_parent, ignore_errors=True)
            return None

        if not (dest_pkg / "__init__.py").is_file():
            shutil.rmtree(dest_parent, ignore_errors=True)
            return None

        _snapshot_dir = str(dest_parent)
        atexit.register(_cleanup_repo_snapshot, str(dest_parent))
        # ``tick_descendant_seal``'s namespace init parks and is terminated,
        # so Python ``atexit`` never runs there.  Register the snapshot with
        # its child-local normal-path registry when one is active.  Outside a
        # seal this returns False and the historical process-wide cache plus
        # atexit ownership remain unchanged.
        try:
            from agi_v8_1.runtime import tick_descendant_seal

            cleanup_scope_active = (
                tick_descendant_seal._child_cleanup_scope_active()
            )
            cleanup_registered = tick_descendant_seal._register_child_cleanup(
                lambda path=str(dest_parent): _cleanup_repo_snapshot_from_seal(path)
            )
            if cleanup_scope_active and not cleanup_registered:
                raise RuntimeError("sealed snapshot cleanup registration refused")
        except Exception as exc:  # pragma: no cover - defensive fail-closed
            record_critical_failure(
                exc,
                site="core.sandbox_runner._ensure_repo_snapshot:seal_cleanup",
                category="persist",
            )
            _cleanup_repo_snapshot_from_seal(str(dest_parent))
            return None
        return dest_parent


def _image_exists(image: str, docker_path: str | None = None) -> bool:
    """Return True iff ``docker image inspect <image>`` succeeds within 5s.

    Backend availability is checked separately in :func:`run`; this helper
    only answers the image-presence question.
    """

    docker_path = docker_path or _docker_executable()
    if docker_path is None:
        return False
    try:
        result = subprocess.run(
            [docker_path, "image", "inspect", image],
            capture_output=True,
            timeout=5,
            text=True,
            env=_subprocess_env(),
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as _ff_exc:
        _swallowed(_ff_exc, site="core.sandbox_runner._image_exists:204", category="verify")
        return False
    except Exception as _ff_exc:  # pragma: no cover - defensive: never propagate
        _swallowed(_ff_exc, site="core.sandbox_runner._image_exists:206", category="verify")
        return False
    return result.returncode == 0


def _image_exists_for_run(image: str, docker_path: str) -> bool:
    """Runner-private image-presence check on the captured daemon."""

    try:
        result = subprocess.run(
            [docker_path, "image", "inspect", image],
            capture_output=True,
            timeout=5,
            text=True,
            env=_docker_subprocess_env(),
        )
    except _DockerEndpointAuthorityUnavailable:
        raise
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        _swallowed(
            exc,
            site="core.sandbox_runner._image_exists_for_run",
            category="verify",
        )
        return False
    except Exception as exc:  # pragma: no cover - defensive: never propagate
        _swallowed(
            exc,
            site="core.sandbox_runner._image_exists_for_run",
            category="verify",
        )
        return False
    return result.returncode == 0


def _image_id_trusted(
    image: str,
    docker_path: str,
    *,
    _docker_env: dict[str, str] | None = None,
) -> tuple[bool, bool, str]:
    """Digest-pin check transplanted from ``verify_isolation._trusted_verify_image``.

    ``_image_exists`` only proves the tag is *present* — it says nothing
    about whether the tag still points at the image an operator actually
    reviewed. ``docker tag`` can repoint any local tag at any local image
    ID, so a compromised host (or a stray ``docker pull``) can silently
    swap what ``agi_v8_sandbox:latest`` resolves to between review time and
    run time. This closes that gap the same way verify_isolation does: by
    pinning the immutable image ID, not the mutable tag name.

    Returns ``(trusted, armed, stderr_tail)``:

    - Pin not armed (``AGI_V8_SWARM_SANDBOX_IMAGE_ID`` unset/blank):
      ``(True, False, "")`` — current, pre-patch behaviour is preserved
      exactly (tag-presence-only, already checked by the ``_image_exists``
      call site). A one-line warning is logged (once per process, not per
      call) recommending the pin be armed for production/hardened
      deployments.
    - Pin armed and the live ``docker image inspect --format {{.Id}}``
      output matches the pinned value exactly: ``(True, True, "")``.
    - Pin armed and anything else (malformed pin value, inspect failure,
      non-zero exit, malformed output, or a real mismatch):
      ``(False, True, "...")`` — fail-closed, never a warning-and-proceed.

    The caller uses ``armed`` (independent of ``trusted``) to decide whether
    the subsequent ``docker run`` should add ``--pull=never`` — that flag is
    only meaningful, and only added, once an operator has actually armed the
    pin (see :func:`_build_isolation_args`'s ``pull_never`` parameter).
    """

    global _image_id_pin_warned
    expected = os.getenv(_IMAGE_ID_ENV, "").strip()  # tier: T5
    if not expected:
        if not _image_id_pin_warned:
            logger.warning(
                "%s is not set — sandbox image digest pinning is NOT armed "
                "(tag-presence check only); set it to the trusted `docker "
                "image inspect --format {{.Id}}` output for %s to harden "
                "against image-substitution (`docker tag`) attacks",
                _IMAGE_ID_ENV,
                image,
            )
            _image_id_pin_warned = True
        return True, False, ""
    if not _IMAGE_ID_RE.fullmatch(expected):
        return False, True, f"{_IMAGE_ID_ENV} is malformed (expected sha256:<64 hex>)"
    try:
        result = subprocess.run(
            [docker_path, "image", "inspect", "--format", "{{.Id}}", image],
            capture_output=True,
            timeout=5,
            text=True,
            env=_subprocess_env() if _docker_env is None else _docker_env,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as _ff_exc:
        _swallowed(_ff_exc, site="core.sandbox_runner._image_id_trusted:inspect", category="verify")
        return False, True, "sandbox image id inspect failed"
    except Exception as _ff_exc:  # pragma: no cover - defensive: never propagate
        _swallowed(_ff_exc, site="core.sandbox_runner._image_id_trusted:inspect", category="verify")
        return False, True, "sandbox image id inspect failed"
    actual = result.stdout.strip()
    if (
        result.returncode != 0
        or not _IMAGE_ID_RE.fullmatch(actual)
        or actual != expected
    ):
        return False, True, f"sandbox image id does not match {_IMAGE_ID_ENV} pin"
    return True, True, ""


def _image_id_trusted_for_run(
    image: str, docker_path: str
) -> tuple[bool, bool, str]:
    """Runner-private image-ID check on the captured daemon."""

    return _image_id_trusted(
        image,
        docker_path,
        _docker_env=_docker_subprocess_env(),
    )


def _entry_digest_trusted(
    image: str,
    docker_path: str,
    *,
    _docker_env: dict[str, str] | None = None,
) -> tuple[bool, str]:
    """Does *image*'s baked harness match this checkout's ``entry.py``?

    See ``__SLOT_SANDBOX_ENTRY_DIGEST_R17_2026_08_21__``. Returns
    ``(trusted, stderr_tail)``. Gate OFF (default) is always ``(True, "")`` —
    byte-identical to before this slot. Every failure mode (unreadable local
    harness, inspect failure, missing label, mismatch) is a REFUSAL, because
    "cannot prove the running harness is the reviewed one" must not be
    cheaper than proving it.
    """
    if not is_entry_digest_check_enabled():
        return True, ""
    try:
        local = hashlib.sha256(_BWRAP_ENTRY_PATH.read_bytes()).hexdigest()
    except OSError as _ff_exc:
        _swallowed(_ff_exc, site="core.sandbox_runner._entry_digest_trusted:local",
                   category="verify")
        return False, "local sandbox harness unreadable; cannot verify image harness"
    try:
        result = subprocess.run(
            [docker_path, "image", "inspect", "--format",
             '{{index .Config.Labels "' + _ENTRY_DIGEST_LABEL + '"}}', image],
            capture_output=True, timeout=5, text=True,
            env=_subprocess_env() if _docker_env is None else _docker_env,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as _ff_exc:
        _swallowed(_ff_exc, site="core.sandbox_runner._entry_digest_trusted:inspect",
                   category="verify")
        return False, "sandbox image harness-digest inspect failed"
    except Exception as _ff_exc:  # pragma: no cover - defensive: never propagate
        _swallowed(_ff_exc, site="core.sandbox_runner._entry_digest_trusted:inspect",
                   category="verify")
        return False, "sandbox image harness-digest inspect failed"
    baked = (result.stdout or "").strip()
    if result.returncode != 0 or not baked or baked in ("<no value>", "null"):
        return False, (
            f"sandbox image carries no {_ENTRY_DIGEST_LABEL} label — rebuild it "
            "so the running harness can be proven to match this checkout"
        )
    if baked != local:
        return False, (
            f"sandbox image harness digest does not match this checkout's "
            f"{_BWRAP_ENTRY_PATH.name} — rebuild the image and refresh the pin"
        )
    return True, ""


def _entry_digest_trusted_for_run(
    image: str, docker_path: str
) -> tuple[bool, str]:
    """Runner-private entry-label check on the captured daemon."""

    return _entry_digest_trusted(
        image,
        docker_path,
        _docker_env=_docker_subprocess_env(),
    )


def _bounded_int_from_env(
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> tuple[int, str]:
    raw = os.getenv(name)
    if raw is None:
        return default, ""
    raw = raw.strip()
    if not raw:
        return default, f"{name} is empty"
    try:
        value = int(raw)
    except ValueError as _ff_exc:
        _swallowed(_ff_exc, site="core.sandbox_runner._bounded_int_from_env:226", category="verify")
        return default, f"{name} must be an integer"
    if value < minimum or value > maximum:
        return default, f"{name} must be between {minimum} and {maximum}"
    return value, ""


def _clamped_int_from_env(
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    value, error = _bounded_int_from_env(
        name,
        default=default,
        minimum=minimum,
        maximum=maximum,
    )
    if error:
        return default
    return value


def _bounded_timeout(timeout_sec: float | None) -> tuple[float, str]:
    raw: object
    if timeout_sec is None:
        raw = os.getenv("AGI_V8_SWARM_SANDBOX_TIMEOUT_SEC", str(_DEFAULT_TIMEOUT_SEC))  # tier: T5
    else:
        raw = timeout_sec
    if isinstance(raw, bool):
        return _DEFAULT_TIMEOUT_SEC, "sandbox timeout must be numeric"
    try:
        value = float(raw)
    except (TypeError, ValueError) as _ff_exc:
        _swallowed(_ff_exc, site="core.sandbox_runner._bounded_timeout:261", category="verify")
        return _DEFAULT_TIMEOUT_SEC, "sandbox timeout must be numeric"
    if not math.isfinite(value):
        return _DEFAULT_TIMEOUT_SEC, "sandbox timeout must be finite"
    if value < _MIN_TIMEOUT_SEC or value > _MAX_TIMEOUT_SEC:
        return (
            _DEFAULT_TIMEOUT_SEC,
            f"sandbox timeout must be between {_MIN_TIMEOUT_SEC:g} and {_MAX_TIMEOUT_SEC:g}",
        )
    return value, ""


def _normalise_image(image: object) -> tuple[str, str]:
    raw = (
        os.getenv("AGI_V8_SWARM_SANDBOX_IMAGE", _DEFAULT_IMAGE)  # tier: T5
        if image is None
        else image
    )
    if not isinstance(raw, str):
        return _DEFAULT_IMAGE, "sandbox image must be a string"
    value = raw.strip()
    if not value:
        return _DEFAULT_IMAGE, "sandbox image is empty"
    if not _SAFE_IMAGE_RE.fullmatch(value):
        return _DEFAULT_IMAGE, "sandbox image contains unsupported characters"
    return value, ""


def _build_run_config(
    *,
    image: object,
    timeout_sec: float | None,
) -> tuple[_SandboxRunConfig | None, str]:
    image_value, image_error = _normalise_image(image)
    if image_error:
        return None, image_error
    timeout, timeout_error = _bounded_timeout(timeout_sec)
    if timeout_error:
        return None, timeout_error
    memory_mb, memory_error = _bounded_int_from_env(  # tier: T5
        "AGI_V8_SWARM_SANDBOX_MEMORY_MB",
        default=_DEFAULT_MEMORY_MB,
        minimum=_MIN_MEMORY_MB,
        maximum=_MAX_MEMORY_MB,
    )
    if memory_error:
        return None, memory_error
    pids_limit, pids_error = _bounded_int_from_env(  # tier: T5
        "AGI_V8_SWARM_SANDBOX_PIDS_LIMIT",
        default=_DEFAULT_PIDS_LIMIT,
        minimum=_MIN_PIDS_LIMIT,
        maximum=_MAX_PIDS_LIMIT,
    )
    if pids_error:
        return None, pids_error
    return (
        _SandboxRunConfig(
            image=image_value,
            timeout_sec=timeout,
            memory_mb=memory_mb,
            pids_limit=pids_limit,
        ),
        "",
    )


def _validate_sources(code: object, test: object | None) -> str:
    if not isinstance(code, str):
        return "code must be a string"
    if len(code.encode("utf-8")) > _MAX_SOURCE_BYTES:
        return f"code exceeds {_MAX_SOURCE_BYTES} bytes"
    if test is None:
        return ""
    if not isinstance(test, str):
        return "test must be a string or None"
    if len(test.encode("utf-8")) > _MAX_SOURCE_BYTES:
        return f"test exceeds {_MAX_SOURCE_BYTES} bytes"
    return ""


_BIND_INPUT_DIR_MODE = 0o755
_BIND_INPUT_FILE_MODE = 0o644


def _same_owned_node(before: os.stat_result, after: os.stat_result) -> bool:
    """Compare the identity fields that must not change across FD checks."""

    return (
        before.st_dev,
        before.st_ino,
        before.st_uid,
        before.st_gid,
        before.st_nlink,
        stat.S_IFMT(before.st_mode),
    ) == (
        after.st_dev,
        after.st_ino,
        after.st_uid,
        after.st_gid,
        after.st_nlink,
        stat.S_IFMT(after.st_mode),
    )


def _write_bind_input_file(dir_fd: int, name: str, content: str) -> None:
    """Create one exact Docker input file without trusting the process umask."""

    if name not in {"code.py", "test_candidate.py"}:
        raise ValueError("invalid sandbox bind-input filename")
    raw = content.encode("utf-8", errors="strict")
    if len(raw) > _MAX_SOURCE_BYTES:
        raise ValueError("sandbox bind-input file exceeds source bound")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | os.O_CLOEXEC
        | os.O_NOFOLLOW
    )
    fd = os.open(name, flags, 0o600, dir_fd=dir_fd)
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
            or before.st_size != 0
        ):
            raise OSError("unsafe sandbox bind-input file identity")
        view = memoryview(raw)
        while view:
            written = os.write(fd, view)
            if written <= 0 or written > len(view):
                raise OSError("sandbox bind-input partial write failed")
            view = view[written:]
        os.fchmod(fd, _BIND_INPUT_FILE_MODE)
        after = os.fstat(fd)
        if (
            not _same_owned_node(before, after)
            or not stat.S_ISREG(after.st_mode)
            or stat.S_IMODE(after.st_mode) != _BIND_INPUT_FILE_MODE
            or after.st_uid != os.getuid()
            or after.st_nlink != 1
            or after.st_size != len(raw)
        ):
            raise OSError("unsafe sandbox bind-input file after write")
        path_info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        if (
            not _same_owned_node(after, path_info)
            or not stat.S_ISREG(path_info.st_mode)
            or stat.S_IMODE(path_info.st_mode) != _BIND_INPUT_FILE_MODE
            or path_info.st_uid != os.getuid()
            or path_info.st_nlink != 1
            or path_info.st_size != len(raw)
        ):
            raise OSError("sandbox bind-input file path changed after write")
    finally:
        os.close(fd)


def _prepare_bind_input(root: Path, code: str, test: str | None) -> Path:
    """Prepare the read-only bind root for Docker's fixed non-owner UID.

    ``TemporaryDirectory`` itself stays owner-private.  Only the directory
    mounted directly at ``/sandbox/input`` and its two bounded source files
    are made world-traversable/readable.  Explicit ``fchmod`` calls are
    required because the paid supervisor deliberately runs with umask 077.
    """

    root_info = os.stat(root, follow_symlinks=False)
    if (
        not stat.S_ISDIR(root_info.st_mode)
        or root_info.st_uid != os.getuid()
        or root_info.st_nlink < 2
        or stat.S_IMODE(root_info.st_mode) & 0o077
    ):
        raise OSError("unsafe sandbox temporary root")

    input_dir = root / "input"
    input_dir.mkdir(mode=0o700)
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY
    dir_fd = os.open(input_dir, flags)
    try:
        before = os.fstat(dir_fd)
        if (
            not stat.S_ISDIR(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_nlink < 2
        ):
            raise OSError("unsafe sandbox bind-input directory identity")
        os.fchmod(dir_fd, _BIND_INPUT_DIR_MODE)
        widened = os.fstat(dir_fd)
        if (
            not _same_owned_node(before, widened)
            or not stat.S_ISDIR(widened.st_mode)
            or stat.S_IMODE(widened.st_mode) != _BIND_INPUT_DIR_MODE
            or widened.st_uid != os.getuid()
            or widened.st_nlink < 2
        ):
            raise OSError("unsafe sandbox bind-input directory mode")

        _write_bind_input_file(dir_fd, "code.py", code)
        if test is not None:
            _write_bind_input_file(dir_fd, "test_candidate.py", test)

        final = os.fstat(dir_fd)
        path_info = os.stat(input_dir, follow_symlinks=False)
        if (
            not _same_owned_node(widened, final)
            or not _same_owned_node(final, path_info)
            or not stat.S_ISDIR(path_info.st_mode)
            or stat.S_IMODE(final.st_mode) != _BIND_INPUT_DIR_MODE
            or stat.S_IMODE(path_info.st_mode) != _BIND_INPUT_DIR_MODE
            or path_info.st_uid != os.getuid()
            or path_info.st_nlink < 2
        ):
            raise OSError("sandbox bind-input directory changed during preparation")
    finally:
        os.close(dir_fd)

    resolved_input = input_dir.resolve(strict=True)
    if resolved_input.parent != root:
        raise ValueError("sandbox bind-input escaped temporary root")
    return resolved_input


def _build_isolation_args(
    image: str,
    *,
    docker_path: str | None = None,
    memory_mb: int | None = None,
    pids_limit: int | None = None,
    pull_never: bool = False,
    interactive: bool = False,
) -> list[str]:
    """Return the base ``docker run`` argv with isolation flags applied.

    The caller is expected to splice the input-volume bind mount in
    after ``docker run`` (see :func:`run`). Keeping the args returned
    here image-terminated makes the contract easy to audit.

    ``pull_never`` defaults to False, reproducing this function's argv
    byte-for-byte as before the P1-f image-pin slot existed. Callers that
    have verified the image ID against ``AGI_V8_SWARM_SANDBOX_IMAGE_ID``
    (see ``_image_id_trusted``) pass True so ``--pull=never`` (mirroring
    ``verify_isolation._build_docker_argv``) closes the narrow TOCTOU gap
    between that inspect call and this ``docker run`` — otherwise a local
    tag could in principle be reassigned or a registry pull triggered
    between the two.

    ``interactive`` (``__SLOT_SANDBOX_GRADE_RECEIPT_2026_08_24__``) defaults
    to False, reproducing this argv byte-for-byte. True adds ``-i`` so the
    per-run nonce + signing key can travel on the container's stdin — the
    ONLY channel that is not readable from ``/proc/<PPID>/{cmdline,environ}``
    by a candidate running as the same uid. Set only by :func:`run` when
    ``AGI_V8_SANDBOX_GRADE_RECEIPT_ENABLED`` is armed.
    """

    if memory_mb is None:
        memory_mb = _clamped_int_from_env(
            "AGI_V8_SWARM_SANDBOX_MEMORY_MB",
            default=_DEFAULT_MEMORY_MB,
            minimum=_MIN_MEMORY_MB,
            maximum=_MAX_MEMORY_MB,
        )
    if pids_limit is None:
        pids_limit = _clamped_int_from_env(
            "AGI_V8_SWARM_SANDBOX_PIDS_LIMIT",
            default=_DEFAULT_PIDS_LIMIT,
            minimum=_MIN_PIDS_LIMIT,
            maximum=_MAX_PIDS_LIMIT,
        )

    argv = [
        docker_path or "docker",
        "run",
        "--rm",
    ]
    if pull_never:
        argv.append("--pull=never")
    if interactive:
        argv.append("-i")
    argv += [
        "--network=none",
        "--read-only",
        "--tmpfs",
        "/tmp:size=64m",
        f"--memory={memory_mb}m",
        "--cpus=1",
        f"--pids-limit={pids_limit}",
        "--user",
        "65532:65532",
        image,
    ]
    return argv


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _stub(
    *,
    reason: str,
    lane_id: str,
    image: str,
    wall_clock_sec: float = 0.0,
    stderr_tail: str = "",
    exit_code: int = -1,
    backend: str = "",
) -> SandboxTelemetry:
    """Build a stub :class:`SandboxTelemetry` with the canonical reason."""

    if reason not in STUB_REASONS:
        # Defensive: surface an unexpected reason rather than silently
        # accepting a typo. Callers in this module only ever pass
        # canonical tokens.
        raise ValueError(f"unknown stub_reason: {reason!r}")
    return SandboxTelemetry(
        stub=True,
        stub_reason=reason,
        status=_status_for_stub_reason(reason) if _sandbox_status_enabled() else "",
        lane_id=lane_id,
        image=image,
        wall_clock_sec=wall_clock_sec,
        stderr_tail=_tail(stderr_tail),
        exit_code=exit_code,
        backend=backend,
    )


def _cleanup_container(docker_path: str, cidfile: Path) -> None:
    try:
        cid = cidfile.read_text(encoding="utf-8").strip()
    except OSError as _ff_exc:
        _swallowed(_ff_exc, site="core.sandbox_runner._cleanup_container:422", category="verify")
        return
    if not _CONTAINER_ID_RE.fullmatch(cid):
        return
    try:
        subprocess.run(
            [docker_path, "rm", "-f", cid],
            capture_output=True,
            timeout=5,
            text=True,
            env=(
                _docker_subprocess_env()
                if _preseal_identity_enabled()
                else _subprocess_env()
            ),
        )
    except Exception as _ff_exc:  # pragma: no cover - best-effort cleanup only
        _swallowed(_ff_exc, site="core.sandbox_runner._cleanup_container:434", category="verify")
        return


def run(
    *,
    code: str,
    test: str | None = None,
    lane_id: str = "",
    image: str | None = None,
    timeout_sec: float | None = None,
) -> SandboxTelemetry:
    """Synchronous sandbox call.

    Writes ``code`` (and optional ``test``) to a tempdir bind-mounted
    read-only at ``/sandbox/input`` inside the container, runs the entry
    harness baked into the image (Worker A), and parses the last line of
    stdout as a JSON envelope.

    Returns a :class:`SandboxTelemetry` for installed execution failures,
    with the appropriate ``stub_reason``. Missing optional T5 code raises
    ``PayloadUnavailable`` before backend work.
    """

    if not is_sandbox_enabled():
        image_label, _ = _normalise_image(image)
        return _stub(reason="disabled", lane_id=lane_id, image=image_label)

    config, config_error = _build_run_config(image=image, timeout_sec=timeout_sec)
    if config is None:
        image_label, _ = _normalise_image(image)
        return _stub(
            reason="invalid_config",
            lane_id=lane_id,
            image=image_label,
            stderr_tail=config_error,
        )

    source_error = _validate_sources(code, test)
    if source_error:
        return _stub(
            reason="input_rejected",
            lane_id=lane_id,
            image=config.image,
            stderr_tail=source_error,
        )

    # Missing optional code is typed separately from Docker/infrastructure
    # failure and refuses before backend probes, receipt reads, or workspace I/O.
    operation = resolve_payload(_SANDBOX_PORT)

    # __SLOT_SANDBOX_PRESEAL_DOCKER_ENDPOINT_2026_09_04__ Validate the
    # fork-inherited endpoint authority before backend selection.  In the
    # descendant user namespace the live uid is the overflow identity, so a
    # fresh ``/run/user/<os.getuid()>`` guess is wrong.  Conversely, treating
    # a missing capture as ordinary Docker downtime would route to bwrap,
    # whose nested userns cannot start there, recreating the opaque
    # ``container_failed`` loop observed in Track2 20260903a.
    preseal_crossed = False
    try:
        _docker_subprocess_env()
        preseal_crossed = _preseal_identity_crossed()
    except _DockerEndpointAuthorityUnavailable as exc:
        record_critical_failure(
            exc,
            site="core.sandbox_runner.run:preseal_authority",
            category="verify",
        )
        return _stub(
            reason="docker_endpoint_untrusted",
            lane_id=lane_id,
            image=config.image,
            stderr_tail="pre-seal Docker authority unavailable",
            backend="docker" if _preseal_identity_enabled() else "",
        )

    # __SLOT_SANDBOX_GRADE_RECEIPT_2026_08_24__ receipt provisioning happens
    # BEFORE any backend is probed or any container starts: a run whose result
    # could never be authenticated must cost nothing and produce nothing.
    # Gate OFF (default) → one env read, ``receipt_ctx`` stays None, and every
    # line below that consults it is a no-op (argv, subprocess kwargs, env and
    # envelope handling all byte-identical to before this slot existed).
    receipt_ctx: dict[str, Any] | None = None
    if is_grade_receipt_enabled():
        receipt_ctx, receipt_setup_error = _build_receipt_context(code=code, test=test)
        if receipt_ctx is None:
            return _stub(
                reason="receipt_untrusted",
                lane_id=lane_id,
                image=config.image,
                stderr_tail=receipt_setup_error,
            )

    try:
        docker_path = _docker_executable_for_run()
        docker_ready = bool(
            docker_path is not None
            and (
                _docker_backend_available_for_run(docker_path)
                if _preseal_identity_enabled()
                else _docker_backend_available(docker_path)
            )
        )
    except _DockerEndpointAuthorityUnavailable as exc:
        record_critical_failure(
            exc,
            site="core.sandbox_runner.run:docker_authority",
            category="verify",
        )
        return _stub(
            reason="docker_endpoint_untrusted",
            lane_id=lane_id,
            image=config.image,
            stderr_tail="pre-seal Docker authority unavailable",
            backend="docker" if _preseal_identity_enabled() else "",
        )
    if docker_path is not None and not docker_ready and _preseal_identity_enabled():
        try:
            _docker_subprocess_env()
        except _DockerEndpointAuthorityUnavailable as exc:
            record_critical_failure(
                exc,
                site="core.sandbox_runner.run:docker_recheck",
                category="verify",
            )
            return _stub(
                reason="docker_endpoint_untrusted",
                lane_id=lane_id,
                image=config.image,
                stderr_tail="pre-seal Docker authority unavailable",
                backend="docker",
            )

    # __SLOT_SANDBOX_BWRAP_2026_08_16__ bwrap fallback branch point. Gate
    # OFF (the default) → this whole block is skipped and the four lines
    # below reproduce the pre-2026-08-16 docker_missing/docker_unavailable
    # sequence exactly (byte-identical control flow and stub payloads).
    use_bwrap = False
    bwrap_path: str | None = None
    if not docker_ready:
        if preseal_crossed:
            # The pre-seal contract means this call is destined for (or
            # already inside) the descendant user namespace.  A nested bwrap
            # user namespace is unavailable in the production topology; 03a
            # proved that fallback only converts Docker downtime into an
            # opaque outer rc=1.  Refuse before probing or executing bwrap.
            return _stub(
                reason=(
                    "docker_missing" if docker_path is None
                    else "docker_unavailable"
                ),
                lane_id=lane_id,
                image=config.image,
                backend="docker",
            )
        if is_bwrap_isolation_enabled():
            bwrap_path = _bwrap_executable()
            if bwrap_path is None:
                # Fail-closed: docker AND bwrap both unavailable while the
                # bwrap gate is armed → refuse to run unisolated. Never
                # fall through to a bare, unisolated subprocess.
                return _stub(
                    reason="no_isolation_available",
                    lane_id=lane_id,
                    image=config.image,
                    stderr_tail=(
                        f"{_BWRAP_GATE_ENV} is set but docker and bwrap are "
                        "both unavailable; refusing to execute unisolated"
                    ),
                )
            use_bwrap = True
        elif docker_path is None:
            return _stub(reason="docker_missing", lane_id=lane_id, image=config.image)
        else:
            return _stub(reason="docker_unavailable", lane_id=lane_id, image=config.image)
    else:
        try:
            image_exists = (
                _image_exists_for_run(config.image, docker_path)
                if _preseal_identity_enabled()
                else _image_exists(config.image, docker_path)
            )
        except _DockerEndpointAuthorityUnavailable as exc:
            record_critical_failure(
                exc,
                site="core.sandbox_runner.run:image_presence_authority",
                category="verify",
            )
            return _stub(
                reason="docker_endpoint_untrusted",
                lane_id=lane_id,
                image=config.image,
                stderr_tail="pre-seal Docker authority unavailable",
                backend="docker",
            )
        if not image_exists:
            if _preseal_identity_enabled():
                try:
                    _docker_subprocess_env()
                except _DockerEndpointAuthorityUnavailable as exc:
                    record_critical_failure(
                        exc,
                        site="core.sandbox_runner.run:image_presence_recheck",
                        category="verify",
                    )
                    return _stub(
                        reason="docker_endpoint_untrusted",
                        lane_id=lane_id,
                        image=config.image,
                        stderr_tail=(
                            "pre-seal Docker authority unavailable"
                        ),
                        backend="docker",
                    )
            return _stub(
                reason="image_missing",
                lane_id=lane_id,
                image=config.image,
                backend="docker" if _preseal_identity_enabled() else "",
            )

    # __SLOT_SANDBOX_IMAGE_PIN_2026_08_19__ digest-pin branch point. Reached
    # only when docker_ready and the tag is present (bwrap never sets
    # use_bwrap here, and both docker-unavailable branches above already
    # returned). Gate OFF (``AGI_V8_SWARM_SANDBOX_IMAGE_ID`` unset, the
    # default) → ``_image_id_trusted`` always returns ``(True, False, "")``
    # and ``image_pull_never`` stays False, so the docker argv assembled
    # further down is byte-identical to before this slot existed.
    image_pull_never = False
    if not use_bwrap:
        assert docker_path is not None  # guaranteed by the docker_ready branch above
        try:
            if _preseal_identity_enabled():
                image_trusted, image_pin_armed, image_pin_error = (
                    _image_id_trusted_for_run(config.image, docker_path)
                )
            else:
                image_trusted, image_pin_armed, image_pin_error = (
                    _image_id_trusted(config.image, docker_path)
                )
        except _DockerEndpointAuthorityUnavailable as exc:
            record_critical_failure(
                exc,
                site="core.sandbox_runner.run:image_identity_authority",
                category="verify",
            )
            return _stub(
                reason="docker_endpoint_untrusted",
                lane_id=lane_id,
                image=config.image,
                stderr_tail="pre-seal Docker authority unavailable",
                backend="docker",
            )
        if not image_trusted:
            if _preseal_identity_enabled():
                try:
                    _docker_subprocess_env()
                except _DockerEndpointAuthorityUnavailable as exc:
                    record_critical_failure(
                        exc,
                        site="core.sandbox_runner.run:image_identity_recheck",
                        category="verify",
                    )
                    return _stub(
                        reason="docker_endpoint_untrusted",
                        lane_id=lane_id,
                        image=config.image,
                        stderr_tail=(
                            "pre-seal Docker authority unavailable"
                        ),
                        backend="docker",
                    )
            return _stub(
                reason="image_untrusted",
                lane_id=lane_id,
                image=config.image,
                stderr_tail=image_pin_error,
                backend="docker" if _preseal_identity_enabled() else "",
            )
        # __SLOT_SANDBOX_ENTRY_DIGEST_R17_2026_08_21__ the image-ID pin above
        # proves the tag still points at the reviewed image; it cannot notice
        # that the reviewed image itself bakes a stale harness. Gate OFF
        # (default) → this is a single env read and byte-identical.
        try:
            if _preseal_identity_enabled():
                entry_trusted, entry_error = _entry_digest_trusted_for_run(
                    config.image, docker_path
                )
            else:
                entry_trusted, entry_error = _entry_digest_trusted(
                    config.image, docker_path
                )
        except _DockerEndpointAuthorityUnavailable as exc:
            record_critical_failure(
                exc,
                site="core.sandbox_runner.run:entry_identity_authority",
                category="verify",
            )
            return _stub(
                reason="docker_endpoint_untrusted",
                lane_id=lane_id,
                image=config.image,
                stderr_tail="pre-seal Docker authority unavailable",
                backend="docker",
            )
        if not entry_trusted:
            if _preseal_identity_enabled():
                try:
                    _docker_subprocess_env()
                except _DockerEndpointAuthorityUnavailable as exc:
                    record_critical_failure(
                        exc,
                        site="core.sandbox_runner.run:entry_identity_recheck",
                        category="verify",
                    )
                    return _stub(
                        reason="docker_endpoint_untrusted",
                        lane_id=lane_id,
                        image=config.image,
                        stderr_tail=(
                            "pre-seal Docker authority unavailable"
                        ),
                        backend="docker",
                    )
            return _stub(
                reason="image_untrusted",
                lane_id=lane_id,
                image=config.image,
                stderr_tail=entry_error,
                backend="docker" if _preseal_identity_enabled() else "",
            )
        image_pull_never = image_pin_armed

    selected_backend = "bwrap" if use_bwrap else "docker"
    # Preserve the long-standing telemetry shape for generic gate-OFF users.
    # Track2 already arms the pre-seal identity contract, and that is exactly
    # the topology where backend provenance is needed to disambiguate a
    # Docker routing refusal from a nested-bwrap execution failure.
    telemetry_backend = (
        selected_backend if _preseal_identity_enabled() else ""
    )

    # __SLOT_SANDBOX_WORKTREE_2026_08_16__ repo-context branch point. Gate OFF
    # (the default) → ``is_sandbox_worktree_enabled`` is a single env read and
    # ``worktree_dir`` stays ``None`` — every line below that consults it is a
    # no-op, so the docker/bwrap argv assembled further down is byte-identical
    # to before this gate existed.
    worktree_dir: Path | None = None
    if is_sandbox_worktree_enabled():
        worktree_dir = _ensure_repo_snapshot()
        if worktree_dir is None:
            return _stub(
                reason="worktree_unavailable",
                lane_id=lane_id,
                image=config.image,
                stderr_tail=(
                    f"{_WORKTREE_GATE_ENV} is set but the repo snapshot could "
                    "not be built (no .git ancestor, or git archive/tar "
                    "failed); refusing to fall back to the standalone "
                    "no-repo-imports harness"
                ),
                backend=telemetry_backend,
            )

    return operation(
        api=sys.modules[__name__], code=code, test=test, lane_id=lane_id,
        config=config, use_bwrap=use_bwrap, bwrap_path=bwrap_path,
        docker_path=docker_path, image_pull_never=image_pull_never,
        worktree_dir=worktree_dir, receipt_ctx=receipt_ctx,
        telemetry_backend=telemetry_backend,
    )


def _sandbox_temporary_context(*, lane_id, config, telemetry_backend):
    """Public workspace failure handling; no operation-specific execution."""
    try:
        return tempfile.TemporaryDirectory(prefix="r16_sandbox_")
    except OSError as exc:
        _swallowed(exc, site="core.sandbox_runner.run:490", category="verify")
        return _stub(
            reason="exception",
            lane_id=lane_id,
            image=config.image,
            stderr_tail=_tail(
                "tempdir: "
                + format_text_for_sink(
                    exc,
                    max_chars=SINK_MASK_INPUT_MAX_CHARS,
                    one_line=False,
                )
            ),
            backend=telemetry_backend,
        )
def _prepare_sandbox_launch(*, tmpdir, code, test, lane_id, config, use_bwrap,
                            bwrap_path, docker_path, image_pull_never,
                            worktree_dir, receipt_ctx, telemetry_backend):
    """Public containment, isolation argv and last-moment authority checks."""
    root = Path(tmpdir).resolve()
    cidfile = root / "container.cid"
    # Only created/used on the bwrap path — the writable scratch dir
    # bound at /tmp/work. Never used for docker (unaffected).
    work_dir = root / "work"
    try:
        # __SLOT_SANDBOX_BIND_INPUT_UMASK_2026_09_04__ The supervisor runs
        # under umask 077 while Docker deliberately executes the harness as
        # fixed uid 65532.  Keep the temporary parent/cidfile private, but
        # prepare the directory mounted *as* /sandbox/input and its source
        # files through stable no-follow descriptors with explicit modes.
        # Otherwise the same readiness probe is 0700/0600 under the
        # supervisor and 0755/0644 in an operator shell.
        resolved_input = _prepare_bind_input(root, code, test)
        if use_bwrap:
            work_dir.mkdir()
    except (OSError, ValueError) as exc:
        _swallowed(exc, site="core.sandbox_runner.run:512", category="verify")
        return _stub(
            reason="exception",
            lane_id=lane_id,
            image=config.image,
            stderr_tail=_tail(
                "input prep: "
                + format_text_for_sink(
                    exc,
                    max_chars=SINK_MASK_INPUT_MAX_CHARS,
                    one_line=False,
                )
            ),
            backend=telemetry_backend,
        )

    if use_bwrap:
        assert bwrap_path is not None  # guaranteed by the branch above
        cmd = _build_bwrap_argv(
            bwrap_path=bwrap_path,
            entry_path=str(_BWRAP_ENTRY_PATH),
            input_dir=resolved_input,
            work_dir=work_dir.resolve(strict=True),
            repo_dir=worktree_dir,
        )
    else:
        # Splice the bind mount in between ``docker run`` and the
        # isolation flags — keeping the image at the tail of the argv.
        base = _build_isolation_args(
            config.image,
            docker_path=docker_path,
            memory_mb=config.memory_mb,
            pids_limit=config.pids_limit,
            pull_never=image_pull_never,
            interactive=receipt_ctx is not None,
        )
        extra_args = [
            "--cidfile",
            str(cidfile),
            "-v",
            f"{resolved_input}:/sandbox/input:ro",
        ]
        if receipt_ctx is not None:
            # 게이트 **이름**만 컨테이너에 알린다 — 하네스가 stdin 채널을
            # 읽어야 하는지 아는 유일한 신호다. ⛔ nonce·키는 env 에 절대
            # 싣지 않는다(같은 uid 의 후보가 /proc 로 읽는다).
            extra_args += [
                "-e",
                "AGI_V8_SANDBOX_GRADE_RECEIPT_ENABLED=true",
            ]
        if worktree_dir is not None:
            # A container doesn't inherit host env vars automatically —
            # unlike the bwrap branch below, PYTHONPATH must travel as an
            # explicit ``-e`` flag, not via ``run_env`` (that only sets
            # the docker CLI client process's own env).
            extra_args += [
                "-v",
                f"{worktree_dir}:/sandbox/repo:ro",
                "-e",
                "PYTHONPATH=/sandbox/repo",
            ]
        cmd = base[:2] + extra_args + base[2:]

    try:
        run_env = (
            _subprocess_env()
            if use_bwrap
            else _docker_subprocess_env()
        )
    except _DockerEndpointAuthorityUnavailable as exc:
        record_critical_failure(
            exc,
            site="core.sandbox_runner.run:spawn_authority",
            category="verify",
        )
        return _stub(
            reason="docker_endpoint_untrusted",
            lane_id=lane_id,
            image=config.image,
            stderr_tail="pre-seal Docker authority unavailable",
            backend="docker",
        )
    if use_bwrap:
        # Tell the harness (inside the bwrap sandbox) where its input
        # actually landed — see ``_BWRAP_INPUT_DIR_VALUE``. Docker's
        # call below never sets this key, so docker stays unaffected.
        run_env = {**run_env, _BWRAP_INPUT_DIR_ENV: _BWRAP_INPUT_DIR_VALUE}
        if worktree_dir is not None:
            # bwrap (unlike docker) execs its child directly and passes
            # its own env straight through (no --clearenv here), so
            # setting PYTHONPATH on the launching process's env is
            # sufficient — no extra flag needed, mirroring the input-dir
            # override immediately above.
            run_env["PYTHONPATH"] = "/tmp/sandbox/repo"
    if receipt_ctx is not None and use_bwrap:
        # __SLOT_SANDBOX_GRADE_RECEIPT_2026_08_24__ bwrap execs its child
        # directly with its own env (no --clearenv), so setting the gate
        # here reaches the harness — mirroring the input-dir override
        # above. The docker branch uses an explicit ``-e`` flag instead
        # (a container inherits nothing from the CLI client's env).
        run_env = {**run_env, "AGI_V8_SANDBOX_GRADE_RECEIPT_ENABLED": "true"}
    # OFF (receipt_ctx None) → this dict is EXACTLY the four kwargs the
    # pre-slot call site passed, so ``subprocess.run(cmd, **run_kwargs)``
    # is the same call, byte for byte.
    run_kwargs: dict[str, Any] = {
        "capture_output": True,
        "timeout": config.timeout_sec,
        "text": True,
        "env": run_env,
    }
    if receipt_ctx is not None:
        # 🔑 nonce+키가 지나가는 **유일한** 통로. stdin 은 argv/env 와 달리
        # /proc 로 사후 관측되지 않고, 하네스가 읽자마자 fd0 을 /dev/null 로
        # 덮어 어떤 자식도 잔여 바이트에 못 닿는다.
        run_kwargs["input"] = receipt_ctx["stdin_line"]
    return cmd, run_kwargs, cidfile


def _execute_sandbox_launch(*, launch, lane_id, config, use_bwrap,
                            docker_path, telemetry_backend):
    """Public bounded process execution and existing timeout cleanup contract."""
    cmd, run_kwargs, cidfile = launch
    try:
        result = subprocess.run(cmd, **run_kwargs)
    except subprocess.TimeoutExpired as exc:
        _swallowed(exc, site="core.sandbox_runner.run:547", category="verify")
        if not use_bwrap:
            _cleanup_container(docker_path, cidfile)
        logger.warning(
            "sandbox_runner timeout lane=%s image=%s timeout=%s",
            lane_id,
            config.image,
            config.timeout_sec,
        )
        return _stub(
            reason="timeout",
            lane_id=lane_id,
            image=config.image,
            wall_clock_sec=float(config.timeout_sec),
            stderr_tail=_tail(exc),
            backend=telemetry_backend,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        _swallowed(exc, site="core.sandbox_runner.run:562", category="verify")
        logger.warning(
            "sandbox_runner subprocess error lane=%s image=%s err=%s",
            lane_id,
            config.image,
            format_exception_for_log(exc),
        )
        return _stub(
            reason="exception",
            lane_id=lane_id,
            image=config.image,
            stderr_tail=_tail(exc),
            backend=telemetry_backend,
        )
    except Exception as exc:  # pragma: no cover - defensive catch-all
        _swallowed(exc, site="core.sandbox_runner.run:575", category="verify")
        logger.warning(
            "sandbox_runner unexpected error lane=%s image=%s err=%s",
            lane_id,
            config.image,
            format_exception_for_log(exc),
        )
        return _stub(
            reason="exception",
            lane_id=lane_id,
            image=config.image,
            stderr_tail=_tail(exc),
            backend=telemetry_backend,
        )

    return result


def _parse_envelope(
    *,
    stdout: str,
    stderr: str,
    returncode: int,
    lane_id: str,
    image: str,
    receipt_ctx: dict[str, Any] | None = None,
    backend: str = "",
) -> SandboxTelemetry:
    """Extract the JSON envelope from the harness stdout, with fallback.

    ``receipt_ctx`` (``__SLOT_SANDBOX_GRADE_RECEIPT_2026_08_24__``) defaults
    to ``None`` — every line that consults it is then a no-op and this
    function behaves exactly as before the slot existed. When supplied (the
    grade-receipt gate is armed) the envelope's numbers are adopted ONLY
    after its HMAC receipt verifies against this call's nonce, the mounted
    input digests, and the operator harness allowlist; every failure is a
    named ``receipt_untrusted`` refusal, never a lenient pass.
    """

    if returncode != 0:
        return _stub(
            reason="container_failed",
            lane_id=lane_id,
            image=image,
            stderr_tail=stderr,
            exit_code=returncode,
            backend=backend,
        )

    lines = [ln for ln in stdout.strip().splitlines() if ln.strip()]
    if not lines:
        return _stub(
            reason="invalid_envelope",
            lane_id=lane_id,
            image=image,
            stderr_tail=stderr,
            exit_code=returncode,
            backend=backend,
        )
    try:
        envelope = json.loads(lines[-1])
    except json.JSONDecodeError as _ff_exc:
        _swallowed(_ff_exc, site="core.sandbox_runner._parse_envelope:631", category="verify")
        return _stub(
            reason="invalid_envelope",
            lane_id=lane_id,
            image=image,
            stderr_tail=stderr,
            exit_code=returncode,
            backend=backend,
        )
    if not isinstance(envelope, dict):
        return _stub(
            reason="invalid_envelope",
            lane_id=lane_id,
            image=image,
            stderr_tail=stderr,
            exit_code=returncode,
            backend=backend,
        )

    # __SLOT_SANDBOX_GRADE_RECEIPT_2026_08_24__ authenticate BEFORE any of the
    # envelope's numbers are read as values. An unauthenticated row is not a
    # low score, it is an evaluator-integrity refusal.
    if receipt_ctx is not None:
        receipt_error = _verify_grade_receipt(envelope, receipt_ctx)
        if receipt_error:
            logger.warning(
                "sandbox_runner grade receipt refused lane=%s image=%s reason=%s",
                lane_id,
                image,
                receipt_error.split(":", 1)[0],
            )
            return _stub(
                reason="receipt_untrusted",
                lane_id=lane_id,
                image=image,
                stderr_tail=receipt_error,
                exit_code=returncode,
                backend=backend,
            )

    def _as_int(key: str) -> int | None:
        value = envelope.get(key)
        if isinstance(value, bool):
            return None
        try:
            return int(value)
        except (TypeError, ValueError) as _ff_exc:
            _swallowed(_ff_exc, site="core.sandbox_runner._as_int:654", category="verify")
            return None

    def _as_float(key: str) -> float | None:
        value = envelope.get(key)
        if isinstance(value, bool):
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError) as _ff_exc:
            _swallowed(_ff_exc, site="core.sandbox_runner._as_float:663", category="verify")
            return None
        if not math.isfinite(parsed):
            return None
        return parsed

    int_fields: dict[str, int] = {}
    for key in (
        "exit_code",
        "ruff_warnings",
        "ruff_errors",
        "mypy_errors",
        "pytest_passed",
        "pytest_failed",
    ):
        parsed_int = _as_int(key)
        if parsed_int is None:
            return _stub(
                reason="invalid_envelope",
                lane_id=lane_id,
                image=image,
                stderr_tail=f"invalid or missing envelope field: {key}",
                exit_code=returncode,
                backend=backend,
            )
        int_fields[key] = parsed_int

    float_fields: dict[str, float] = {}
    for key in ("wall_clock_sec", "peak_rss_mb"):
        parsed_float = _as_float(key)
        if parsed_float is None:
            return _stub(
                reason="invalid_envelope",
                lane_id=lane_id,
                image=image,
                stderr_tail=f"invalid or missing envelope field: {key}",
                exit_code=returncode,
                backend=backend,
            )
        float_fields[key] = parsed_float

    if int_fields["exit_code"] < 0 or int_fields["exit_code"] > 255:
        return _stub(
            reason="invalid_envelope",
            lane_id=lane_id,
            image=image,
            stderr_tail="exit_code out of bounds",
            exit_code=returncode,
            backend=backend,
        )
    if any(value < 0 for key, value in int_fields.items() if key != "exit_code"):
        return _stub(
            reason="invalid_envelope",
            lane_id=lane_id,
            image=image,
            stderr_tail="negative count in envelope",
            exit_code=returncode,
            backend=backend,
        )
    if any(value < 0.0 for value in float_fields.values()):
        return _stub(
            reason="invalid_envelope",
            lane_id=lane_id,
            image=image,
            stderr_tail="negative float in envelope",
            exit_code=returncode,
            backend=backend,
        )

    if _sandbox_status_enabled():
        _candidate_ok = (
            int_fields["exit_code"] == 0 and int_fields["pytest_failed"] == 0
        )
        _status = SANDBOX_STATUS_PASS if _candidate_ok else SANDBOX_STATUS_CANDIDATE_FAIL
    else:
        _status = ""
    return SandboxTelemetry(
        stub=False,
        stub_reason="",
        status=_status,
        lane_id=lane_id,
        image=image,
        exit_code=int_fields["exit_code"],
        wall_clock_sec=float_fields["wall_clock_sec"],
        peak_rss_mb=float_fields["peak_rss_mb"],
        ruff_warnings=int_fields["ruff_warnings"],
        ruff_errors=int_fields["ruff_errors"],
        mypy_errors=int_fields["mypy_errors"],
        pytest_passed=int_fields["pytest_passed"],
        pytest_failed=int_fields["pytest_failed"],
        stderr_tail=_tail(envelope.get("stderr_tail", "")),
        grade_receipt_verified=receipt_ctx is not None,
        backend=backend,
    )


async def run_async(
    *,
    code: str,
    test: str | None = None,
    lane_id: str = "",
    image: str | None = None,
    timeout_sec: float | None = None,
) -> SandboxTelemetry:
    """Async wrapper around :func:`run`.

    The synchronous body is dispatched via :func:`asyncio.to_thread` so
    callers in the streaming-meta dispatch path do not block the event
    loop. Shares the same never-raises contract.
    """

    return await asyncio.to_thread(
        run,
        code=code,
        test=test,
        lane_id=lane_id,
        image=image,
        timeout_sec=timeout_sec,
    )


__all__ = [
    "SCHEMA_VERSION",
    "STUB_REASONS",
    "SANDBOX_STATUSES",
    "SANDBOX_STATUS_NOT_CONFIGURED",
    "SANDBOX_STATUS_INFRA_UNAVAILABLE",
    "SANDBOX_STATUS_EVALUATOR_ERROR",
    "SANDBOX_STATUS_CANDIDATE_TIMEOUT",
    "SANDBOX_STATUS_PASS",
    "SANDBOX_STATUS_CANDIDATE_FAIL",
    "SandboxTelemetry",
    "RECEIPT_REFUSALS",
    "is_sandbox_enabled",
    "is_bwrap_isolation_enabled",
    "is_sandbox_worktree_enabled",
    "is_grade_receipt_enabled",
    "run",
    "run_async",
]
