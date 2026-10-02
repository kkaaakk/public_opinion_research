"""LangChain official callback consumer; it never invokes a model or creates a span."""

from __future__ import annotations

import asyncio
import re
import threading
from functools import wraps

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.messages import BaseMessage

from open_deep_research.trajectory.lifecycle import RuntimeLifecycle
from open_deep_research.trajectory.sanitization import bounded, error_data, sanitize
from open_deep_research.trajectory.session import TrajectorySession


def fail_open(method):
    """Contain recorder faults without swallowing the workflow's own exceptions."""
    @wraps(method)
    async def guarded(self, *args, **kwargs):
        try:
            with self._lock:
                if not self.session.finished:
                    return await method(self, *args, **kwargs)
        except Exception:
            self.session.mark_degraded(method.__name__)
    return guarded


def _header(serialized: dict, metadata: dict, kwargs: dict) -> dict:
    params = kwargs.get("invocation_params") or {}
    options = kwargs.get("options") or {}
    config = {}
    for key in ("model", "model_name", "temperature", "top_p", "max_tokens", "stop", "reasoning_effort", "response_format"):
        value = params.get(key, options.get(key))
        if value is not None:
            config["model" if key == "model_name" else key] = value
    if "model" not in config and metadata.get("ls_model_name"):
        config["model"] = metadata["ls_model_name"]
    provider = metadata.get("ls_provider") or metadata.get("provider")
    if provider and provider != "langsmith":
        config["provider"] = provider
    tools = []
    for item in params.get("tools", []):
        tool = item.get("function", item)
        if tool.get("name"):
            tools.append({"name": tool["name"], "description": tool.get("description", ""),
                          "parameters": bounded(tool.get("parameters", tool.get("input_schema", {})), 262_144)})
    return sanitize({"config": config, **({"tools": tools} if tools else {})})


class TrajectorySessionRecorder(AsyncCallbackHandler):
    """Collect native request/error/tool/chain lifecycles in the same RunnableConfig."""

    run_inline = True
    raise_error = False

    def __init__(self, session: TrajectorySession, *, stream_limit: int = 4 * 1024 * 1024):
        """Attach an isolated collector to one logical user interaction."""
        self.session = session
        self._lock = threading.RLock()
        self.lifecycle = RuntimeLifecycle(session, stream_limit=stream_limit)

    @fail_open
    async def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None, metadata=None, **kwargs):
        """Capture hierarchy and real agent owners, omitting chain inputs/state."""
        self.lifecycle.chain_start(str(run_id), str(parent_run_id) if parent_run_id else None,
                                   kwargs.get("name") or (serialized or {}).get("name", ""), metadata or {})

    @fail_open
    async def on_chain_end(self, outputs, *, run_id, **kwargs):
        """Close agent context without persisting LangGraph state."""
        self.lifecycle.chain_end(str(run_id))

    @fail_open
    async def on_chain_error(self, error, *, run_id, **kwargs):
        """Preserve real agent failures; workflow errors belong to the root boundary."""
        self.lifecycle.chain_end(str(run_id), error)

    @fail_open
    async def on_retriever_start(self, serialized, query, *, run_id, parent_run_id=None, metadata=None, **kwargs):
        """Retain ancestry through retrievers without duplicating their query/span data."""
        self.lifecycle.context(str(run_id), str(parent_run_id) if parent_run_id else None, metadata or {})

    @fail_open
    async def on_chat_model_start(self, serialized: dict, messages: list[list[BaseMessage]], *, run_id,
                                  parent_run_id=None, metadata=None, **kwargs):
        """Read invocation parameters and messages from the official model callback."""
        self._runtime_retry(run_id, parent_run_id, kwargs.get("tags", []))
        self.lifecycle.model_start(str(run_id), str(parent_run_id) if parent_run_id else None,
            messages[0] if messages else [], _header(serialized, metadata or {}, kwargs), metadata or {})

    @fail_open
    async def on_llm_start(self, serialized, prompts, *, run_id, parent_run_id=None, metadata=None, **kwargs):
        """Support native text LLM requests without duplicating user history."""
        self._runtime_retry(run_id, parent_run_id, kwargs.get("tags", []))
        self.lifecycle.model_start(str(run_id), str(parent_run_id) if parent_run_id else None,
            [], _header(serialized, metadata or {}, kwargs), metadata or {})

    def _runtime_retry(self, run_id, parent_run_id, tags):
        for tag in tags or []:
            match = re.fullmatch(r"retry:attempt:(\d+)", tag)
            if match:
                self.session.append("retry/started", {"turn": self.session.turn,
                    "retry_ordinal": int(match[1]), "evidence": "LangChain RunnableRetry tag"},
                    **self.lifecycle.context(str(run_id), str(parent_run_id) if parent_run_id else None))

    @fail_open
    async def on_llm_new_token(self, token, *, run_id, chunk=None, **kwargs):
        """Buffer real chunks, including tool argument and reasoning deltas."""
        message = getattr(chunk, "message", chunk)
        payload = {"content": getattr(message, "content", token)}
        for key in ("tool_call_chunks", "usage_metadata", "response_metadata"):
            value = getattr(message, key, None)
            if value:
                payload[key] = value
        reasoning = (getattr(message, "additional_kwargs", {}) or {}).get("reasoning_content")
        if reasoning:
            payload["reasoning_content"] = reasoning
        self.lifecycle.model_chunk(str(run_id), payload)

    @fail_open
    async def on_llm_end(self, response, *, run_id, **kwargs):
        """Settle final provider messages and usage, never estimate absent values."""
        generation = response.generations[0][0] if response.generations and response.generations[0] else None
        message = getattr(generation, "message", None)
        if message is None and generation is not None:
            from langchain_core.messages import AIMessage
            message = AIMessage(content=generation.text)
        self.lifecycle.model_end(str(run_id), message)

    @fail_open
    async def on_llm_error(self, error, *, run_id, **kwargs):
        """Retain every failed/cancelled Attempt even when no first token arrived."""
        partial = kwargs.get("response")
        generations = getattr(partial, "generations", None)
        message = getattr(generations[0][0], "message", None) if generations and generations[0] else None
        self.lifecycle.model_end(str(run_id), message=message, error=error,
            status="cancelled" if isinstance(error, asyncio.CancelledError) else "model_error")

    @fail_open
    async def on_tool_start(self, serialized, input_str, *, run_id, parent_run_id=None,
                            metadata=None, inputs=None, **kwargs):
        """Read actual arguments, native call ids and parent runs automatically."""
        self.lifecycle.tool_start(str(run_id), str(parent_run_id) if parent_run_id else None,
            (serialized or {}).get("name", "unknown"), inputs if inputs is not None else input_str,
            metadata or {}, kwargs.get("tool_call_id"))

    @fail_open
    async def on_tool_end(self, output, *, run_id, **kwargs):
        """Pair successful or tool-reported error results."""
        self.lifecycle.tool_end(str(run_id), output)

    @fail_open
    async def on_tool_error(self, error, *, run_id, **kwargs):
        """Keep tool error semantics distinct from model/workflow failure."""
        self.lifecycle.tool_end(str(run_id), error=error,
            status="cancelled" if isinstance(error, asyncio.CancelledError) else "tool_error")

    @fail_open
    async def on_retry(self, retry_state, *, run_id, parent_run_id=None, **kwargs):
        """Record a retry only when the official runtime explicitly reports it."""
        self.session.append("retry/scheduled", {"turn": self.session.turn,
            "attempt": retry_state.attempt_number, "sleep": getattr(retry_state.next_action, "sleep", None)},
            **self.lifecycle.context(str(run_id), str(parent_run_id) if parent_run_id else None))

    def finish(self, status: str, *, error: BaseException | None = None, budget_usage: dict | None = None):
        """Settle the centralized workflow boundary without invoking callback methods manually."""
        with self._lock:
            self._finish(status, error=error, budget_usage=budget_usage)

    def _finish(self, status: str, *, error: BaseException | None = None, budget_usage: dict | None = None):
        if self.session.finished:
            return
        try:
            self.lifecycle.finish(status)
            if error is not None:
                self.session.append("workflow/error", {"turn": self.session.turn, "error": error_data(error, "workflow_error")})
        except Exception:
            self.session.mark_degraded("settlement")
        finally:
            self.session.finish(status, budget_usage)
