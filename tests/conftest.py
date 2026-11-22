# 隔离测试环境，避免仓库本地环境文件影响配置和外部服务开关。
"""Shared test environment guards.

LangSmith tracing is disabled by default for the test suite so unit tests never
depend on the SaaS or upload traces.  Integration tests that exercise the
tracing boundary enable it explicitly with monkeypatch.
"""

import os
from typing import Any

import dotenv
import pytest


# 在测试中禁用仓库环境文件自动加载，保持测试配置可控。
def _disabled_load_dotenv(*_args: Any, **_kwargs: Any) -> bool:
    """Keep repository-local environment files out of the test process."""
    return False


# Prevent repository-local .env files from leaking real credentials or service
# configuration into deterministic tests during module collection.
os.environ["PYTHON_DOTENV_DISABLED"] = "1"
dotenv.load_dotenv = _disabled_load_dotenv
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["OPENAI_AGENTS_DISABLE_TRACING"] = "1"
os.environ.setdefault("LANGSMITH_PROJECT", "public-opinion-research")

# Native execution fixtures are lazy; integration selection controls service use.
pytest_plugins = ["dbos_support", "telemetry_support"]


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    """Anchor the project benchmark default before its storage plugin starts."""
    if (
        config.getoption("benchmark_storage", default=None)
        == "file://./.tmp/benchmarks"
    ):
        config.option.benchmark_storage = "file://" + str(
            config.rootpath / ".tmp" / "benchmarks"
        )


@pytest.fixture
def memory_ledger(monkeypatch):
    """Keep the call ledger in memory for a Run simulated without PostgreSQL.

    The executor writes the model, tool and operation ledgers outside every
    DBOS Step, so a unit test that fabricates a StageContext must either provide
    a database or account for the tool ledger itself.
    """
    from open_deep_research.runtime import tool_executor

    monkeypatch.setattr(tool_executor, "admit_tool_call", lambda *_args: True)
    monkeypatch.setattr(tool_executor, "finish_tool_call", lambda *_args: True)
