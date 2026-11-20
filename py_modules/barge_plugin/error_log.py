"""In-memory, capped log of machine errors shown from the panel's log view.

Nothing is persisted: the log is for the current Decky session, newest first.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, List

ERROR_LOG_CAPACITY = 50


@dataclass(frozen=True)
class ErrorEntry:
    """One logged error. `machine_id` and `machine_name` are empty for settings-file errors."""

    at_unix_ms: int
    machine_id: str
    machine_name: str
    kind: str
    message: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "at_unix_ms": self.at_unix_ms,
            "machine_id": self.machine_id,
            "machine_name": self.machine_name,
            "kind": self.kind,
            "message": self.message,
        }


class ErrorLog:
    """Bounded FIFO: once full, the oldest entry is dropped as a new one arrives."""

    def __init__(self, capacity: int = ERROR_LOG_CAPACITY) -> None:
        if capacity < 1:
            raise ValueError("error log capacity must be at least 1")
        self._entries: Deque[ErrorEntry] = deque(maxlen=capacity)

    def add(self, entry: ErrorEntry) -> None:
        self._entries.append(entry)

    def entries(self) -> List[Dict[str, Any]]:
        """Return entries newest first as plain dicts for the frontend."""
        return [entry.to_dict() for entry in reversed(self._entries)]

    def __len__(self) -> int:
        return len(self._entries)
