"""SI ticket_writer lane logic (R20 W1 — port of v7.1 SI ticket ordering).

v7.1 self_improvement_agent.py emits a sorted list of proposals for stable
diffing in the cycle log. The V8 port distils this into
:func:`stable_sort_tickets` which orders SIPolicyTicket records by
ticket_id (lexicographic).

Stable sort preserves input order for equal ticket_ids — important because
multiple lanes can emit tickets with the same ID prefix.

Hard rules:
  - pure logic, no I/O, no provider calls
  - import-clean: only stdlib + agi_v8_1.core.messages
"""

from __future__ import annotations

from typing import Sequence

from agi_v8_1.core.messages import SIPolicyTicket


def stable_sort_tickets(
    tickets: Sequence[SIPolicyTicket],
) -> tuple[SIPolicyTicket, ...]:
    """Stable-sort tickets by ticket_id (ascending).

    The sort is on ``(ticket_id,)`` only; ties keep input ordering since
    :func:`sorted` is stable. Returns a new tuple — input is not mutated.
    """

    return tuple(sorted(tickets, key=lambda t: t.ticket_id))


def merge_and_sort(
    *batches: Sequence[SIPolicyTicket],
) -> tuple[SIPolicyTicket, ...]:
    """Concatenate multiple batches and stable-sort the result.

    Convenience wrapper for the ticket_writer lane which typically
    consumes proposer + rollback_guard outputs.
    """

    merged: list[SIPolicyTicket] = []
    for batch in batches:
        merged.extend(batch)
    return stable_sort_tickets(merged)


__all__ = ["merge_and_sort", "stable_sort_tickets"]
