"""The deterministic evidence layer and the verdict derived from it.

Two things are being pinned here.

**That a broken block degrades the report and nothing else.** The alert has already gone out
by the time any of this runs, so the worst a failure here may cost is a paragraph. A block
that raised must be named rather than silently missing — "we could not check the neighbours"
and "the neighbours were fine" are opposite findings.

**That the verdict is derived, not guessed.** Every line it emits has to be arithmetic or a
set comparison over measured facts, and when the evidence supports nothing it has to say
nothing. There is no default conclusion, because a confident sentence with nothing behind it
is the exact failure this project was written after.
"""

from __future__ import annotations

from sentinel.evidence import Evidence, Siblings, collect, summarise_history
from sentinel.verdict import (
    correlation_line,
    duration_line,
    scope_line,
    trigger_line,
    verdict,
)

EVENT = {"eventid": "500", "itemid": "42", "host": "switch-07", "objectid": "9001",
         "name": "Unavailable by ICMP ping"}


class FakeReader:
    """Answers the four queries from canned data; any of them can be told to raise."""

    def __init__(self, *, history=None, events=None, checks=None, trigger=None, boom=()):
        self._history = history or []
        self._events = events or []
        self._checks = checks or []
        self._trigger = trigger or {}
        self._boom = set(boom)
        self.seen: dict[str, tuple] = {}

    def _maybe_raise(self, name):
        if name in self._boom:
            raise RuntimeError(f"{name} unavailable")

    def history(self, itemid, since_ts, until_ts):
        self._maybe_raise("history")
        self.seen["history"] = (itemid, since_ts, until_ts)
        return self._history

    def events_in_window(self, since_ts, until_ts):
        self._maybe_raise("correlated")
        self.seen["events"] = (since_ts, until_ts)
        return self._events

    def group_reachability(self, host, max_hosts, max_items):
        self._maybe_raise("siblings")
        self.seen["siblings"] = (host, max_hosts, max_items)
        return self._checks

    def trigger_expression(self, objectid):
        self._maybe_raise("why_no_trigger")
        self.seen["trigger"] = (objectid,)
        return self._trigger


def _samples(start=1000, count=6, step=15, value="0"):
    return [{"clock": start + i * step, "value": value} for i in range(count)]


def _collect(reader):
    return collect(reader, EVENT, now_ts=2000, window_sec=900, max_hosts=8, max_items=12)


# --- collection --------------------------------------------------------------

def test_all_four_blocks_are_collected():
    ev = _collect(FakeReader(history=_samples(), events=[{"eventid": "9", "host": "sw-02"}],
                             checks=[{"host": "a", "item": "icmp", "failed": True}],
                             trigger={"expression": "last(/x/y)=0", "priority": "4"}))
    assert ev.history and ev.correlated and ev.siblings.total and ev.why_no_trigger
    assert ev.degraded == []
    assert not ev.is_empty


def test_the_caps_are_passed_to_the_query_not_applied_after():
    # A ceiling applied to the answer has already cost you the sweep it was meant to prevent.
    reader = FakeReader(checks=[])
    _collect(reader)
    assert reader.seen["siblings"] == ("switch-07", 8, 12)


def test_the_event_itself_is_not_reported_as_correlated_with_itself():
    reader = FakeReader(events=[{"eventid": "500", "host": "switch-07"},
                                {"eventid": "501", "host": "switch-08"}])
    ev = _collect(reader)
    assert [e["eventid"] for e in ev.correlated] == ["501"]


def test_one_failed_block_degrades_the_report_and_the_rest_still_arrives():
    ev = _collect(FakeReader(history=_samples(), boom=("siblings",),
                             trigger={"expression": "last(/x/y)=0"}))
    assert ev.degraded == ["siblings"]
    assert ev.history and ev.why_no_trigger      # the others were unaffected
    assert ev.siblings.total == 0


def test_every_block_failing_still_returns_rather_than_raising():
    ev = _collect(FakeReader(boom=("history", "correlated", "siblings", "why_no_trigger")))
    assert sorted(ev.degraded) == ["correlated", "history", "siblings", "why_no_trigger"]
    assert ev.is_empty


def test_an_event_without_an_item_skips_history_without_failing():
    ev = collect(FakeReader(history=_samples()), {"eventid": "1", "host": "h"},
                 now_ts=2000, window_sec=900, max_hosts=8, max_items=12)
    assert ev.history == []
    assert "history" not in ev.degraded    # not degraded — there was nothing to ask for


# --- history summary ---------------------------------------------------------

def test_duration_and_cadence_come_out_of_the_samples():
    s = summarise_history(_samples(count=6, step=15))
    assert s["samples"] == 6
    assert s["duration_s"] == 75
    assert s["cadence_s"] == 15
    assert s["gap_detected"] is False


def test_cadence_uses_the_median_so_one_missing_sample_does_not_move_it():
    # A mean would report a cadence the poller never had.
    samples = [{"clock": c, "value": "0"} for c in (0, 15, 30, 75, 90, 105)]
    s = summarise_history(samples)
    assert s["cadence_s"] == 15
    assert s["gap_detected"] is True


def test_a_single_sample_has_no_duration_to_report():
    assert summarise_history(_samples(count=1))["duration_s"] is None


# --- verdict -----------------------------------------------------------------

def test_duration_line_says_how_long_instead_of_a_while():
    line = duration_line(Evidence(history=_samples(count=6, step=15)))
    assert "1m15s" in line and "6 samples" in line and "every 15s" in line


def test_all_neighbours_failing_points_upstream():
    ev = Evidence(siblings=Siblings(failed=[{"host": "a", "item": "icmp"},
                                            {"host": "b", "item": "icmp"}]))
    assert "upstream" in scope_line(ev)


def test_the_checks_that_held_are_half_of_the_finding():
    # "wired and wireless died together" and "HTTP kept answering while ICMP did not" are
    # different diagnoses, and only the second one needs the healthy checks to be reported.
    ev = Evidence(siblings=Siblings(
        failed=[{"host": "a", "item": "icmp ping"}, {"host": "a", "item": "dns resolve"}],
        healthy=[{"host": "a", "item": "http check"}],
    ))
    line = scope_line(ev)
    assert "icmp ping" in line and "http check" in line
    assert "did not take the whole path" in line


def test_no_neighbours_failing_is_also_a_finding():
    ev = Evidence(siblings=Siblings(healthy=[{"host": "a", "item": "icmp"}]))
    assert "does not extend to its neighbours" in scope_line(ev)


def test_lines_are_absent_when_the_evidence_supports_nothing():
    empty = Evidence()
    assert duration_line(empty) is None
    assert scope_line(empty) is None
    assert correlation_line(empty) is None
    assert trigger_line(empty) is None
    assert verdict(empty) == ""


def test_a_degraded_block_is_named_in_the_verdict():
    # Absent because it could not be checked reads very differently from absent because
    # there was nothing to say.
    out = verdict(Evidence(degraded=["siblings"]))
    assert "Evidence incomplete" in out and "siblings" in out


def test_verdict_composes_the_lines_it_has():
    ev = Evidence(
        history=_samples(count=4, step=30),
        correlated=[{"eventid": "9", "host": "sw-02"}],
        siblings=Siblings(failed=[{"host": "a", "item": "icmp"}]),
        why_no_trigger={"expression": "last(/sw/icmp)=0", "priority": "4"},
    )
    out = verdict(ev)
    assert len(out.splitlines()) == 4
    assert "1m30s" in out and "sw-02" in out and "upstream" in out and "priority 4" in out
