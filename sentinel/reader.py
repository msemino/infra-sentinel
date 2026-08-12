"""The concrete `evidence.Reader`: four read-only Zabbix queries, all bounded.

Nothing here writes. Ceilings are applied to the query rather than to the result — a limit
applied after the fact has already paid for the sweep it was meant to prevent.
"""

from __future__ import annotations

from .config import Config
from .http import http_json


class ZabbixReader:
    def __init__(self, cfg: Config, token: str) -> None:
        self._cfg = cfg
        self._token = token

    def _call(self, method: str, params: dict, rid: int) -> list | dict:
        r = http_json(
            self._cfg.zabbix_url,
            {"jsonrpc": "2.0", "method": method, "id": rid, "params": params},
            headers={"Authorization": f"Bearer {self._token}"},
        )
        return r.get("result", [])

    def history(self, itemid: str, since_ts: float, until_ts: float) -> list[dict]:
        # history=0 is float, which is what a reachability or utilisation item stores.
        # `limit` is here so a fast-polled item over a wide window cannot return tens of
        # thousands of rows into a process that only needs to describe a shape.
        rows = self._call("history.get", {
            "itemids": [itemid], "history": 0,
            "time_from": int(since_ts), "time_till": int(until_ts),
            "output": ["clock", "value"], "sortfield": "clock", "sortorder": "ASC",
            "limit": 500,
        }, 10)
        return [{"clock": int(x["clock"]), "value": x["value"]} for x in rows]

    def events_in_window(self, since_ts: float, until_ts: float) -> list[dict]:
        rows = self._call("event.get", {
            "output": ["eventid", "name", "clock", "objectid"],
            "selectHosts": ["name"],
            "time_from": int(since_ts), "time_till": int(until_ts),
            "sortfield": ["clock"], "sortorder": "ASC", "limit": 100,
        }, 11)
        return [{
            "eventid": x["eventid"], "name": x["name"], "clock": int(x["clock"]),
            "host": (x.get("hosts") or [{}])[0].get("name", "?"),
        } for x in rows]

    def group_reachability(self, host: str, max_hosts: int, max_items: int) -> list[dict]:
        """Reachability checks for the host's **most specific** group.

        Most specific matters. A host usually belongs to a broad group ("Linux servers") and
        a narrow one ("branch-office-3 access layer"). Asking the broad one returns neighbours
        that share nothing but an operating system, and "all its neighbours are fine" then
        means nothing at all. The narrow group is the one whose members share a path.
        """
        groups = self._call("host.get", {
            "filter": {"name": [host]}, "output": ["hostid"],
            "selectHostGroups": ["groupid", "name"],
        }, 12)
        if not groups:
            return []
        candidates = (groups[0].get("hostgroups") or groups[0].get("groups") or [])
        if not candidates:
            return []
        # Smallest membership = most specific. Ties break on the longer name, which in every
        # naming scheme worth having is the more specific one.
        sizes = []
        for g in candidates:
            members = self._call("host.get", {"groupids": [g["groupid"]], "output": ["hostid"]}, 13)
            sizes.append((len(members), -len(g.get("name", "")), g["groupid"]))
        sizes.sort()
        groupid = sizes[0][2]

        hosts = self._call("host.get", {
            "groupids": [groupid], "output": ["hostid", "name"], "limit": max_hosts,
        }, 14)
        items = self._call("item.get", {
            "hostids": [h["hostid"] for h in hosts],
            "search": {"key_": "icmpping"}, "searchByAny": True,
            "output": ["itemid", "name", "hostid", "lastvalue"], "limit": max_items,
        }, 15)
        names = {h["hostid"]: h["name"] for h in hosts}
        return [{
            "host": names.get(i["hostid"], "?"),
            "item": i["name"],
            # A reachability item reads 0 when the check failed. `lastvalue` can be missing
            # entirely on an item that has never been polled; that is not a failure, and
            # counting it as one would invent an outage.
            "failed": str(i.get("lastvalue", "")) == "0",
        } for i in items if i.get("lastvalue") not in (None, "")]

    def trigger_expression(self, objectid: str) -> dict:
        rows = self._call("trigger.get", {
            "triggerids": [objectid], "output": ["expression", "priority"],
            "expandExpression": True,
        }, 16)
        if not rows:
            return {}
        return {"expression": rows[0].get("expression", ""),
                "priority": rows[0].get("priority", "")}
