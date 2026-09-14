"""Fail-closed selection for the objective-driven patch producer.

The whole-file proposer (inc3) and anchored editor (inc4) both emit a full
postimage for each declared target.  Their jail-facing shadow paths differ,
but the verifier maps both postimages back to the same canonical target.  They
are therefore alternatives, not members of one atomic patch batch.
"""

from __future__ import annotations

import os
from typing import Mapping


PROPOSER_ENV = "AGI_V8_SI_OBJECTIVE_PROPOSER_ENABLED"
EDITOR_ENV = "AGI_V8_SI_OBJECTIVE_EDIT_ENABLED"

MODE_OFF = "off"
MODE_PROPOSER = "whole_file_proposer"
MODE_EDITOR = "anchored_editor"
CONFLICT_MESSAGE = (
    f"{PROPOSER_ENV} and {EDITOR_ENV} cannot both be enabled"
)


class ObjectivePatchModeConflict(ValueError):
    """Raised before cycle work when both alternative producers are armed."""


def proposer_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Return the strict, default-OFF inc3 gate state."""

    source = os.environ if env is None else env
    # Keep the explicit empty default beside the named gate.  Besides being
    # easier to audit, this lets the generated gate census prove default-OFF
    # instead of losing the default behind a generic ``name`` parameter.
    return source.get(PROPOSER_ENV, "") in ("true", "1")


def editor_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Return the strict, default-OFF inc4 gate state."""

    source = os.environ if env is None else env
    return source.get(EDITOR_ENV, "") in ("true", "1")


def select_mode(env: Mapping[str, str] | None = None) -> str:
    """Select exactly one producer, refusing the ambiguous both-ON state."""

    source = os.environ if env is None else env
    proposer = proposer_enabled(source)
    editor = editor_enabled(source)
    if proposer and editor:
        raise ObjectivePatchModeConflict(CONFLICT_MESSAGE)
    if proposer:
        return MODE_PROPOSER
    if editor:
        return MODE_EDITOR
    return MODE_OFF
