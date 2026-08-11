"""Entry point: run one watchdog cycle, in two phases.

    detect -> ALERT (seconds, no model) -> advance baseline -> investigate -> REPORT

v1 ran ``detect -> reason -> notify`` and persisted state only after the whole pipeline
succeeded. That gave a clean retry guarantee and one property nobody had priced: **the model
sat on the critical path of the notification.** A slow model delayed the alert; an
unreachable one withheld it. The README argued for that as a feature — an alert is either
reasoned or it does not go out.

Then it got measured. A single call to the local model took **9 minutes**. On a 15-minute
round that is an alert arriving up to half an hour after a three-minute outage, which is not
a reasoned alert, it is an obituary. The design decision was correct about what it optimised
for and wrong about what mattered.

So the phases now carry different guarantees, and the difference is the point:

======  ==========================  ==================================================
Phase   Message                     Guarantee
======  ==========================  ==================================================
1       ALERT — raw facts           **Load-bearing.** No model. The baseline advances
                                    *here*: if delivery fails, it does not advance and
                                    the next round retries. Nothing is lost.
2       REPORT — the investigation  **Best-effort.** If it fails, a short message says
                                    so. Never silent, never holds the alert back.
======  ==========================  ==================================================

The policy was in the design notes all along — *degrade the enrichment, never the delivery*.
v2 is where it became true of the code rather than of the prose.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime

from . import deadman
from .analyzer import analyze
from .config import Config
from .evidence import collect
from .logging import safe_print as _safe_print
from .notifiers import build_notifier
from .reader import ZabbixReader
from .sources import (
    DISPLAY_TZ,
    prometheus_down,
    zabbix_events_window,
    zabbix_login,
    zabbix_problems,
)
from .state import load_state, save_state
from .verdict import verdict
from .watchdog import (
    affected_hosts,
    build_snapshot,
    compute_diff,
    is_mass_outage,
    mark_alerted,
    mark_notified,
    prune_alerted,
    select_unalerted,
    window_start,
)


def log(msg: str) -> None:
    _safe_print(f"[sentinel] {msg}")


def _fmt_transient(events: list[dict]) -> str:
    """Render the events that opened *and closed* between rounds.

    These are the ones the active-problems query is structurally unable to report, so they
    are labelled: an operator seeing "already recovered" for something nobody paged them
    about needs to know it was never visible, not assume they missed a message.
    """
    lines = []
    for e in events:
        dur = e.get("duration_s")
        tail = f" — lasted {int(dur)}s, already recovered" if dur is not None else ""
        lines.append(f"• <code>{e.get('name', '?')}</code> at {e.get('since', '?')}{tail}")
    return "\n".join(lines)


def run_cycle(cfg: Config) -> None:
    state = load_state(cfg.state_file)
    notifier = build_notifier(cfg)
    cycle = int(state.last_round_ts)

    token = zabbix_login(cfg)
    problems = zabbix_problems(cfg, token)
    prom = prometheus_down(cfg)
    now_ts = time.time()

    # --- Guardrail 1: baseline seeding ---
    if not state.seeded:
        state.zabbix, state.prom, state.seeded = problems, prom, True
        state.last_notified = {}
        state.last_round_ts = now_ts
        save_state(cfg.state_file, state)
        deadman.write(cfg.heartbeat_file, "seed", cycle)
        log(
            f"first run: baseline seeded ({len(problems)} Zabbix problems, "
            f"{len(prom)} down Prometheus targets). No alerts."
        )
        notifier.send(
            "<b>Sentinel is on watch</b>\n"
            f"Baseline: {len(problems)} known High+ problems in Zabbix, "
            f"{len(prom)} known down targets in Prometheus.\n"
            "From now on I only alert on <b>changes</b>."
        )
        return

    diff = compute_diff(state, problems, prom, now_ts, cfg.dedup_cooldown_sec)

    # --- Guardrail 5: the event window, for incidents that opened and closed between rounds ---
    since = window_start(state.last_round_ts, now_ts, cfg.event_window_max_sec)
    if since > state.last_round_ts:
        log(
            f"window lookback capped at {cfg.event_window_max_sec}s "
            f"(last round was {int(now_ts - state.last_round_ts)}s ago) — not replaying the gap."
        )
    try:
        window = zabbix_events_window(cfg, token, since, now_ts)
    except Exception as e:  # noqa: BLE001 — the active-problem path must still run
        log(f"event window unavailable ({type(e).__name__}: {e}); active problems only")
        window = []
    transient = select_unalerted([e for e in window if e.get("transient")],
                                 state.alerted_eventids)

    if diff.suppressed:
        log(
            f"{diff.suppressed} event(s) suppressed by the {cfg.dedup_cooldown_sec}s "
            "cooldown (flapping, same trigger alerted recently)."
        )

    # --- Guardrail 2: nothing new → the model is not touched ---
    if not diff.has_news and not transient:
        state.zabbix, state.prom = problems, prom
        state.last_round_ts = now_ts
        state.alerted_eventids = prune_alerted(state.alerted_eventids, now_ts,
                                               cfg.event_window_max_sec)
        save_state(cfg.state_file, state)
        deadman.write(cfg.heartbeat_file, "quiet", cycle)
        log("quiet cycle: no net changes. Model not used.")
        return

    stamp = datetime.now(DISPLAY_TZ).strftime("%Y-%m-%d %H:%M")

    # --- Guardrail 3: mass-outage circuit breaker ---
    if is_mass_outage(diff, cfg.mass_outage_threshold):
        log(
            f"MASS OUTAGE detected: {diff.new_count} new problems in one cycle -> "
            "single terse alert, investigation skipped."
        )
        hosts = affected_hosts(diff)
        sample = ", ".join(hosts[:8]) + ("…" if len(hosts) > 8 else "")
        notifier.send(
            f"<b>Sentinel</b> · {stamp}\n\n"
            f"🚨 <b>Possible mass outage</b>: {diff.new_count} new problems this cycle "
            f"({len(diff.new_zabbix)} Zabbix + {len(diff.new_prom)} Prometheus).\n"
            f"Hosts: <code>{sample}</code>\n"
            "Check Zabbix/Grafana directly — details withheld to avoid flooding."
        )
        mark_notified(state, diff, now_ts)
        state.zabbix, state.prom = problems, prom
        state.last_round_ts = now_ts
        save_state(cfg.state_file, state)
        deadman.write(cfg.heartbeat_file, "mass-outage", cycle)
        return

    # ================================ PHASE 1 — ALERT ================================
    # Raw facts, no model, seconds. This is the load-bearing message: everything below it
    # can fail without the operator losing the notification.
    body = []
    if diff.new_zabbix:
        body.append("\n".join(
            f"• <code>{v.get('name', '?')}</code> on <b>{v.get('host', '?')}</b> "
            f"({v.get('severity', '?')}, since {v.get('since', '?')})"
            for v in diff.new_zabbix.values()
        ))
    if diff.new_prom:
        body.append("\n".join(
            f"• target down: <code>{v.get('instance', '?')}</code> (job {v.get('job', '?')})"
            for v in diff.new_prom.values()
        ))
    if transient:
        body.append("<b>Opened and closed between rounds</b> — invisible to the active "
                    "problem query:\n" + _fmt_transient(transient))
    recovered = len(diff.recovered_zabbix) + len(diff.recovered_prom)
    if recovered:
        body.append(f"{recovered} recovered.")

    notifier.send(f"🚨 <b>Sentinel — ALERT</b> · {stamp}\n\n" + "\n\n".join(body))
    log(f"phase 1: alert sent ({diff.new_count} new, {len(transient)} transient).")

    # The baseline advances *here*, tied to the alert and to nothing after it. If the send
    # above raised, we never got to this line, state is unchanged, and the next round picks
    # the same events up again.
    mark_notified(state, diff, now_ts)
    mark_alerted(state, transient, now_ts)
    state.zabbix, state.prom = problems, prom
    state.last_round_ts = now_ts
    state.alerted_eventids = prune_alerted(state.alerted_eventids, now_ts,
                                           cfg.event_window_max_sec)
    save_state(cfg.state_file, state)
    deadman.write(cfg.heartbeat_file, "alerted", cycle)

    # ============================ PHASE 2 — INVESTIGATION ============================
    # Everything from here is best-effort by construction: the operator has been told.
    lead = next(iter(diff.new_zabbix.values()), None) or (transient[0] if transient else None)
    if lead is None:
        return

    try:
        ev = collect(
            ZabbixReader(cfg, token), lead, now_ts,
            cfg.evidence_window_sec, cfg.siblings_max_hosts, cfg.siblings_max_items,
        )
        log(f"phase 2: evidence collected in {ev.elapsed_s * 1000:.0f}ms"
            + (f" (degraded: {', '.join(ev.degraded)})" if ev.degraded else ""))

        # Derived without a model, from the evidence above.
        conclusions = verdict(ev)

        t0 = time.time()
        message = analyze(cfg, {
            **build_snapshot(diff),
            "transient": transient,
            "evidence": {
                "history": ev.history[:40],
                "correlated": ev.correlated,
                "siblings_failed": ev.siblings.failed,
                "siblings_healthy": ev.siblings.healthy,
                "why_no_trigger": ev.why_no_trigger,
            },
            "verdict": conclusions,
        })
        log(f"phase 2: model responded in {time.time() - t0:.0f}s")
        notifier.send(f"🔎 <b>Sentinel — REPORT</b> · {stamp}\n\n{conclusions}\n\n{message}")
    except Exception as e:  # noqa: BLE001
        # Never silent. An investigation that failed and said nothing is indistinguishable
        # from one that ran and found nothing, and those need very different responses.
        log(f"phase 2 failed: {type(e).__name__}: {e}")
        notifier.send(
            f"🔎 <b>Sentinel — REPORT</b> · {stamp}\n\n"
            f"Investigation failed ({type(e).__name__}). The alert above stands; "
            "this message only means there is no enrichment for it."
        )
    deadman.write(cfg.heartbeat_file, "reported", cycle)


def main() -> int:
    try:
        run_cycle(Config.from_env())
        return 0
    except Exception as e:  # noqa: BLE001 — top-level guard; event retried next cycle
        log(f"ERROR (will retry next cycle): {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
