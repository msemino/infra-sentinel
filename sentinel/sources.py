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


def zabbix_events_window(cfg: Config, token: str, since_ts: float, until_ts: float) -> list[dict]:
    """Events that *happened* in a time range, including ones already resolved.

    ``problem.get`` answers "what is broken now", which is the wrong question for a watchdog
    that wakes up periodically: an incident that opened and closed between two rounds is
    absent from every snapshot the watchdog ever takes, even though the monitoring system
    recorded it perfectly. That is not a rare edge — short outages are the common kind.

    ``event.get`` over ``[since, until]`` fills the gap. Resolved events carry ``r_eventid``,
    whose clock gives the **real duration** rather than "it was down at some point", so a
    transient can be reported as the thing it was instead of as a mystery.
    """
    r = http_json(
        cfg.zabbix_url,
        {
            "jsonrpc": "2.0",
            "method": "event.get",
            "id": 4,
            "params": {
                "output": ["eventid", "name", "severity", "clock", "objectid", "r_eventid",
                           "value"],
                "severities": list(range(cfg.zabbix_min_severity, 6)),
                "time_from": int(since_ts),
                "time_till": int(until_ts),
                "sortfield": ["clock"],
                "sortorder": "ASC",
            },
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    raw = r.get("result", [])

    # Resolution clocks, so a closed event can report how long it actually lasted.
    resolution_ids = [e["r_eventid"] for e in raw if e.get("r_eventid") not in (None, "", "0")]
    closed: dict[str, int] = {}
    if resolution_ids:
        rr = http_json(
            cfg.zabbix_url,
            {"jsonrpc": "2.0", "method": "event.get", "id": 5,
             "params": {"eventids": resolution_ids, "output": ["eventid", "clock"]}},
            headers={"Authorization": f"Bearer {token}"},
        )
        closed = {e["eventid"]: int(e["clock"]) for e in rr.get("result", [])}

    events = []
    for e in raw:
        if str(e.get("value", "1")) != "1":     # value 0 is the recovery event itself
            continue
        opened = int(e["clock"])
        r_id = e.get("r_eventid")
        closed_at = closed.get(r_id) if r_id not in (None, "", "0") else None
        events.append({
            "eventid": e["eventid"],
            "name": e["name"],
            "severity": SEVERITY.get(e["severity"], e["severity"]),
            "objectid": e["objectid"],
            "since": datetime.fromtimestamp(opened, DISPLAY_TZ).strftime("%Y-%m-%d %H:%M"),
            "clock": opened,
            # Present and already over: the case the active-problems query cannot see.
            "transient": closed_at is not None,
            "duration_s": (closed_at - opened) if closed_at is not None else None,
        })
    return events


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
