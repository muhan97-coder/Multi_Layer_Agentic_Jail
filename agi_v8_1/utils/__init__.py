"""agi_v8_1.utils — R27 lean utility helpers (no subprocess/network at top).

Public surface ported from agi_v7.1/agent_system/utils.py + utils/ subpackage:
- ID/run/time helpers (utc_now, generate_run_id, generate_task_id)
- atomic JSON I/O (load_json, save_json, ensure_directory)
- hash helpers (stable_hash, content_hash)
- exception hierarchy (AgentSystemError + ValidationError + StorageError ...)
- FailureLogger (lightweight failure persistence)
- artifact_checks helpers (verify_pytest_output, verify_artifact_list)
- debug_log.log_debug_event

R27 invariants:
- env knobs OFF by default
- no subprocess/requests/urllib/socket/http at module top
- all heavy operational code deferred (`# R27: deferred for later round`)
"""

from agi_v8_1.utils.exceptions import (
    AgentSystemError,
    ValidationError,
    ProviderError,
    StorageError,
    PipelineStepError,
    PathTraversalError,
)
from agi_v8_1.utils.time_ids import (
    utc_now,
    utc_now_dt,
    generate_run_id,
    generate_task_id,
    monotonic_ns,
)
from agi_v8_1.utils.io_helpers import (
    ensure_directory,
    load_json,
    load_jsonl,
    save_json,
    atomic_write_text,
)
from agi_v8_1.utils.hashing import (
    stable_hash,
    content_hash,
    json_signature,
)
from agi_v8_1.utils.failure_logger import FailureLogger
from agi_v8_1.utils.artifact_checks import (
    verify_pytest_output,
    verify_artifact_list,
    verify_sample_pdf,
)
from agi_v8_1.utils.debug_log import log_debug_event

__all__ = [
    "AgentSystemError",
    "ValidationError",
    "ProviderError",
    "StorageError",
    "PipelineStepError",
    "PathTraversalError",
    "utc_now",
    "utc_now_dt",
    "generate_run_id",
    "generate_task_id",
    "monotonic_ns",
    "ensure_directory",
    "load_json",
    "load_jsonl",
    "save_json",
    "atomic_write_text",
    "stable_hash",
    "content_hash",
    "json_signature",
    "FailureLogger",
    "verify_pytest_output",
    "verify_artifact_list",
    "verify_sample_pdf",
    "log_debug_event",
]
