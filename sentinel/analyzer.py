"""LLM analysis via a self-hosted Ollama endpoint — phase 2 only.

**This is best-effort work and nothing waits for it.** The alert has already been delivered
by the time anything here runs (see :mod:`sentinel.cli`), so a slow, broken or missing model
costs the operator a paragraph of context, never a notification and never a delay.

That is a reversal of how v1 worked, and it was a measurement that reversed it — see the
README section "What changed in v2, and why". The short version: when the model lives on
whatever hardware you actually have rather than on the hardware in the design document, a
single call can take minutes, and an ordering of `detect -> reason -> notify` turns that
latency into late alerts.

The model is also no longer asked to investigate. :mod:`sentinel.evidence` collects the facts
and :mod:`sentinel.verdict` derives the conclusions, both deterministically and in about a
tenth of a second. What arrives here is evidence plus an already-derived verdict, and the job
is to write it up and cross-reference the runbook — not to re-derive arithmetic it was
handed.
"""

from __future__ import annotations

import json
import re

from .config import Config
from .http import HTTPError, http_json

SYSTEM_PROMPT = (
    "You are Sentinel, an infrastructure watchdog for a network operations team.\n"
    "You receive a JSON document with what just changed in monitoring:\n"
    '- "new_problems": new High/Disaster problems on networking or telephony gear '
    "(core/access switches, APs, WLC, uplink radios, internet egress).\n"
    '- "down_targets": servers that stopped responding (mail, directory, DHCP, DMZ, backup).\n'
    '- "recovered": things that were broken and came back.\n'
    '- "evidence": facts already measured from the monitoring history — duration, what else '
    "moved in the same window, which neighbour checks failed and which kept answering.\n"
    '- "verdict": conclusions ALREADY derived from that evidence, deterministically.\n\n'
    "The verdict is not a draft for you to improve. It was computed from the measurements and "
    "it is correct by construction. Carry its statements through unchanged, and add only what "
    "it cannot know: what this means for the service, and what to look at first.\n\n"
    "Write ONE short operator message (max ~900 characters), technical and direct, no filler:\n"
    "1. What happened (group related events together).\n"
    "2. Likely impact on service.\n"
    "3. One concrete suggested action (what to look at first).\n"
    "Format: simple Telegram HTML (ONLY <b> and <code>; no <br>, <p> or any other tag — use "
    "real newlines). No markdown. No emojis (the system adds the header). Mark anything you "
    "are inferring as a hypothesis; anything from evidence or verdict is a fact and may be "
    "stated flatly. Do NOT invent data that is not in the document: if you don't know the "
    "impact, say so. Reply with ONLY the final message, no preamble."
)


def analyze(cfg: Config, document: dict) -> str:
    """Ask the LLM to write up ``document`` (snapshot + evidence + verdict)."""
    payload = {
        "model": cfg.ollama_model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(document, ensure_ascii=False, indent=1)},
        ],
        "stream": False,
        "think": False,
        "options": {"temperature": 0.2, "num_ctx": 8192},
    }
    url = cfg.ollama_url.rstrip("/") + "/api/chat"
    try:
        r = http_json(url, payload, timeout=cfg.ollama_timeout)
    except HTTPError as e:
        if e.code == 400:  # some models reject the "think" option
            payload.pop("think", None)
            r = http_json(url, payload, timeout=cfg.ollama_timeout)
        else:
            raise
    msg = r["message"]["content"]
    # Strip any chain-of-thought the model leaked despite think=False.
    msg = re.sub(r"<think>.*?</think>", "", msg, flags=re.S).strip()
    return msg
