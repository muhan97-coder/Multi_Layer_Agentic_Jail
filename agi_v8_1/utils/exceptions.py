"""Exception hierarchy — ported from agi_v7.1/agent_system/utils.py public API.

These exception types are used as classification anchors across orchestrator,
SI, agents, providers. R27 ports them verbatim (zero behavioral coupling).
"""
from __future__ import annotations


class AgentSystemError(Exception):
    """Base exception for recoverable system errors."""


class ValidationError(AgentSystemError):
    """Raised when structured JSON output does not satisfy the schema."""


class ProviderError(AgentSystemError):
    """Raised when a provider cannot produce usable structured output."""


class StorageError(AgentSystemError):
    """Raised when persistent state or memory cannot be read or written."""


class PipelineStepError(AgentSystemError):
    """Raised when a pipeline phase fails and should propagate clearly."""


class PathTraversalError(AgentSystemError):
    """Raised when a path attempts to escape the allowed workspace."""


__all__ = [
    "AgentSystemError",
    "ValidationError",
    "ProviderError",
    "StorageError",
    "PipelineStepError",
    "PathTraversalError",
]
