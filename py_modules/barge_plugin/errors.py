"""Domain errors for Barge protocol, configuration, and Wake-on-LAN."""

from __future__ import annotations

from typing import Literal

ErrorKind = Literal[
    "usage",
    "refused",
    "not_found",
    "io",
    "unknown_state",
    "unreachable",
]


class BargeError(Exception):
    """Typed domain error matching barge-machine's error classes."""

    def __init__(self, kind: ErrorKind, message: str, *, not_sent: bool = False) -> None:
        super().__init__(message)
        self.kind: ErrorKind = kind
        self.message: str = message
        self.not_sent: bool = not_sent
