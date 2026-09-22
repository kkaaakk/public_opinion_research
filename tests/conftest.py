"""Shared test environment guards.

LangSmith tracing is disabled by default for the test suite so unit tests never
depend on the SaaS or upload traces.  Integration tests that exercise the
tracing boundary enable it explicitly with monkeypatch.
"""

import os
from typing import Any

import dotenv


def _disabled_load_dotenv(*_args: Any, **_kwargs: Any) -> bool:
    """Keep repository-local environment files out of the test process."""
    return False

# Prevent repository-local .env files from leaking real credentials or service
# configuration into deterministic tests during module collection.
os.environ["PYTHON_DOTENV_DISABLED"] = "1"
dotenv.load_dotenv = _disabled_load_dotenv
os.environ["LANGSMITH_TRACING"] = "false"
os.environ.setdefault("LANGSMITH_PROJECT", "public-opinion-research")
