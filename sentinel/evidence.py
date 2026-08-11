"""The deterministic evidence layer — what gets collected *before* the model is called.

With nothing but "host + trigger name + last value", a language model can only say that
something happened on a host. That is the sentence the operator already had.

What made a good manual diagnosis good was never eloquence: it was four boring lookups
anyone can do, and the model was never going to guess their answers. So they are done here,
in code, and handed to the model as facts with timestamps. The model writes the paragraph
and cross-references the runbook; it does not investigate.

The four blocks, and what each one *licenses you to say*:

===================  ============================================================
``history``          "it lasted 2m44s, six samples at 100% every 15 seconds"
                     instead of "it was down for a while"
``correlated``       what else moved in the same window, on any host
``siblings``         the reachability checks on the host's most specific group,
                     **split into the ones that failed and the ones that did not**
``why_no_trigger``   the expanded trigger expression and its priority — "a sample
                     was missing" instead of "nobody told me"
===================  ============================================================

The third one is the one worth arguing for. "Wired and wireless went down together" points
upstream. "HTTP kept answering while ICMP and DNS died" points at a protocol, not a link.
**The checks that did not fail narrow the hypothesis as much as the ones that did** — and a
collector that only gathers failures can never produce that second sentence.

Cost and blast radius:

* The whole collection is four bounded read-only queries. It takes on the order of a tenth
  of a second and needs no model, so it happens on every investigation rather than being
  saved for the interesting ones.
* ``max_hosts`` / ``max_items`` are what keep "check the host's siblings" from turning into
  a sweep of the monitoring API on a large group.
* Every block is in its own ``try``/``except``. A hiccup in one degrades the report by one
  paragraph; it does not lose the investigation. The alert has already gone out regardless
  (see :mod:`sentinel.cli`) — this layer can only ever make a report better or shorter.
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
