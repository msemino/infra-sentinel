"""Notification channels.

Two implementations behind a common interface:

- :class:`TelegramNotifier` — sends to a real Telegram chat, with automatic
  fallback from HTML to plain text if the message is rejected.
- :class:`InboxNotifier` — for demos / CI: appends the message to a JSONL "inbox"
  file and echoes it to stdout, so the full pipeline is observable without any
  external service.

Selected via the ``NOTIFIER`` env var.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Protocol

from .config import Config
from .http import HTTPError, http_json
from .logging import safe_print


class Notifier(Protocol):
    def send(self, text: str) -> None: ...


class TelegramNotifier:
    """Send messages to a Telegram chat via the Bot API."""

    def __init__(self, token: str, chat_id: str) -> None:
        self._token = token
        self._chat_id = chat_id

    def send(self, text: str) -> None:
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        base = {"chat_id": self._chat_id, "disable_web_page_preview": True}
        # Telegram supports a tiny HTML subset; convert unsupported tags to newlines.
        text = re.sub(r"</?(br|p)\s*/?>", "\n", text)
        if len(text) > 4000:  # Telegram hard limit is 4096
            text = text[:4000] + "…"
        try:
            http_json(url, {**base, "text": text, "parse_mode": "HTML"})
        except HTTPError as e:
            body = e.read().decode(errors="replace")[:200]
            safe_print(
                f"[sentinel] Telegram rejected HTML ({e.code}: {body}) -> retrying as plain text"
            )
            plain = re.sub(r"<[^>]+>", "", text)
            http_json(url, {**base, "text": plain})


class InboxNotifier:
    """Write messages to a local JSONL inbox and echo to stdout (demo / CI mode)."""

    def __init__(self, path: str) -> None:
        self._path = path

    def send(self, text: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self._path)), exist_ok=True)
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "message": text,
        }
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        safe_print("\n" + "=" * 72)
        safe_print("[sentinel] ALERT (written to inbox: " + self._path + ")")
        safe_print("-" * 72)
        safe_print(text)
        safe_print("=" * 72 + "\n")


def build_notifier(cfg: Config) -> Notifier:
    """Construct the notifier selected by ``cfg.notifier``."""
    if cfg.notifier == "telegram":
        if not cfg.telegram_token or not cfg.telegram_chat_id:
            raise ValueError("NOTIFIER=telegram requires TELEGRAM_TOKEN and TELEGRAM_CHAT_ID")
        return TelegramNotifier(cfg.telegram_token, cfg.telegram_chat_id)
    return InboxNotifier(cfg.inbox_path)
