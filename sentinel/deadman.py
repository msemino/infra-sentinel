"""Dead-man switch: who watches the watchdog.

A watchdog cannot report its own death. Every guardrail in :mod:`sentinel.watchdog` is about
not alerting too much; none of them can fire when the process stops running at all, and that
failure is silent by construction — the operator sees a quiet channel, which is exactly what
a healthy day also looks like.

So the main cycle leaves a heartbeat on disk, and a **separate** unit on its own timer reads
it. The separation is the whole design:

* it does not import the monitoring clients, so a monitoring outage cannot take it down;
* it never calls the model, so a slow or missing model cannot delay it;
* it runs on a different schedule, so a hung cycle cannot block it.

It has one job and it can only do that job by not sharing anything with the thing it is
watching. The moment it needs the same network, the same credentials or the same process,
the two die together and it stops being a check.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class Heartbeat:
    ts: float = 0.0
    phase: str = ""
    cycle: int = 0

    @property
    def never_ran(self) -> bool:
        return self.ts <= 0


def write(path: str, phase: str, cycle: int) -> None:
    """Record that the cycle got this far. Called by the main loop, never by the checker."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"ts": time.time(), "phase": phase, "cycle": cycle}, f)
    # Atomic replace: a checker reading mid-write must never see half a heartbeat and
    # conclude the watchdog is dead. A false "it is dead" costs the same as a false "it is
    # alive" the first time, and much more the second, because you stop believing it.
    os.replace(tmp, path)


def read(path: str) -> Heartbeat:
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return Heartbeat()
    return Heartbeat(ts=float(d.get("ts", 0)), phase=str(d.get("phase", "")),
                     cycle=int(d.get("cycle", 0)))


def is_stale(hb: Heartbeat, now_ts: float, max_age_sec: int) -> bool:
    """True when the heartbeat is old enough that the cycle is not running.

    A heartbeat that was never written counts as stale. The alternative — treat "no file" as
    "not started yet, give it time" — is how a watchdog that never came up after a deploy
    goes unnoticed for as long as nobody happens to look.
    """
    if hb.never_ran:
        return True
    return (now_ts - hb.ts) > max_age_sec


def describe(hb: Heartbeat, now_ts: float) -> str:
    if hb.never_ran:
        return "no heartbeat has ever been written: the cycle has not completed since deploy"
    age = int(now_ts - hb.ts)
    return (f"last heartbeat {age}s ago (cycle {hb.cycle}, phase {hb.phase!r}) — "
            f"the cycle is not completing")


def main() -> int:
    """Entry point for the separate timer. Deliberately tiny and dependency-free."""
    from .config import Config
    from .notifiers import build_notifier

    cfg = Config.from_env()
    hb = read(cfg.heartbeat_file)
    now = time.time()
    if not is_stale(hb, now, cfg.heartbeat_max_age_sec):
        print(f"[deadman] ok — {describe(hb, now)}")
        return 0

    print(f"[deadman] STALE — {describe(hb, now)}")
    build_notifier(cfg).send(
        "<b>Sentinel is not running</b>\n"
        f"{describe(hb, now)}.\n"
        f"Threshold: {cfg.heartbeat_max_age_sec}s. This message comes from the dead-man "
        "timer, not from the cycle."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
