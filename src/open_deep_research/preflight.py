"""Reusable pre-start checks for `por doctor`, `por run` and `por smoke`.

Checks are static where possible and probe PostgreSQL/DBOS only when asked.
Reports never include API keys, tokens or database passwords.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import text

from open_deep_research.config_file import (
    DEFAULT_CONFIG_FILE,
    ConfigFileError,
    config_summary,
    load_configuration,
)
from open_deep_research.configuration import Configuration
from open_deep_research.models.providers import (
    ModelConfigurationError,
    credential_env_name,
    parse_model_ref,
)
from open_deep_research.tools.registry import rag_requested

ERROR = "ERROR"
WARNING = "WARNING"
READY = "READY"
_LEVEL_ORDER = {READY: 0, WARNING: 1, ERROR: 2}

_SDK_PROVIDERS = frozenset({"openai", "deepseek", "volcengine", "groq"})
_LANGCHAIN_MODULES = {
    "anthropic": "langchain_anthropic",
    "google_genai": "langchain_google_genai",
    "google_vertexai": "langchain_google_vertexai",
    "bedrock": "langchain_aws",
    "cohere": "langchain_cohere",
    "mistralai": "langchain_mistralai",
    "ollama": "langchain_ollama",
}
_INDEXABLE_SUFFIXES = frozenset(
    {
        ".md",
        ".markdown",
        ".txt",
        ".json",
        ".jsonl",
        ".csv",
        ".pdf",
        ".py",
        ".ts",
        ".js",
        ".java",
        ".go",
        ".rs",
        ".sql",
        ".yaml",
        ".yml",
        ".png",
        ".jpg",
        ".jpeg",
        ".tif",
        ".tiff",
        ".webp",
    }
)
_MIN_RUN_MODEL_CALLS = 8


@dataclass(frozen=True)
class Check:
    level: str
    name: str
    detail: str


@dataclass
class PreflightReport:
    config_path: Path
    configuration: Configuration | None = None
    checks: list[Check] = field(default_factory=list)

    @property
    def level(self) -> str:
        return max(
            (check.level for check in self.checks),
            key=lambda level: _LEVEL_ORDER[level],
            default=READY,
        )

    @property
    def errors(self) -> list[Check]:
        return [check for check in self.checks if check.level == ERROR]

    @property
    def warnings(self) -> list[Check]:
        return [check for check in self.checks if check.level == WARNING]

    def add(self, level: str, name: str, detail: str) -> None:
        self.checks.append(Check(level, name, detail))

    def summary(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "config": str(self.config_path),
            "errors": len(self.errors),
            "warnings": len(self.warnings),
            "settings": config_summary(self.configuration)
            if self.configuration is not None
            else {},
        }


def run_preflight(
    config_path: str | Path | None = None,
    *,
    check_database: bool = True,
) -> PreflightReport:
    """Run the shared pre-start checks for one configuration file."""
    path = Path(config_path) if config_path is not None else DEFAULT_CONFIG_FILE
    report = PreflightReport(config_path=path)
    try:
        configuration = load_configuration(path)
    except (ConfigFileError, ValueError) as exc:
        report.add(ERROR, "configuration", str(exc))
        return report
    report.configuration = configuration
    report.add(READY, "configuration", f"Loaded {path.resolve()}")

    _check_models(report, configuration)
    _check_retrieval(report, configuration)
    _check_rag(report, configuration)
    _check_plugins(report, configuration)
    _check_optional_mcp(report, configuration)
    _check_budget_and_timeouts(report, configuration)
    _check_conflicts(report, configuration)
    _check_observability(report, configuration)
    if check_database:
        _check_database(report)
    return report


def _check_models(report: PreflightReport, configuration: Configuration) -> None:
    """Validate model references, credentials and the execution interface."""
    routes = {
        "research_model": (configuration.research_model, "sdk"),
        "compression_model": (configuration.compression_model, "langchain"),
        "final_report_model": (configuration.final_report_model, "langchain"),
        "summarization_model": (configuration.summarization_model, "langchain"),
        "rolling_compaction_model": (
            configuration.rolling_compaction_model,
            "langchain",
        ),
        "planner_model": (
            configuration.planner_model or configuration.research_model,
            "langchain",
        ),
        "section_writer_model": (
            configuration.section_writer_model or configuration.final_report_model,
            "langchain",
        ),
    }
    if configuration.rag_vision_enabled or configuration.rag_query_image_enabled:
        routes["rag_vision_model"] = (configuration.rag_vision_model, "langchain")
    if configuration.rag_query_rewrite_enabled and rag_requested(configuration):
        routes["rag_query_rewrite_model"] = (
            configuration.rag_query_rewrite_model,
            "langchain",
        )
    for field_name, (model_ref, interface) in routes.items():
        try:
            ref = parse_model_ref(model_ref)
        except ModelConfigurationError as exc:
            report.add(ERROR, f"model:{field_name}", str(exc))
            continue
        credential = _credential_ref(ref)
        if credential:
            report.add(
                ERROR,
                f"model:{field_name}",
                f"{model_ref} requires {credential} in .env.",
            )
        if interface == "sdk" and ref.provider not in _SDK_PROVIDERS:
            report.add(
                ERROR,
                "model:research_model",
                f"Agents SDK has no verified adapter for {ref.provider!r}; "
                f"use one of {', '.join(sorted(_SDK_PROVIDERS))}.",
            )
        module = _LANGCHAIN_MODULES.get(ref.provider)
        if module and importlib.util.find_spec(module) is None:
            report.add(
                ERROR,
                f"model:{field_name}",
                f"{model_ref} requires the {module!r} package.",
            )
    if not report.errors:
        report.add(
            READY,
            "models",
            "Configured model providers and credentials are available.",
        )
    if configuration.rag_vision_enabled or configuration.rag_query_image_enabled:
        report.add(
            WARNING,
            "model:vision",
            "Vision capability cannot be verified statically; confirm "
            f"{configuration.rag_vision_model} accepts image input.",
        )


def _credential_ref(ref) -> str | None:
    if os.getenv("GET_API_KEYS_FROM_CONFIG", "false").lower() == "true":
        return None
    env_name = credential_env_name(ref.provider)
    if not env_name:
        return None
    if os.getenv(env_name):
        return None
    if ref.provider == "google_genai" and os.getenv("GEMINI_API_KEY"):
        return None
    return env_name


def _check_retrieval(report: PreflightReport, configuration: Configuration) -> None:
    """Validate the web search interface required by enabled producers."""
    mode = configuration.retrieval_mode.value
    if mode not in {"web_only", "hybrid"}:
        if configuration.search_api.value != "none":
            report.add(
                READY,
                "retrieval",
                "Web search is not used by the selected retrieval mode.",
            )
        return
    if configuration.search_api.value != "tavily":
        report.add(
            ERROR,
            "retrieval",
            "The verified Agents SDK research path requires search_api=tavily; "
            "native OpenAI/Anthropic web search is not wired for SDK roles.",
        )
        return
    if not os.getenv("TAVILY_API_KEY"):
        report.add(ERROR, "retrieval", "TAVILY_API_KEY is required for tavily search.")
        return
    report.add(READY, "retrieval", "Tavily search is configured.")


def _check_rag(report: PreflightReport, configuration: Configuration) -> None:
    """Validate index sources, embedding/vector/reranker backends and OCR."""
    active = rag_requested(configuration)
    if not active:
        if "internal_knowledge" in (configuration.enabled_business_agents or []):
            report.add(
                ERROR,
                "rag:role",
                "internal_knowledge is enabled but local RAG is not active.",
            )
        report.add(READY, "rag", "Local RAG is disabled for this configuration.")
        return

    paths = configuration.rag_knowledge_base_paths or []
    missing = [path for path in paths if "://" not in path and not Path(path).exists()]
    if missing:
        report.add(
            ERROR,
            "rag:paths",
            "Configured knowledge paths do not exist: " + ", ".join(missing),
        )
    elif not _has_indexable_file(paths):
        level = (
            ERROR
            if "internal_knowledge" in (configuration.enabled_business_agents or [])
            else WARNING
        )
        report.add(
            level,
            "rag:paths",
            "Configured knowledge paths contain no indexable files.",
        )
    else:
        report.add(READY, "rag:paths", "Knowledge sources contain indexable files.")

    provider = configuration.rag_embedding_provider.strip().lower()
    if provider in {"hash"}:
        report.add(READY, "rag:embedding", "Hash embeddings need no external model.")
    elif importlib.util.find_spec("sentence_transformers") is None:
        report.add(
            ERROR,
            "rag:embedding",
            "sentence_transformers is not installed for semantic embeddings.",
        )
    else:
        report.add(
            WARNING,
            "rag:embedding",
            f"{configuration.rag_embedding_model} may download on first use; "
            "pre-warm the model for offline operation.",
        )

    vectorstore = configuration.rag_vectorstore_provider.strip().lower()
    if vectorstore == "milvus":
        if importlib.util.find_spec("pymilvus") is None:
            report.add(ERROR, "rag:vectorstore", "pymilvus is not installed.")
        else:
            _check_local_parent(report, configuration.rag_milvus_uri, "rag:vectorstore")
    elif vectorstore == "faiss":
        if importlib.util.find_spec("faiss") is None:
            report.add(ERROR, "rag:vectorstore", "faiss is not installed.")
    elif vectorstore == "chroma":
        if importlib.util.find_spec("chromadb") is None:
            report.add(ERROR, "rag:vectorstore", "chromadb is not installed.")
    else:
        report.add(
            WARNING,
            "rag:vectorstore",
            "The memory vector store is not persistent across processes.",
        )

    reranker = configuration.rag_reranker_provider.strip().lower()
    if reranker in {"none", "disabled", "simple"}:
        report.add(READY, "rag:reranker", f"Reranker backend {reranker} needs no model.")
    elif importlib.util.find_spec("sentence_transformers") is None:
        report.add(
            ERROR,
            "rag:reranker",
            "sentence_transformers is required for cross-encoder reranking.",
        )
    else:
        report.add(
            WARNING,
            "rag:reranker",
            f"{configuration.rag_reranker_model} may download on first use.",
        )

    if configuration.rag_keyword_backend.strip().lower() == "elasticsearch":
        if importlib.util.find_spec("elasticsearch") is None:
            report.add(
                ERROR,
                "rag:keyword",
                "rag_keyword_backend=elasticsearch requires the elasticsearch package.",
            )
        else:
            report.add(
                WARNING,
                "rag:keyword",
                f"Confirm Elasticsearch is reachable at "
                f"{configuration.rag_elasticsearch_url}.",
            )

    if configuration.rag_multimodal_enabled:
        if configuration.rag_multimodal_provider.strip().lower() in {
            "ocr",
            "tesseract",
            "local_ocr",
        }:
            if importlib.util.find_spec("pytesseract") is None:
                report.add(ERROR, "rag:ocr", "pytesseract is not installed.")
            elif shutil.which("tesseract") is None:
                report.add(
                    ERROR,
                    "rag:ocr",
                    "Tesseract binary is not installed; install it or set "
                    "rag_multimodal_provider: disabled in config.yaml.",
                )
            else:
                report.add(READY, "rag:ocr", "Tesseract OCR is available.")
        else:
            report.add(READY, "rag:ocr", "OCR is explicitly disabled.")


def _check_plugins(report: PreflightReport, configuration: Configuration) -> None:
    """Load enabled plugin manifests, skills and required MCP credentials."""
    enabled = list(configuration.enabled_plugins or [])
    if not enabled:
        report.add(READY, "plugins", "No plugins are enabled.")
        return
    directory = Path(configuration.plugins_dir or "")
    if not directory.is_dir():
        report.add(ERROR, "plugins", f"Plugin directory does not exist: {directory}")
        return
    try:
        from open_deep_research.public_opinion_agents.registry import (
            PUBLIC_OPINION_AGENT_SPECS,
        )
        from open_deep_research.tools.plugins import load_role_plugins

        loaded_roles = 0
        for role in configuration.enabled_business_agents or []:
            spec = PUBLIC_OPINION_AGENT_SPECS.get(str(role).strip().lower())
            if spec is None:
                continue
            role_plugins = load_role_plugins(
                configuration, spec.role, spec.allowed_domains
            )
            if role_plugins.plugins:
                loaded_roles += 1
        if not loaded_roles:
            report.add(
                ERROR,
                "plugins",
                "Enabled plugins are not authorized for any enabled business role.",
            )
        else:
            report.add(
                READY,
                "plugins",
                f"Loaded {', '.join(sorted(enabled))} for {loaded_roles} role(s).",
            )
    except (ValueError, KeyError) as exc:
        report.add(ERROR, "plugins", str(exc))


def _check_optional_mcp(report: PreflightReport, configuration: Configuration) -> None:
    """Fail fast for explicitly enabled non-core MCP servers."""
    if configuration.dbhub_enabled:
        if shutil.which("node") is None:
            report.add(ERROR, "mcp:dbhub", "DBHub requires Node.js >= 22.5.0.")
        elif not _node_supports_dbhub():
            report.add(ERROR, "mcp:dbhub", "DBHub requires Node.js >= 22.5.0.")
        if not os.getenv("DBHUB_DSN"):
            report.add(
                ERROR,
                "mcp:dbhub",
                "dbhub_enabled requires DBHUB_DSN in .env.",
            )
        else:
            report.add(READY, "mcp:dbhub", "DBHub dependency checks passed.")
    if configuration.markitdown_enabled:
        if shutil.which("uv") is None or importlib.util.find_spec("markitdown_mcp") is None:
            report.add(
                ERROR,
                "mcp:markitdown",
                "markitdown_enabled requires uv and the markitdown-mcp package.",
            )
        else:
            report.add(READY, "mcp:markitdown", "MarkItDown dependency checks passed.")
    if configuration.feishu_enabled:
        feishu_ready = True
        if shutil.which("node") is None:
            report.add(ERROR, "mcp:feishu", "Feishu MCP requires Node.js.")
            feishu_ready = False
        if not (configuration.feishu_app_id or "").strip():
            report.add(ERROR, "mcp:feishu", "feishu_enabled requires feishu_app_id.")
            feishu_ready = False
        if not os.getenv("FEISHU_APP_SECRET"):
            report.add(
                ERROR, "mcp:feishu", "feishu_enabled requires FEISHU_APP_SECRET in .env."
            )
            feishu_ready = False
        if feishu_ready:
            report.add(READY, "mcp:feishu", "Feishu MCP dependency checks passed.")


def _check_budget_and_timeouts(
    report: PreflightReport, configuration: Configuration
) -> None:
    """Validate budget ceilings and the relationship to DBOS run timeouts."""
    if not configuration.budget_enabled:
        report.add(
            WARNING,
            "budget",
            "Budget control is disabled; model and tool usage are unbounded.",
        )
    else:
        limits = (
            configuration.max_model_calls,
            configuration.max_tool_calls,
            configuration.max_search_calls,
            configuration.max_input_tokens,
            configuration.max_output_tokens,
        )
        if all(limit is None for limit in limits):
            report.add(
                WARNING,
                "budget",
                "budget_enabled is true but every limit is unset.",
            )
        else:
            report.add(
                READY,
                "budget",
                f"Budget enabled: models={configuration.max_model_calls}, "
                f"tools={configuration.max_tool_calls}, "
                f"searches={configuration.max_search_calls}.",
            )
        if (
            configuration.max_model_calls is not None
            and configuration.max_model_calls < _MIN_RUN_MODEL_CALLS
        ):
            report.add(
                ERROR,
                "budget",
                f"max_model_calls below {_MIN_RUN_MODEL_CALLS} cannot complete "
                "the four-role workflow and final report.",
            )
        if not configuration.reserve_final_report_call:
            report.add(
                WARNING,
                "budget",
                "reserve_final_report_call is false; research may consume the "
                "final-report model call.",
            )

    run_seconds = float(os.environ.get("RUN_TIMEOUT_SECONDS", "3600"))
    step_timeout = configuration.workflow_step_timeout_seconds
    if step_timeout is None:
        report.add(
            WARNING,
            "timeout",
            "No per-stage deadline is configured; a hung model or tool call "
            "relies on the DBOS run timeout only.",
        )
    elif step_timeout >= run_seconds:
        report.add(
            WARNING,
            "timeout",
            f"workflow_step_timeout_seconds={step_timeout:g} is not shorter than "
            f"the DBOS RUN_TIMEOUT_SECONDS={run_seconds:g}; the child workflow "
            "timeout will fire first.",
        )
    else:
        report.add(
            READY,
            "timeout",
            f"Stage deadline {step_timeout:g}s < DBOS run timeout {run_seconds:g}s.",
        )


def _check_conflicts(report: PreflightReport, configuration: Configuration) -> None:
    """Reject feature combinations that would silently disable each other."""
    agents = [str(role).strip().lower() for role in configuration.enabled_business_agents or []]
    if "internal_knowledge" in agents and not rag_requested(configuration):
        report.add(
            ERROR,
            "conflict",
            "internal_knowledge requires rag_enabled=true with retrieval_mode "
            "rag_only or hybrid.",
        )
    web_available = (
        configuration.search_api.value != "none"
        and configuration.retrieval_mode.value in {"web_only", "hybrid"}
    )
    social_available = (
        "social-research" in (configuration.enabled_plugins or [])
        and bool(os.getenv("APIFY_TOKEN"))
    )
    if "public_signal" in agents and not (web_available or social_available):
        report.add(
            ERROR,
            "conflict",
            "public_signal has no external research interface; enable Tavily "
            "or the social-research plugin with APIFY_TOKEN.",
        )
    if not {"public_signal", "internal_knowledge"} & set(agents):
        report.add(
            WARNING,
            "conflict",
            "No producer role is enabled; Review and the final report will have "
            "no fresh evidence.",
        )
    if configuration.report_mode == "sections" and not (
        configuration.planner_model or configuration.research_model
    ):
        report.add(ERROR, "conflict", "sections report mode requires a planner model.")
    sensitive = [
        name
        for name in ("dbhub_dsn", "feishu_app_secret", "rag_milvus_token")
        if getattr(configuration, name)
    ]
    if os.getenv("GET_API_KEYS_FROM_CONFIG", "false").lower() == "true":
        report.add(
            WARNING,
            "secrets",
            "GET_API_KEYS_FROM_CONFIG=true is an Open Agent Platform path; "
            "Run snapshots never persist configurable.apiKeys. Use .env for "
            "local CLI deployments.",
        )
    if sensitive:
        report.add(
            ERROR,
            "secrets",
            "Sensitive values must not be stored in config.yaml ("
            + ", ".join(sensitive)
            + "); move them to .env.",
        )
    if not report.errors:
        report.add(READY, "conflict", "No conflicting feature combinations.")


def _check_observability(
    report: PreflightReport, configuration: Configuration
) -> None:
    tracing = configuration.tracing_enabled
    override = os.environ.get("POR_TRACING_ENABLED", "").strip()
    if override:
        tracing = override.lower() in {"1", "true", "yes"}
    keys = bool(os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"))
    if tracing and not keys:
        report.add(
            WARNING,
            "observability",
            "tracing_enabled is true but Langfuse keys are missing; "
            "telemetry stays disabled.",
        )
    elif keys and not tracing:
        report.add(
            WARNING,
            "observability",
            "Langfuse keys are present but tracing_enabled is false.",
        )
    elif tracing:
        report.add(READY, "observability", "Langfuse tracing is enabled.")
    else:
        report.add(READY, "observability", "Tracing is explicitly disabled.")


def _check_database(report: PreflightReport) -> None:
    """Verify PostgreSQL, the business policy and compatible DBOS journals."""
    if not os.getenv("DBOS_DATABASE_URL"):
        report.add(
            ERROR,
            "database",
            "DBOS_DATABASE_URL is required in .env for CLI Runs and the Executor.",
        )
        return
    try:
        from open_deep_research.workflow.dbos_config import (
            close_connections,
            domain_engine,
            native_client,
            settings,
        )
        from open_deep_research.workflow.domain_schema import (
            require_matching_journals,
            verify_policy,
        )
    except Exception as exc:  # pragma: no cover - import guard
        report.add(ERROR, "database", f"DBOS dependencies are unavailable: {exc}")
        return
    try:
        settings()
        with domain_engine().connect() as db:
            db.execute(text("SELECT 1"))
        native_client().list_workflows(limit=1, load_input=False, load_output=False)
        verify_policy()
        require_matching_journals()
    except Exception as exc:
        report.add(ERROR, "database", f"PostgreSQL/DBOS is not ready: {exc}")
    else:
        report.add(READY, "database", "PostgreSQL and DBOS are reachable.")
        report.add(
            WARNING,
            "executor",
            "DBOS 3.3 does not expose executor liveness; start "
            "`deep-research-executor` before the first Run.",
        )
    finally:
        close_connections()


def _has_indexable_file(paths: list[str]) -> bool:
    for value in paths:
        if "://" in value:
            return True
        path = Path(value)
        if path.is_file() and path.suffix.lower() in _INDEXABLE_SUFFIXES:
            return True
        if path.is_dir():
            for item in path.rglob("*"):
                if item.is_file() and item.suffix.lower() in _INDEXABLE_SUFFIXES:
                    return True
    return False


def _check_local_parent(report: PreflightReport, value: str | None, name: str) -> None:
    if not value or "://" in value:
        return
    parent = Path(value).expanduser().resolve().parent
    if not parent.exists():
        report.add(ERROR, name, f"Vector store directory does not exist: {parent}")
    elif not os.access(parent, os.W_OK):
        report.add(ERROR, name, f"Vector store directory is not writable: {parent}")


def _node_supports_dbhub() -> bool:
    try:
        result = subprocess.run(
            ["node", "--version"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    version = result.stdout.strip().lstrip("v")
    try:
        major, minor = (int(part) for part in version.split(".")[:2])
    except (ValueError, IndexError):
        return False
    return major > 22 or (major == 22 and minor >= 5)


__all__ = ["Check", "ERROR", "PreflightReport", "READY", "WARNING", "run_preflight", "config_summary"]
