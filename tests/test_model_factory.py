# 验证供应商模型解析、凭据优先级及方舟和 DeepSeek 的结构化输出适配。
"""Tests for unified provider/model construction and default routing."""

from __future__ import annotations

import pytest

from open_deep_research.configuration import Configuration
from open_deep_research.models import (
    DEFAULT_ARK_BASE_URL,
    ModelConfigurationError,
    parse_model_ref,
    resolve_api_key,
    resolve_ark_base_url,
)


# 验证 DeepSeek 模型引用被正确拆分。
def test_parse_deepseek_model_ref() -> None:
    ref = parse_model_ref("deepseek:deepseek-flash")
    assert (ref.provider, ref.model, ref.original) == (
        "deepseek",
        "deepseek-flash",
        "deepseek:deepseek-flash",
    )


# 验证方舟引用接受预置模型和自定义端点 ID。
@pytest.mark.parametrize(
    "model_id",
    ["doubao-seed-2-0-mini-260428", "ep-20240920-example"],
)
def test_parse_volcengine_model_ref_accepts_preset_and_endpoint(model_id: str) -> None:
    ref = parse_model_ref(f"volcengine:{model_id}")
    assert ref.provider == "volcengine"
    assert ref.model == model_id


# 验证凭据可以从供应商环境变量读取。
@pytest.mark.parametrize(
    ("model_ref", "env_name"),
    [
        ("deepseek:deepseek-flash", "DEEPSEEK_API_KEY"),
        ("volcengine:doubao-seed-2-0-mini-260428", "ARK_API_KEY"),
    ],
)
def test_credentials_from_environment(
    monkeypatch: pytest.MonkeyPatch, model_ref: str, env_name: str
) -> None:
    monkeypatch.setenv("GET_API_KEYS_FROM_CONFIG", "false")
    monkeypatch.setenv(env_name, "environment-secret")
    assert resolve_api_key(model_ref, {}) == "environment-secret"


# 验证运行时配置可以提供模型凭据。
@pytest.mark.parametrize(
    ("model_ref", "env_name"),
    [
        ("openai:gpt-4.1-mini", "OPENAI_API_KEY"),
        ("anthropic:claude-sonnet-4", "ANTHROPIC_API_KEY"),
        ("google:gemini-2.5-pro", "GOOGLE_API_KEY"),
        ("deepseek:deepseek-flash", "DEEPSEEK_API_KEY"),
        ("groq:llama-3.3-70b-versatile", "GROQ_API_KEY"),
        ("volcengine:doubao-seed-2-0-mini-260428", "ARK_API_KEY"),
    ],
)
def test_credentials_from_oap_config(
    monkeypatch: pytest.MonkeyPatch, model_ref: str, env_name: str
) -> None:
    monkeypatch.setenv("GET_API_KEYS_FROM_CONFIG", "true")
    config = {"configurable": {"apiKeys": {env_name: "injected-secret"}}}
    assert resolve_api_key(model_ref, config) == "injected-secret"


# 验证方舟地址默认值及显式覆盖。
def test_ark_base_url_default_and_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARK_BASE_URL", raising=False)
    assert resolve_ark_base_url() == DEFAULT_ARK_BASE_URL
    monkeypatch.setenv("ARK_BASE_URL", "https://example.invalid/v3/")
    assert resolve_ark_base_url() == "https://example.invalid/v3"


def test_unknown_and_malformed_model_refs_are_rejected():
    for reference in ("typo:model", "deepseek", "volcengine:"):
        with pytest.raises(ModelConfigurationError):
            parse_model_ref(reference)


# 验证默认角色与模型路由符合配置契约。
def test_default_model_routing() -> None:
    config = Configuration()
    doubao = "volcengine:doubao-seed-2-0-mini-260428"
    deepseek = "deepseek:deepseek-flash"
    assert config.summarization_model == doubao
    assert config.rag_query_rewrite_model == doubao
    assert config.rolling_compaction_model == doubao
    assert config.research_model == deepseek
    assert config.compression_model == deepseek
    assert config.final_report_model == deepseek
