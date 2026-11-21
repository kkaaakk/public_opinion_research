"""Offline inference fixtures for the application's bounded SDK calls."""

import inspect
import json
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from agents import Usage, generation_span
from agents.items import ModelResponse
from agents.testing import assistant_message, function_call
from pydantic import BaseModel

from open_deep_research.llm import generation
from open_deep_research.models import sdk


@dataclass
class InferenceRequest:
    model_ref: str
    input: Any
    settings: Any
    schema_name: str | None
    function_name: str | None
    instructions: str | None

    @property
    def messages(self):
        return (
            [{"role": "user", "content": self.input}]
            if isinstance(self.input, str)
            else self.input
        )

    @property
    def prompt(self):
        return self.messages[0].get("content", "")

    @property
    def max_tokens(self):
        return self.settings.max_tokens or (self.settings.extra_args or {}).get(
            "max_completion_tokens"
        )


@dataclass
class Reply:
    content: Any
    input_tokens: int = 7
    output_tokens: int = 3
    status: str = "stop"
    input_details: dict | None = None
    output_details: dict | None = None


def install_inference(patch, respond, *, open_model=None):
    """Intercept Model.get_response while retaining Steps, validation and receipts.

    A responder may return None to delegate to the previous fixture. The opened
    hook is useful for distinguishing setup errors from actual paid attempts.
    """
    original = generation.sdk_model

    @asynccontextmanager
    async def factory(model_ref, *, config=None, api_key=None):
        if open_model is not None:
            open_model(model_ref, config, api_key)

        class Inference:
            async def get_response(
                self,
                *,
                input,
                model_settings,
                output_schema,
                system_instructions,
                tracing,
                **kwargs,
            ):
                args = model_settings.extra_args or {}
                functions = args.get("tools") or []
                function_name = functions[0]["function"]["name"] if functions else None
                schema_name = output_schema.name() if output_schema else function_name
                if (
                    not schema_name
                    and system_instructions
                    and "\n{" in system_instructions
                ):
                    schema_name = json.loads(system_instructions.split("\n", 1)[1]).get(
                        "title"
                    )
                request = InferenceRequest(
                    model_ref,
                    input,
                    model_settings,
                    schema_name,
                    function_name,
                    system_instructions,
                )
                reply = respond(request)
                if inspect.isawaitable(reply):
                    reply = await reply
                if reply is None:
                    async with original(
                        model_ref, config=config, api_key=api_key
                    ) as model:
                        return await model.get_response(
                            input=input,
                            model_settings=model_settings,
                            output_schema=output_schema,
                            system_instructions=system_instructions,
                            tracing=tracing,
                            **kwargs,
                        )
                if not isinstance(reply, Reply):
                    reply = Reply(reply)
                content = reply.content
                if isinstance(content, BaseModel):
                    content = content.model_dump_json()
                elif not isinstance(content, str):
                    content = json.dumps(content, ensure_ascii=False)
                completion = sdk._completion.get()
                if completion is not None:
                    completion.status = reply.status
                usage = Usage(
                    requests=1,
                    input_tokens=reply.input_tokens,
                    output_tokens=reply.output_tokens,
                    total_tokens=reply.input_tokens + reply.output_tokens,
                    input_tokens_details={
                        "cached_tokens": 0,
                        "cache_write_tokens": 0,
                        **(reply.input_details or {}),
                    },
                    output_tokens_details={
                        "reasoning_tokens": 0,
                        **(reply.output_details or {}),
                    },
                )
                with generation_span(
                    model=model_ref, disabled=tracing.is_disabled()
                ) as span:
                    span.span_data.usage = {
                        "input_tokens": usage.input_tokens,
                        "output_tokens": usage.output_tokens,
                        "total_tokens": usage.total_tokens,
                    }
                    item = (
                        function_call(
                            function_name, content, call_id="structured-fixture"
                        )
                        if function_name
                        else assistant_message(content)
                    )
                    return ModelResponse(
                        output=[item], usage=usage, response_id="fixture"
                    )

        yield Inference()

    patch.setattr(generation, "sdk_model", factory)


def install_sequence(patch, *replies):
    """Queue one reply per request and return the list of observed requests."""
    queued = deque(replies)
    requests = []

    def respond(request):
        requests.append(request)
        assert queued, "Unexpected provider request"
        reply = queued.popleft()
        if isinstance(reply, BaseException):
            raise reply
        return reply

    install_inference(patch, respond)
    return requests
