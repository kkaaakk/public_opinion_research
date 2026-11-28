"""Offline observations of native Agents SDK tool batches and agent-as-tool calls."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from importlib.metadata import version
from pathlib import Path
from typing import Any

from agents import (
    Agent,
    FunctionTool,
    Model,
    ModelResponse,
    RunConfig,
    RunHooks,
    Runner,
    RunResult,
    Usage,
    function_tool,
    set_tracing_disabled,
)
from agents.exceptions import ModelBehaviorError, UserError
from agents.tool import default_tool_error_function
from agents.tool_context import ToolContext
from dbos import DBOS
from openai.types.responses import (
    Response,
    ResponseCompletedEvent,
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
    ResponseUsage,
)
from pytest_socket import socket_allow_hosts

LOCKED = {"openai-agents": "0.24.0", "dbos": "3.3.0", "dbos-openai-agents": "0.3.0"}
WAIT_SECONDS = 10


def configure_offline() -> None:
    actual = {name: version(name) for name in LOCKED}
    if actual != LOCKED:
        raise RuntimeError(f"Expected {LOCKED}, found {actual}")
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    os.environ["OPENAI_AGENTS_DISABLE_TRACING"] = "1"
    set_tracing_disabled(True)
    socket_allow_hosts(["127.0.0.1", "::1"], allow_unix_socket=True)


class OrdinaryFailure(RuntimeError):
    pass


class BudgetStop(RuntimeError):
    """Stand-in for a workflow execution-control error that must escape the loop."""


@dataclass(frozen=True)
class Call:
    tool: str
    call_id: str
    arguments: dict[str, Any]

    def item(self) -> ResponseFunctionToolCall:
        return ResponseFunctionToolCall(
            id=f"fc_{self.call_id}",
            call_id=self.call_id,
            name=self.tool,
            arguments=json.dumps(self.arguments),
            type="function_call",
            status="completed",
        )


def agent_call(role: str, label: str, call_id: str | None = None) -> Call:
    return Call(f"{role}_agent", call_id or f"call-{label}", {"input": label})


def business_call(tool: str, label: str, call_id: str | None = None) -> Call:
    return Call(f"{tool}_tool", call_id or label, {"slot": label})


class Probe:
    def __init__(self, name: str, trace: Path | None = None):
        self.name = name
        self.trace = trace
        self.events: list[dict[str, Any]] = []
        self.signals: dict[tuple[str, str], asyncio.Event] = defaultdict(asyncio.Event)
        self.gates: dict[str, asyncio.Event] = defaultdict(asyncio.Event)
        self.tasks: dict[str, dict[str, asyncio.Task]] = {
            family: {} for family in ("agent", "tool", "remote")
        }
        self.program: dict[str, list[list[Call]]] = {}
        self.failures: dict[str, Callable[[str], Exception]] = {}
        self.held_tools: set[str] = set()
        self.held_tool_cleanup: set[str] = set()
        self.held_wrapper_cleanup: set[str] = set()
        self.held_tool_end_hooks: set[str] = set()
        self.remote_tools: set[str] = set()
        self.contexts: dict[str, Any] = {}
        self.results: dict[str, RunResult] = {}
        self.baseline = asyncio.all_tasks()

    def emit(self, kind: str, name: str, **details: Any) -> None:
        event = dict(
            case=self.name,
            pid=os.getpid(),
            seq=len(self.events) + 1,
            kind=kind,
            name=name,
            **details,
        )
        self.events.append(event)
        if self.trace is not None:
            with self.trace.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        self.signals[kind, name].set()

    async def wait(self, kind: str, name: str) -> None:
        try:
            await asyncio.wait_for(self.signals[kind, name].wait(), WAIT_SECONDS)
        except TimeoutError as error:
            detail = [(item["kind"], item["name"]) for item in self.events]
            raise AssertionError(f"Missing {kind} {name}. Observed {detail}") from error

    def seen(self, kind: str, name: str) -> bool:
        return self.signals[kind, name].is_set()

    def entries(self, kind: str, name: str) -> list[dict[str, Any]]:
        return [
            event
            for event in self.events
            if event["kind"] == kind and event["name"] == name
        ]

    def release(self, gate: str) -> None:
        self.gates[gate].set()

    def pending(self) -> dict[str, list[str]]:
        exits = {
            "tool": "tool-exit",
            "agent": "agent-tool-exit",
            "remote": "remote-return",
        }
        return {
            family: sorted(
                name
                for name, task in tasks.items()
                if not task.done() and not self.seen(exits[family], name)
            )
            for family, tasks in self.tasks.items()
        }

    def snapshot(self, name: str) -> dict[str, list[str]]:
        pending = self.pending()
        self.emit(
            "snapshot", name, pending=pending, extra_tasks=len(self.extra_tasks())
        )
        return pending

    def extra_tasks(self) -> set[asyncio.Task]:
        return asyncio.all_tasks() - self.baseline - {asyncio.current_task()}

    async def finish(self) -> None:
        for gate in self.gates.values():
            gate.set()
        if tasks := self.extra_tasks():
            _, pending = await asyncio.wait(tasks, timeout=WAIT_SECONDS)
            assert not pending, [task.get_coro().__qualname__ for task in pending]
        assert not any(self.snapshot("quiescent").values())

    async def business(self, context: ToolContext, slot: str) -> str:
        self.tasks["tool"][slot] = asyncio.current_task()
        if isinstance(context.context, ChildBusiness):
            context.context.calls[context.tool_call_id] = slot
        self.emit("tool-start", slot, native_id=context.tool_call_id, step=DBOS.step_id)
        try:
            if slot in self.remote_tools:
                receipt = f"simulated-{slot}"
                asyncio.create_task(self.remote(receipt))
                await self.wait("remote-start", receipt)
            if slot in self.held_tools:
                await self.gates[slot].wait()
            if make_error := self.failures.get(slot):
                error = make_error(slot)
                self.emit("tool-exception", slot, error=type(error).__name__)
                raise error
            self.emit("tool-return", slot)
            return f"returned:{slot}"
        except asyncio.CancelledError:
            self.emit("tool-cancel", slot)
            raise
        finally:
            self.emit("tool-finally", slot)
            if slot in self.held_tool_cleanup:
                await self.gates[f"cleanup:{slot}"].wait()
            self.emit("tool-exit", slot)

    async def remote(self, receipt: str) -> None:
        self.tasks["remote"][receipt] = asyncio.current_task()
        self.emit("remote-start", receipt)
        await self.gates[receipt].wait()
        self.emit("remote-return", receipt)


@dataclass
class ChildBusiness:
    probe: Probe
    calls: dict[str, str] = field(default_factory=dict)


class ScriptedModel(Model):
    def __init__(self, probe: Probe):
        self.probe = probe

    async def get_response(
        self, system_instructions, input, *args, **kwargs
    ) -> ModelResponse:
        if isinstance(input, str):
            label, outputs = input, []
        else:
            content = next(
                item["content"] for item in input if item.get("role") == "user"
            )
            label = content if isinstance(content, str) else content[0]["text"]
            outputs = [
                item for item in input if item.get("type") == "function_call_output"
            ]
        batches = self.probe.program[label]
        completed = {item["call_id"] for item in outputs}
        turn = sum(
            all(call.call_id in completed for call in batch) for batch in batches
        )
        self.probe.emit(
            "model", label, turn=turn + 1, outputs=outputs, step=DBOS.step_id
        )
        if turn < len(batches):
            items = [call.item() for call in batches[turn]]
        else:
            items = [
                ResponseOutputMessage(
                    id=f"msg_{label}",
                    type="message",
                    role="assistant",
                    status="completed",
                    content=[
                        ResponseOutputText(
                            type="output_text", text=f"done:{label}", annotations=[]
                        )
                    ],
                )
            ]
        return ModelResponse(
            items,
            Usage(requests=1, input_tokens=2, output_tokens=1, total_tokens=3),
            None,
        )

    async def stream_response(self, *args, **kwargs):
        reply = await self.get_response(*args, **kwargs)
        yield ResponseCompletedEvent(
            type="response.completed",
            sequence_number=0,
            response=Response(
                id="resp_lab",
                object="response",
                status="completed",
                output=reply.output,
                created_at=0,
                model="offline-lab",
                parallel_tool_calls=True,
                tool_choice="auto",
                tools=[],
                usage=ResponseUsage(
                    input_tokens=2,
                    output_tokens=1,
                    total_tokens=3,
                    input_tokens_details={"cached_tokens": 0, "cache_write_tokens": 0},
                    output_tokens_details={"reasoning_tokens": 0},
                ),
            ),
        )


class Hooks(RunHooks):
    def __init__(self, probe: Probe):
        self.probe = probe

    async def on_llm_start(self, context, agent, system_prompt, input_items):
        call_id = getattr(context, "tool_call_id", "manager")
        self.probe.contexts[call_id] = context
        self.probe.emit(
            "llm-start",
            call_id,
            agent=agent.name,
            structured_input=context.tool_input,
            step=DBOS.step_id,
        )

    async def on_agent_start(self, context, agent):
        self.probe.emit("agent-hook-start", agent.name)

    async def on_agent_end(self, context, agent, output):
        self.probe.emit("agent-hook-end", agent.name, output=output)

    async def on_tool_start(self, context, agent, tool):
        self.probe.emit("tool-hook-start", context.tool_call_id, agent=agent.name)

    async def on_tool_end(self, context, agent, tool, result):
        if context.tool_call_id in self.probe.held_tool_end_hooks:
            self.probe.emit("tool-hook-end-enter", context.tool_call_id)
            await self.probe.gates[f"hook-end:{context.tool_call_id}"].wait()
        self.probe.emit(
            "tool-hook-end",
            context.tool_call_id,
            agent=agent.name,
            result=result,
            parent_context=context.context is self.probe,
        )


def run_config(**options) -> RunConfig:
    return RunConfig(
        tracing_disabled=True, trace_include_sensitive_data=False, **options
    )


def observe_invocation(
    tool: FunctionTool, probe: Probe, *, isolate: bool = False
) -> FunctionTool:
    original = tool.on_invoke_tool

    async def invoke(context: ToolContext, arguments: str):
        call_id = context.tool_call_id
        shared_business = context.context
        probe.tasks["agent"][call_id] = asyncio.current_task()
        probe.emit("agent-tool-start", call_id, step=DBOS.step_id)
        if isolate:
            context.context = ChildBusiness(probe)
        try:
            output = await original(context, arguments)
            probe.emit("agent-tool-return", call_id, output=output)
            return output
        except BaseException as error:
            probe.emit("agent-tool-error", call_id, error=type(error).__name__)
            raise
        finally:
            context.context = shared_business
            probe.emit("agent-tool-finally", call_id)
            try:
                if call_id in probe.held_wrapper_cleanup:
                    await probe.gates[f"cleanup:{call_id}"].wait()
            except asyncio.CancelledError:
                probe.emit("agent-tool-cleanup-cancel", call_id)
                raise
            finally:
                probe.emit("agent-tool-exit", call_id)

    return replace(tool, on_invoke_tool=invoke)


def build_manager(
    probe: Probe,
    *,
    business_error=None,
    agent_error=None,
    wrap=True,
    isolate=False,
    child_stream=False,
    child_config=None,
    child_hooks=True,
    agent_options=None,
) -> Agent:
    hooks = Hooks(probe)

    def business_tool(name, error_handler):
        async def perform(context: ToolContext, slot: str) -> str:
            """Run one controlled operation."""
            return await probe.business(context, slot)

        error_options = (
            {}
            if error_handler is default_tool_error_function
            else {
                "failure_error_function": error_handler,
            }
        )
        return function_tool(perform, name_override=f"{name}_tool", **error_options)

    async def extract(result):
        invocation = result.agent_tool_invocation
        assert invocation is not None
        probe.results[invocation.tool_call_id] = result
        probe.emit(
            "extract", invocation.tool_call_id, arguments=invocation.tool_arguments
        )
        return result.final_output

    async def on_stream(payload):
        probe.emit(
            "child-stream-event",
            payload["tool_call"].call_id,
            event_type=payload["event"].type,
        )

    public = Agent(
        name="Public",
        model=ScriptedModel(probe),
        tools=[
            business_tool("slow", None),
            business_tool("failure", business_error),
            business_tool("normal", None),
        ],
    )
    knowledge = Agent(
        name="Knowledge",
        model=ScriptedModel(probe),
        tools=[
            business_tool("slow", None),
        ],
    )
    tools = [
        child.as_tool(
            f"{child.name.lower()}_agent",
            f"Run {child.name}",
            hooks=hooks if child_hooks else None,
            failure_error_function=agent_error,
            run_config=child_config,
            custom_output_extractor=extract,
            on_stream=on_stream if child_stream else None,
            **(agent_options or {}),
        )
        for child in (public, knowledge)
    ]
    if wrap:
        tools = [observe_invocation(tool, probe, isolate=isolate) for tool in tools]
    return Agent(name="Manager", model=ScriptedModel(probe), tools=tools)


def launch(probe: Probe, *, config=None, **options) -> asyncio.Task:
    return asyncio.create_task(
        Runner.run(
            build_manager(probe, **options),
            "manager",
            context=probe,
            hooks=Hooks(probe),
            run_config=config or run_config(),
        )
    )


async def observe_exit(probe, task, error_type=None):
    done, _ = await asyncio.wait({task}, timeout=WAIT_SECONDS)
    assert task in done, f"Runner did not exit. Live {probe.pending()}"
    probe.snapshot("runner-exit")
    if error_type is None:
        result = task.result()
        probe.emit(
            "runner-return", "manager", result=getattr(result, "final_output", result)
        )
        return result
    try:
        task.result()
    except error_type as error:
        probe.emit(
            "runner-error",
            "manager",
            error=type(error).__name__,
            cause=type(error.__cause__).__name__ if error.__cause__ else None,
        )
        return error
    raise AssertionError(f"Expected {error_type}")


def setup_public(probe, calls):
    probe.program = {"manager": [[agent_call("public", "public")]], "public": [calls]}


def setup_pair(probe, *, failure=False):
    probe.program = {
        "manager": [
            [agent_call("public", "public"), agent_call("knowledge", "knowledge")]
        ],
        "public": [[business_call("failure" if failure else "slow", "public-tool")]],
        "knowledge": [[business_call("slow", "knowledge-tool")]],
    }
    probe.held_tools.update(("public-tool", "knowledge-tool"))
    if failure:
        probe.failures["public-tool"] = OrdinaryFailure


async def parallel(probe):
    setup_public(
        probe, [business_call("slow", "slow"), business_call("normal", "normal")]
    )
    probe.held_tools.update(("slow", "normal"))
    task = launch(probe)
    await probe.wait("tool-start", "slow")
    await probe.wait("tool-start", "normal")
    assert probe.snapshot("overlap")["tool"] == ["normal", "slow"]
    probe.release("normal")
    await probe.wait("tool-hook-end", "normal")
    assert not task.done() and not probe.seen("agent-hook-end", "Public")
    probe.release("slow")
    assert (await observe_exit(probe, task)).final_output == "done:manager"
    assert not any(probe.pending().values())
    assert probe.seen("agent-hook-end", "Public") and probe.seen(
        "tool-hook-end", "slow"
    )


async def business_failure(probe, *, default=False, control=False, hold_cleanup=False):
    setup_public(
        probe, [business_call("failure", "failure"), business_call("slow", "slow")]
    )
    failure_type = BudgetStop if control else OrdinaryFailure
    probe.failures["failure"] = failure_type
    probe.held_tools.update(("failure", "slow"))
    if control:
        probe.program["public"].append([business_call("normal", "after-error")])
    if hold_cleanup:
        probe.held_tool_cleanup.add("slow")
    task = launch(
        probe, business_error=default_tool_error_function if default else None
    )
    await probe.wait("tool-start", "failure")
    await probe.wait("tool-start", "slow")
    probe.release("failure")
    if default:
        await probe.wait("tool-hook-end", "failure")
        assert probe.pending()["tool"] == ["slow"] and not task.done()
        probe.release("slow")
        await observe_exit(probe, task)
        outputs = probe.entries("model", "public")[1]["outputs"]
        message = next(
            item["output"] for item in outputs if item["call_id"] == "failure"
        )
        assert "An error occurred while running the tool" in message
        assert probe.seen("agent-hook-end", "Public")
        assert probe.seen("tool-return", "after-error") == control
    else:
        error = await observe_exit(probe, task, UserError)
        assert isinstance(error.__cause__, failure_type)
        await probe.wait("tool-cancel", "slow")
        assert not probe.seen("tool-hook-end", "failure")
        assert not probe.seen("tool-hook-end", "slow")
        assert not probe.seen("agent-hook-end", "Public")
        assert not probe.seen("tool-start", "after-error")
        assert ("slow" in probe.pending()["tool"]) == hold_cleanup
        probe.release("cleanup:slow")
        await probe.wait("tool-exit", "slow")


async def child_cancel(probe):
    setup_public(probe, [business_call("slow", "one"), business_call("slow", "two")])
    probe.held_tools.update(("one", "two"))
    probe.held_tool_cleanup.update(("one", "two"))
    task = launch(probe)
    await probe.wait("tool-start", "one")
    await probe.wait("tool-start", "two")
    probe.tasks["agent"]["call-public"].cancel()
    await observe_exit(probe, task, asyncio.CancelledError)
    for name in ("one", "two"):
        await probe.wait("tool-finally", name)
        assert not probe.seen("tool-hook-end", name)
    assert probe.pending()["tool"] == ["one", "two"]
    assert not probe.seen("agent-hook-end", "Public")


async def post_invoke_hook(probe):
    setup_public(
        probe, [business_call("failure", "failure"), business_call("normal", "normal")]
    )
    probe.held_tools.add("failure")
    probe.held_tool_end_hooks.add("normal")
    probe.failures["failure"] = OrdinaryFailure
    task = launch(probe)
    await probe.wait("tool-start", "failure")
    await probe.wait("tool-hook-end-enter", "normal")
    probe.release("failure")
    await observe_exit(probe, task, UserError)
    assert probe.pending()["tool"] == [] and not probe.seen("tool-hook-end", "normal")
    assert not probe.tasks["tool"]["normal"].done()
    probe.release("hook-end:normal")
    await probe.wait("tool-hook-end", "normal")


async def manager_failure(probe, *, default=False, child_stream=False):
    setup_pair(probe, failure=True)
    if not default:
        probe.held_tool_cleanup.add("knowledge-tool")
    task = launch(
        probe,
        agent_error=default_tool_error_function if default else None,
        child_stream=child_stream,
    )
    await probe.wait("tool-start", "knowledge-tool")
    await probe.wait("tool-start", "public-tool")
    probe.release("public-tool")
    if default:
        await probe.wait("tool-hook-end", "call-public")
        error_output = probe.entries("tool-hook-end", "call-public")[0]["result"]
        assert "An error occurred while running the tool" in error_output
        assert probe.pending()["tool"] == ["knowledge-tool"] and not task.done()
        probe.release("knowledge-tool")
        await observe_exit(probe, task)
    else:
        error = await observe_exit(probe, task, UserError)
        assert isinstance(error.__cause__, OrdinaryFailure)
        await probe.wait("tool-finally", "knowledge-tool")
        assert probe.pending()["tool"] == ["knowledge-tool"]
        assert not probe.seen("tool-hook-end", "call-public")
        assert not probe.seen("agent-hook-end", "Manager")
    assert not probe.seen("agent-hook-end", "Public")
    if child_stream:
        assert probe.seen("child-stream-event", "call-public")


async def manager_cancel(probe, *, child_stream=False):
    setup_pair(probe)
    probe.held_tool_cleanup.update(("public-tool", "knowledge-tool"))
    task = launch(probe, child_stream=child_stream)
    await probe.wait("tool-start", "public-tool")
    await probe.wait("tool-start", "knowledge-tool")
    task.cancel()
    await observe_exit(probe, task, asyncio.CancelledError)
    for name in ("public", "knowledge"):
        await probe.wait("agent-tool-finally", f"call-{name}")
        await probe.wait("tool-finally", f"{name}-tool")
        assert not probe.seen("agent-hook-end", name.title())
    assert probe.pending()["tool"] == ["knowledge-tool", "public-tool"]
    assert not probe.seen("agent-hook-end", "Manager")


async def duplicate_agent(
    probe, *, cancel=False, fail=False, isolate=False, contain=True
):
    probe.program = {
        "manager": [[agent_call("public", "left"), agent_call("public", "right")]],
        "left": [[business_call("failure" if fail else "slow", "left-tool", "inner")]],
        "right": [[business_call("slow", "right-tool", "inner")]],
    }
    probe.held_tools.update(("left-tool", "right-tool"))
    if fail:
        probe.failures["left-tool"] = OrdinaryFailure
    task = launch(
        probe,
        isolate=isolate,
        agent_error=default_tool_error_function if contain else None,
    )
    await probe.wait("tool-start", "left-tool")
    await probe.wait("tool-start", "right-tool")
    manager, left, right = (
        probe.contexts[key] for key in ("manager", "call-left", "call-right")
    )
    assert left is not right and left is not manager and right is not manager
    assert left.usage is right.usage is manager.usage
    assert left.run_config is right.run_config
    if isolate:
        assert left.context is not right.context and manager.context is probe
        assert left.context.calls == {"inner": "left-tool"}
        assert right.context.calls == {"inner": "right-tool"}
    else:
        assert manager.context is left.context is right.context is probe
    probe.emit(
        "identity",
        "parallel-public",
        same_usage=True,
        shared_business=not isolate,
        outer_ids=[left.tool_call_id, right.tool_call_id],
        inner_ids=["inner", "inner"],
    )
    if cancel:
        probe.tasks["agent"]["call-left"].cancel()
    else:
        probe.release("left-tool")
    if not contain:
        await observe_exit(probe, task, asyncio.CancelledError if cancel else UserError)
        await probe.wait("tool-cancel", "right-tool")
        assert not probe.seen("tool-return", "right-tool")
        assert not probe.seen("agent-hook-end", "Manager")
        return
    await probe.wait("tool-hook-end", "call-left")
    assert probe.pending()["tool"] == ["right-tool"] and not task.done()
    probe.release("right-tool")
    result = await observe_exit(probe, task)
    assert len(probe.results["call-right"].raw_responses) == 2
    if not (cancel or fail):
        left_result = probe.results["call-left"]
        assert len(left_result.raw_responses) == 2
        assert (
            left_result.raw_responses is not probe.results["call-right"].raw_responses
        )
        assert result.context_wrapper.usage.requests == 6
        assert probe.entries("tool-hook-end", "call-left")[0]["parent_context"]
    else:
        assert (
            "call-left" not in probe.results
            and result.context_wrapper.usage.requests == 5
        )


async def remote_survives(probe):
    setup_public(probe, [business_call("slow", "remote")])
    probe.held_tools.add("remote")
    probe.remote_tools.add("remote")
    task = launch(probe)
    await probe.wait("remote-start", "simulated-remote")
    task.cancel()
    await observe_exit(probe, task, asyncio.CancelledError)
    await probe.wait("tool-exit", "remote")
    assert probe.pending()["tool"] == []
    assert probe.snapshot("after-local-exit")["remote"] == ["simulated-remote"]
    probe.release("simulated-remote")
    await probe.wait("remote-return", "simulated-remote")


@dataclass
class TypedInput:
    task: str
    edition: int


async def wrapper_parity(probe, *, wrap, invalid=False, inherit=False):
    probe.program = {
        "manager": [
            [
                Call(
                    "public_agent",
                    "typed",
                    {"task": "public", "edition": "bad" if invalid else 7},
                )
            ]
        ],
        "public-7": [[business_call("normal", "normal")]],
    }

    def builder(options):
        return f"{options['params']['task']}-{options['params']['edition']}"

    def config_filter(data):
        probe.emit("config-filter", data.agent.name)
        return data.model_data

    filtered = run_config(call_model_input_filter=config_filter)
    task = launch(
        probe,
        wrap=wrap,
        config=filtered if inherit else None,
        child_config=None if inherit else filtered,
        child_hooks=not inherit,
        agent_options=dict(parameters=TypedInput, input_builder=builder),
    )
    if invalid:
        await observe_exit(probe, task, ModelBehaviorError)
        assert not probe.contexts.get("typed")
        assert probe.seen("agent-tool-finally", "typed") == wrap
        return
    result = await observe_exit(probe, task)
    nested = probe.results["typed"]
    assert nested.final_output == "done:public-7"
    assert nested.context_wrapper.context is result.context_wrapper.context is probe
    assert nested.context_wrapper.usage is result.context_wrapper.usage
    assert result.context_wrapper.usage.requests == 4
    assert nested.agent_tool_invocation.tool_call_id == "typed"
    assert nested.context_wrapper.tool_input == {"task": "public", "edition": 7}
    assert len(probe.entries("config-filter", "Public")) == 2
    assert probe.seen("config-filter", "Manager") == inherit
    assert probe.seen("agent-hook-end", "Public") != inherit
    assert probe.seen("tool-hook-end", "typed")
    assert probe.seen("agent-tool-finally", "typed") == wrap


async def wrapper_second_cancel(probe):
    setup_public(probe, [business_call("slow", "slow")])
    probe.held_tools.add("slow")
    probe.held_wrapper_cleanup.add("call-public")
    task = launch(probe)
    await probe.wait("tool-start", "slow")
    nested = probe.tasks["agent"]["call-public"]
    nested.cancel()
    await probe.wait("agent-tool-finally", "call-public")
    assert not nested.done() and not probe.seen("agent-tool-exit", "call-public")
    nested.cancel()
    await observe_exit(probe, task, asyncio.CancelledError)
    assert probe.seen("agent-tool-cleanup-cancel", "call-public")


async def streaming_manager(probe, *, mode):
    setup_pair(probe)
    if mode == "immediate":
        probe.held_tool_cleanup.update(("public-tool", "knowledge-tool"))
    if mode == "failure":
        probe.failures["public-tool"] = OrdinaryFailure
        probe.held_tool_cleanup.add("knowledge-tool")
    result = Runner.run_streamed(
        build_manager(probe),
        "manager",
        context=probe,
        hooks=Hooks(probe),
        run_config=run_config(),
    )

    async def consume():
        async for _ in result.stream_events():
            pass
        return result

    task = asyncio.create_task(consume())
    await probe.wait("tool-start", "public-tool")
    await probe.wait("tool-start", "knowledge-tool")
    if mode == "failure":
        probe.release("public-tool")
        error = await observe_exit(probe, task, UserError)
        assert isinstance(error.__cause__, OrdinaryFailure)
        await probe.wait("tool-finally", "knowledge-tool")
        assert probe.pending()["tool"] == ["knowledge-tool"]
    else:
        result.cancel(mode=mode)
        if mode == "after_turn":
            assert not task.done() and len(probe.pending()["tool"]) == 2
            probe.release("public-tool")
            probe.release("knowledge-tool")
        await observe_exit(probe, task)
        if mode == "immediate":
            await probe.wait("tool-finally", "public-tool")
            await probe.wait("tool-finally", "knowledge-tool")
            assert probe.pending()["tool"] == ["knowledge-tool", "public-tool"]
        else:
            assert not any(probe.pending().values())
            assert len(probe.entries("model", "manager")) == 1
            assert probe.seen("tool-return", "public-tool")
            assert probe.seen("tool-return", "knowledge-tool")


CASES = {
    "01-normal-parallel": parallel,
    "02-ordinary-default": lambda p: business_failure(p, default=True),
    "02-ordinary-propagated": business_failure,
    "02-bounded-failure-drain": lambda p: business_failure(p, hold_cleanup=True),
    "02-bounded-post-invoke-hook": post_invoke_hook,
    "03-control-default": lambda p: business_failure(p, default=True, control=True),
    "03-control-propagated": lambda p: business_failure(p, control=True),
    "04-child-cancel": child_cancel,
    "05-manager-failure-default": lambda p: manager_failure(p, default=True),
    "05-manager-failure-propagated": manager_failure,
    "05-streamed-child-failure": lambda p: manager_failure(p, child_stream=True),
    "06-manager-cancel": manager_cancel,
    "06-streamed-child-cancel": lambda p: manager_cancel(p, child_stream=True),
    "07-duplicate-shared": duplicate_agent,
    "07-duplicate-contained-error": lambda p: duplicate_agent(p, fail=True),
    "07-duplicate-contained-cancel": lambda p: duplicate_agent(p, cancel=True),
    "07-duplicate-propagated-error": lambda p: duplicate_agent(
        p, fail=True, contain=False
    ),
    "07-duplicate-propagated-cancel": lambda p: duplicate_agent(
        p, cancel=True, contain=False
    ),
    "07-duplicate-isolated": lambda p: duplicate_agent(p, isolate=True),
    "08-remote-survives": remote_survives,
    "09-unwrapped-valid": lambda p: wrapper_parity(p, wrap=False),
    "09-wrapped-valid": lambda p: wrapper_parity(p, wrap=True),
    "09-unwrapped-invalid": lambda p: wrapper_parity(p, wrap=False, invalid=True),
    "09-wrapped-invalid": lambda p: wrapper_parity(p, wrap=True, invalid=True),
    "09-inherited-config-separate-hooks": lambda p: wrapper_parity(
        p, wrap=True, inherit=True
    ),
    "09-wrapper-second-cancel": wrapper_second_cancel,
    "stream-manager-failure": lambda p: streaming_manager(p, mode="failure"),
    "stream-manager-immediate": lambda p: streaming_manager(p, mode="immediate"),
    "stream-manager-after-turn": lambda p: streaming_manager(p, mode="after_turn"),
}


async def main(trace: Path | None, prefixes: list[str]) -> None:
    selected = {
        name: case
        for name, case in CASES.items()
        if not prefixes or any(name.startswith(prefix) for prefix in prefixes)
    }
    if not selected:
        raise ValueError(f"No cases matching {prefixes}")
    for name, case in selected.items():
        probe = Probe(name, trace)
        try:
            await case(probe)
        finally:
            await probe.finish()
        print(f"PASS {name}", flush=True)
    print(f"{len(selected)} SDK observations passed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", type=Path, help="append a JSON line per observation")
    parser.add_argument("prefix", nargs="*", help="optional case prefixes")
    command = parser.parse_args()
    configure_offline()
    asyncio.run(main(command.trace, command.prefix))
