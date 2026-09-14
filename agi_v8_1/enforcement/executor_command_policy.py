# R25 W4 (ported from v7.1 agents/executor_command_policy.py)
"""Pure command allowlist + helper policy for V8 executors.

Subset of v7.1's executor_command_policy.py: allowlist + describe +
allow/read-only lookup. The v7.1 file additionally embeds shell-parsing /
validation / subprocess plumbing that is NOT advisory-safe and is therefore
deliberately excluded from this V8 port (V8 executors do not exist yet —
this layer is purely declarative for downstream agents).

R5 invariant preserved: NO bare ``("git",)`` catch-all entry. Every git
subcommand must be EXPLICITLY listed; destructive subcommands (restore,
stash, checkout, branch, reset, clean) are deliberately omitted so the
allowlist-by-default policy rejects them.

R12 coupling: this module re-exports ``destructive_command_policy`` so a
single import gives callers both layers (allowlist + DENY_CLASSES).

V8 hardening: ``evaluate_command()`` composes the allowlist with destructive
policy decisions so prefix matches cannot silently allow explicit deny flags,
shell pipelines, or dangerous filesystem actions.
"""

from __future__ import annotations

import os
from typing import NamedTuple

# Re-export the machine-readable deny schema (R12 invariant).
from . import destructive_command_policy as destructive_policy  # noqa: F401


class CommandDecision(NamedTuple):
    """Structured executor policy result."""

    allowed: bool
    read_only: bool
    category: str
    reason: str
    matched_prefix: tuple[str, ...] | None = None
    destructive_hit: destructive_policy.DenyDecision | None = None

# Each tuple: (command_prefix_tokens, read_only)
# Commands must start with one of these token sequences.
# read_only=True means the command does not modify the filesystem.
COMMAND_ALLOWLIST: list[tuple[tuple[str, ...], bool]] = [
    # crow-L4-meta metacognition
    (("ask_self",), True),
    # Git (subcommand-level only — no bare ("git",) catch-all; R5 invariant).
    (("git", "log"), True),
    (("git", "show"), True),
    (("git", "diff"), True),
    (("git", "blame"), True),
    (("git", "rev-parse"), True),
    (("git", "ls-files"), True),
    (("git", "status"), True),
    (("git", "add"), False),
    (("git", "commit"), False),
    (("git", "grep"), True),
    # Search
    (("grep",), True),
    (("rg",), True),
    (("ripgrep",), True),
    # Interpreter probes
    (("node", "--version"), True),
    (("node", "-v"), True),
    (("node", "--check"), True),
    (("node", "-c"), True),
    (("node",), False),
    (("python3", "-c"), True),
    (("python", "-c"), True),
    (("python3", "-m", "py_compile"), True),
    (("python", "-m", "py_compile"), True),
    (("python3", "-m", "pytest"), False),
    (("python", "-m", "pytest"), False),
    (("python3",), False),
    (("python",), False),
    (("uvicorn",), False),
    # Test runners and JS package scripts
    (("npm", "test"), False),
    (("npx", "vitest"), False),
    (("pytest",), False),
    (("tox",), False),
    # Build commands
    (("npm", "run", "build"), False),
    (("npx", "tsc"), False),
    (("npx", "vite", "build"), False),
    # Local formatting/check
    (("black",), True),
    (("isort",), True),
    (("pylint",), True),
    (("pre-commit", "validate-config"), True),
    # Filesystem
    (("mkdir",), False),
    (("ls",), True),
    (("cp",), False),
    (("mv",), False),
    (("touch",), False),
    (("ln",), False),
    # Utility
    (("wc",), True),
    (("sort",), True),
    (("head",), True),
    (("tail",), True),
    (("cat",), True),
    (("find",), True),
    (("echo",), True),
    (("printf",), True),
    (("tee",), False),
    (("test",), True),
    (("basename",), True),
    (("dirname",), True),
    (("realpath",), True),
    (("stat",), True),
    (("diff",), True),
    (("sed",), False),
    (("patch", "--version"), True),
    (("patch",), False),
    (("awk",), True),
    (("xargs",), True),
    (("pwd",), True),
    (("which",), True),
    (("type",), True),
    (("command",), True),
    (("jq",), True),
    (("sqlite3",), True),
    (("sha256sum",), True),
    (("md5sum",), True),
    (("tar",), False),
    (("date",), True),
    (("du",), True),
    (("uname",), True),
    (("df",), True),
    (("free",), True),
    (("uptime",), True),
    # Network (GET only - downstream validate_curl_get_only enforces semantics)
    (("curl",), True),
    (("wget",), False),
    # Package management
    (("pip3", "install"), False),
    (("pip3", "freeze"), True),
    (("pip3", "list"), True),
    (("pip3", "show"), True),
    (("pip", "install"), False),
    (("pip", "freeze"), True),
    (("pip", "list"), True),
    (("pip", "show"), True),
    (("python3", "-m", "pip"), False),
    (("python", "-m", "pip"), False),
    # Document conversion
    (("pandoc",), True),
]


def describe_allowed_commands() -> str:
    """Compact, stable allowlist description for executor errors.

    COMMAND_ALLOWLIST intentionally has multiple entries per base command so
    subcommands can carry precise read/write metadata. User-facing denial
    messages should not expose that internal duplication.
    """
    bases = sorted(
        {prefix[0] for prefix, _read_only in COMMAND_ALLOWLIST if prefix}
    )
    return ", ".join([*bases, "timeout-wrapper", "export-env-prefix"])


COMMAND_TIMEOUT: int = int(os.getenv("EXECUTOR_COMMAND_TIMEOUT", "300"))
COMMAND_MAX_OUTPUT: int = 512_000

_SHELL_OPERATOR_TOKENS = {
    "&&",
    "&",
    "||",
    "|",
    ";",
    ">",
    ">>",
    "<",
    "<<",
    "2>",
    "2>>",
}


def _match_prefix(
    tokens: tuple[str, ...] | list[str],
    allowlist: list[tuple[tuple[str, ...], bool]] = COMMAND_ALLOWLIST,
) -> tuple[tuple[str, ...], bool] | None:
    """Return (matched_prefix, read_only) or None.

    Prefix matching: tokens must start with the entry's prefix tuple.
    First match wins (allowlist is ordered most-specific-first per command).
    """
    if not tokens:
        return None
    tup = tuple(tokens)
    for prefix, read_only in allowlist:
        if not prefix:
            continue
        if len(tup) < len(prefix):
            continue
        if tup[: len(prefix)] == prefix:
            return (prefix, read_only)
    return None


def _as_token_tuple(tokens: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    return tuple(str(token) for token in tokens)


def _has_shell_operator(tokens: tuple[str, ...]) -> bool:
    return any(token in _SHELL_OPERATOR_TOKENS for token in tokens)


def evaluate_command(tokens: tuple[str, ...] | list[str]) -> CommandDecision:
    """Evaluate executor command tokens against all policy layers.

    This composes the allowlist with ``destructive_command_policy`` so a
    prefix match cannot silently allow an explicit deny flag, shell pipeline,
    or dangerous filesystem action.
    """
    tup = _as_token_tuple(tokens)
    if not tup:
        return CommandDecision(
            allowed=False,
            read_only=False,
            category="empty",
            reason="empty command tokens are denied",
        )

    destructive_hit = destructive_policy.check_deny(" ".join(tup))
    if destructive_hit is not None:
        return CommandDecision(
            allowed=False,
            read_only=False,
            category=destructive_hit.category,
            reason=destructive_hit.reason,
            destructive_hit=destructive_hit,
        )

    if _has_shell_operator(tup):
        return CommandDecision(
            allowed=False,
            read_only=False,
            category="ambiguous_shell",
            reason="shell control operators require shell-form validation",
        )

    hit = _match_prefix(tup)
    if hit is None:
        return CommandDecision(
            allowed=False,
            read_only=False,
            category="not_allowlisted",
            reason="command prefix is not in the executor allowlist",
        )

    prefix, read_only = hit
    return CommandDecision(
        allowed=True,
        read_only=read_only,
        category="read_only" if read_only else "explicit_write",
        reason=(
            "matched read-only executor allowlist prefix"
            if read_only
            else "matched explicit write executor allowlist prefix"
        ),
        matched_prefix=prefix,
    )


def is_command_allowed(tokens: tuple[str, ...] | list[str]) -> bool:
    """Return True iff *tokens* match an allowlist entry (prefix match)."""
    return evaluate_command(tokens).allowed


def is_read_only_command(tokens: tuple[str, ...] | list[str]) -> bool:
    """Return True iff *tokens* match an allowlist entry marked read_only.

    False for both "writes filesystem" and "not in allowlist" — callers
    that need to distinguish should first check :func:`is_command_allowed`.
    """
    decision = evaluate_command(tokens)
    return bool(decision.allowed and decision.read_only)


__all__ = [
    "COMMAND_ALLOWLIST",
    "COMMAND_TIMEOUT",
    "COMMAND_MAX_OUTPUT",
    "CommandDecision",
    "describe_allowed_commands",
    "evaluate_command",
    "is_command_allowed",
    "is_read_only_command",
]
