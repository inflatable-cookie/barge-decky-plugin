"""Pytest configuration ensuring py_modules is importable."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY_MODULES = ROOT / "py_modules"
if str(PY_MODULES) not in sys.path:
    sys.path.insert(0, str(PY_MODULES))
