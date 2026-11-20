"""Atomic owner-only file writes."""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Optional

from .errors import BargeError


def atomic_write_owner_only(
    path: Path,
    data: bytes,
    *,
    temp_prefix: str,
    label: str,
) -> None:
    """Atomically write `data` to `path` with mode 0o600 from creation."""
    parent = path.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BargeError("io", f"cannot create directory {parent} for {label}: {exc}") from exc

    temp_path = parent / f"{temp_prefix}{secrets.token_hex(16)}"
    fd: Optional[int] = None
    try:
        fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.chmod(temp_path, 0o600)
        view = memoryview(data)
        written = 0
        while written < len(data):
            written += os.write(fd, view[written:])
        os.fsync(fd)
        os.close(fd)
        fd = None
        os.replace(temp_path, path)
    except OSError as exc:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise BargeError("io", f"cannot store {label}: {exc}") from exc
