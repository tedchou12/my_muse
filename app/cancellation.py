"""Cooperative cancellation for long-running agent turns.

The web request can be disconnected while the agent keeps working. This tiny
registry lets the UI ask a run to stop at the next safe boundary (between model
rounds and between tool calls).
"""
from __future__ import annotations

import threading
from typing import Dict, Optional


class CancellationRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: Dict[str, threading.Event] = {}

    def begin(self, chat_id: str) -> threading.Event:
        event = threading.Event()
        with self._lock:
            self._events[chat_id] = event
        return event

    def cancel(self, chat_id: str) -> bool:
        with self._lock:
            event = self._events.get(chat_id)
            if event is None:
                return False
            event.set()
            return True

    def is_cancelled(self, chat_id: str) -> bool:
        with self._lock:
            event = self._events.get(chat_id)
            return bool(event and event.is_set())

    def finish(self, chat_id: str, event: Optional[threading.Event] = None) -> None:
        with self._lock:
            if event is None or self._events.get(chat_id) is event:
                self._events.pop(chat_id, None)
