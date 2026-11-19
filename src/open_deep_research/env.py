"""Load operator credentials and deployment connections from ``.env`` once.

Business settings are never read from the environment; only credentials and
service locations live there. Calling this helper is idempotent and safe from
any process (CLI, Web API, Executor).
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_LOADED = False


def load_environment() -> bool:
    """Load ``.env`` without overriding real environment variables."""
    global _LOADED
    if _LOADED:
        return True
    if os.environ.get("PYTHON_DOTENV_DISABLED"):
        _LOADED = True
        return False
    seen: set[Path] = set()
    for candidate in (Path.cwd() / ".env", PROJECT_ROOT / ".env"):
        path = candidate.resolve()
        if path in seen:
            continue
        seen.add(path)
        if path.is_file():
            load_dotenv(path, override=False, interpolate=False)
    _LOADED = True
    return True


__all__ = ["PROJECT_ROOT", "load_environment"]
