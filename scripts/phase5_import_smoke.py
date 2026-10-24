"""Import production entrypoints with credentials, tracing and retired engines blocked."""

import importlib
import importlib.abc
import os
import socket
import sys


class RetiredEngineGuard(importlib.abc.MetaPathFinder):
    """Fail on an indirect engine import, even if an orphan package is installed."""

    def find_spec(self, fullname, path=None, target=None):
        """Simulate absent optional integrations and reject retired application modules."""
        if fullname.startswith("langgraph"):
            # Some dependencies probe an optional LangGraph integration. Make
            # that probe see an absent package; any required import still fails.
            raise ModuleNotFoundError("Retired engine unavailable: " + fullname)
        if fullname.startswith(
            (
                "open_deep_research.runtime.agent_runtime",
                "open_deep_research.workflow.graph",
            )
        ):
            raise AssertionError("Retired engine import: " + fullname)
        return None


def main():
    """Exercise package boundaries without any model or service connection."""
    for name in list(os.environ):
        if name.endswith(("_API_KEY", "_TOKEN", "_SECRET")):
            os.environ.pop(name)
    os.environ.update(
        PYTHON_DOTENV_DISABLED="1",
        OPENAI_AGENTS_DISABLE_TRACING="1",
        LANGSMITH_TRACING="false",
        LANGCHAIN_TRACING_V2="false",
    )

    def blocked(*args, **kwargs):
        raise AssertionError("Import smoke attempted a network connection")

    socket.socket.connect = blocked
    sys.meta_path.insert(0, RetiredEngineGuard())
    modules = [
        "open_deep_research",
        "deep_researcher",
        "configuration",
        "state",
        "budget",
        "models",
        "models.sdk",
        "runtime",
        "runtime.business_agent",
        "runtime.sdk_runtime",
        "runtime.sdk_context",
        "workflow",
        "workflow.dbos_workflow",
        "workflow.dbos_config",
        "workflow.run_facts",
        "workflow.python_state",
        "workflow.intake",
        "workflow.reporting",
        "workflow.research",
        "web.server",
        "web.runs",
        "web.durable",
        "web.executor",
        "web.history",
        "web.cli",
        "rag",
        "rag.mcp_server",
        "social_media_mcp",
        "social_media_apify_api",
        "memory",
        "mcp.tools",
        "tools.registry",
    ]
    for name in modules:
        importlib.import_module(
            name if name == "open_deep_research" else "open_deep_research." + name
        )
    assert not any(name.startswith("langgraph") for name in sys.modules)
    print(  # noqa: T201
        f"{len(modules)} production imports passed; retired engines and network blocked"
    )


if __name__ == "__main__":
    main()
