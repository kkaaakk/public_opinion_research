"""Load the single business configuration file and freeze it for a Run.

``config.yaml`` is the only business configuration source. ``config.smoke.yaml``
extends it and overrides a bounded set of fields for low-cost end-to-end checks.
Credentials and deployment connections stay in ``.env`` and are never copied
into a Run snapshot.
"""

from __future__ import annotations

import os
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

import yaml
from pydantic import ValidationError

from open_deep_research.config_values import without_retired_execution_selectors
from open_deep_research.configuration import Configuration, RetrievalMode

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_FILE = PROJECT_ROOT / "config.yaml"
SMOKE_CONFIG_FILE = PROJECT_ROOT / "config.smoke.yaml"

# Credential-shaped fields must be supplied through the process environment so
# they never enter the persisted Run snapshot or DBOS Workflow input.
SENSITIVE_CONFIG_FIELDS = frozenset(
    {
        "dbhub_dsn",
        "feishu_app_secret",
        "rag_milvus_token",
    }
)

# Roles whose only external research capability is the local RAG pipeline.
RAG_REQUIRED_ROLES = frozenset({"internal_knowledge"})


class ConfigFileError(ValueError):
    """Report an invalid configuration file without echoing secret values."""


def load_configuration(path: str | Path | None = None) -> Configuration:
    """Load one YAML configuration file, resolving a single ``extends`` chain."""
    without_retired_execution_selectors(
        {
            name: os.environ[name.upper()]
            for name in ("workflow_backend", "business_agent_runtime")
            if name.upper() in os.environ
        }
    )
    config_path = Path(path) if path is not None else DEFAULT_CONFIG_FILE
    data = _load_mapping(config_path, stack=())
    try:
        return Configuration(**data)
    except ValidationError as exc:
        raise ConfigFileError(_format_validation_error(config_path, exc)) from None


def run_configuration(
    configuration: Configuration, *, rag_enabled: bool
) -> Configuration:
    """Derive the effective configuration of one Run from the server settings.

    The Web entry point lets the user decide whether this single Run may query
    the local enterprise knowledge base. ``rag_enabled`` is the only user-level
    switch; every other field keeps the value ``config.yaml`` declares, and the
    shared ``Configuration`` object is never mutated.

    Two dependent fields move with it, because leaving them behind would produce
    a configuration the rest of the runtime contradicts:

    * ``retrieval_mode`` — tool assembly reads it together with ``rag_enabled``.
      Turning RAG off returns to ``web_only`` so web search keeps working.
      Turning RAG on promotes a ``web_only`` Run to ``hybrid``; an already
      RAG-capable mode stays as configured.
    * ``enabled_business_agents`` — ``internal_knowledge`` may only use
      ``rag_search``, so an RAG-off Run drops that role instead of failing it
      for a capability the user turned off. Enabling RAG never adds a role the
      operator did not enable.
    """
    mode = configuration.retrieval_mode
    if not rag_enabled:
        mode = RetrievalMode.WEB_ONLY
    elif mode == RetrievalMode.WEB_ONLY:
        mode = RetrievalMode.HYBRID
    agents = list(configuration.enabled_business_agents or [])
    if not rag_enabled:
        agents = [role for role in agents if role not in RAG_REQUIRED_ROLES]
    updates = {"rag_enabled": rag_enabled, "retrieval_mode": mode}
    if agents != list(configuration.enabled_business_agents or []):
        updates["enabled_business_agents"] = agents
    updates = {
        name: value
        for name, value in updates.items()
        if getattr(configuration, name) != value
    }
    if not updates:
        return configuration
    return configuration.model_copy(update=updates)


def frozen_config(configuration: Configuration) -> dict[str, Any]:
    """Build the non-sensitive Run snapshot that Executors and Agents reuse."""
    return {"configurable": _without_sensitive(configuration.model_dump(mode="json"))}


def effective_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize one trusted Run snapshot into its effective settings."""
    return Configuration.from_runnable_config(config).model_dump(mode="json")


def non_sensitive_config(values: Mapping[str, Any]) -> dict[str, Any]:
    """Drop credential-shaped fields from an already-normalized snapshot."""
    return _without_sensitive(values)


def _without_sensitive(values: Mapping[str, Any]) -> dict[str, Any]:
    cleaned = dict(values)
    for field in SENSITIVE_CONFIG_FIELDS:
        cleaned.pop(field, None)
    return cleaned


def request_payload(
    topic: str,
    configuration: Configuration,
    *,
    org_context: str | None = None,
) -> dict[str, Any]:
    """Describe the effective business settings for one Run and its archive."""
    return {
        "topic": topic,
        "model": configuration.research_model,
        "search_api": configuration.search_api.value,
        "mode": "normal",
        "org_context": org_context
        if org_context is not None
        else (configuration.organization_context or ""),
        "rag_enabled": configuration.rag_enabled,
        "require_approval": configuration.allow_plan_feedback,
        "allow_clarification": configuration.allow_clarification,
    }


def config_summary(configuration: Configuration) -> dict[str, Any]:
    """Return the effective, non-sensitive settings printed by ``por doctor``."""
    return {
        "research_model": configuration.research_model,
        "compression_model": configuration.compression_model,
        "final_report_model": configuration.final_report_model,
        "summarization_model": configuration.summarization_model,
        "rolling_compaction_model": configuration.rolling_compaction_model,
        "rag_query_rewrite_model": configuration.rag_query_rewrite_model,
        "rag_vision_model": configuration.rag_vision_model,
        "enabled_business_agents": list(configuration.enabled_business_agents or []),
        "enabled_plugins": list(configuration.enabled_plugins or []),
        "max_research_rounds": configuration.max_research_rounds,
        "max_react_tool_calls": configuration.max_react_tool_calls,
        "search_api": configuration.search_api.value,
        "retrieval_mode": configuration.retrieval_mode.value,
        "rag_enabled": configuration.rag_enabled,
        "allow_clarification": configuration.allow_clarification,
        "allow_plan_feedback": configuration.allow_plan_feedback,
        "tracing_enabled": configuration.tracing_enabled,
        "tracing_scope": "process",
        "budget_enabled": configuration.budget_enabled,
        "max_model_calls": configuration.max_model_calls,
        "max_tool_calls": configuration.max_tool_calls,
        "max_search_calls": configuration.max_search_calls,
        "max_input_tokens": configuration.max_input_tokens,
        "max_output_tokens": configuration.max_output_tokens,
        "workflow_step_timeout_seconds": configuration.workflow_step_timeout_seconds,
    }


def _load_mapping(path: Path, *, stack: tuple[Path, ...]) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    if resolved in stack:
        raise ConfigFileError(f"Configuration inheritance cycle at {resolved}.")
    if not resolved.is_file():
        raise ConfigFileError(f"Configuration file not found: {resolved}")
    try:
        raw = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigFileError(
            f"Configuration file is not valid YAML: {resolved}: {exc}"
        ) from None
    if not isinstance(raw, Mapping):
        raise ConfigFileError(
            f"Configuration file must contain a YAML mapping: {resolved}"
        )
    data = dict(raw)
    parent = data.pop("extends", None)
    if parent is None:
        return _resolve_local_paths(data, resolved.parent)
    if not isinstance(parent, str) or not parent.strip():
        raise ConfigFileError(
            f"'extends' must be a relative file path in {resolved}."
        )
    base_path = (resolved.parent / parent).resolve()
    base = _load_mapping(base_path, stack=(*stack, resolved))
    return _resolve_local_paths(_merge(base, data), resolved.parent)


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _merge(dict(merged[key]), dict(value))
        else:
            merged[key] = deepcopy(value)
    return merged


def _resolve_local_paths(data: dict[str, Any], base_dir: Path) -> dict[str, Any]:
    """Resolve local paths against the config file so cwd cannot change meaning."""
    resolved = dict(data)
    plugins_dir = resolved.get("plugins_dir")
    if isinstance(plugins_dir, str) and plugins_dir.strip():
        resolved["plugins_dir"] = _local_path(plugins_dir, base_dir)
    knowledge_paths = resolved.get("rag_knowledge_base_paths")
    if isinstance(knowledge_paths, list):
        resolved["rag_knowledge_base_paths"] = [
            _local_path(item, base_dir)
            if isinstance(item, str) and item.strip() and "://" not in item
            else item
            for item in knowledge_paths
        ]
    for field in ("rag_vectorstore_path", "rag_milvus_uri"):
        value = resolved.get(field)
        if (
            isinstance(value, str)
            and value.strip()
            and "://" not in value
            and not Path(value).is_absolute()
        ):
            resolved[field] = _local_path(value, base_dir)
    return resolved


def _local_path(value: str, base_dir: Path) -> str:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return str(path.resolve())


def _format_validation_error(path: Path, exc: ValidationError) -> str:
    details = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", ())) or "<root>"
        details.append(f"{location}: {error.get('msg', 'invalid value')}")
    return f"Invalid configuration in {path}:\n  " + "\n  ".join(details)


__all__ = [
    "ConfigFileError",
    "DEFAULT_CONFIG_FILE",
    "PROJECT_ROOT",
    "SENSITIVE_CONFIG_FIELDS",
    "SMOKE_CONFIG_FILE",
    "config_summary",
    "effective_config",
    "frozen_config",
    "load_configuration",
    "non_sensitive_config",
    "request_payload",
    "run_configuration",
]
