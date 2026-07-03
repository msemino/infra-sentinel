"""Unit tests for the guardrail logic — the heart of Sentinel.

These are pure functions, so we test them exhaustively: baseline diffing,
deduplication by objectid with a cooldown window, and the mass-outage breaker.
"""

from __future__ import annotations

from sentinel.watchdog import (
    Diff,
    State,
    affected_hosts,
    build_snapshot,
    compute_diff,
    is_mass_outage,
    mark_notified,
)


def _problem(eventid: str, objectid: str, host: str = "switch-01") -> dict:
    return {"name": "link down", "severity": "High", "since": "2026-01-01 00:00",
            "objectid": objectid, "host": host}


def _down(job: str, instance: str) -> dict:
    return {"job": job, "instance": instance}


# --- baseline diff -----------------------------------------------------------

def test_no_change_produces_no_news():
    state = State(zabbix={"1": _problem("1", "10")}, prom={}, seeded=True)
    diff = compute_diff(state, {"1": _problem("1", "10")}, {}, now_ts=1000, cooldown_sec=60)
    assert not diff.has_news
    assert diff.new_count == 0


def test_new_problem_is_detected():
    state = State(zabbix={"1": _problem("1", "10")}, prom={}, seeded=True)
    now = {"1": _problem("1", "10"), "2": _problem("2", "20", host="ap-05")}
    diff = compute_diff(state, now, {}, now_ts=1000, cooldown_sec=60)
    assert diff.has_news
    assert "2" in diff.new_zabbix
    assert diff.new_count == 1


def test_recovery_is_detected():
    state = State(zabbix={"1": _problem("1", "10")}, prom={}, seeded=True)
    diff = compute_diff(state, {}, {}, now_ts=1000, cooldown_sec=60)
    assert diff.has_news
    assert "1" in diff.recovered_zabbix
    assert diff.new_count == 0  # a recovery is not a "new problem"


def test_prometheus_down_target_detected():
    state = State(zabbix={}, prom={}, seeded=True)
    diff = compute_diff(state, {}, {"node/10.0.0.5:9100": _down("node", "10.0.0.5:9100")},
                        now_ts=1000, cooldown_sec=60)
    assert diff.has_news
    assert diff.new_count == 1


# --- dedup by objectid + cooldown -------------------------------------------

def test_flapping_trigger_suppressed_within_cooldown():
    # Same trigger (objectid 10) alerted at t=1000; reappears with a NEW eventid at t=1030.
    state = State(zabbix={}, prom={}, seeded=True, last_notified={"10": 1000.0})
    now = {"eventid-new": _problem("eventid-new", "10")}
    diff = compute_diff(state, now, {}, now_ts=1030, cooldown_sec=60)
    assert diff.new_count == 0
    assert diff.suppressed == 1
    assert not diff.new_zabbix


def test_flapping_trigger_alerts_again_after_cooldown():
    state = State(zabbix={}, prom={}, seeded=True, last_notified={"10": 1000.0})
    now = {"eventid-new": _problem("eventid-new", "10")}
    diff = compute_diff(state, now, {}, now_ts=1100, cooldown_sec=60)  # 100s > 60s
    assert diff.new_count == 1
    assert diff.suppressed == 0


def test_different_trigger_not_suppressed_by_unrelated_cooldown():
    # objectid 10 was recently notified, but the new problem is objectid 20.
    state = State(zabbix={}, prom={}, seeded=True, last_notified={"10": 1000.0})
    now = {"e2": _problem("e2", "20")}
    diff = compute_diff(state, now, {}, now_ts=1010, cooldown_sec=60)
    assert diff.new_count == 1
    assert diff.suppressed == 0


def test_mark_notified_updates_cooldown_clock():
    state = State(zabbix={}, prom={}, seeded=True)
    diff = Diff(new_zabbix={"e1": _problem("e1", "77")}, recovered_zabbix={},
                new_prom={}, recovered_prom={})
    mark_notified(state, diff, now_ts=5000.0)
    assert state.last_notified["77"] == 5000.0


# --- mass-outage circuit breaker --------------------------------------------

def test_mass_outage_trips_at_threshold():
    new_z = {f"e{i}": _problem(f"e{i}", str(i)) for i in range(5)}
    new_p = {f"p{i}": _down("node", f"10.0.0.{i}:9100") for i in range(3)}
    diff = Diff(new_zabbix=new_z, recovered_zabbix={}, new_prom=new_p, recovered_prom={})
    assert diff.new_count == 8
    assert is_mass_outage(diff, threshold=8)


def test_mass_outage_does_not_trip_below_threshold():
    new_z = {f"e{i}": _problem(f"e{i}", str(i)) for i in range(3)}
    diff = Diff(new_zabbix=new_z, recovered_zabbix={}, new_prom={}, recovered_prom={})
    assert not is_mass_outage(diff, threshold=8)


# --- helpers -----------------------------------------------------------------

def test_affected_hosts_dedups_and_sorts():
    diff = Diff(
        new_zabbix={"a": _problem("a", "1", host="switch-02"),
                    "b": _problem("b", "2", host="switch-01")},
        recovered_zabbix={},
        new_prom={"p": _down("node", "10.0.0.9:9100")},
        recovered_prom={},
    )
    assert affected_hosts(diff) == ["10.0.0.9:9100", "switch-01", "switch-02"]


def test_build_snapshot_shape():
    diff = Diff(
        new_zabbix={"a": _problem("a", "1")},
        recovered_zabbix={"r": _problem("r", "9")},
        new_prom={"p": _down("node", "10.0.0.9:9100")},
        recovered_prom={},
    )
    snap = build_snapshot(diff)
    assert len(snap["new_problems"]) == 1
    assert len(snap["down_targets"]) == 1
    assert snap["recovered"][0]["source"] == "zabbix"
