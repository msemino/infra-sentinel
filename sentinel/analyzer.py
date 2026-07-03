"""LLM analysis via a self-hosted Ollama endpoint.

The LLM is only ever called when the watchdog decided there is genuinely new
information *and* the mass-outage breaker did not trip. There is deliberately
**no fallback**: if the model is unreachable, we raise, the cycle aborts, and the
event is retried next cycle. We never ship a raw, un-reasoned alert.
"""

from __future__ import annotations

import json
import re

from .config import Config
from .http import HTTPError, http_json

SYSTEM_PROMPT = (
    "You are Sentinel, an infrastructure watchdog for a network operations team.\n"
    "You receive a JSON snapshot of what just changed in monitoring:\n"
    '- "new_problems": new High/Disaster problems on networking or telephony gear '
    "(core/access switches, APs, WLC, uplink radios, internet egress).\n"
    '- "down_targets": servers that stopped responding (mail, directory, DHCP, DMZ, backup).\n'
    '- "recovered": things that were broken and came back.\n\n'
    "Write ONE short operator message (max ~900 characters), technical and direct, no filler:\n"
    "1. What happened (group related events together).\n"
    "2. Likely impact on service.\n"
    "3. One concrete suggested action (what to look at first).\n"
    "Format: simple Telegram HTML (ONLY <b> and <code>; no <br>, <p> or any other tag — use "
    "real newlines). No markdown. No emojis (the system adds the header). Do NOT invent data "
    "that is not in the snapshot: if you don't know the impact, say so. Reply with ONLY the "
    "final message, no preamble."
)


def analyze(cfg: Config, snapshot: dict) -> str:
    """Ask the LLM to reason about ``snapshot`` and draft the operator message."""
    payload = {
        "model": cfg.ollama_model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(snapshot, ensure_ascii=False, indent=1)},
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
