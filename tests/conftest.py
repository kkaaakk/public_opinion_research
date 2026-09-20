"""Shared test environment guards.

LangSmith tracing is disabled by default for the test suite so unit tests never
depend on the SaaS or upload traces.  Integration tests that exercise the
tracing boundary enable it explicitly with monkeypatch.
"""

import os

os.environ["LANGSMITH_TRACING"] = "false"
os.environ.setdefault("LANGSMITH_PROJECT", "public-opinion-research")
