# R25 W4 (ported from v7.1 __RPLAN_R12_S1__) - DENY_CLASSES + carve-out token
"""Machine-readable destructive command policy with tiered classes and
process-local single-use carve-out tokens.

R12 invariants (ported from v7.1/agent_system/agents/
destructive_command_policy.py):

* ``DENY_CLASSES``: 3 tiers
  - ``class-1-irreversible``  — git push --force/--mirror, git reset --hard,
    git clean -fd, rm -rf, mkfs, dd if=… of=/dev/…
  - ``class-2-network-exec``  — curl|sh, wget|bash, nc -l, ssh -R
  - ``class-3-confirm``       — chmod -R 777, chown -R, sudo

* NFKC + casefold normalization defeats Unicode/full-width bypass
  (e.g. ``ｓｕｄｏ`` → ``sudo``).

* ``DENY_FLAGS``: 17 entries across 6 categories (force, delete, hard,
  shell, clean_aggressive, skip_verify). R12 expanded the legacy 5-flag
  set (``--delete`` / ``--force`` / ``--hard`` / ``rm`` / ``sudo``) to
  cover R12.S2-identified short/long variants including ``--no-verify``
  and ``--no-gpg-sign``.

* Carve-out token: process-local UUID, pid-keyed, single-use, never
  serialized to env or disk. ``issue_carve_out_token()`` mints; first
  ``check_deny(..., bypass_token=...)`` consumes.

V8 hardening: ``check_deny()`` returns a tuple-compatible
``DenyDecision`` with explicit category/reason fields, enforces declared
deny flags, and fails closed for ambiguous shell/file-destructive forms.

Advisory-only: this module performs NO subprocess execution.
"""

from __future__ import annotations

import os
import re
import shlex
import unicodedata
import uuid
from typing import NamedTuple, Optional

# __SLOT_FAIL_FAST_2026_07_25__ Swallowed failures route through one choke
# point: counted + named always, re-raised under AGI_V8_STRICT_FAIL_FAST.
from agi_v8_1.policy.fail_fast import swallowed as _swallowed


class DenyDecision(NamedTuple):
    """Machine-readable deny decision.

    Tuple ordering preserves the legacy ``(class_name, matched_pattern)``
    contract while adding explicit category/reason fields for callers that
    need auditable policy outcomes.
    """

    class_name: str
    matched_pattern: str
    category: str
    reason: str

# Class-1: irreversible / destructive system state
# Class-2: network-bound code execution
# Class-3: privilege/permission elevation (confirm required)
DENY_CLASSES: dict[str, list[re.Pattern]] = {
    "class-1-irreversible": [
        re.compile(
            r"\bgit\s+push\b.*"
            r"(?:--force(?:\b|=)|--force-with-lease(?:=\S*)?\b|"
            r"--force-if-includes(?:=\S*)?\b|--mirror\b|\+(?:head|refs/))",
            re.IGNORECASE,
        ),
        re.compile(r"\bgit\s+reset\s+--hard\b", re.IGNORECASE),
        re.compile(r"\bgit\s+clean\s+-[a-z]*f[a-z]*d?[a-z]*\b", re.IGNORECASE),
        re.compile(
            r"\brm\s+-[a-z]*r[a-z]*f[a-z]*\b|\brm\s+-[a-z]*f[a-z]*r[a-z]*\b",
            re.IGNORECASE,
        ),
        re.compile(r"\bmkfs\b", re.IGNORECASE),
        re.compile(r"\bdd\s+if=.+of=/dev/", re.IGNORECASE),
    ],
    "class-2-network-exec": [
        re.compile(r"\bcurl\b.+\|\s*(sh|bash|zsh|python)\b", re.IGNORECASE),
        re.compile(r"\bwget\b.+\|\s*(sh|bash|zsh|python)\b", re.IGNORECASE),
        re.compile(r"\bnc\b\s+-l", re.IGNORECASE),
        re.compile(r"\bssh\s+-[a-zA-Z]*R\b", re.IGNORECASE),
    ],
    "class-3-confirm": [
        re.compile(
            r"\bchmod\s+-[a-zA-Z]*R[a-zA-Z]*\s+(?:0?777|a\+rwx)",
            re.IGNORECASE,
        ),
        re.compile(r"\bchown\s+-[a-zA-Z]*R[a-zA-Z]*\b", re.IGNORECASE),
        re.compile(r"\bsudo\b", re.IGNORECASE),
    ],
}

_CLASS_METADATA: dict[str, tuple[str, str]] = {
    "class-1-irreversible": (
        "irreversible",
        "command matches an irreversible/destructive deny rule",
    ),
    "class-2-network-exec": (
        "network_exec",
        "command matches a network-bound code execution deny rule",
    ),
    "class-3-confirm": (
        "confirm_required",
        "command requires explicit confirmation and is denied by default",
    ),
}

_SHELL_INTERPRETERS = {
    "bash",
    "cmd",
    "dash",
    "fish",
    "ksh",
    "powershell",
    "pwsh",
    "sh",
    "zsh",
}

_DEFAULT_DENY_FILE_COMMANDS = {
    "rm": ("class-1-irreversible", "file_delete", "rm deletes files"),
    "rmdir": (
        "class-1-irreversible",
        "file_delete",
        "rmdir deletes directories",
    ),
    "shred": (
        "class-1-irreversible",
        "file_delete",
        "shred destroys file contents",
    ),
    "truncate": (
        "class-1-irreversible",
        "file_overwrite",
        "truncate can discard file contents",
    ),
    "unlink": ("class-1-irreversible", "file_delete", "unlink deletes files"),
    "chmod": (
        "class-3-confirm",
        "permission_change",
        "chmod changes file permissions",
    ),
    "chown": (
        "class-3-confirm",
        "ownership_change",
        "chown changes file ownership",
    ),
}

# Process-local carve-out token registry.
# Token = process-local UUID, single-use, consumed on read, never serialised to
# env or disk. Keyed by os.getpid() so a forked child cannot reuse a parent
# token unless explicitly handed over via a non-env path.
_CARVE_OUT_TOKENS: dict[int, set[str]] = {}


def issue_carve_out_token() -> str:
    """Issue a single-use bypass token for framework-internal destructive ops.

    Returns a UUID hex string that must be consumed on the next
    :func:`check_deny` call. Token is keyed to current PID and is NOT
    propagated to subprocess env.
    """
    pid = os.getpid()
    token = uuid.uuid4().hex
    _CARVE_OUT_TOKENS.setdefault(pid, set()).add(token)
    return token


def _consume_carve_out_token(token: str) -> bool:
    """Consume a carve-out token (single-use).

    Returns True if valid and consumed; False otherwise. After consumption
    the token is removed from the registry and subsequent calls return False.
    """
    pid = os.getpid()
    tokens = _CARVE_OUT_TOKENS.get(pid, set())
    if token in tokens:
        tokens.discard(token)
        return True
    return False


def _normalize(cmd: str) -> str:
    """NFKC + casefold for case/Unicode bypass resistance.

    Defeats full-width letters (ｓｕｄｏ → sudo), mixed case (SuDo → sudo),
    and other NFKC-collapsible variants.
    """
    return unicodedata.normalize("NFKC", cmd).casefold()


def _decision(
    class_name: str,
    matched_pattern: str,
    category: str | None = None,
    reason: str | None = None,
) -> DenyDecision:
    default_category, default_reason = _CLASS_METADATA[class_name]
    return DenyDecision(
        class_name=class_name,
        matched_pattern=matched_pattern,
        category=category or default_category,
        reason=reason or default_reason,
    )


def _has_short_flag(token: str, flag: str) -> bool:
    return (
        token.startswith("-")
        and not token.startswith("--")
        and flag in token[1:]
    )


def _tokenized_deny_hit(normalized: str) -> Optional[DenyDecision]:
    """Token-level catch for destructive forms the regexes under-match."""
    try:
        tokens = shlex.split(normalized)
    except ValueError as _ff_exc:
        _swallowed(_ff_exc, site="enforcement.destructive_command_policy._tokenized_deny_hit:220", category="apply")
        return _decision(
            "class-3-confirm",
            "token:parse_error",
            "ambiguous_command",
            "command could not be parsed safely",
        )
    if not tokens:
        return None

    head = tokens[0]
    if head in _SHELL_INTERPRETERS:
        return _decision(
            "class-3-confirm",
            f"token:shell:{head}",
            "shell_exec",
            "shell interpreter commands are denied by default",
        )

    if head in _DEFAULT_DENY_FILE_COMMANDS:
        class_name, category, reason = _DEFAULT_DENY_FILE_COMMANDS[head]
        return _decision(class_name, f"token:file_command:{head}", category, reason)

    if head == "dd" and any(token.startswith("of=") for token in tokens[1:]):
        return _decision(
            "class-1-irreversible",
            "token:dd_output",
            "file_overwrite",
            "dd with an output target can overwrite devices or files",
        )

    if head == "sed" and any(
        token == "-i" or token.startswith("-i") or token.startswith("--in-place")
        for token in tokens[1:]
    ):
        return _decision(
            "class-1-irreversible",
            "token:sed_in_place",
            "file_overwrite",
            "sed in-place editing can overwrite files",
        )

    if head in {"cp", "mv", "ln"}:
        for token in tokens[1:]:
            if token == "--force" or _has_short_flag(token, "f"):
                return _decision(
                    "class-3-confirm",
                    f"token:{head}_force",
                    "file_overwrite",
                    f"{head} force mode can overwrite filesystem state",
                )

    if head == "tar" and any(
        token in {"--extract", "--get"} or _has_short_flag(token, "x")
        for token in tokens[1:]
    ):
        return _decision(
            "class-3-confirm",
            "token:tar_extract",
            "file_overwrite",
            "tar extraction can overwrite filesystem state",
        )

    if any(token in DENY_FLAGS["skip_verify"] for token in tokens[1:]):
        return _decision(
            "class-3-confirm",
            "flag:skip_verify",
            "skip_verify",
            "commands that skip verification are denied by default",
        )

    if len(tokens) >= 2 and tokens[0] == "git" and tokens[1] == "push":
        for token in tokens[2:]:
            if (
                token == "--force"
                or token.startswith("--force=")
                or token == "--force-with-lease"
                or token.startswith("--force-with-lease=")
                or token == "--force-if-includes"
                or token.startswith("--force-if-includes=")
                or token == "--mirror"
                or token.startswith("+head")
                or token.startswith("+refs/")
            ):
                return _decision(
                    "class-1-irreversible",
                    "token:git_push_force",
                    "force",
                    "git push force/mirror/refspec rewrites remote history",
                )

    if len(tokens) >= 2 and tokens[0] == "git":
        subcommand = tokens[1]
        if subcommand == "checkout" and any(
            token == "--force" or _has_short_flag(token, "f")
            for token in tokens[2:]
        ):
            return _decision(
                "class-1-irreversible",
                "token:git_checkout_force",
                "force",
                "git checkout force can discard worktree changes",
            )
        if subcommand == "branch" and any(
            token == "-D" or token == "--delete" or token == "--delete-force"
            for token in tokens[2:]
        ):
            return _decision(
                "class-1-irreversible",
                "token:git_branch_delete",
                "delete",
                "git branch deletion is denied by default",
            )

    if tokens[0] == "rm":
        recursive = False
        force = False
        for token in tokens[1:]:
            if token in {"--recursive", "--dir", "-R"}:
                recursive = True
            elif token == "--force":
                force = True
            elif token.startswith("-") and not token.startswith("--"):
                flags = token[1:]
                recursive = recursive or "r" in flags or "R" in flags
                force = force or "f" in flags
        if recursive and force:
            return _decision(
                "class-1-irreversible",
                "token:rm_recursive_force",
                "file_delete",
                "rm recursive force deletes files without confirmation",
            )

    return None


def _find_deny_hit(normalized: str) -> Optional[DenyDecision]:
    token_hit = _tokenized_deny_hit(normalized)
    if token_hit is not None:
        return token_hit
    for class_name, patterns in DENY_CLASSES.items():
        for pat in patterns:
            if pat.search(normalized):
                return _decision(class_name, pat.pattern)
    return None


def check_deny(
    cmd: str, bypass_token: Optional[str] = None
) -> Optional[DenyDecision]:
    """Check if a command is in any deny class.

    Args:
        cmd: Full command string (joined tokens for token-mode invocations
            or the raw ``bash -c`` payload for shell-form).
        bypass_token: Optional process-local carve-out token previously minted
            by :func:`issue_carve_out_token`. Consumed on use.

    Returns:
        None if allowed; :class:`DenyDecision` if denied. The decision remains
        tuple-compatible with the legacy ``(class_name, matched_pattern)``
        shape, and additionally carries ``category`` and ``reason``.
    """
    normalized = _normalize(cmd)
    hit = _find_deny_hit(normalized)
    if hit is None:
        return None
    if bypass_token and _consume_carve_out_token(bypass_token):
        return None
    return hit


# 17-entry expansion (R12.S2). Legacy 5-flag baseline preserved
# (``--delete`` / ``--force`` / ``--hard`` / ``rm`` / ``sudo``).
DENY_FLAGS: dict[str, set[str]] = {
    "force": {"-f", "--force", "--force-if-includes", "--force-with-lease"},
    "delete": {"-d", "-D", "--delete", "--delete-force"},
    "hard": {"--hard", "--mirror"},
    "shell": {"rm", "sudo"},
    "clean_aggressive": {"-fd", "-fdx", "-fx"},
    "skip_verify": {"--no-verify", "--no-gpg-sign"},
}


__all__ = [
    "DENY_CLASSES",
    "DENY_FLAGS",
    "DenyDecision",
    "check_deny",
    "issue_carve_out_token",
]
