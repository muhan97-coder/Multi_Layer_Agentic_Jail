"""agi_v8_1 prompts layer.

R23 W2: ports the v7.1 prompts/ directory's structure as a Python
PromptLoader + small embedded core templates (so we never depend on
agent_system at import time). Heavy raw .md/.txt templates from v7.1 are
*not* embedded; callers needing the full text load files from a
``prompts_dir`` path supplied at PromptLoader construction time.

Env knob: ``AGI_V8_PROMPTS_ENABLED`` (default OFF). When OFF, ``load_prompt``
still returns the registered template (read-only): the knob only gates any
future *write* path (rule append / archive).
"""

from __future__ import annotations

from agi_v8_1.prompts.loader import (
    PromptLoader,
    PromptResolution,
    PromptTemplate,
    prompts_enabled,
    AGENT_NAMES,
    DEFAULT_TEMPLATES,
)

__all__ = [
    "PromptLoader",
    "PromptResolution",
    "PromptTemplate",
    "prompts_enabled",
    "AGENT_NAMES",
    "DEFAULT_TEMPLATES",
]
