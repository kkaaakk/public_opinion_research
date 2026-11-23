"""Configuration single-source, freeze, preflight and CLI contracts."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from open_deep_research import cli
from open_deep_research.config_file import (
    DEFAULT_CONFIG_FILE,
    SENSITIVE_CONFIG_FIELDS,
    SMOKE_CONFIG_FILE,
    ConfigFileError,
    config_summary,
    effective_config,
    frozen_config,
    load_configuration,
    non_sensitive_config,
    request_payload,
)
from open_deep_research.configuration import Configuration
from open_deep_research.preflight import (
    ERROR,
    READY,
    PreflightReport,
    _check_observability,
    run_preflight,
)
from open_deep_research.web.durable import (
    matches_stored_request,
    request_fingerprint,
    validate_request,
)
from open_deep_research.web.run_data import digest
from open_deep_research.workflow.dbos_config import VERSION

ROOT = Path(__file__).resolve().parents[1]


def test_formal_and_smoke_configs_share_one_schema_and_all_fields():
    formal = load_configuration(DEFAULT_CONFIG_FILE)
    smoke = load_configuration(SMOKE_CONFIG_FILE)

    assert set(Configuration.model_fields) <= set(formal.model_dump())
    assert smoke.business_scenario == formal.business_scenario
    assert smoke.report_structure == formal.report_structure
    assert smoke.final_report_model_max_tokens == formal.final_report_model_max_tokens
    assert not {
        "report_mode",
        "planner_model",
        "planner_model_max_tokens",
        "section_writer_model",
        "section_writer_model_max_tokens",
    } & Configuration.model_fields.keys()
    assert smoke.enabled_plugins == []
    assert smoke.enabled_business_agents == [
        "public_signal",
        "risk_assessment",
        "response_strategy",
    ]
    assert smoke.rag_enabled is False
    assert smoke.retrieval_mode.value == "web_only"
    assert smoke.max_research_rounds == 1
    assert smoke.allow_plan_feedback is False
    assert smoke.allow_clarification is False
    assert smoke.research_model == formal.research_model
    assert smoke.final_report_model == formal.final_report_model


def test_unknown_fields_and_bad_yaml_are_rejected(tmp_path):
    unknown = tmp_path / "unknown.yaml"
    unknown.write_text("research_model: openai:gpt-4.1\nnot_a_field: true\n", "utf-8")
    with pytest.raises(ConfigFileError, match="not_a_field"):
        load_configuration(unknown)

    malformed = tmp_path / "malformed.yaml"
    malformed.write_text("research_model: [unterminated\n", "utf-8")
    with pytest.raises(ConfigFileError, match="not valid YAML"):
        load_configuration(malformed)

    missing = tmp_path / "missing.yaml"
    with pytest.raises(ConfigFileError, match="not found"):
        load_configuration(missing)


def test_environment_does_not_override_business_configuration(monkeypatch):
    monkeypatch.setenv("RESEARCH_MODEL", "openai:gpt-4.1")
    monkeypatch.setenv("RAG_ENABLED", "false")
    monkeypatch.setenv("FINAL_REPORT_MODEL", "openai:gpt-4.1")
    monkeypatch.setenv("ENABLED_PLUGINS", "[]")
    configuration = Configuration.from_runnable_config(
        {"configurable": {"research_model": "deepseek:deepseek-flash"}}
    )
    assert configuration.research_model == "deepseek:deepseek-flash"
    assert configuration.rag_enabled is False
    assert configuration.final_report_model == "deepseek:deepseek-flash"
    assert configuration.enabled_plugins == []


def test_frozen_snapshot_excludes_credentials_and_is_environment_stable(
    monkeypatch,
):
    configuration = load_configuration()
    snapshot = frozen_config(configuration)
    for field in SENSITIVE_CONFIG_FIELDS:
        assert field not in snapshot["configurable"]

    monkeypatch.setenv("RESEARCH_MODEL", "openai:gpt-4.1")
    monkeypatch.setenv("RAG_ENABLED", "false")
    effective = effective_config(snapshot)
    raw = Configuration.from_runnable_config(snapshot).model_dump(mode="json")
    assert effective == raw
    request = {
        "data": {
            "version": VERSION,
            "sdk": "0.24.0",
            "runtime": "sdk-vertical-v1",
            "configuration": digest(effective),
            "config": snapshot,
        }
    }
    validate_request(request)


def test_model_routing_has_distinct_tasks():
    configuration = load_configuration()
    summary = config_summary(configuration)
    assert summary["research_model"] == "deepseek:deepseek-flash"
    assert summary["compression_model"] == "deepseek:deepseek-flash"
    assert summary["final_report_model"] == "deepseek:deepseek-flash"
    assert summary["summarization_model"].startswith("volcengine:")
    assert summary["rag_query_rewrite_model"].startswith("volcengine:")
    assert summary["rolling_compaction_model"].startswith("volcengine:")
    assert summary["research_model"] != summary["summarization_model"]
    assert summary["rag_vision_model"].startswith("volcengine:")


def test_request_payload_records_effective_settings():
    configuration = load_configuration()
    payload = request_payload("brand", configuration)
    assert payload["model"] == configuration.research_model
    assert payload["rag_enabled"] is True
    assert payload["require_approval"] is True
    assert payload["allow_clarification"] is True


def test_tracing_intent_comes_from_each_yaml_configuration(monkeypatch):
    monkeypatch.setenv("POR_TRACING_ENABLED", "false")
    formal = load_configuration(DEFAULT_CONFIG_FILE)
    assert formal.tracing_enabled is True
    assert config_summary(formal)["tracing_scope"] == "process"

    monkeypatch.setenv("POR_TRACING_ENABLED", "true")
    smoke = load_configuration(SMOKE_CONFIG_FILE)
    assert smoke.tracing_enabled is False


def test_preflight_tracing_follows_yaml_not_environment(monkeypatch):
    monkeypatch.setenv("POR_TRACING_ENABLED", "true")
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    smoke = load_configuration(SMOKE_CONFIG_FILE)
    report = PreflightReport(config_path=SMOKE_CONFIG_FILE, configuration=smoke)
    _check_observability(report, smoke)
    assert any("explicitly disabled" in check.detail for check in report.checks)

    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.setenv("POR_TRACING_ENABLED", "false")
    formal = load_configuration(DEFAULT_CONFIG_FILE)
    report = PreflightReport(config_path=DEFAULT_CONFIG_FILE, configuration=formal)
    _check_observability(report, formal)
    assert any("process startup" in check.detail for check in report.checks)


def test_executor_tracing_policy_reads_yaml_and_fails_closed(monkeypatch):
    from open_deep_research.web import executor

    monkeypatch.setenv("POR_TRACING_ENABLED", "false")
    assert executor._configured_tracing() is True

    import open_deep_research.config_file as config_file

    def unavailable(*_args, **_kwargs):
        raise ConfigFileError("unavailable")

    monkeypatch.setattr(config_file, "load_configuration", unavailable)
    monkeypatch.setenv("POR_TRACING_ENABLED", "true")
    assert executor._configured_tracing() is False


def _effective_digest(configuration: Configuration) -> str:
    return digest(effective_config(frozen_config(configuration)))


def _fingerprint_digest(configuration: Configuration) -> str:
    return digest(non_sensitive_config(effective_config(frozen_config(configuration))))


def test_request_fingerprint_changes_with_effective_configuration():
    base = load_configuration(DEFAULT_CONFIG_FILE)
    changed = base.model_copy(
        update={"max_research_rounds": base.max_research_rounds + 1}
    )
    request = request_payload("brand risk", base)
    base_digest = _fingerprint_digest(base)
    changed_digest = _fingerprint_digest(changed)

    assert request_fingerprint(request, base_digest) == request_fingerprint(
        request, base_digest
    )
    assert request_fingerprint(request, base_digest) != request_fingerprint(
        request, changed_digest
    )


def test_stored_request_replay_requires_same_request_and_configuration():
    base = load_configuration(DEFAULT_CONFIG_FILE)
    changed = base.model_copy(
        update={"max_research_rounds": base.max_research_rounds + 1}
    )
    request = request_payload("brand risk", base)
    fingerprint_configuration = _fingerprint_digest(base)
    stored = {
        "fingerprint": request_fingerprint(request, fingerprint_configuration),
        "configuration": _effective_digest(base),
    }

    assert matches_stored_request(
        stored,
        request=request,
        fingerprint_configuration=fingerprint_configuration,
        configuration=_effective_digest(base),
    )
    assert not matches_stored_request(
        stored,
        request=request,
        fingerprint_configuration=_fingerprint_digest(changed),
        configuration=_effective_digest(changed),
    )
    assert not matches_stored_request(
        stored,
        request={**request, "topic": "another topic"},
        fingerprint_configuration=fingerprint_configuration,
        configuration=_effective_digest(base),
    )


def test_legacy_admission_replays_only_while_configuration_is_unchanged():
    base = load_configuration(DEFAULT_CONFIG_FILE)
    changed = base.model_copy(
        update={"max_research_rounds": base.max_research_rounds + 1}
    )
    request = request_payload("brand risk", base)
    configuration = _effective_digest(base)
    source = str(DEFAULT_CONFIG_FILE)
    legacy = {
        "fingerprint": digest(
            {
                "request": request,
                "session_id": None,
                "configuration_source": source,
            }
        ),
        "configuration": configuration,
    }

    assert matches_stored_request(
        legacy,
        request=request,
        fingerprint_configuration=_fingerprint_digest(base),
        configuration=configuration,
        config_source=source,
    )
    assert not matches_stored_request(
        legacy,
        request=request,
        fingerprint_configuration=_fingerprint_digest(changed),
        configuration=_effective_digest(changed),
        config_source=source,
    )


def test_fingerprint_input_excludes_credentials():
    base = load_configuration(DEFAULT_CONFIG_FILE)
    snapshot = frozen_config(base)
    with_secrets = {
        "configurable": {
            **snapshot["configurable"],
            "dbhub_dsn": "postgresql://user:secret@host/db",
            "feishu_app_secret": "secret",
            "rag_milvus_token": "secret-token",
        }
    }
    effective = non_sensitive_config(effective_config(with_secrets))
    for field in SENSITIVE_CONFIG_FIELDS:
        assert field not in effective
    assert effective == non_sensitive_config(effective_config(snapshot))
    assert request_fingerprint(
        request_payload("brand", base), digest(effective)
    ) == request_fingerprint(
        request_payload("brand", base), _fingerprint_digest(base)
    )


def test_rag_pipeline_config_has_no_parallel_defaults():
    from open_deep_research.rag.service import build_rag_pipeline_config

    configuration = load_configuration()
    pipeline = build_rag_pipeline_config(configuration)
    assert pipeline.knowledge_base_paths == configuration.rag_knowledge_base_paths
    assert pipeline.chunk_size == configuration.rag_chunk_size
    assert pipeline.top_k == configuration.rag_top_k
    assert pipeline.embedding_provider == configuration.rag_embedding_provider
    assert pipeline.embedding_model == configuration.rag_embedding_model
    assert pipeline.vectorstore_provider == configuration.rag_vectorstore_provider
    assert pipeline.vectorstore_path == configuration.rag_vectorstore_path
    assert pipeline.milvus_uri == configuration.rag_milvus_uri
    assert pipeline.reranker_provider == configuration.rag_reranker_provider
    assert pipeline.keyword_backend == configuration.rag_keyword_backend


def _temp_config(tmp_path: Path, **overrides) -> Path:
    lines = [f"extends: {DEFAULT_CONFIG_FILE.as_posix()}"]
    for key, value in overrides.items():
        lines.append(f"{key}: {json.dumps(value)}")
    path = tmp_path / "config.yaml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_preflight_reports_missing_credentials_and_conflicts(tmp_path, monkeypatch):
    for name in (
        "TAVILY_API_KEY",
        "APIFY_TOKEN",
        "DEEPSEEK_API_KEY",
        "ARK_API_KEY",
        "RUN_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    path = _temp_config(tmp_path)
    report = run_preflight(path, check_database=False)
    assert report.level == ERROR
    messages = "\n".join(check.detail for check in report.errors)
    assert "TAVILY_API_KEY" in messages
    assert "DEEPSEEK_API_KEY" in messages
    assert "APIFY_TOKEN" in messages

    no_rag = _temp_config(
        tmp_path,
        rag_enabled=False,
        retrieval_mode="web_only",
        enabled_plugins=[],
    )
    report = run_preflight(no_rag, check_database=False)
    assert any("internal_knowledge" in check.detail for check in report.errors)


def test_preflight_accepts_smoke_configuration_without_external_plugins(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "fixture")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture")
    monkeypatch.setenv("ARK_API_KEY", "fixture")
    report = run_preflight(SMOKE_CONFIG_FILE, check_database=False)
    assert report.level in {READY, "WARNING"}
    assert not report.errors


class FakeClient:
    def __init__(self):
        self.created = []
        self.retried = []
        self.approved = []
        self.clarified = []
        self.run = {
            "run_id": "run-1",
            "status": "awaiting_approval",
            "native_status": "PENDING",
            "settlement": "settled",
            "phase": "awaiting_approval",
            "current_step": "",
            "completed_steps": [],
            "report": "",
            "archive_status": "pending",
            "history_id": None,
            "plan": {"version": "v1", "revision": 1, "locked": False},
            "clarification": None,
            "error": {},
            "state_version": 7,
            "retry_safe": True,
            "events_complete": True,
            "configuration_source": "config.yaml",
        }

    def create(self, owner, key, request, config, state, **kwargs):
        self.created.append((owner, key, request, config, state, kwargs))
        return dict(self.run, status="queued", plan=None), True

    def get(self, run_id, owner):
        return dict(self.run)

    def approve(self, run_id, owner, version, decision, brief_md=None, **kwargs):
        self.approved.append((version, decision, brief_md, kwargs))
        return dict(self.run, status="running")

    def clarify(self, run_id, owner, version, answers, skip=False):
        self.clarified.append((version, answers, skip))
        return dict(self.run, status="running")

    def retry(self, run_id, owner, key, state_version):
        self.retried.append((key, state_version))
        return dict(self.run, status="running")


@pytest.fixture
def ready_preflight(monkeypatch):
    def report_for(path, **_kwargs):
        configuration = load_configuration(path)
        report = PreflightReport(config_path=Path(path), configuration=configuration)
        report.add(READY, "test", str(path))
        return report

    monkeypatch.setattr(cli, "run_preflight", report_for)
    return load_configuration()


def test_cli_run_uses_formal_config_and_no_business_overrides(
    monkeypatch, ready_preflight, capsys
):
    client = FakeClient()
    monkeypatch.setattr(cli, "_client", lambda: client)
    assert cli.main(["run", "brand risk", "--key", "fixed-key"]) == 0
    owner, key, request, config, state, kwargs = client.created[0]
    assert (owner, key) == ("operator", "fixed-key")
    assert request["model"] == ready_preflight.research_model
    assert config["configurable"]["rag_enabled"] is True
    assert kwargs["config_source"].endswith("config.yaml")
    assert state["messages"][0].content == "brand risk"
    output = capsys.readouterr().out
    assert "Run: run-1" in output


def test_cli_smoke_reads_smoke_config(monkeypatch, ready_preflight):
    seen = []
    original = cli.run_preflight

    def capture(path, **kwargs):
        seen.append(Path(path))
        return original(path, **kwargs)

    monkeypatch.setattr(cli, "run_preflight", capture)
    client = FakeClient()
    monkeypatch.setattr(cli, "_client", lambda: client)
    assert cli.main(["smoke", "brand risk"]) == 0
    assert seen and seen[0].name == "config.smoke.yaml"
    assert client.created[0][3]["configurable"]["rag_enabled"] is False
    assert client.created[0][3]["configurable"]["max_model_calls"] == 96


def test_cli_run_and_smoke_do_not_modify_global_configuration(
    monkeypatch, ready_preflight
):
    client = FakeClient()
    monkeypatch.setattr(cli, "_client", lambda: client)
    names = ("RESEARCH_MODEL", "RAG_ENABLED", "TRACING_ENABLED", "POR_TRACING_ENABLED")
    before = {name: os.environ.get(name) for name in names}
    defaults = Configuration().model_dump(mode="json")
    formal = DEFAULT_CONFIG_FILE.read_bytes()
    smoke = SMOKE_CONFIG_FILE.read_bytes()

    assert cli.main(["run", "brand risk"]) == 0
    assert cli.main(["smoke", "brand risk"]) == 0

    assert {name: os.environ.get(name) for name in names} == before
    assert Configuration().model_dump(mode="json") == defaults
    assert DEFAULT_CONFIG_FILE.read_bytes() == formal
    assert SMOKE_CONFIG_FILE.read_bytes() == smoke


def test_cli_approve_clarify_report_and_retry(monkeypatch, tmp_path, capsys):
    client = FakeClient()
    monkeypatch.setattr(cli, "_client", lambda: client)
    assert cli.main(["approve", "run-1"]) == 0
    assert client.approved[-1][:2] == ("v1", "approve")

    client.run = dict(
        client.run,
        status="awaiting_clarification",
        clarification={
            "version": "c1",
            "questions": [{"id": "brand", "text": "Which brand?", "kind": "text"}],
        },
    )
    assert (
        cli.main(["clarify", "run-1", "--answers", '{"brand": "Brand A"}']) == 0
    )
    assert client.clarified[-1] == ("c1", {"brand": "Brand A"}, False)

    client.run = dict(
        client.run,
        status="completed",
        report="# Report\n\nbody",
        history_id=None,
    )
    output = tmp_path / "report.md"
    assert cli.main(["report", "run-1", "--output", str(output)]) == 0
    assert output.read_text(encoding="utf-8") == "# Report\n\nbody"

    assert cli.main(["retry", "run-1", "--key", "retry-key"]) == 0
    assert client.retried[-1] == ("retry-key", 7)
    capsys.readouterr()


def test_cli_rejects_configuration_overrides():
    with pytest.raises(SystemExit):
        cli.main(["run", "brand", "--model", "openai:gpt-4.1"])


def test_preflight_summary_has_no_secret_values():
    configuration = load_configuration()
    report = PreflightReport(config_path=DEFAULT_CONFIG_FILE, configuration=configuration)
    report.add(READY, "test", "ok")
    summary = report.summary()
    text = json.dumps(summary)
    for name in ("DEEPSEEK_API_KEY", "ARK_API_KEY", "TAVILY_API_KEY", "APIFY_TOKEN"):
        assert name not in text


def test_required_rag_failure_is_exposed_to_the_workflow(monkeypatch):
    import asyncio

    from open_deep_research.tools import rag_tool

    def unavailable(_configuration):
        raise RuntimeError("index unavailable")

    monkeypatch.setattr(rag_tool, "build_rag_pipeline_config", unavailable)
    configuration = Configuration(
        rag_enabled=True,
        retrieval_mode="hybrid",
        rag_knowledge_base_paths=["data/knowledge/public_opinion"],
    )
    with pytest.raises(rag_tool.RAGRetrievalError):
        asyncio.run(
            rag_tool._rag_search(
                "brand",
                {"configurable": configuration.model_dump(mode="json")},
            )
        )


def test_cli_inspect_counts_only_succeeded_tools_as_success():
    events = [
        {
            "type": "sdk_event",
            "event": "tool_result",
            "name": "web_search",
            "status": "succeeded",
        },
        {
            "type": "sdk_event",
            "event": "tool_result",
            "name": "web_search",
            "status": "failed",
            "stage": "public_signal_agent",
            "error_type": "ValueError",
        },
        {
            "type": "sdk_event",
            "event": "tool_result",
            "name": "rag_search",
            "status": "cancelled",
        },
        {
            "type": "sdk_event",
            "event": "tool_result",
            "name": "rag_search",
            "status": "unobserved",
        },
        {"type": "sdk_event", "event": "model_completed"},
        {"type": "run_status", "status": "running"},
    ]

    model_calls, calls, failures, failed_events = cli._observation_counts(events)

    assert model_calls == 1
    assert calls == {"web_search": 2, "rag_search": 2}
    assert failures == {"web_search": 1, "rag_search": 2}
    assert failed_events == [
        {
            "event": "tool_result",
            "stage": "public_signal_agent",
            "error_type": "ValueError",
        }
    ]
