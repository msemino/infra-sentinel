"""Bounded read-only collection, run before the model is called.

Four blocks, roughly a tenth of a second, no model:

===================  ============================================================
``history``          samples of the trigger's item around the event, so duration
                     and cadence can be stated rather than estimated
``correlated``       what else moved in the same window, on any host
``siblings``         reachability checks on the host's most specific group, split
                     into the ones that failed and the ones that did not
``why_no_trigger``   the expanded trigger expression and its priority
===================  ============================================================

`siblings` is split rather than filtered because the checks that held carry as much
information as the ones that failed: everything down together points upstream, while one
protocol failing while another answers points at the protocol.

`max_hosts` / `max_items` bound the queries so a large host group cannot turn this into a
sweep of the monitoring API. Each block is caught separately: a failure degrades the report by
one paragraph and is named in it, never propagates.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Protocol


class Reader(Protocol):
    """The read-only surface `collect` needs. Implemented by the real client and by tests.

    Kept as a protocol rather than a concrete client so the evidence logic can be tested
    against hand-written answers. The interesting behaviour here is what is *asked for* and
    what is done with the answers, not the transport.
    """

    def history(self, itemid: str, since_ts: float, until_ts: float) -> list[dict]:
        """Samples of one item in a time range: ``[{"clock": int, "value": str}, ...]``."""

    def events_in_window(self, since_ts: float, until_ts: float) -> list[dict]:
        """Every event in a range, any host."""

    def group_reachability(self, host: str, max_hosts: int, max_items: int) -> list[dict]:
        """Reachability checks for the host's most specific group.

        ``[{"host": str, "item": str, "failed": bool}, ...]``, already capped by the caller's
        limits so an unbounded group cannot turn this into a sweep.
        """

    def trigger_expression(self, objectid: str) -> dict:
        """``{"expression": str, "priority": str}`` for one trigger."""


@dataclass
class Siblings:
    failed: list[dict] = field(default_factory=list)
    healthy: list[dict] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.failed) + len(self.healthy)


@dataclass
class Evidence:
    """Facts with timestamps, ready to be quoted. Never a conclusion."""

    history: list[dict] = field(default_factory=list)
    correlated: list[dict] = field(default_factory=list)
    siblings: Siblings = field(default_factory=Siblings)
    why_no_trigger: dict = field(default_factory=dict)
    # Blocks that raised, by name. Named in the report rather than silently missing:
    # a paragraph that is absent for a reason reads very differently from one that is
    # absent because there was nothing to say.
    degraded: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0

    @property
    def is_empty(self) -> bool:
        return not (self.history or self.correlated or self.siblings.total or self.why_no_trigger)


def _duration(samples: list[dict]) -> int | None:
    """Seconds between the first and last sample, or None if there is nothing to measure."""
    if len(samples) < 2:
        return None
    clocks = sorted(int(s["clock"]) for s in samples)
    return clocks[-1] - clocks[0]


def summarise_history(samples: list[dict]) -> dict:
    """Turn raw samples into the numbers a sentence can be built from.

    The cadence is derived from the median gap rather than the mean: one missing sample
    doubles a single gap, and the mean would report a cadence the poller never had.
    """
    if not samples:
        return {}
    ordered = sorted(samples, key=lambda s: int(s["clock"]))
    gaps = [int(b["clock"]) - int(a["clock"]) for a, b in zip(ordered, ordered[1:], strict=False)]
    cadence = None
    if gaps:
        gaps_sorted = sorted(gaps)
        cadence = gaps_sorted[len(gaps_sorted) // 2]
    return {
        "samples": len(ordered),
        "duration_s": _duration(ordered),
        "cadence_s": cadence,
        "first_value": ordered[0]["value"],
        "last_value": ordered[-1]["value"],
        "gap_detected": bool(cadence and max(gaps) > cadence * 1.5),
    }


def collect(
    reader: Reader,
    event: dict,
    now_ts: float,
    window_sec: int,
    max_hosts: int,
    max_items: int,
) -> Evidence:
    """Run the four blocks. Never raises: a failed block is recorded, not propagated."""
    started = time.monotonic()
    ev = Evidence()
    since, until = now_ts - window_sec, now_ts

    itemid = str(event.get("itemid", "") or "")
    host = str(event.get("host", "") or "")
    objectid = str(event.get("objectid", "") or "")

    if itemid:
        try:
            ev.history = reader.history(itemid, since, until)
        except Exception:  # noqa: BLE001 — degrade the report, never the investigation
            ev.degraded.append("history")

    try:
        mine = str(event.get("eventid", ""))
        ev.correlated = [
            e for e in reader.events_in_window(since, until)
            if str(e.get("eventid", "")) != mine
        ]
    except Exception:  # noqa: BLE001
        ev.degraded.append("correlated")

    if host:
        try:
            checks = reader.group_reachability(host, max_hosts, max_items)
            ev.siblings = Siblings(
                failed=[c for c in checks if c.get("failed")],
                healthy=[c for c in checks if not c.get("failed")],
            )
        except Exception:  # noqa: BLE001
            ev.degraded.append("siblings")

    if objectid:
        try:
            ev.why_no_trigger = reader.trigger_expression(objectid)
        except Exception:  # noqa: BLE001
            ev.degraded.append("why_no_trigger")

    ev.elapsed_s = time.monotonic() - started
    return ev
