"""Round-trip tests for state persistence."""

from __future__ import annotations

from sentinel.state import load_state, save_state
from sentinel.watchdog import State


def test_state_roundtrip(tmp_path):
    path = str(tmp_path / "state.json")
    original = State(
        zabbix={"1": {"name": "x", "objectid": "10"}},
        prom={"node/1": {"job": "node", "instance": "1"}},
        seeded=True,
        last_notified={"10": 1234.5},
    )
    save_state(path, original)
    loaded = load_state(path)
    assert loaded.seeded is True
    assert loaded.zabbix == original.zabbix
    assert loaded.prom == original.prom
    assert loaded.last_notified == {"10": 1234.5}


def test_load_missing_state_returns_empty(tmp_path):
    loaded = load_state(str(tmp_path / "nope.json"))
    assert loaded.seeded is False
    assert loaded.zabbix == {}
