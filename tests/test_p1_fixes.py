# 回归检查用量记账、索引批次、异常传播以及 Web 输入与授权行为。
"""Focused regression tests for the P1 reliability and safety fixes."""

import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError
from sdk_model_support import Reply, install_sequence
from starlette.requests import Request

import open_deep_research.runtime.business_agent as business_agent_module
from open_deep_research.configuration import Configuration
from open_deep_research.state import Section
from open_deep_research.workflow import reporting


# 构造章节写作所需的隔离配置。
def _writer_config() -> dict:
    return {
        "configurable": {
            "section_writer_model": "openai:writer-fixture",
        }
    }


# 构造包含计划章节和角色证据的测试状态。
def _section_state(section: Section) -> dict:
    return {
        "report": {"sections": [section], "completed_sections": []},
        "agents": {"risk_assessment": {"report": "risk evidence"}},
        "runtime": {"budget": {}},
    }


# 验证研究章节与最终章节写作均记录响应令牌。
def test_section_and_final_section_writer_record_response_usage(monkeypatch) -> None:
    """Both parallel writer stages merge model calls and token usage."""
    install_sequence(monkeypatch, Reply("written", 11, 7), Reply("written", 11, 7))

    research_result = asyncio.run(
        reporting.section_writer(
            _section_state(
                Section(
                    name="Risk",
                    description="Risk evidence",
                    research=True,
                    agent_role="risk_assessment",
                )
            ),
            _writer_config(),
        )
    )
    final_result = asyncio.run(
        reporting.write_final_sections(
            {
                "report": {
                    "sections": [
                        Section(
                            name="Conclusion",
                            description="Summarize the report",
                            research=False,
                        )
                    ],
                    "completed_sections": [
                        Section(
                            name="Risk",
                            description="Risk evidence",
                            research=True,
                            content="risk section",
                            status="done",
                        )
                    ],
                },
                "agents": {},
                "runtime": {"budget": {}},
            },
            _writer_config(),
        )
    )

    for result in (research_result, final_result):
        assert result["runtime"]["budget"]["model_calls"] == 1
        assert result["runtime"]["budget"]["input_tokens"] == 11
        assert result["runtime"]["budget"]["output_tokens"] == 7
        assert result["runtime"]["budget"]["total_tokens"] == 18


# 验证缺少令牌元数据时仍记录真实模型调用次数。
def test_writer_without_usage_still_records_one_model_call(monkeypatch) -> None:
    """Providers without usage metadata are counted without fabricated tokens."""
    install_sequence(monkeypatch, Reply("written", 0, 0))

    result = asyncio.run(
        reporting.section_writer(
            _section_state(
                Section(
                    name="Risk",
                    description="Risk evidence",
                    research=True,
                    agent_role="risk_assessment",
                )
            ),
            _writer_config(),
        )
    )

    assert result["runtime"]["budget"]["model_calls"] == 1
    assert result["runtime"]["budget"]["input_tokens"] == 0
    assert result["runtime"]["budget"]["output_tokens"] == 0
    assert result["runtime"]["budget"]["total_tokens"] == 0


# 验证未知压缩错误向外传播。
def test_unknown_compression_error_is_not_swallowed(monkeypatch) -> None:
    """Research compression re-raises programming errors instead of degrading."""

    async def fail(*args, **kwargs):
        raise NameError("test compression bug")

    monkeypatch.setattr(business_agent_module, "generate_text", fail)

    with pytest.raises(NameError, match="test compression bug"):
        asyncio.run(
            business_agent_module._compress_research(
                [],
                "task",
                "output",
                {},
                configurable=Configuration.from_runnable_config({}),
                config={},
            )
        )


# 验证明确上下文超限时研究压缩可以降级。
def test_token_limit_compression_error_keeps_graceful_degradation(monkeypatch) -> None:
    """Expected token-limit failures retain the existing bounded retry behavior."""

    install_sequence(monkeypatch, *(RuntimeError("context length exceeded") for _ in range(3)))
    monkeypatch.setattr(
        business_agent_module,
        "is_token_limit_exceeded",
        lambda *_args: True,
    )
    monkeypatch.setattr(
        business_agent_module,
        "remove_up_to_last_ai_message",
        lambda messages: messages,
    )

    result = asyncio.run(
        business_agent_module._compress_research(
            [],
            "task",
            "output",
            {},
            configurable=Configuration.from_runnable_config({}),
            config={},
        )
    )

    assert "Maximum retries exceeded" in result[0]


# 验证 Web 请求的模型白名单和主题长度限制。
def test_web_request_model_and_prompt_limits() -> None:
    """The Demo API rejects arbitrary models and oversized prompts."""
    from open_deep_research.web import server

    with pytest.raises(ValidationError):
        server.ResearchRequest(topic="brand risk", model="provider:untrusted-model")
    with pytest.raises(ValidationError):
        server.ResearchRequest(topic="x" * (server.MAX_TOPIC_LENGTH + 1))


# 验证令牌可选，但配置后所有受保护请求必须校验。
def test_web_api_token_is_optional_but_enforced_when_configured(monkeypatch) -> None:
    """Configured API tokens protect the research endpoint without OAuth complexity."""
    from open_deep_research.web import server

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/research",
            "headers": [(b"authorization", b"Bearer correct")],
        }
    )
    monkeypatch.setattr(server, "WEB_API_TOKEN", "correct")
    assert server._request_is_authorized(request) is True

    wrong_request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/research",
            "headers": [(b"authorization", b"Bearer wrong")],
        }
    )
    assert server._request_is_authorized(wrong_request) is False


# 验证流错误对用户脱敏且服务端保留诊断日志。
def test_web_stream_error_is_sanitized_and_logged(monkeypatch):
    from fastapi.testclient import TestClient
    from sqlalchemy.exc import SQLAlchemyError

    from open_deep_research.web import runs, server

    def fail():
        raise SQLAlchemyError("SECRET_DSN=mysql://user:password@db/internal")

    monkeypatch.setattr(server, "WEB_API_TOKEN", "error-test-token")
    monkeypatch.setattr(runs, "get_run_client", fail)
    with TestClient(server.app) as client:
        response = client.post(
            "/api/research",
            json={"topic": "brand risk"},
            headers={
                "Authorization": "Bearer error-test-token",
                "Idempotency-Key": "error-test",
            },
        )
    assert response.status_code == 503
    assert response.json()["detail"] == "Durable Run storage is unavailable."
    assert "SECRET_DSN" not in response.text


# 验证研究请求受并发信号量限制。
@pytest.mark.integration
@pytest.mark.parametrize("native_case", ["cancel"], indirect=True)
@pytest.mark.parametrize(
    "pg_database", [{"RUN_MAX_ACTIVE": 1, "RUN_OWNER_ACTIVE": 1}], indirect=True
)
def test_web_research_requests_are_semaphore_limited(native_case):
    """The native business queue enforces the quota independently of subscribers."""
    import time

    case = native_case
    first = case.create()
    second = case.create()
    end = time.monotonic() + 20
    while not case.blocked.is_set() and time.monotonic() < end:
        time.sleep(0.01)
    assert case.blocked.is_set()
    assert len(case.models) == 2
    active_ids = {
        wf.workflow_id.removesuffix(":research")
        for wf in case.client.client.list_workflows(
            name="por.research", status="PENDING", load_input=False, load_output=False
        )
    }
    assert len(active_ids) == 1
    active = next(r for r in (first, second) if r["run_id"] in active_ids)
    waiting = next(r for r in (first, second) if r is not active)
    case.client.cancel(active["run_id"], "alice")
    assert case.wait(active)["status"] == "cancelled"
    end = time.monotonic() + 20
    while len(case.models) < 4 and time.monotonic() < end:
        time.sleep(0.01)
    assert len(case.models) == 4
    case.client.cancel(waiting["run_id"], "alice")
    assert case.wait(waiting)["status"] == "cancelled"
    assert all(rt.harness.last_settlement.succeeded for rt in case.runtimes)


# 验证 Markdown 报告渲染必须经过净化或回退纯文本。
def test_markdown_report_rendering_is_sanitized() -> None:
    """The frontend sanitizes marked HTML and fails closed if the CDN is absent."""
    app_js = Path("src/open_deep_research/web/static/app.js").read_text(
        encoding="utf-8"
    )
    index_html = Path("src/open_deep_research/web/static/index.html").read_text(
        encoding="utf-8"
    )

    assert "DOMPurify.sanitize" in app_js
    assert "reportEl.innerHTML = marked.parse" not in app_js
    assert "dompurify@3.2.6" in index_html


# 验证存储授权使用显式拒绝逻辑，避免优化模式禁用 assert。
def test_store_authorization_does_not_use_assert() -> None:
    """Authorization checks remain active under python -O."""
    auth_source = Path("src/open_deep_research/web/runs.py").read_text(encoding="utf-8")
    assert "assert " not in auth_source
