# R25 W4 (ported from v7.1 agents/executor_integrity.py - __RPLAN_R6_S6__)
"""Runtime integrity checks for builder execution output.

Validation functions to detect anti-simulation patterns, replace-line
mistakes, and missing evidence before a task is marked completed.

R6.S6 honest-evidence rules (preserved):
  - Substring mention of "stdout"/"exit_code"/... in implementation_notes
    is NOT evidence on its own. Must be paired with a captured-output
    field in task_results.
  - A non-empty artifacts list is NOT evidence on its own. At least one
    listed path must exist on disk inside project_root and be non-empty.
  - task_results entries carrying captured output (stdout/stderr/
    exit_code/return_code/artifacts) remain valid evidence.

Usage:
    from agi_v8_1.enforcement import validate_builder_output
    errors = validate_builder_output(builder_output)
    if errors:
        raise ValidationError(f"Builder output integrity failed: {errors}")
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

# __SLOT_FAIL_FAST_2026_07_25__ Swallowed failures route through one choke
# point: counted + named always, re-raised under AGI_V8_STRICT_FAIL_FAST.
from agi_v8_1.policy.fail_fast import swallowed as _swallowed

# Patterns indicating a builder is faking execution
_FAKE_EXECUTION_MARKERS = [
    "executor_task",
    "stub declaration",
    "no run_command",
    "no safeautoapply",
    "zero execution evidence",
    "falsely claimed",
    "path mismatch",
    "simulate",
    "zero artifacts",
]

_AGENT_SYSTEM_PATH_PREFIX = "agent_system/"
_V8_PATH_PREFIX = "agi_v8_1/"

# Paired-hint keywords (R6.S6): require BOTH presence in notes AND a real
# captured-output field on task_results to count as evidence.
_CAPTURED_OUTPUT_HINTS = (
    "stdout",
    "stderr",
    "exit_code",
    "run_command",
    "safeautoapply",
    "checkpoint",
)


def _check_empty_file_changes(
    file_changes: Optional[List[Dict[str, Any]]],
) -> Optional[str]:
    """Return error string if file_changes is missing/empty or has empty content."""
    if not file_changes:
        return "file_changes is missing or empty."
    for fc in file_changes:
        if not isinstance(fc, dict):
            continue
        content = fc.get("content", "")
        if not content or not str(content).strip():
            return (
                f"Empty content in file_changes for path={fc.get('path', 'unknown')}"
            )
    return None


def _artifacts_verified_on_disk(
    artifacts: Any,
    *,
    project_root: Optional[Path] = None,
) -> bool:
    """True iff at least one entry in *artifacts* points to a real, non-empty file.

    Containment check vs project_root; size>0 required.
    """
    if not isinstance(artifacts, list) or not artifacts:
        return False
    if project_root is None:
        env_root = os.environ.get("PROJECT_ROOT")
        project_root = Path(env_root) if env_root else Path.cwd()
    try:
        root_resolved = Path(project_root).resolve()
    except OSError as _ff_exc:
        _swallowed(_ff_exc, site="enforcement.executor_integrity._artifacts_verified_on_disk:90", category="apply")
        return False
    for entry in artifacts:
        candidate = (
            str(entry.get("path", "")).strip()
            if isinstance(entry, dict)
            else str(entry).strip()
        )
        if not candidate:
            continue
        try:
            full_path = (root_resolved / candidate).resolve()
            full_path.relative_to(root_resolved)
        except (OSError, ValueError) as _ff_exc:
            _swallowed(_ff_exc, site="enforcement.executor_integrity._artifacts_verified_on_disk:103", category="apply")
            continue
        try:
            if full_path.is_file() and full_path.stat().st_size > 0:
                return True
        except OSError as _ff_exc:
            _swallowed(_ff_exc, site="enforcement.executor_integrity._artifacts_verified_on_disk:108", category="apply")
            continue
    return False


def _task_results_have_captured_output(
    task_results: Any,
    *,
    project_root: Optional[Path] = None,
) -> bool:
    """True iff at least one task_result dict carries a non-empty captured-output field.

    Captured-output fields: stdout, stderr, output (non-empty string/bytes);
    exit_code, return_code (any non-None value); or artifacts list with at
    least one verified on-disk file.
    """
    if not isinstance(task_results, list):
        return False
    for tr in task_results:
        if not isinstance(tr, dict):
            continue
        for key in ("stdout", "stderr", "output"):
            val = tr.get(key)
            if isinstance(val, str) and val.strip():
                return True
            if isinstance(val, (bytes, bytearray)) and len(val) > 0:
                return True
        for key in ("exit_code", "return_code"):
            if key in tr and tr.get(key) is not None:
                return True
        if _artifacts_verified_on_disk(tr.get("artifacts"), project_root=project_root):
            return True
    return False


def _check_execution_evidence(
    builder_output: Dict[str, Any],
    *,
    project_root: Optional[Path] = None,
) -> Optional[str]:
    """Check for genuine run_command / SafeAutoApply evidence."""
    implementation_notes = str(builder_output.get("implementation_notes", "")).lower()
    artifacts = builder_output.get("artifacts", [])
    task_results = builder_output.get("task_results", [])

    notes_has_captured_hint = any(
        word in implementation_notes for word in _CAPTURED_OUTPUT_HINTS
    )
    task_results_have_captured = _task_results_have_captured_output(
        task_results, project_root=project_root
    )

    has_evidence = False
    # Paired-hint: prose mention only counts when paired with real captured output.
    if notes_has_captured_hint and task_results_have_captured:
        has_evidence = True
    if not has_evidence and _artifacts_verified_on_disk(
        artifacts, project_root=project_root
    ):
        has_evidence = True
    # task_results carrying captured output / artifacts are valid alone.
    if not has_evidence and task_results_have_captured:
        has_evidence = True

    if not has_evidence:
        # Diagnostic: anti-simulation markers
        for marker in _FAKE_EXECUTION_MARKERS:
            if marker in implementation_notes:
                return f"Builder output likely simulated: found '{marker}' in notes."
        if notes_has_captured_hint and not task_results_have_captured:
            return (
                "Execution-evidence keyword present in implementation_notes but no "
                "captured task_results — keyword substring alone is not real evidence."
            )
        if isinstance(artifacts, list) and artifacts:
            return (
                "Claimed artifacts could not be verified on disk inside project_root "
                "— unverified artifact paths are not real evidence."
            )
        return "No execution evidence (no artifacts, no task_results)."
    return None


def validate_builder_output(
    builder_output: Dict[str, Any],
    *,
    project_root: Optional[Path] = None,
) -> List[str]:
    """Validate builder output for execution discipline.

    Returns a list of error strings; empty list means valid.

    R6.S6 fail-closed: absent file_changes is treated identically to empty
    file_changes (previous fail-open gate ``if file_changes is not None``
    silently accepted a builder that produced no code at all).
    """
    errors: List[str] = []
    file_changes = builder_output.get("file_changes")
    err = _check_empty_file_changes(file_changes)
    if err:
        errors.append(err)
    err = _check_execution_evidence(builder_output, project_root=project_root)
    if err:
        errors.append(err)
    return errors


__all__ = ["validate_builder_output"]
