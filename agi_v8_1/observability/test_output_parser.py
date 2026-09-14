# __TEST_OUTPUT_PARSER__   <- slot marker (preserved from v7.1)
"""Parse pytest stdout/stderr to extract structured failure information.

Verbatim port of agi_v7.1/agent_system/observability/test_output_parser.py.
Fail-open: all public functions catch all exceptions and return safe empty
defaults.
"""
from __future__ import annotations

import re
from typing import Any

# __SLOT_FAIL_FAST_2026_07_25__ Swallowed failures route through one choke
# point: counted + named always, re-raised under AGI_V8_STRICT_FAIL_FAST.
from agi_v8_1.policy.fail_fast import swallowed as _swallowed

__all__ = ["parse_pytest_failures"]

# Matches lines like:
#   FAILED tests/foo/bar.py::TestClass::test_method - reason
#   FAILED tests/foo/bar.py::test_function
_FAILED_LINE_RE = re.compile(r"^FAILED\s+([\w./\\-]+(?:::\S+)*)", re.MULTILINE)

# Matches pytest assertion/exception lines beginning with "E "
# (after optional leading whitespace)
_E_LINE_RE = re.compile(r"^\s*E\s+(.+)", re.MULTILINE)


def parse_pytest_failures(text: str, *, max_failed: int = 50) -> dict[str, Any]:
    """Return {"failed_tests": [...], "error_summary": "..."} from pytest output.

    - ``failed_tests``: de-duplicated list of test node IDs, capped at ``max_failed``.
    - ``error_summary``: first non-empty match of ``E <message>`` lines, capped at 240 chars.

    Never raises — returns empty defaults on any error.
    """
    try:
        if not text or not isinstance(text, str):
            return {"failed_tests": [], "error_summary": ""}

        # --- failed_tests ---
        seen: dict[str, None] = {}  # ordered-set via insertion-ordered dict
        for m in _FAILED_LINE_RE.finditer(text):
            node_id = m.group(1).strip()
            if node_id and node_id not in seen:
                seen[node_id] = None
            if len(seen) >= max_failed:
                break
        failed_tests = list(seen.keys())

        # --- error_summary ---
        error_summary = ""
        for m in _E_LINE_RE.finditer(text):
            line = m.group(1).strip()
            if line:
                error_summary = line[:240]
                break

        return {"failed_tests": failed_tests, "error_summary": error_summary}

    except Exception as _ff_exc:  # noqa: BLE001 - fail-open by contract
        _swallowed(_ff_exc, site="observability.test_output_parser.parse_pytest_failures:57", category="telemetry")
        return {"failed_tests": [], "error_summary": ""}
