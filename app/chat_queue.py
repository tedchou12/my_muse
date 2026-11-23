"""Per-chat active-run tracking and queued user messages.

While a chat is running, new user messages are stored immediately and appended to
the agent context at the next safe boundary (between model rounds / tool calls).
"""
from __future__ import annotations

import threading
from typing import Any, Dict, List


class ChatQueue:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: set[str] = set()
        self._pending: Dict[str, List[Any]] = {}

    def try_begin(self, chat_id: str) -> bool:
        with self._lock:
            if chat_id in self._active:
                return False
            self._active.add(chat_id)
            self._pending.setdefault(chat_id, [])
            return True

    def finish(self, chat_id: str) -> None:
        with self._lock:
            self._active.discard(chat_id)
            self._pending.pop(chat_id, None)

    def is_active(self, chat_id: str) -> bool:
        with self._lock:
            return chat_id in self._active

    def status(self, chat_id: str) -> Dict[str, Any]:
        with self._lock:
            return {
                "active": chat_id in self._active,
                "queued": len(self._pending.get(chat_id, [])),
            }

    def enqueue(self, chat_id: str, text: str) -> bool:
        with self._lock:
            if chat_id not in self._active:
                return False
            self._pending.setdefault(chat_id, []).append(text)
            return True

    def enqueue_message(
        self, chat_id: str, text: str, attachments: List[Dict[str, Any]]
    ) -> bool:
        with self._lock:
            if chat_id not in self._active:
                return False
            self._pending.setdefault(chat_id, []).append(
                {"text": text, "attachments": list(attachments)}
            )
            return True

    def drain(self, chat_id: str) -> List[Any]:
        with self._lock:
            if chat_id not in self._active:
                return []
            return self._pending.pop(chat_id, [])
