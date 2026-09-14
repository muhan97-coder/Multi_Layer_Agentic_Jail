"""Artifact verification helpers — ported verbatim from agi_v7.1.

Validates that executor task outputs exist on disk and are non-empty.
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = ["verify_pytest_output", "verify_artifact_list", "verify_sample_pdf"]


def verify_pytest_output(output_file: str | Path, min_size: int = 1) -> bool:
    """Check that the pytest output file exists and has content."""
    path = Path(output_file)
    if not path.exists() or path.stat().st_size < min_size:
        logger.warning("Pytest output artifact missing or empty: %s", output_file)
        return False
    return True


def verify_artifact_list(artifacts: list[str | Path]) -> bool:
    """Verify that all listed artifact files exist and are non-empty."""
    all_ok = True
    for artifact in artifacts:
        if not verify_pytest_output(artifact):
            all_ok = False
    return all_ok


def verify_sample_pdf(pdf_path: str | Path) -> bool:
    """Verify that the sample PDF artifact exists and has content."""
    path = Path(pdf_path)
    if not path.exists() or path.stat().st_size < 10:
        logger.warning("Sample PDF missing or too small: %s", pdf_path)
        return False
    return True
