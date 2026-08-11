"""The dead-man switch.

The failure it exists for is silence, and silence is what a healthy day also looks like.
So the two cases that matter are the ones where a reasonable-seeming choice makes the check
useless: treating "no heartbeat file" as "not started yet, give it time", and reading a
heartbeat mid-write and concluding the watchdog is dead.
"""

from __future__ import annotations

import json
import os

from sentinel.deadman import Heartbeat, describe, is_stale, read, write


def test_a_fresh_heartbeat_is_not_stale():
    assert not is_stale(Heartbeat(ts=1000, phase="alerted", cycle=3), now_ts=1100,
                        max_age_sec=600)


def test_an_old_heartbeat_is_stale():
    assert is_stale(Heartbeat(ts=1000, phase="alerted", cycle=3), now_ts=2000,
                    max_age_sec=600)


def test_a_heartbeat_that_never_existed_is_stale():
    # The tempting alternative is "no file yet, give it time", which is how a watchdog that
    # never came up after a deploy goes unnoticed until somebody happens to look.
    assert is_stale(Heartbeat(), now_ts=1_000_000, max_age_sec=600)
    assert "has not completed since deploy" in describe(Heartbeat(), now_ts=1_000_000)


def test_exactly_at_the_threshold_is_not_yet_stale():
    assert not is_stale(Heartbeat(ts=1000), now_ts=1600, max_age_sec=600)
    assert is_stale(Heartbeat(ts=1000), now_ts=1601, max_age_sec=600)


def test_write_then_read_round_trips(tmp_path):
    path = str(tmp_path / "sub" / "heartbeat.json")
    write(path, "alerted", 7)
    hb = read(path)
    assert hb.phase == "alerted" and hb.cycle == 7 and hb.ts > 0
    assert not hb.never_ran


def test_write_leaves_no_partial_file_behind(tmp_path):
    # The write goes to a temp file and is renamed into place, so a checker reading at the
    # wrong moment sees the old heartbeat, never half of the new one.
    path = str(tmp_path / "heartbeat.json")
    write(path, "quiet", 1)
    write(path, "alerted", 2)
    assert not os.path.exists(path + ".tmp")
    assert json.loads(open(path, encoding="utf-8").read())["cycle"] == 2


def test_a_corrupt_heartbeat_reads_as_never_ran(tmp_path):
    path = tmp_path / "heartbeat.json"
    path.write_text("{ this is not json", encoding="utf-8")
    assert read(str(path)).never_ran


def test_describe_reports_the_age_and_the_phase_it_died_in():
    out = describe(Heartbeat(ts=1000, phase="alerted", cycle=12), now_ts=1900)
    assert "900s ago" in out and "cycle 12" in out and "alerted" in out
