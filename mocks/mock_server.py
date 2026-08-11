"""Combined mock server for Zabbix, Prometheus and Ollama — stdlib only.

Runs three roles on three ports (set by env) from one small process so the demo
has no external dependencies. Scenario state is held in memory and flipped via
control endpoints so you can drive the pipeline deterministically:

  POST /control/new-problem   → adds one new High problem (drives a normal cycle)
  POST /control/mass-outage   → adds many new problems at once (trips the breaker)
  POST /control/transient     → an incident that opened AND closed since the last round.
                                It is deliberately NOT added to the active problems, because
                                that is the whole point: the active-problem query cannot see
                                it, and only the event window can.
  POST /control/slow-model    → the model takes 30s to answer. In v1 that delayed the alert
                                by 30s; in v2 the alert is already gone and only the report
                                waits. This is the scenario the architecture changed for.
  POST /control/break-model   → the model returns 500. The alert must still arrive, followed
                                by a short "investigation failed" message.
  POST /control/reset         → back to baseline

Roles are selected by the MOCK_ROLE env var: "zabbix" | "prometheus" | "ollama".
"""

from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLE = os.path.join(os.path.dirname(HERE), "sample_data")


def _load(name: str) -> dict:
    with open(os.path.join(SAMPLE, name), encoding="utf-8") as f:
        return json.load(f)


# In-memory scenario state, shared across requests.
_LOCK = threading.Lock()
_STATE = {
    "extra_problems": [],  # list of problem dicts appended to the baseline
    "down_extra": [],      # list of down target dicts appended to the baseline
    # v2: events that already closed. They never appear in `problem.get` — that is the bug
    # the event window exists for, so the mock has to reproduce it rather than paper over it.
    "transient": [],
    "model_delay_s": 0,
    "model_broken": False,
}

# A pool of synthetic "new" problems the control endpoints draw from.
_NEW_PROBLEM = {
    "eventid": "900001",
    "name": "Interface Gi1/0/24: link down",
    "severity": "4",
    "clock": "1893456000",
    "objectid": "50024",
    "host": "switch-access-07",
}
_MASS_PROBLEMS = [
    {"eventid": f"9100{i:02d}", "name": "Unavailable by ICMP ping",
     "severity": "5", "clock": "1893456000", "objectid": f"6{i:04d}",
     "host": f"switch-access-{i:02d}"}
    for i in range(1, 11)
]
_MASS_DOWN = [
    {"job": "node", "instance": f"10.0.2.{i}:9100"} for i in range(20, 26)
]


def _transient_event(now: int) -> dict:
    """An incident that opened 4 minutes ago and closed 2m44s later.

    Short enough to fit entirely between two rounds, which is exactly the shape that used to
    go unreported: the monitoring system recorded it perfectly and the watchdog never saw it.
    """
    opened = now - 240
    return {
        "eventid": "800001", "r_eventid": "800002", "value": "1",
        "name": "Unavailable by ICMP ping", "severity": "5",
        "clock": str(opened), "objectid": "50099", "host": "switch-access-03",
        "_closed_at": opened + 164,
    }


def _zabbix(method: str, params: dict) -> dict:
    now = int(time.time())

    if method == "user.login":
        return {"jsonrpc": "2.0", "result": "mock-token-abc123", "id": 1}

    if method == "event.get":
        with _LOCK:
            transient = list(_STATE["transient"])
        # Resolution lookup: the second call the window makes, to get the real duration.
        if params.get("eventids"):
            wanted = set(params["eventids"])
            return {"jsonrpc": "2.0", "result": [
                {"eventid": e["r_eventid"], "clock": str(e["_closed_at"])}
                for e in transient if e["r_eventid"] in wanted
            ], "id": 5}
        frm = int(params.get("time_from", 0))
        till = int(params.get("time_till", now))
        result = [
            {k: v for k, v in e.items() if not k.startswith("_")}
            for e in transient if frm <= int(e["clock"]) <= till
        ]
        # Correlated-events block asks with selectHosts and no severities filter.
        if "selectHosts" in params:
            result = [{**e, "hosts": [{"name": e.get("host", "?")}]} for e in result]
        return {"jsonrpc": "2.0", "result": result, "id": 4}

    if method == "history.get":
        # Six samples of a failed reachability check, 15s apart — the shape the evidence
        # layer turns into "lasted 1m15s, 6 samples every 15s".
        start = now - 300
        return {"jsonrpc": "2.0", "result": [
            {"clock": str(start + i * 15), "value": "0"} for i in range(6)
        ], "id": 10}

    if method == "host.get":
        if params.get("groupids"):
            return {"jsonrpc": "2.0", "result": [
                {"hostid": f"1000{i}", "name": f"switch-access-{i:02d}"} for i in range(1, 4)
            ], "id": 14}
        return {"jsonrpc": "2.0", "result": [{
            "hostid": "10001",
            "hostgroups": [{"groupid": "5", "name": "Network"},
                           {"groupid": "9", "name": "Branch 3 access layer"}],
        }], "id": 12}

    if method == "item.get":
        # Two failed and one healthy on purpose: the split result is the finding the
        # evidence layer exists to produce, and a mock where everything fails could not
        # exercise it.
        return {"jsonrpc": "2.0", "result": [
            {"itemid": "1", "hostid": "10001", "name": "ICMP ping", "lastvalue": "0"},
            {"itemid": "2", "hostid": "10002", "name": "ICMP ping", "lastvalue": "0"},
            {"itemid": "3", "hostid": "10003", "name": "HTTP service check", "lastvalue": "1"},
        ], "id": 15}

    if method == "problem.get":
        base = _load("zabbix_problems.json")["problems"]
        with _LOCK:
            extra = list(_STATE["extra_problems"])
        minsev = min((int(s) for s in params.get("severities", ["4"])), default=4)
        result = [
            {"eventid": p["eventid"], "name": p["name"], "severity": p["severity"],
             "clock": p["clock"], "objectid": p["objectid"]}
            for p in base + extra
            if int(p["severity"]) >= minsev
        ]
        return {"jsonrpc": "2.0", "result": result, "id": 2}

    if method == "trigger.get":
        # The evidence layer asks the same method for the expanded expression instead of
        # the host, and is told apart by the requested output.
        if "expression" in (params.get("output") or []):
            return {"jsonrpc": "2.0", "result": [
                {"expression": "last(/switch-access-03/icmpping)=0", "priority": "5"}
            ], "id": 16}
        base = _load("zabbix_problems.json")["problems"]
        with _LOCK:
            extra = list(_STATE["extra_problems"])
        wanted = set(params.get("triggerids", []))
        result = [
            {"triggerid": p["objectid"], "hosts": [{"name": p["host"]}]}
            for p in base + extra
            if p["objectid"] in wanted
        ]
        return {"jsonrpc": "2.0", "result": result, "id": 3}

    return {"jsonrpc": "2.0", "result": [], "id": 0}


def _prometheus() -> dict:
    base = _load("prometheus_up.json")["down"]
    with _LOCK:
        extra = list(_STATE["down_extra"])
    result = [
        {"metric": {"__name__": "up", "job": t["job"], "instance": t["instance"]},
         "value": [1893456000, "0"]}
        for t in base + extra
    ]
    return {"status": "success", "data": {"resultType": "vector", "result": result}}


def _ollama() -> dict:
    with _LOCK:
        delay, broken = _STATE["model_delay_s"], _STATE["model_broken"]
    if delay:
        # In v1 this delay landed on the notification. In v2 it lands on the report, which
        # is the entire architectural change made observable in the demo.
        time.sleep(delay)
    if broken:
        raise RuntimeError("model unavailable (simulated)")
    return _load("ollama_response.json")


class Handler(BaseHTTPRequestHandler):
    role = os.environ.get("MOCK_ROLE", "zabbix")

    def log_message(self, *args):  # quieter logs
        pass

    def _send(self, obj: dict, code: int = 200) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode() or "{}")

    def do_GET(self):
        path = urlparse(self.path).path
        if self.role == "prometheus" and path == "/api/v1/query":
            return self._send(_prometheus())
        if path == "/health":
            return self._send({"ok": True, "role": self.role})
        return self._send({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path

        # Scenario control endpoints (available on any role for convenience).
        if path.startswith("/control/"):
            return self._control(path)

        if self.role == "zabbix":
            payload = self._read_json()
            return self._send(_zabbix(payload.get("method", ""), payload.get("params", {})))

        if self.role == "ollama" and path == "/api/chat":
            try:
                return self._send(_ollama())
            except RuntimeError as e:
                return self._send({"error": str(e)}, 500)

        return self._send({"error": "not found"}, 404)

    def _control(self, path: str):
        action = path.rsplit("/", 1)[-1]
        with _LOCK:
            if action == "reset":
                _STATE["extra_problems"] = []
                _STATE["down_extra"] = []
                _STATE["transient"] = []
                _STATE["model_delay_s"] = 0
                _STATE["model_broken"] = False
            elif action == "new-problem":
                _STATE["extra_problems"] = [dict(_NEW_PROBLEM)]
            elif action == "mass-outage":
                _STATE["extra_problems"] = [dict(p) for p in _MASS_PROBLEMS]
                _STATE["down_extra"] = [dict(t) for t in _MASS_DOWN]
            elif action == "transient":
                # Note what is NOT touched: extra_problems. The incident is over, so the
                # active-problem query correctly shows nothing. Only the window can see it.
                _STATE["transient"] = [_transient_event(int(time.time()))]
            elif action == "slow-model":
                _STATE["model_delay_s"] = 30
            elif action == "break-model":
                _STATE["model_broken"] = True
            else:
                return self._send({"error": f"unknown action {action}"}, 400)
            snapshot = {k: v for k, v in _STATE.items()}
        return self._send({"ok": True, "action": action, "state": snapshot})


def main() -> None:
    role = os.environ.get("MOCK_ROLE", "zabbix")
    port = int(os.environ.get("MOCK_PORT", "8080"))
    Handler.role = role
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)  # noqa: S104
    print(f"[mock:{role}] listening on :{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
