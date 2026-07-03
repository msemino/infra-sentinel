"""Combined mock server for Zabbix, Prometheus and Ollama — stdlib only.

Runs three roles on three ports (set by env) from one small process so the demo
has no external dependencies. Scenario state is held in memory and flipped via
control endpoints so you can drive the pipeline deterministically:

  POST /control/new-problem   → adds one new High problem (drives a normal LLM cycle)
  POST /control/mass-outage   → adds many new problems at once (trips the breaker)
  POST /control/reset         → back to baseline

Roles are selected by the MOCK_ROLE env var: "zabbix" | "prometheus" | "ollama".
"""

from __future__ import annotations

import json
import os
import threading
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


def _zabbix(method: str, params: dict) -> dict:
    if method == "user.login":
        return {"jsonrpc": "2.0", "result": "mock-token-abc123", "id": 1}

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
    canned = _load("ollama_response.json")
    return canned


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
            return self._send(_ollama())

        return self._send({"error": "not found"}, 404)

    def _control(self, path: str):
        action = path.rsplit("/", 1)[-1]
        with _LOCK:
            if action == "reset":
                _STATE["extra_problems"] = []
                _STATE["down_extra"] = []
            elif action == "new-problem":
                _STATE["extra_problems"] = [dict(_NEW_PROBLEM)]
            elif action == "mass-outage":
                _STATE["extra_problems"] = [dict(p) for p in _MASS_PROBLEMS]
                _STATE["down_extra"] = [dict(t) for t in _MASS_DOWN]
            else:
                return self._send({"error": f"unknown action {action}"}, 400)
        return self._send({"ok": True, "action": action, "state": _STATE})


def main() -> None:
    role = os.environ.get("MOCK_ROLE", "zabbix")
    port = int(os.environ.get("MOCK_PORT", "8080"))
    Handler.role = role
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)  # noqa: S104
    print(f"[mock:{role}] listening on :{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
