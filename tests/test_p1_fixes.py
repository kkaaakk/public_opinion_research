# 回归检查用量记账、索引批次、异常传播以及 Web 输入与授权行为。
"""Focused regression tests for the P1 reliability and safety fixes."""

import asyncio
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import ValidationError
from sdk_model_support import Reply, install_sequence
from starlette.requests import Request

import open_deep_research.runtime.business_agent as business_agent_module
from open_deep_research.configuration import Configuration
from open_deep_research.workflow import reporting


@pytest.mark.parametrize("input_tokens,output_tokens", [(11, 7), (0, 0)])
def test_final_report_records_actual_usage(monkeypatch, input_tokens, output_tokens):
    requests = install_sequence(monkeypatch, Reply("# Report", input_tokens, output_tokens))
    result = asyncio.run(
        reporting.generate_final_report(
            {
                "workflow": {"brief": "Report on brand risk"},
                "agents": {"risk_assessment": {"report": "risk evidence"}},
            },
            {"configurable": {"final_report_model": "openai:writer-fixture"}},
        )
    )

    usage = result["runtime"]["budget"]
    assert usage["model_calls"] == len(requests) == 1
    assert usage["input_tokens"] == input_tokens
    assert usage["output_tokens"] == output_tokens
    assert usage["total_tokens"] == input_tokens + output_tokens


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
            {},
            configurable=Configuration.from_runnable_config({}),
            config={},
        )
    )

    assert "Maximum retries exceeded" in result[0]


def test_compression_prompt_answers_every_emitted_tool_call(monkeypatch) -> None:
    """A transcript that ends after tool_calls stays a valid provider request."""

    captured: list[list[dict]] = []

    async def record(model, prompt, **kwargs):
        captured.append(list(prompt))
        return "compressed report", {}

    monkeypatch.setattr(business_agent_module, "generate_text", record)
    messages = [
        AIMessage(
            content="",
            tool_calls=[
                {"name": "web_search", "id": "answered", "args": {"query": "a"}},
                {"name": "web_search", "id": "abandoned", "args": {"query": "b"}},
            ],
        ),
        ToolMessage(content="evidence a", tool_call_id="answered"),
        HumanMessage(content="summarize"),
    ]

    result = asyncio.run(
        business_agent_module._compress_research(
            messages,
            {},
            configurable=Configuration.from_runnable_config({}),
            config={},
        )
    )

    assert result[0] == "compressed report"
    calls = [item for item in captured[0] if item.get("type") == "function_call"]
    outputs = [item for item in captured[0] if item.get("type") == "function_call_output"]
    assert {item["call_id"] for item in calls} == {"answered", "abandoned"}
    assert {item["call_id"] for item in outputs} == {"answered", "abandoned"}
    # The unpaired call is answered in place, right after the call it belongs to.
    abandoned_index = next(
        index
        for index, item in enumerate(captured[0])
        if item.get("call_id") == "abandoned" and item["type"] == "function_call"
    )
    assert captured[0][abandoned_index + 1]["call_id"] == "abandoned"


# 验证 Web 请求的模型白名单和主题长度限制。
def test_web_request_model_and_prompt_limits() -> None:
    """The Demo API rejects arbitrary models and oversized prompts."""
    from open_deep_research.web import runs

    with pytest.raises(ValidationError):
        runs.ResearchRequest(topic="brand risk", model="provider:untrusted-model")
    with pytest.raises(ValidationError):
        runs.ResearchRequest(topic="x" * (runs.MAX_TOPIC_LENGTH + 1))


# 验证令牌可选，但配置后所有受保护请求必须校验。
def test_web_api_token_is_optional_but_enforced_when_configured(monkeypatch) -> None:
    """Configured API tokens protect the Run endpoint without OAuth complexity."""
    from open_deep_research.web import server

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/runs",
            "headers": [(b"authorization", b"Bearer correct")],
        }
    )
    monkeypatch.setattr(server, "WEB_API_TOKEN", "correct")
    assert server._request_is_authorized(request) is True

    wrong_request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/runs",
            "headers": [(b"authorization", b"Bearer wrong")],
        }
    )
    assert server._request_is_authorized(wrong_request) is False


# 验证创建失败对用户脱敏且服务端保留诊断日志。
def test_web_create_error_is_sanitized_and_logged(monkeypatch):
    from fastapi.testclient import TestClient
    from sqlalchemy.exc import SQLAlchemyError

    from open_deep_research.web import runs, server

    def fail():
        raise SQLAlchemyError("SECRET_DSN=mysql://user:password@db/internal")

    monkeypatch.setattr(server, "WEB_API_TOKEN", "error-test-token")
    monkeypatch.setattr(runs, "get_run_client", fail)
    with TestClient(server.app) as client:
        response = client.post(
            "/api/runs",
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


def test_markdown_report_rendering_is_sanitized() -> None:
    """Report Markdown skips raw HTML and applies an explicit safe URL policy."""
    renderer = Path("frontend/src/components/Markdown.tsx").read_text(encoding="utf-8")

    assert "from 'react-markdown'" in renderer
    assert "<ReactMarkdown" in renderer
    assert "skipHtml" in renderer
    assert "urlTransform={reportUrl}" in renderer
    assert r"/^(https?:\/\/|mailto:|#)/i.test(url) ? defaultUrlTransform(url) : ''" in renderer
    assert "dangerouslySetInnerHTML" not in renderer


# 验证存储授权使用显式拒绝逻辑，避免优化模式禁用 assert。
def test_store_authorization_does_not_use_assert() -> None:
    """Authorization checks remain active under python -O."""
    auth_source = Path("src/open_deep_research/web/runs.py").read_text(encoding="utf-8")
    assert "assert " not in auth_source
