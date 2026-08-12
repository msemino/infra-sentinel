"""Derive the conclusions from the evidence. Deterministic, no model.

Every line emitted is arithmetic or a set comparison over measured facts. When the evidence
supports nothing, nothing is emitted — there is no default conclusion, because a confident
sentence with nothing behind it is worse than a short report.

The model receives this as input rather than as a task: re-deriving arithmetic it has already
been handed is where a small model reliably goes wrong.
"""

from __future__ import annotations

from .evidence import Evidence, summarise_history


def _hms(seconds: int) -> str:
    m, s = divmod(int(seconds), 60)
    if m and s:
        return f"{m}m{s:02d}s"
    if m:
        return f"{m}m"
    return f"{s}s"


def duration_line(ev: Evidence) -> str | None:
    """"It lasted 2m44s" instead of "it was down for a while"."""
    h = summarise_history(ev.history)
    if not h or h.get("duration_s") is None:
        return None
    line = f"Lasted {_hms(h['duration_s'])} — {h['samples']} samples"
    if h.get("cadence_s"):
        line += f" every {h['cadence_s']}s"
    if h.get("gap_detected"):
        line += "; at least one sample is missing from the series"
    return line + "."


def scope_line(ev: Evidence) -> str | None:
    """Where the failure stops. The checks that held are half of this sentence."""
    s = ev.siblings
    if not s.total:
        return None
    if not s.failed:
        return (f"The other {s.total} reachability checks on this host's group all kept "
                f"answering: the failure does not extend to its neighbours.")
    if not s.healthy:
        return (f"All {len(s.failed)} reachability checks on this host's group failed "
                f"together: this points upstream of the host, not at it.")
    failed = ", ".join(sorted({str(c.get("item", "?")) for c in s.failed})[:4])
    held = ", ".join(sorted({str(c.get("item", "?")) for c in s.healthy})[:4])
    return (f"Split result: {len(s.failed)} checks failed ({failed}) while "
            f"{len(s.healthy)} kept answering ({held}). Whatever broke did not take the "
            f"whole path with it.")


def correlation_line(ev: Evidence) -> str | None:
    if not ev.correlated:
        return None
    hosts = sorted({str(e.get("host", "?")) for e in ev.correlated})
    shown = ", ".join(hosts[:5]) + ("…" if len(hosts) > 5 else "")
    return (f"{len(ev.correlated)} other event(s) on {len(hosts)} host(s) in the same "
            f"window: {shown}.")


def trigger_line(ev: Evidence) -> str | None:
    w = ev.why_no_trigger
    if not w or not w.get("expression"):
        return None
    line = f"Trigger expression: {w['expression']}"
    if w.get("priority"):
        line += f" (priority {w['priority']})"
    return line + "."


def verdict(ev: Evidence) -> str:
    """The short synthesis. Empty string when the evidence supports nothing."""
    lines = [
        line for line in (
            duration_line(ev),
            scope_line(ev),
            correlation_line(ev),
            trigger_line(ev),
        ) if line
    ]
    if ev.degraded:
        lines.append(
            "Evidence incomplete — could not collect: " + ", ".join(sorted(ev.degraded)) + "."
        )
    return "\n".join(lines)
