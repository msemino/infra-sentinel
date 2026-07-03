"""Monitoring sources: Zabbix (problems) and Prometheus (down targets).

Both functions return a plain ``dict`` keyed by a stable event identity, so the
watchdog can diff two snapshots without caring where the data came from.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .config import Config
from .http import http_json

# Display timezone for "since" timestamps. UTC-3 by default (configurable in the
# formatting layer if you need something else); kept simple on purpose.
DISPLAY_TZ = timezone(timedelta(hours=-3))

SEVERITY = {
    "0": "Not classified",
    "1": "Information",
    "2": "Warning",
    "3": "Average",
    "4": "High",
    "5": "Disaster",
}


def zabbix_login(cfg: Config) -> str:
    """Authenticate against the Zabbix JSON-RPC API and return an auth token."""
    r = http_json(
        cfg.zabbix_url,
        {
            "jsonrpc": "2.0",
            "method": "user.login",
            "id": 1,
            "params": {"username": cfg.zabbix_user, "password": cfg.zabbix_pass},
        },
    )
    return r["result"]


def zabbix_problems(cfg: Config, token: str) -> dict[str, dict]:
    """Active problems with severity >= ``ZABBIX_MIN_SEVERITY`` (default High=4).

    Returns ``{eventid: {name, severity, since, objectid, host}}``. The ``objectid``
    is the trigger id and is the identity used for deduplication: a flapping trigger
    produces a *new* eventid every cycle but keeps the same objectid.
    """
    minsev = cfg.zabbix_min_severity
    r = http_json(
        cfg.zabbix_url,
        {
            "jsonrpc": "2.0",
            "method": "problem.get",
            "id": 2,
            "params": {
                "output": ["eventid", "name", "severity", "clock", "objectid"],
                "severities": list(range(minsev, 6)),
                "recent": False,
                "sortfield": "eventid",
            },
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    problems: dict[str, dict] = {}
    for p in r["result"]:
        problems[p["eventid"]] = {
            "name": p["name"],
            "severity": SEVERITY.get(p["severity"], p["severity"]),
            "since": datetime.fromtimestamp(int(p["clock"]), DISPLAY_TZ).strftime(
                "%Y-%m-%d %H:%M"
            ),
            "objectid": p["objectid"],
        }

    # Resolve the host name for each problem via its trigger.
    trigger_ids = [p["objectid"] for p in r["result"]]
    if trigger_ids:
        t = http_json(
            cfg.zabbix_url,
            {
                "jsonrpc": "2.0",
                "method": "trigger.get",
                "id": 3,
                "params": {
                    "triggerids": trigger_ids,
                    "output": ["triggerid"],
                    "selectHosts": ["name"],
                },
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        hostmap = {
            x["triggerid"]: (x["hosts"][0]["name"] if x["hosts"] else "?")
            for x in t["result"]
        }
        for p in r["result"]:
            problems[p["eventid"]]["host"] = hostmap.get(p["objectid"], "?")

    return problems


def prometheus_down(cfg: Config) -> dict[str, dict]:
    """Targets where ``up == 0``, keyed by ``"job/instance"``."""
    url = cfg.prometheus_url.rstrip("/") + "/api/v1/query?query=up%3D%3D0"
    r = http_json(url)
    down: dict[str, dict] = {}
    for res in r.get("data", {}).get("result", []):
        m = res["metric"]
        key = f"{m.get('job', '?')}/{m.get('instance', '?')}"
        down[key] = {"job": m.get("job", "?"), "instance": m.get("instance", "?")}
    return down
