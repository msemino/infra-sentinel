"""Entry point: run one watchdog cycle.

Wiring only — the interesting decisions live in :mod:`sentinel.watchdog`. This
module reads config, pulls the sources, diffs against state, applies the
guardrails, optionally calls the LLM, notifies, and persists state. State is only
written when the full pipeline succeeded, which is what gives us the retry
guarantee: a failed LLM call or notification leaves the event pending for the
next cycle instead of being lost or sent raw.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime

from .analyzer import analyze
from .config import Config
from .logging import safe_print as _safe_print
from .notifiers import build_notifier
from .sources import DISPLAY_TZ, prometheus_down, zabbix_login, zabbix_problems
from .state import load_state, save_state
from .watchdog import (
    affected_hosts,
    build_snapshot,
    compute_diff,
    is_mass_outage,
    mark_notified,
)


def log(msg: str) -> None:
    _safe_print(f"[sentinel] {msg}")


def run_cycle(cfg: Config) -> None:
    state = load_state(cfg.state_file)
    notifier = build_notifier(cfg)

    token = zabbix_login(cfg)
    problems = zabbix_problems(cfg, token)
    prom = prometheus_down(cfg)

    # --- Guardrail 1: baseline seeding ---
    if not state.seeded:
        state.zabbix, state.prom, state.seeded = problems, prom, True
        state.last_notified = {}
        save_state(cfg.state_file, state)
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

    now_ts = time.time()
    diff = compute_diff(state, problems, prom, now_ts, cfg.dedup_cooldown_sec)

    if diff.suppressed:
        log(
            f"{diff.suppressed} event(s) suppressed by the {cfg.dedup_cooldown_sec}s "
            "cooldown (flapping, same trigger alerted recently)."
        )

    # --- Guardrail 2: nothing new → do not touch the GPU ---
    if not diff.has_news:
        state.zabbix, state.prom = problems, prom  # persist in case only cooldown fired
        save_state(cfg.state_file, state)
        log("quiet cycle: no net changes. LLM not used.")
        return

    stamp = datetime.now(DISPLAY_TZ).strftime("%Y-%m-%d %H:%M")

    # --- Guardrail 3: mass-outage circuit breaker ---
    if is_mass_outage(diff, cfg.mass_outage_threshold):
        log(
            f"MASS OUTAGE detected: {diff.new_count} new problems in one cycle -> "
            "single terse alert, LLM skipped."
        )
        hosts = affected_hosts(diff)
        sample = ", ".join(hosts[:8]) + ("…" if len(hosts) > 8 else "")
        message = (
            f"🚨 <b>Possible mass outage</b>: {diff.new_count} new problems this cycle "
            f"({len(diff.new_zabbix)} Zabbix + {len(diff.new_prom)} Prometheus).\n"
            f"Hosts: <code>{sample}</code>\n"
            "Check Zabbix/Grafana directly — details withheld to avoid flooding."
        )
        notifier.send(f"<b>Sentinel</b> · {stamp}\n\n{message}")
        mark_notified(state, diff, now_ts)
        state.zabbix, state.prom = problems, prom
        save_state(cfg.state_file, state)
        return

    # --- LLM reasoning (only reached when there is real, bounded news) ---
    snapshot = build_snapshot(diff)
    log(
        f"changes: {len(diff.new_zabbix)} new Zabbix, {len(diff.new_prom)} down Prometheus, "
        f"{len(diff.recovered_zabbix) + len(diff.recovered_prom)} recovered -> asking the LLM..."
    )
    t0 = time.time()
    message = analyze(cfg, snapshot)  # no fallback: on failure, raise and retry next cycle
    log(f"LLM responded in {time.time() - t0:.0f}s")

    notifier.send(f"<b>Sentinel</b> · {stamp}\n\n{message}")
    log("alert sent.")

    # Persist state only after the full pipeline succeeded (retry guarantee).
    mark_notified(state, diff, now_ts)
    state.zabbix, state.prom = problems, prom
    save_state(cfg.state_file, state)


def main() -> int:
    try:
        run_cycle(Config.from_env())
        return 0
    except Exception as e:  # noqa: BLE001 — top-level guard; event retried next cycle
        log(f"ERROR (will retry next cycle): {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
