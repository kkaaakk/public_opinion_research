"""Bounded text and structured generation through the SDK Model interface."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from contextvars import copy_context
from typing import Any, TypeVar

from agents import (
    AgentOutputSchema,
    ModelSettings,
    ModelTracing,
    TResponseInputItem,
    get_current_trace,
    trace,
)
from agents.exceptions import ModelBehaviorError
from agents.items import ItemHelpers, ModelResponse
from dbos import DBOS
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError

from open_deep_research.budget import (
    admit_model_call,
    begin_model_call,
    budget_from_sdk_usage,
    capture_budget_usage,
    end_model_call,
    merge_budget_usage,
    physical_model_usage,
)
from open_deep_research.llm.errors import ModelInvocationError, is_token_limit_exceeded
from open_deep_research.models.providers import parse_model_ref, sdk_api_for
from open_deep_research.models.sdk import (
    ProviderCompletion,
    observe_provider_completion,
    sdk_model,
)
from open_deep_research.observability.telemetry import telemetry_enabled

Output = TypeVar("Output", bound=BaseModel)
ModelInput = str | list[TResponseInputItem]


class ModelOutputError(ValueError):
    """A provider returned an unusable result for a bounded generation."""

    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


def _completion_error(
    completion: ProviderCompletion, *, allow_tool_call: bool = False
) -> ModelOutputError | None:
    if completion.status in {"length", "max_tokens"} or (
        completion.status == "incomplete" and completion.reason == "max_output_tokens"
    ):
        return ModelOutputError("Model output was truncated", retryable=True)
    if completion.status == "content_filter" or completion.reason == "content_filter":
        return ModelOutputError("Model refused the request")
    if completion.status in {"stop", "completed"} or (
        allow_tool_call and completion.status == "tool_calls"
    ):
        return None
    return ModelOutputError("Provider did not confirm model completion")


async def metered_model_response(
    model_ref: str,
    request: Callable[[str | None], Awaitable[ModelResponse]],
    *,
    max_output_tokens: int | None = None,
) -> tuple[ModelResponse, dict[str, Any], ProviderCompletion]:
    """Admit and settle exactly one SDK request inside its owning DBOS Step."""
    from open_deep_research.workflow.execution import (
        ExecutionControlError,
        is_execution_control_failure,
    )

    try:
        identity = await begin_model_call(model_ref, max_output_tokens)
    except SQLAlchemyError as exc:
        raise ExecutionControlError("Model admission storage unavailable") from exc
    with observe_provider_completion() as completion:
        try:
            response = await request(identity)
        except BaseException as exc:
            usage = budget_from_sdk_usage(completion.usage)
            usage["model_calls"] = 1
            await end_model_call(identity, physical_model_usage(usage, failed=True))
            error = (
                _completion_error(completion, allow_tool_call=True)
                if isinstance(exc, ModelBehaviorError) and completion.status is not None
                else None
            )
            if (
                error is None
                and isinstance(exc, Exception)
                and not is_execution_control_failure(exc)
            ):
                error = ModelInvocationError(
                    type(exc).__name__,
                    context_limit=is_token_limit_exceeded(exc, model_ref),
                )
            if error is None:
                exc.budget_usage = usage
                raise
            error.budget_usage = usage
            raise error from exc
        if completion.usage and completion.usage.input_tokens_details.cached_tokens:
            # DeepSeek can report cache hits outside the SDK-normalized details.
            # Keep the same count in the receipt and the replayed SDK response.
            input_details = response.usage.input_tokens_details
            input_details.cached_tokens = max(
                input_details.cached_tokens or 0,
                completion.usage.input_tokens_details.cached_tokens,
            )
        usage = budget_from_sdk_usage(response.usage)
        usage["model_calls"] = 1
        await end_model_call(identity, physical_model_usage(usage))
        return response, usage, completion


def _completed_text(
    response: ModelResponse, completion: ProviderCompletion, function_name: str | None
) -> str:
    if any(ItemHelpers.extract_refusal(item) for item in response.output):
        raise ModelOutputError("Model refused the request")
    if error := _completion_error(completion, allow_tool_call=bool(function_name)):
        raise error
    if any(
        isinstance(item, ResponseOutputMessage) and item.status != "completed"
        for item in response.output
    ):
        raise ModelOutputError("Provider did not confirm model completion")
    if function_name:
        arguments = [
            item.arguments
            for item in response.output
            if isinstance(item, ResponseFunctionToolCall) and item.name == function_name
        ]
        if len(arguments) != 1:
            raise ModelOutputError(
                "Expected one structured function response", retryable=True
            )
        text = arguments[0]
    else:
        text = "".join(ItemHelpers.extract_text(item) or "" for item in response.output)
    if not text.strip():
        raise ModelOutputError("Model returned empty output", retryable=True)
    return text


async def _generate(
    model_ref: str,
    input: ModelInput,
    *,
    schema: AgentOutputSchema | None = None,
    config: Mapping[str, Any] | None,
    max_tokens: int | None,
    component: str,
    api_key: str | None = None,
    attempts: int = 1,
    extra_body: dict[str, Any] | None = None,
) -> tuple[Any, dict[str, Any]]:
    from open_deep_research.workflow.execution import native_step_available

    if attempts < 1:
        raise ValueError("Model generation requires at least one attempt")
    ref = parse_model_ref(model_ref)
    chat = sdk_api_for(ref, config) == "chat_completions"
    settings = ModelSettings(
        max_tokens=max_tokens, extra_body=extra_body, store=None if chat else False
    )
    instructions = function_name = None
    wire_schema = schema
    if chat and ref.provider in {"volcengine", "anthropic"} and max_tokens is not None:
        # The previous Ark writer capped the answer AND reasoning via this field.
        settings.max_tokens = None
        settings.extra_args = {"max_completion_tokens": max_tokens}
    if schema and chat:
        if ref.provider in {"deepseek", "groq", "mistralai", "cohere"}:
            wire_schema = None
            json_format: dict[str, Any] = {"type": "json_object"}
            if ref.provider == "cohere":
                json_format["schema"] = schema.json_schema()
            settings.extra_args = {"response_format": json_format}
            instructions = _json_instructions(schema)
            if ref.provider == "deepseek" and ref.model.startswith(
                ("deepseek-flash", "deepseek-v4-", "deepseek-reasoner")
            ):
                settings.extra_body = {
                    **(extra_body or {}),
                    "thinking": {"type": "disabled"},
                }
        elif ref.provider in {"volcengine", "anthropic"}:
            # Ark endpoint IDs and older Claude models support forced function
            # output. The function carries a result only and is never executed.
            wire_schema = None
            if ref.provider == "anthropic" and (extra_body or {}).get("thinking"):
                # Claude disallows forced tools with manual extended thinking.
                instructions = _json_instructions(schema)
            else:
                function_name = schema.name()
                settings.extra_args = {
                    **(settings.extra_args or {}),
                    "tool_choice": {
                        "type": "function",
                        "function": {"name": function_name},
                    },
                    "tools": [
                        {
                            "type": "function",
                            "function": {
                                "name": function_name,
                                "parameters": schema.json_schema(),
                            },
                        }
                    ],
                }

    total: dict[str, Any] = {}

    async def execute() -> tuple[str, dict[str, Any], tuple[str, bool] | None]:
        admit_model_call(total)
        async with sdk_model(model_ref, config=config, api_key=api_key) as model:
            enabled = telemetry_enabled()
            observing = (
                trace(component)
                if enabled and get_current_trace() is None
                else nullcontext()
            )

            async def request(_identity):
                with observing:
                    return await model.get_response(
                        system_instructions=instructions,
                        input=input,
                        model_settings=settings,
                        tools=[],
                        output_schema=wire_schema,
                        handoffs=[],
                        tracing=(
                            ModelTracing.ENABLED_WITHOUT_DATA
                            if enabled
                            else ModelTracing.DISABLED
                        ),
                        previous_response_id=None,
                        conversation_id=None,
                        prompt=None,
                    )

            try:
                response, delta, completion = await metered_model_response(
                    model_ref, request, max_output_tokens=max_tokens
                )
            except ModelOutputError as exc:
                return "", exc.budget_usage, (str(exc), exc.retryable)
            try:
                return _completed_text(response, completion, function_name), delta, None
            except ModelOutputError as exc:
                # Journal the settled result and its disposition together. Replay
                # must reach the same retry decision without sending it again.
                return "", delta, (str(exc), exc.retryable)

    try:
        for attempt in range(attempts):
            try:
                text, delta, invalid = (
                    await DBOS.run_step_async(
                        {"name": "por.llm", "preemptible": True}, execute
                    )
                    if native_step_available()
                    else await execute()
                )
            except BaseException as exc:
                total = merge_budget_usage(total, getattr(exc, "budget_usage", {}))
                exc.budget_usage = total
                raise
            total = merge_budget_usage(total, delta)
            if invalid is not None:
                reason, retryable = invalid
                if retryable and attempt + 1 < attempts:
                    continue
                error = ModelOutputError(reason, retryable=retryable)
                error.budget_usage = total
                raise error
            if schema is None:
                return text, total
            try:
                return schema.validate_json(text), total
            except ModelBehaviorError:
                if attempt + 1 == attempts:
                    error = ModelOutputError(
                        f"Invalid structured output for {schema.name()}"
                    )
                    error.budget_usage = total
                    raise error from None
    finally:
        capture_budget_usage(total)


def _json_instructions(schema: AgentOutputSchema) -> str:
    return (
        "Return one JSON object matching this schema, with no prose or code fences:\n"
        + json.dumps(schema.json_schema(), ensure_ascii=False, separators=(",", ":"))
    )


async def generate_text(
    model_ref: str,
    input: ModelInput,
    *,
    config: Mapping[str, Any] | None = None,
    max_tokens: int | None = None,
    component: str = "generate_text",
    api_key: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Generate complete text and return the budget delta."""
    return await _generate(
        model_ref,
        input,
        config=config,
        max_tokens=max_tokens,
        component=component,
        api_key=api_key,
    )


async def generate_structured(
    model_ref: str,
    input: ModelInput,
    schema: type[Output],
    *,
    config: Mapping[str, Any] | None = None,
    max_tokens: int | None = None,
    component: str = "generate_structured",
    max_attempts: int = 1,
    extra_body: dict[str, Any] | None = None,
) -> tuple[Output, dict[str, Any]]:
    """Return locally validated Pydantic output and usage across bounded attempts."""
    # Some business models have defaults for omitted fields. Let their
    # Pydantic validators own that contract across providers.
    return await _generate(
        model_ref,
        input,
        schema=AgentOutputSchema(schema, strict_json_schema=False),
        config=config,
        max_tokens=max_tokens,
        component=component,
        attempts=max_attempts,
        extra_body=extra_body,
    )


def generate_text_sync(
    model_ref: str,
    input: ModelInput,
    *,
    config: Mapping[str, Any] | None = None,
    api_key: str | None = None,
    max_tokens: int | None = None,
    component: str = "generate_text",
) -> tuple[str, dict[str, Any]]:
    """Call the same SDK path from synchronous RAG loaders and MCP handlers."""

    def run():
        return asyncio.run(
            _generate(
                model_ref,
                input,
                config=config,
                api_key=api_key,
                max_tokens=max_tokens,
                component=component,
            )
        )

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return run()
    # FastMCP can invoke its synchronous RAG tool on the server event loop.
    with ThreadPoolExecutor(max_workers=1) as worker:
        return worker.submit(copy_context().run, run).result()
