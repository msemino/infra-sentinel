"""The event window and its exactly-once ledger.

The window exists because the active-problems query cannot see an incident that opened and
closed between two rounds. Covering that gap means the windows must overlap, and overlap
means the same event arrives more than once — so the ledger is not an optimisation, it is
the other half of the mechanism. These tests pin both halves, including the two failure
modes that would quietly undo them: a window that stops overlapping, and a ledger that
forgets too early.
"""

from __future__ import annotations

from sentinel.watchdog import (
    State,
    mark_alerted,
    prune_alerted,
    select_unalerted,
    window_start,
)

HOUR = 3600
MAX_LOOKBACK = 6 * HOUR


def _event(eventid: str, transient: bool = True) -> dict:
    return {"eventid": eventid, "name": "link down", "transient": transient,
            "duration_s": 164 if transient else None}


# --- where the window begins -------------------------------------------------

def test_window_reaches_back_to_the_last_completed_round():
    # The normal case: five minutes since the last round, so the window covers those
    # five minutes and nothing more.
    assert window_start(last_round_ts=1000, now_ts=1300, max_lookback_sec=MAX_LOOKBACK) == 1000


def test_skipped_round_is_covered_rather_than_lost():
    # The scheduler will not start a run while the previous one is still going, so rounds
    # get skipped. The window simply stretches to the last one that finished. This is the
    # property that let the cadence go up without raising the risk of missing anything.
    three_rounds_ago = 1000
    now = three_rounds_ago + 3 * 300
    assert window_start(three_rounds_ago, now, MAX_LOOKBACK) == three_rounds_ago


def test_long_outage_is_capped_instead_of_replayed():
    # Down for two days. Reaching back to the last round would replay 48 hours of events as
    # if they had just happened, and bury the operator on the first round after recovery.
    now = 10 * 24 * HOUR
    last_round = now - 2 * 24 * HOUR
    assert window_start(last_round, now, MAX_LOOKBACK) == now - MAX_LOOKBACK


def test_first_run_gets_the_cap_not_the_epoch():
    # last_round_ts == 0 means "never ran". Treating that as a real timestamp would ask for
    # every event since 1970.
    assert window_start(0, now_ts=5 * HOUR, max_lookback_sec=MAX_LOOKBACK) == 5 * HOUR - MAX_LOOKBACK


def test_window_never_starts_after_now():
    # A clock that jumped backwards must not produce an inverted range.
    assert window_start(last_round_ts=9000, now_ts=9000, max_lookback_sec=MAX_LOOKBACK) == 9000


# --- exactly-once ------------------------------------------------------------

def test_unseen_events_pass_through():
    events = [_event("1"), _event("2")]
    assert len(select_unalerted(events, {})) == 2


def test_overlapping_windows_do_not_alert_twice():
    state = State(seeded=True)
    events = [_event("1"), _event("2")]

    first = select_unalerted(events, state.alerted_eventids)
    assert len(first) == 2
    mark_alerted(state, first, now_ts=1000)

    # The next window overlaps on purpose and hands back the same two events, plus a new one.
    second = select_unalerted([*events, _event("3")], state.alerted_eventids)
    assert [e["eventid"] for e in second] == ["3"]


def test_ledger_is_pruned_but_not_before_the_widest_window():
    now = 100_000.0
    alerted = {
        "old": now - MAX_LOOKBACK - 1,   # can no longer appear in any window
        "edge": now - MAX_LOOKBACK,      # still reachable by the widest window
        "recent": now - 60,
    }
    kept = prune_alerted(alerted, now, MAX_LOOKBACK)
    assert set(kept) == {"edge", "recent"}


def test_pruning_too_early_would_let_an_event_come_back():
    # Guards the mistake this pruning invites: keeping less than the lookback cap means a
    # long window can reach an event the ledger has already forgotten, and re-alert it.
    now = 100_000.0
    alerted = {"edge": now - MAX_LOOKBACK}
    assert "edge" in prune_alerted(alerted, now, MAX_LOOKBACK)
    assert "edge" not in prune_alerted(alerted, now, MAX_LOOKBACK // 2)


def test_events_without_an_id_are_not_recorded():
    state = State(seeded=True)
    mark_alerted(state, [{"name": "no id here"}], now_ts=1000)
    assert state.alerted_eventids == {}


def test_state_round_trips_the_v2_fields():
    state = State(seeded=True, alerted_eventids={"7": 123.0}, last_round_ts=456.0)
    back = State.from_dict(state.to_dict())
    assert back.alerted_eventids == {"7": 123.0}
    assert back.last_round_ts == 456.0


def test_state_from_v1_file_still_loads():
    # An upgrade must not need the state file to be deleted: a missing ledger is an empty
    # ledger, and a missing last_round_ts means the first v2 window uses the cap.
    v1 = {"zabbix": {}, "prom": {}, "seeded": True, "last_notified": {"10": 5.0}}
    back = State.from_dict(v1)
    assert back.seeded and back.last_notified == {"10": 5.0}
    assert back.alerted_eventids == {}
    assert back.last_round_ts == 0.0
