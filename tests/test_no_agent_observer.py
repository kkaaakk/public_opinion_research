"""Repository-level regression guard: Agent Observer must not return.

The project's observability architecture is exactly:

* LangSmith (``observability.langsmith``) for execution tracing;
* ``budget.py`` on LangChain official callbacks for token/budget accounting.

The third system (an ``agent_observer`` sidecar with ``ObservedGraph`` /
``ObserverRunLifecycle`` / ``observe_*`` wrappers) was removed entirely.
These tests fail if any of its symbols, config fields, or env vars are
reintroduced into the current working tree.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN_SYMBOLS = (
    "agent_observer",
    "AgentObserver",
    "AGENT_OBSERVER",
    "ObservedGraph",
    "ObserverRunLifecycle",
    "observe_graph_node",
    "observe_model_ainvoke",
    "observe_model_invoke",
    "observe_tool_ainvoke",
    "observe_tool_invoke",
    "observer_available",
    "record_tool_call",
    "record_tool_result",
    "register_graph_topology",
    "observer_component",
    "observer_model",
    "observer_structured_output",
    "observer_logical_run_id",
    "get_current_observed_span",
    "get_current_observed_run",
)

SCAN_ROOTS = ("src", "tests", "docs")
SCAN_SUFFIXES = {".py", ".md", ".toml", ".json", ".cfg", ".txt", ".example"}
SKIP_DIRS = {"__pycache__", ".egg-info", "node_modules", ".venv", "dist", "build"}

# Historical changelog entries may keep the name as long as they explicitly
# mark the integration as removed so readers cannot mistake it for current
# architecture.
ALLOWED_HISTORICAL_PATTERN = re.compile(
    r"historical note:.*removed", re.DOTALL
)


def _iter_files():
    self_path = Path(__file__).resolve()
    for root in SCAN_ROOTS:
        base = REPO_ROOT / root
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix not in SCAN_SUFFIXES:
                continue
            if path.resolve() == self_path:
                continue
            if any(part in SKIP_DIRS or part.endswith(".egg-info") for part in path.parts):
                continue
            yield path


def _violations_in(content: str) -> list[str]:
    if ALLOWED_HISTORICAL_PATTERN.search(content):
        return []
    return [symbol for symbol in FORBIDDEN_SYMBOLS if re.search(symbol, content)]


def test_no_agent_observer_symbols_in_repository() -> None:
    """No Agent Observer symbol may exist in source, tests, or docs."""
    violations: list[str] = []
    for path in _iter_files():
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for symbol in _violations_in(content):
            violations.append(f"{path.relative_to(REPO_ROOT)}: {symbol}")
    assert not violations, "Agent Observer symbols reintroduced:\n" + "\n".join(
        violations
    )


def test_observability_package_exposes_only_langsmith() -> None:
    """The observability package exports only the LangSmith integration."""
    import open_deep_research.observability as observability

    expected = {
        "DEFAULT_PROJECT",
        "WORKFLOW_NAME",
        "agent_metadata",
        "correlation_metadata",
        "ensure_langsmith_configuration",
        "invocation_metadata",
        "langsmith_enabled",
        "langsmith_project",
        "model_metadata",
        "node_metadata",
        "record_current_metadata",
        "trace_span",
    }
    assert set(observability.__all__) == expected


def test_env_example_has_no_agent_observer() -> None:
    """The env template keeps only LangSmith as the observability config."""
    env_example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "AGENT_OBSERVER" not in env_example
    assert "LANGSMITH_API_KEY" in env_example
