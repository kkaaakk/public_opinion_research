"""Run one workflow task; HTTP readers subscribe and explicit Stop cancels it."""

from __future__ import annotations

import asyncio
import logging
import uuid

from langchain_core.runnables import RunnableConfig
from langchain_core.runnables.config import merge_configs

from open_deep_research.trajectory.live import LiveBus
from open_deep_research.trajectory.recorder import TrajectorySessionRecorder
from open_deep_research.trajectory.semantics import CURRENT_RECORDER

LOGGER = logging.getLogger(__name__)
_LABELS = {"write_research_brief": "Planning research…", "research_phase": "Analyzing public opinion…",
           "section_writer": "Writing report…", "compile_final_report": "Finalizing report…"}


class ResearchExecution:
    """Keep transport disconnect independent of a single admitted research execution."""

    def __init__(self, graph_factory, initial_state: dict, config: RunnableConfig, semaphore, session=None):
        """Attach the recorder while preserving all existing callbacks and LangSmith tracing."""
        self.session = session
        self.recorder = TrajectorySessionRecorder(session) if session else None
        self.bus = session.bus if session else LiveBus()
        self.graph_factory = graph_factory
        self.initial_state = initial_state
        self.config = merge_configs(config, {"callbacks": [self.recorder]}) if self.recorder else config
        self.semaphore = semaphore
        self.trace_id = uuid.uuid4().hex[:12]
        self.terminal_packets: list[dict] = []
        self.task: asyncio.Task | None = None

    def start(self) -> None:
        """Schedule the workflow once; resubscriptions never call this again."""
        if self.task is None:
            self.task = asyncio.create_task(self._run())

    async def cancel(self) -> None:
        """Cancel and await durable settlement before responding to Stop."""
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                # Cancellation before the task's first scheduled instruction.
                if self.recorder:
                    self.recorder.finish("cancelled")
                self.bus.close()

    async def stream(self):
        """Subscribe without lending cancellation ownership to the SSE generator."""
        initial = self.session.packet() if self.session else {"type": "status", "message": "Starting research…"}
        async for packet in self.bus.listen(initial):
            if packet["type"] == "done":
                for terminal in self.terminal_packets:
                    yield terminal
            yield packet

    async def _run(self):
        token = CURRENT_RECORDER.set(self.recorder)
        final_report, budget = None, None
        status, error = "completed", None
        try:
            async with self.semaphore:
                self.bus.publish({"type": "status", "message": "Starting research…"})
                # astream_events supplies only the existing root report/status
                # transport. Runtime facts are collected by official callbacks;
                # these event dictionaries are never ingested a second time.
                async for source in self.graph_factory(self.config).astream_events(
                    self.initial_state, self.config, version="v2"
                ):
                    if source.get("event") == "on_chain_end" and not source.get("parent_ids"):
                        output = (source.get("data") or {}).get("output")
                        if isinstance(output, dict):
                            final_report = (output.get("report") or {}).get("final") or final_report
                            # Root completion is the authoritative accumulated
                            # budget, while node stream outputs can be deltas.
                            budget = (output.get("runtime") or {}).get("budget", budget)
                    if source.get("event") != "on_chain_stream" or source.get("parent_ids"):
                        continue
                    chunk = (source.get("data") or {}).get("chunk")
                    if not isinstance(chunk, dict):
                        continue
                    for name, output in chunk.items():
                        if name in _LABELS:
                            self.bus.publish({"type": "status", "message": _LABELS[name]})
                        if isinstance(output, dict):
                            update = output.get("report") or {}
                            if update.get("final"):
                                final_report = update["final"]
                if final_report:
                    self.terminal_packets = [{"type": "report", "content": final_report},
                        {"type": "usage", **({k: budget[k] for k in ("model_calls", "input_tokens", "output_tokens", "total_tokens") if k in budget} if budget is not None else {})}]
                else:
                    status = "workflow_error"
                    self.terminal_packets = [{"type": "error", "message": "Research completed but no report was generated."}]
        except asyncio.CancelledError:
            status = "cancelled"
            self.terminal_packets = [{"type": "status", "message": "Cancelled"}]
        except Exception as exc:
            status, error = "workflow_error", exc
            LOGGER.error("Research request failed; trace_id=%s category=%s", self.trace_id, type(exc).__name__)
            self.terminal_packets = [{"type": "error", "message": f"Research request failed. Trace ID: {self.trace_id}."}]
        finally:
            if self.recorder:
                self.recorder.finish(status, error=error, budget_usage=budget)
            CURRENT_RECORDER.reset(token)
            self.bus.close()
