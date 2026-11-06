"""Public SDK extension points for DBOS clone binding and physical usage.

No SDK internals are modified and the official DBOSRunner owns the loop and
model checkpoint. The model delegate only records authorization/cost receipts.
"""

import asyncio

from agents import Agent, Model

from open_deep_research.budget import budget_from_sdk_usage
from open_deep_research.workflow.durability import begin_call, end_call


class BoundAgent(Agent):
    """Bind the single clone made by DBOSRunner through Agent.clone()."""

    def bind_harness(self, harness):
        self._por_harness = harness
        self._por_cloned = False

    def clone(self, **kwargs):
        from dbos_openai_agents.runner import DBOSModelWrapper

        harness = self._por_harness
        if self._por_cloned or harness._agent is not self:
            raise ValueError("Unexpected nested Agent clone")
        if set(kwargs) != {"model", "tools", "handoffs"}:
            raise ValueError("Unexpected DBOS Agent configuration change")
        model = kwargs["model"]
        if not isinstance(model, DBOSModelWrapper) or model.model is not self.model:
            raise ValueError("DBOS model clone lost the admitted model")
        tools = kwargs["tools"]
        if len(tools) != len(self.tools) or kwargs["handoffs"]:
            raise ValueError("DBOS clone changed role tools/handoffs")
        for original, wrapped in zip(self.tools, tools, strict=True):
            for field in original.__dataclass_fields__:
                if field != "on_invoke_tool" and getattr(original, field) != getattr(
                    wrapped, field
                ):
                    raise ValueError("DBOS clone changed an admitted tool policy")
        clone = super().clone(**kwargs)
        self._por_cloned = True
        clone._por_cloned = True
        harness._agent = clone
        return clone


class MeteredModel(Model):
    def __init__(self, model):
        if not isinstance(model, Model):
            raise TypeError("Durable business Agent requires an explicit SDK Model")
        self.model = model
        self.model_name = getattr(model, "model_name", type(model).__name__)

    async def get_response(self, *args, **kwargs):
        from open_deep_research.budget import ModelBudgetExceeded

        try:
            identity = await begin_call("sdk_model")
        except ModelBudgetExceeded as exc:
            from open_deep_research.runtime.sdk_runtime import _ReasoningStopped

            raise _ReasoningStopped(str(exc)) from exc
        pending = asyncio.create_task(self.model.get_response(*args, **kwargs))
        try:
            response = await pending
        except BaseException as exc:
            pending.cancel()
            drain = asyncio.gather(pending, return_exceptions=True)
            while not drain.done():
                try:
                    await asyncio.shield(drain)
                except asyncio.CancelledError:
                    pass
            await end_call(identity, usage={"model_calls": 1}, status="failed")
            from open_deep_research.llm.errors import (
                ModelInvocationError,
                is_token_limit_exceeded,
            )
            from open_deep_research.workflow.execution import ExecutionControlError

            if isinstance(exc, Exception) and not isinstance(
                exc, (ExecutionControlError, ModelBudgetExceeded)
            ):
                raise ModelInvocationError(
                    type(exc).__name__,
                    context_limit=is_token_limit_exceeded(exc, self.model_name),
                    budget_usage={"model_calls": 1},
                ) from exc
            raise
        usage = budget_from_sdk_usage(response.usage)
        usage["model_calls"] = 1
        await end_call(identity, usage=usage)
        return response

    def stream_response(self, *args, **kwargs):
        raise NotImplementedError("Durable business Agents use DBOSRunner.run")
