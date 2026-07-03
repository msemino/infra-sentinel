"""Tiny JSON-over-HTTP helper built on the standard library.

Deliberately dependency-free: the whole agent runs on the Python stdlib so it can
be deployed to a locked-down monitoring host without a virtualenv or pip access.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

__all__ = ["http_json", "HTTPError"]

HTTPError = urllib.error.HTTPError


def http_json(
    url: str,
    payload: Any | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 30,
) -> Any:
    """POST ``payload`` as JSON (or GET when ``payload`` is None) and parse the JSON reply."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(  # noqa: S310 — trusted internal URLs (Zabbix/Prometheus/Ollama)
        url,
        data=data,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode())
