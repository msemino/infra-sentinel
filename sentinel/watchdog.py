"""Core watchdog logic: diff two snapshots and apply the guardrails.

Everything in this module is a **pure function** of its inputs (no I/O, no clock
reads except the ``now_ts`` you pass in). That is deliberate: it makes the
dedup / cooldown / circuit-breaker / event-window behaviour trivial to unit-test,
which is where most of the real value of this project lives.

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
5. **Event window with exactly-once** (v2) — the active-problems query only sees
   what is broken *right now*, so an incident that opens and closes between two
   rounds is invisible even though the monitoring system recorded it perfectly.
   The window query covers the gap; the windows deliberately overlap, and
   ``alerted_eventids`` is what stops the overlap from alerting twice.
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
    # v2 — eventid (str) -> epoch seconds we alerted. The exactly-once ledger for
    # the overlapping event windows. Pruned by `prune_alerted` so it stays bounded.
    alerted_eventids: dict[str, float] = field(default_factory=dict)
    # v2 — start of the next event window: the moment the last round observed.
    last_round_ts: float = 0.0

    @classmethod
    def from_dict(cls, d: dict) -> State:
        return cls(
            zabbix=d.get("zabbix", {}),
            prom=d.get("prom", {}),
            seeded=d.get("seeded", False),
            last_notified=d.get("last_notified", {}),
            alerted_eventids=d.get("alerted_eventids", {}),
            last_round_ts=float(d.get("last_round_ts", 0.0)),
        )

    def to_dict(self) -> dict:
        return {
            "zabbix": self.zabbix,
            "prom": self.prom,
            "seeded": self.seeded,
            "last_notified": self.last_notified,
            "alerted_eventids": self.alerted_eventids,
            "last_round_ts": self.last_round_ts,
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


# --------------------------------------------------------------------------------------
# v2 — the event window
#
# `problem.get` answers "what is broken now". An incident that opened and closed between
# two rounds never appears in it, so the watchdog stayed silent about an outage the
# monitoring system had recorded correctly. The fix is not a shorter round: it is to ask
# for *events in a time range* as well, and to make the ranges overlap so nothing can fall
# between them. Overlap then needs a ledger, which is `alerted_eventids`.
# --------------------------------------------------------------------------------------


def window_start(last_round_ts: float, now_ts: float, max_lookback_sec: int) -> float:
    """Where the next event window begins.

    Two cases matter and they pull in opposite directions:

    * A round was skipped (the scheduler will not start a run while the previous one is
      still going). The window simply stretches back to the last round that *did* finish,
      so the gap is covered and nothing is lost. This is why the cadence could be raised
      without raising the risk of missing anything.
    * The watchdog was down for a long time — a weekend, a failed deploy. Stretching back
      to `last_round_ts` would replay hours of history as if it had just happened and bury
      the operator on the first round after coming back. `max_lookback_sec` caps it.

    A first run (``last_round_ts == 0``) gets the cap, not the epoch.
    """
    if last_round_ts <= 0:
        return now_ts - max_lookback_sec
    return max(last_round_ts, now_ts - max_lookback_sec)


def select_unalerted(events: list[dict], alerted: dict[str, float]) -> list[dict]:
    """Events from the window we have not already alerted about.

    This is the whole of exactly-once. The windows overlap on purpose — an event landing
    exactly on a boundary must not be able to fall between two of them — and overlap means
    the same event *will* be handed to us more than once. Dropping the overlap to avoid
    duplicates would trade a duplicate alert for a missed one, which is the worse failure
    for a watchdog: a duplicate is noticed and a silence is not.
    """
    return [e for e in events if str(e.get("eventid", "")) not in alerted]


def mark_alerted(state: State, events: list[dict], now_ts: float) -> None:
    for e in events:
        eventid = str(e.get("eventid", ""))
        if eventid:
            state.alerted_eventids[eventid] = now_ts


def prune_alerted(alerted: dict[str, float], now_ts: float, keep_sec: int) -> dict[str, float]:
    """Forget ledger entries older than the widest window we can ever ask for.

    An event that can no longer appear in any future window cannot be alerted twice, so
    remembering it only grows the state file. `keep_sec` should be the lookback cap; keeping
    less than that would let an old event come back through a long window as if it were new.
    """
    cutoff = now_ts - keep_sec
    return {k: v for k, v in alerted.items() if v >= cutoff}
