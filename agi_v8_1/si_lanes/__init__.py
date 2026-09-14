"""V8 SI 12-lane logic helpers (R20 W1 — ported from v7.1 SI agent).

This package decomposes the v7.1 SelfImprovementAgent (1,617 LoC) across
four lane-kind modules:

  - proposer.py        — emit SIPolicyTicket per DecisionRecord (4 lanes)
  - rollback_guard.py  — score rollback_risk per ticket (4 lanes)
  - outcome_observer.py — read recent outcomes from log (2 lanes)
  - ticket_writer.py   — stable-sort emitted tickets (2 lanes)

All four are pure logic — no provider calls, no shell, no network. They
operate over V8 message dataclasses and emit new immutable records only.
"""

from agi_v8_1.si_lanes.outcome_observer import observe_outcomes
from agi_v8_1.si_lanes.proposer import propose_tickets
from agi_v8_1.si_lanes.rollback_guard import score_rollback_risk
from agi_v8_1.si_lanes.ticket_writer import stable_sort_tickets

__all__ = [
    "observe_outcomes",
    "propose_tickets",
    "score_rollback_risk",
    "stable_sort_tickets",
]
