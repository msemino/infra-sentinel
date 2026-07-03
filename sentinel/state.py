"""State persistence (JSON file on disk)."""

from __future__ import annotations

import json
import os

from .watchdog import State


def load_state(path: str) -> State:
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return State.from_dict(json.load(f))
    return State()


def save_state(path: str, state: State) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state.to_dict(), f, ensure_ascii=False, indent=1)
