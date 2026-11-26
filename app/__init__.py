"""Muse: a self-hosted personal AI agent."""
from __future__ import annotations

import os
from pathlib import Path

__version__ = "0.1.0"


def _build_stamp() -> str:
    """Fingerprint of the shipped code, used to bust stale browser bundles."""
    override = os.environ.get("MUSE_BUILD", "").strip()
    if override:
        return override
    root = Path(__file__).resolve().parent
    newest = 0.0
    for path in (
        root / "main.py",
        root / "api.py",
        root.parent / "static" / "app.js",
        root.parent / "static" / "styles.css",
    ):
        try:
            newest = max(newest, path.stat().st_mtime)
        except OSError:
            continue
    return "%s-%d" % (__version__, int(newest))


BUILD = _build_stamp()
