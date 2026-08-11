"""End-to-end: the two phases, against the mocks, over real HTTP.

The unit tests pin the pure logic. What they cannot pin is the property the whole v2 rewrite
exists for, because it is a property of the *ordering* and only shows up when the pieces are
wired together:

    the alert does not wait for the model, and does not depend on it succeeding.

So this boots the three mocks as separate processes on ephemeral ports and drives real
cycles. Nothing is stubbed in-process — if the wiring is wrong, these fail.

The scenario that matters is `test_alert_survives_a_broken_model`. In v1 the equivalent case
produced **no message at all**: the analyzer raised, the cycle aborted, state was not
advanced. That was a deliberate design decision, defended in the v1 README. Here the same
failure must still produce the alert, followed by a short note that the enrichment is missing.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MOCK = ROOT / "mocks" / "mock_server.py"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait(port: int, deadline: float = 8.0) -> bool:
    end = time.time() + deadline
    while time.time() < end:
        with socket.socket() as s:
            s.settimeout(0.2)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.05)
    return False


def _post(url: str, timeout: float = 60.0) -> None:
    urllib.request.urlopen(urllib.request.Request(url, method="POST"), timeout=timeout)


@pytest.fixture()
def stack(tmp_path):
    """Three mocks on ephemeral ports plus an env pointing the cycle at them."""
    ports = {role: _free_port() for role in ("zabbix", "prometheus", "ollama")}
    procs = []
    for role, port in ports.items():
        env = dict(os.environ, MOCK_ROLE=role, MOCK_PORT=str(port))
        procs.append(subprocess.Popen([sys.executable, str(MOCK)], env=env,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    try:
        for role, port in ports.items():
            assert _wait(port), f"mock {role} did not come up on {port}"

        env = dict(
            os.environ,
            ZABBIX_URL=f"http://127.0.0.1:{ports['zabbix']}/api_jsonrpc.php",
            ZABBIX_USER="monitor", ZABBIX_PASS="monitor", ZABBIX_MIN_SEVERITY="4",
            PROMETHEUS_URL=f"http://127.0.0.1:{ports['prometheus']}",
            OLLAMA_URL=f"http://127.0.0.1:{ports['ollama']}",
            OLLAMA_MODEL="mock", OLLAMA_TIMEOUT="90",
            NOTIFIER="inbox", INBOX_PATH=str(tmp_path / "inbox.jsonl"),
            STATE_FILE=str(tmp_path / "state.json"),
            HEARTBEAT_FILE=str(tmp_path / "heartbeat.json"),
            DEDUP_COOLDOWN_SEC="0", MASS_OUTAGE_THRESHOLD="8",
        )
        yield {"env": env, "ports": ports, "tmp": tmp_path}
    finally:
        for p in procs:
            p.kill()
            p.wait()


def _cycle(stack) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "sentinel.cli"], env=stack["env"],
                          cwd=ROOT, capture_output=True, text=True, timeout=180)


def _inbox(stack) -> list[str]:
    path = Path(stack["env"]["INBOX_PATH"])
    if not path.exists():
        return []
    return [json.loads(line)["message"] for line in path.read_text(encoding="utf-8").splitlines()]


def _control(stack, action: str, role: str = "zabbix") -> None:
    # Each role is its own process with its own scenario state, so a control endpoint has to
    # be sent to the role that will act on it: model behaviour to the model.
    _post(f"http://127.0.0.1:{stack['ports'][role]}/control/{action}")


def _seed(stack) -> None:
    assert _cycle(stack).returncode == 0
    Path(stack["env"]["INBOX_PATH"]).write_text("", encoding="utf-8")   # drop the "on watch"


def _rewind_last_round(stack, seconds: int) -> None:
    """Pretend `seconds` passed since the last round.

    The event window covers `[last round, now]`, so a scenario about "an incident that
    happened between two rounds" needs there to be a gap between two rounds. Rewinding the
    persisted timestamp is how a test gets one without sleeping through it.
    """
    path = Path(stack["env"]["STATE_FILE"])
    state = json.loads(path.read_text(encoding="utf-8"))
    state["last_round_ts"] -= seconds
    path.write_text(json.dumps(state), encoding="utf-8")


# --- the ordering ------------------------------------------------------------

def test_seeding_alerts_nothing_and_still_leaves_a_heartbeat(stack):
    out = _cycle(stack)
    assert out.returncode == 0, out.stderr
    assert "baseline seeded" in out.stdout
    # The dead-man switch has to be armed from the very first cycle. If the heartbeat only
    # appeared once something happened, a watchdog that never saw an event would be
    # indistinguishable from one that died on startup.
    assert Path(stack["env"]["HEARTBEAT_FILE"]).exists()


def test_alert_comes_first_and_the_report_second(stack):
    _seed(stack)
    _control(stack, "new-problem")
    out = _cycle(stack)
    assert out.returncode == 0, out.stderr

    messages = _inbox(stack)
    assert len(messages) == 2, messages
    assert "ALERT" in messages[0] and "REPORT" in messages[1]
    # The raw facts are in the first message — the one that does not wait for anything.
    assert "switch-access-07" in messages[0]
    assert "phase 1: alert sent" in out.stdout


def test_alert_survives_a_broken_model(stack):
    # v1's answer to this was silence: the analyzer raised, the cycle aborted, state was not
    # advanced, and the operator was told nothing. This is the case the rewrite is for.
    _seed(stack)
    _control(stack, "new-problem")
    _control(stack, "break-model", role="ollama")
    out = _cycle(stack)
    assert out.returncode == 0, out.stderr

    messages = _inbox(stack)
    assert len(messages) == 2, messages
    assert "ALERT" in messages[0] and "switch-access-07" in messages[0]
    # Never silent: the failure is reported as a failure, because "the investigation broke"
    # and "the investigation found nothing" call for different responses.
    assert "Investigation failed" in messages[1]
    assert "the alert above stands" in messages[1].lower()


def test_a_slow_model_does_not_delay_the_alert(stack):
    _seed(stack)
    _control(stack, "new-problem")
    _control(stack, "slow-model", role="ollama")   # 30s inside the mock's /api/chat
    started = time.time()
    out = _cycle(stack)
    elapsed = time.time() - started
    assert out.returncode == 0, out.stderr

    messages = _inbox(stack)
    assert "ALERT" in messages[0]
    # The cycle as a whole waits for the report; the alert did not. The log line for phase 1
    # is emitted before the model is ever contacted, and the inbox proves the alert was
    # written first — in v1 both would have landed after the 30s.
    assert elapsed >= 30, "the mock did not actually stall"
    assert out.stdout.index("phase 1: alert sent") < out.stdout.index("phase 2")


def test_the_baseline_advances_with_the_alert_so_it_is_not_resent(stack):
    _seed(stack)
    _control(stack, "new-problem")
    _cycle(stack)
    before = len(_inbox(stack))

    _cycle(stack)                          # same problem still active, nothing new
    assert len(_inbox(stack)) == before, "an unchanged problem was alerted twice"


# --- the event window --------------------------------------------------------

def test_an_incident_that_opened_and_closed_between_rounds_is_reported(stack):
    # It is never in `problem.get` — it is over. Under v1 this produced no alert at all,
    # even though the monitoring system had recorded it correctly.
    _seed(stack)
    _rewind_last_round(stack, 600)     # ten minutes since the last round
    _control(stack, "transient")
    out = _cycle(stack)
    assert out.returncode == 0, out.stderr

    messages = _inbox(stack)
    assert messages, "a closed incident produced no alert"
    assert "Opened and closed between rounds" in messages[0]
    assert "164s" in messages[0], "the real duration should come from the resolution event"


def test_the_transient_is_not_alerted_twice_by_overlapping_windows(stack):
    _seed(stack)
    _rewind_last_round(stack, 600)
    _control(stack, "transient")
    _cycle(stack)
    first = len(_inbox(stack))
    assert first >= 1

    # The next window overlaps and hands the same closed event back. The ledger is what
    # stops it becoming a second alert.
    _rewind_last_round(stack, 600)
    _cycle(stack)
    assert len(_inbox(stack)) == first, "the overlapping window re-alerted the same event"


# --- the breaker still holds -------------------------------------------------

def test_mass_outage_still_short_circuits_without_the_model(stack):
    _seed(stack)
    _control(stack, "mass-outage")
    out = _cycle(stack)
    assert out.returncode == 0, out.stderr

    messages = _inbox(stack)
    assert len(messages) == 1, "the breaker must produce exactly one message"
    assert "mass outage" in messages[0].lower()
    assert "MASS OUTAGE detected" in out.stdout
