"""Core watchdog logic: diff two snapshots and apply the guardrails.

Everything in this module is a **pure function** of its inputs (no I/O, no clock
reads except the ``now_ts`` you pass in). That is deliberate: it makes the
dedup / cooldown / circuit-breaker behaviour trivial to unit-test, which is where
most of the real value of this project lives.

Guardrails, in order:

1. **Baseline seeding** — the first cycle records what is already broken and
   raises nothing. You don't get paged for pre-existing problems on startup.
2. **Diff vs baseline** — only the delta (new problems, recoveries) is a candidate
   for alerting.
3. **Dedup by trigger objectid + cooldown** — a flapping trigger emits a new
   eventid every cycle. We key "have we already alerted?" on the stable objectid,
   not the eventid, and suppress re-alerts within a cooldown window.
4. **Mass-outage circuit breaker** — if a single cycle brings a flood of new
   problems, we short-circuit to one terse message instead of asking the LLM per
   event and spamming the operator.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class State:
    """Persisted state between cycles."""

    zabbix: dict[str, dict] = field(default_factory=dict)
    prom: dict[str, dict] = field(default_factory=dict)
    seeded: bool = False
    # objectid (str) -> epoch seconds of the last time we alerted about it.
    last_notified: dict[str, float] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> State:
        return cls(
            zabbix=d.get("zabbix", {}),
            prom=d.get("prom", {}),
            seeded=d.get("seeded", False),
            last_notified=d.get("last_notified", {}),
        )

    def to_dict(self) -> dict:
        return {
            "zabbix": self.zabbix,
            "prom": self.prom,
            "seeded": self.seeded,
            "last_notified": self.last_notified,
        }


@dataclass
class Diff:
    """The delta between the previous state and the current observation."""

    new_zabbix: dict[str, dict]
    recovered_zabbix: dict[str, dict]
    new_prom: dict[str, dict]
    recovered_prom: dict[str, dict]
    suppressed: int = 0  # events dropped by the cooldown guardrail

    @property
    def new_count(self) -> int:
        return len(self.new_zabbix) + len(self.new_prom)

    @property
    def has_news(self) -> bool:
        return bool(
            self.new_zabbix or self.recovered_zabbix or self.new_prom or self.recovered_prom
        )


def compute_diff(
    state: State,
    problems: dict[str, dict],
    prom: dict[str, dict],
    now_ts: float,
    cooldown_sec: int,
) -> Diff:
    """Diff the current observation against ``state`` and apply the cooldown guardrail.

    A problem is "new" if its eventid was not present last cycle. But before it
    counts as an *alertable* new problem, we check the cooldown: if we already
    alerted about the same trigger (objectid) within ``cooldown_sec``, it is
    treated as a continuation of the same incident (flapping) and suppressed.
    """
    new_zabbix_raw = {k: v for k, v in problems.items() if k not in state.zabbix}
    recovered_zabbix = {k: v for k, v in state.zabbix.items() if k not in problems}
    new_prom = {k: v for k, v in prom.items() if k not in state.prom}
    recovered_prom = {k: v for k, v in state.prom.items() if k not in prom}

    new_zabbix: dict[str, dict] = {}
    suppressed = 0
    for k, v in new_zabbix_raw.items():
        objectid = str(v.get("objectid", k))
        last = state.last_notified.get(objectid, 0)
        if now_ts - last < cooldown_sec:
            suppressed += 1
            continue
        new_zabbix[k] = v

    return Diff(
        new_zabbix=new_zabbix,
        recovered_zabbix=recovered_zabbix,
        new_prom=new_prom,
        recovered_prom=recovered_prom,
        suppressed=suppressed,
    )


def is_mass_outage(diff: Diff, threshold: int) -> bool:
    """True when this cycle should trip the mass-outage circuit breaker."""
    return diff.new_count >= threshold


def build_snapshot(diff: Diff) -> dict:
    """Build the JSON snapshot handed to the LLM for reasoning."""
    return {
        "new_problems": list(diff.new_zabbix.values()),
        "down_targets": list(diff.new_prom.values()),
        "recovered": (
            [{"source": "zabbix", **v} for v in diff.recovered_zabbix.values()]
            + [{"source": "prometheus", **v} for v in diff.recovered_prom.values()]
        ),
    }


def affected_hosts(diff: Diff) -> list[str]:
    """Sorted, de-duplicated list of hosts/instances touched by the new problems."""
    hosts = {v.get("host", "?") for v in diff.new_zabbix.values()}
    hosts |= {v.get("instance", "?") for v in diff.new_prom.values()}
    return sorted(hosts)


def mark_notified(state: State, diff: Diff, now_ts: float) -> None:
    """Record that we have just alerted about the new problems (updates cooldown clock)."""
    for v in diff.new_zabbix.values():
        objectid = str(v.get("objectid", ""))
        if objectid:
            state.last_notified[objectid] = now_ts
