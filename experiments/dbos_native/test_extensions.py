"""Clone-policy negative cases and whitelist serializer contracts."""

import asyncio

import pytest
from agents import Agent, Runner
from agents.items import ModelResponse
from agents.testing import ScriptedModel, assistant_message
from codec import DomainFailure, NativeJSON
from langchain_core.tools import StructuredTool
from probe import standard_runtime
from runtime_ext import ExtensibleSDKRuntime, ScopedHarness
from test_sdk_runtime import TOKENS


@pytest.mark.parametrize(
    "attack", ["instructions", "name", "schema", "context", "handoff"]
)
def test_scope_admission_rejects_forged_clone_or_context(attack):
    class Port:
        @staticmethod
        async def run(agent, input, **kwargs):
            clone = agent.clone()
            if attack == "instructions":
                clone.instructions = "a different business role"
            if attack == "name":
                clone.name = "internal knowledge"
            if attack == "schema":
                clone.tools[0].params_json_schema["properties"]["forbidden"] = {
                    "type": "string"
                }
            if attack == "context":
                kwargs["context"] = object()
            if attack == "handoff":
                clone.handoffs = [Agent(name="unregistered role")]
            return await Runner.run(clone, input, **kwargs)

    class Harness(ScopedHarness):
        runner = Port

    class Runtime(ExtensibleSDKRuntime):
        harness_type = Harness

    async def search(query: str):
        return "synthetic result"

    tool = StructuredTool.from_function(
        coroutine=search,
        name="web_search",
        description="Allowed tool",
        metadata={"tool_domain": "web_search"},
    )
    model = ScriptedModel([[assistant_message("should never be called")]])
    runtime = standard_runtime("scope-security-" + attack, model, tool, cls=Runtime)
    with pytest.raises(ValueError):
        asyncio.run(runtime.run("role task"))
    assert not model.calls


def test_serializer_roundtrip_known_model_response_and_error():
    codec = NativeJSON()
    original = ModelResponse(
        output=[assistant_message("known")], usage=TOKENS, response_id="response"
    )
    restored = codec.deserialize(codec.serialize(original))
    assert isinstance(restored, ModelResponse) and restored.output == original.output
    assert restored.usage.input_tokens == 10 and restored.usage.output_tokens == 4
    failure = codec.deserialize(
        codec.serialize(DomainFailure("unknown effect", "UnknownToolEffect"))
    )
    assert isinstance(failure, DomainFailure) and failure.kind == "UnknownToolEffect"
    with pytest.raises(TypeError):
        codec.serialize(object())
    assert codec.deserialize('{"_native_type":"os.system","value":"ignored"}') == {
        "_native_type": "os.system",
        "value": "ignored",
    }
