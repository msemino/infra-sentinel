"""Configuration, loaded entirely from environment variables.

Nothing here is hardcoded to any real host, token or path. Point the ``*_URL``
variables at real services (or at the bundled mocks) via a ``.env`` file or the
process environment. See ``.env.example`` for the full list.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _get(key: str, default: str | None = None) -> str:
    val = os.environ.get(key, default)
    if val is None:
        raise KeyError(f"required environment variable {key!r} is not set")
    return val


@dataclass(frozen=True)
class Config:
    """Runtime configuration for a single Sentinel cycle."""

    # --- Zabbix source ---
    zabbix_url: str
    zabbix_user: str
    zabbix_pass: str
    zabbix_min_severity: int  # 4 = High, 5 = Disaster

    # --- Prometheus source ---
    prometheus_url: str

    # --- LLM (Ollama) ---
    ollama_url: str
    ollama_model: str
    ollama_timeout: int

    # --- Notification ---
    notifier: str  # "telegram" | "inbox"
    telegram_token: str
    telegram_chat_id: str
    inbox_path: str

    # --- Guardrails ---
    dedup_cooldown_sec: int
    mass_outage_threshold: int

    # --- State ---
    state_file: str

    @classmethod
    def from_env(cls) -> Config:
        return cls(
            zabbix_url=_get("ZABBIX_URL"),
            zabbix_user=_get("ZABBIX_USER", "monitor"),
            zabbix_pass=_get("ZABBIX_PASS", "monitor"),
            zabbix_min_severity=int(_get("ZABBIX_MIN_SEVERITY", "4")),
            prometheus_url=_get("PROMETHEUS_URL"),
            ollama_url=_get("OLLAMA_URL"),
            ollama_model=_get("OLLAMA_MODEL", "qwen2.5:14b"),
            ollama_timeout=int(_get("OLLAMA_TIMEOUT", "300")),
            notifier=_get("NOTIFIER", "inbox").lower(),
            telegram_token=_get("TELEGRAM_TOKEN", ""),
            telegram_chat_id=_get("TELEGRAM_CHAT_ID", ""),
            inbox_path=_get("INBOX_PATH", "./data/inbox.jsonl"),
            dedup_cooldown_sec=int(_get("DEDUP_COOLDOWN_SEC", "60")),
            mass_outage_threshold=int(_get("MASS_OUTAGE_THRESHOLD", "8")),
            state_file=_get("STATE_FILE", "./data/state.json"),
        )
