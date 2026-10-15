# 隔离测试环境，避免仓库本地环境文件影响配置和外部服务开关。
"""Shared test environment guards.

LangSmith tracing is disabled by default for the test suite so unit tests never
depend on the SaaS or upload traces.  Integration tests that exercise the
tracing boundary enable it explicitly with monkeypatch.
"""

import os
from typing import Any

import dotenv


# 在测试中禁用仓库环境文件自动加载，保持测试配置可控。
def _disabled_load_dotenv(*_args: Any, **_kwargs: Any) -> bool:
    """Keep repository-local environment files out of the test process."""
    return False

# Prevent repository-local .env files from leaking real credentials or service
# configuration into deterministic tests during module collection.
os.environ["PYTHON_DOTENV_DISABLED"] = "1"
dotenv.load_dotenv = _disabled_load_dotenv
os.environ["LANGSMITH_TRACING"] = "false"
os.environ.setdefault("LANGSMITH_PROJECT", "public-opinion-research")
