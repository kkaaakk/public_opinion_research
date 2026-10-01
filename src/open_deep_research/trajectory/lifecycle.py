"""Central turn/step ownership and tool joins built from official runtime facts."""

from __future__ import annotations

import json
from typing import Any

from open_deep_research.trajectory.attempts import ModelAttempt
from open_deep_research.trajectory.events import now_ms
from open_deep_research.trajectory.sanitization import bounded, error_data, sanitize
from open_deep_research.trajectory.session import TrajectorySession


class RuntimeLifecycle:
    """Allocate model steps and settle calls without guessing a recent LLM or agent."""

    def __init__(self, session: TrajectorySession, *, stream_limit: int = 4 * 1024 * 1024):
        """Keep only callback relations and active/declared execution facts."""
        self.session = session
        self.stream_limit = stream_limit
        self.parents: dict[str, str | None] = {}
        self.agents: dict[str, str] = {}
        self.models: dict[str, ModelAttempt] = {}
        self.calls: dict[str, dict] = {}
        self.declarations: dict[str, dict] = {}
        self.systems: dict[str, Any] = {}
        self.next_step = 0
        self.current_turn = session.turn
        for event in session.events:
            if event.type == "system/message":
                self.systems[event.data["scope_id"]] = event.data["content"]
        self.last_live_at: dict[str, int] = {}

    def declaration(self, raw_id: str, context: dict, *, unclaimed: bool = False) -> tuple[str, dict] | tuple[None, None]:
        """Resolve a provider id in its request scope, including ids reused in later requests."""
        matches = [(key, item) for key, item in self.declarations.items()
                   if item["tool_call_id"] == raw_id and not item["settled"]
                   and (not unclaimed or not item["claimed"])]
        scoped = [(key, item) for key, item in matches
                  if self._same_scope(self.models[item["model_id"]], context)]
        candidates = scoped or matches
        return candidates[0] if len(candidates) == 1 else (None, None)

    def _same_scope(self, owner: ModelAttempt, context: dict) -> bool:
        agent_ids = [p for p in owner.context["parent_ids"] if p in self.agents]
        if agent_ids:
            return agent_ids[-1] in context["parent_ids"]
        return self.parents.get(owner.run_id) in context["parent_ids"]

    def context(self, run_id: str, parent_run_id: str | None, metadata: dict | None = None) -> dict:
        """Resolve runtime ancestry, retaining it once in each event envelope."""
        self.parents[run_id] = parent_run_id
        ids: list[str] = []
        seen = {run_id}
        parent = parent_run_id
        while parent and parent not in seen:
            ids.insert(0, parent)
            seen.add(parent)
            parent = self.parents.get(parent)
        metadata = metadata or {}
        agent = metadata.get("agent_name")
        if not agent:
            agent = next((self.agents[p] for p in reversed(ids) if p in self.agents), None)
        return {"run_id": run_id, "parent_run_id": parent_run_id, "parent_ids": tuple(ids),
                "agent_name": agent, "langgraph_node": metadata.get("langgraph_node")}

    def chain_start(self, run_id: str, parent: str | None, name: str, metadata: dict) -> None:
        """Record actual business-agent context without creating a turn or model step."""
        context = self.context(run_id, parent, metadata)
        if metadata.get("agent_name") == name or name.endswith("_agent") and metadata.get("node_kind") == "agent":
            self.agents[run_id] = name
            self.session.append("agent/context", {"turn": self.session.turn, "name": name, "status": "started"}, **context)

    def chain_end(self, run_id: str, error: BaseException | None = None) -> None:
        """Close undispatched declarations only when their actual execution owner ends."""
        if run_id not in self.agents:
            return
        for model in self.models.values():
            if run_id in model.context["parent_ids"] and model.settled and not model.ended:
                self.close_step(model, "unresolved_tools" if model.pending else "completed")
        self.session.append("agent/context", {"turn": self.session.turn, "name": self.agents[run_id],
            "status": "error" if error else "completed", **({"error": error_data(error, "agent_error")} if error else {})},
            **self.context(run_id, self.parents.get(run_id)))

    def model_start(self, run_id: str, parent: str | None, messages: list, header: dict, metadata: dict) -> None:
        """Start exactly one step per real request and observe real prior tool messages."""
        context = self.context(run_id, parent, metadata)
        for message in messages:
            call_id = getattr(message, "tool_call_id", None)
            identity, declaration = self.declaration(str(call_id), context) if call_id else (None, None)
            if declaration and not declaration.get("settled"):
                owner = self.models[declaration["model_id"]]
                self.session.append("tool/result", {"turn": self.session.turn, "step": owner.step,
                    "attempt_id": owner.run_id, "call_id": identity, "tool_call_id": str(call_id), "name": declaration["name"],
                    "arguments": declaration["arguments"], "result": bounded(message.content),
                    "status": "execution_unobserved", "is_error": getattr(message, "status", "") == "error",
                    "origin": "model_request_context"}, **context)
                declaration["settled"] = True
                owner.pending.discard(identity)
                if not owner.pending and owner.settled:
                    self.close_step(owner, "completed")
        if self.current_turn != self.session.turn:
            self.current_turn, self.next_step = self.session.turn, 0
        self.next_step += 1
        scope = context["agent_name"] or metadata.get("langgraph_node") or parent or run_id
        attempt = ModelAttempt(run_id, self.next_step, self.next_step, now_ms(), str(scope), context)
        attempt.structured_output = bool(metadata.get("structured_output") or
            (header.get("config", {}).get("response_format") or {}).get("type") in {"json_schema", "json_object"})
        self.models[run_id] = attempt
        common = {"turn": self.session.turn, "step": attempt.step, "attempt_id": run_id, "started_at": attempt.started_at}
        self.session.append("step/start", common, **context)
        system = [m.content for m in messages if getattr(m, "type", None) == "system"]
        if self.systems.get(str(scope)) != system:
            self.session.append("system/message", {**common, "scope_id": str(scope), "content": bounded(system, 262_144),
                "update": str(scope) in self.systems}, **context)
            self.systems[str(scope)] = system
        self.session.append("request/header", {**common, "scope_id": str(scope), "header": header}, **context)
        safe_context = {k: metadata[k] for k in ("business_scenario", "retrieval_mode", "rag_enabled", "component", "structured_output", "research_run_id") if k in metadata}
        self.session.append("request/context", {**common, **safe_context}, **context)
        self.session.publish("assistant/stream/start")

    def model_chunk(self, run_id: str, chunk: dict) -> None:
        """Buffer timed deltas; transient snapshots are throttled and never stored as rows."""
        model = self.models.get(run_id)
        if not model or model.settled:
            return
        time = now_ms()
        model.push(time, chunk, self.stream_limit)
        content = [{"type": "reasoning", "reasoning": model.reasoning}, {"type": "text", "text": model.content}] if model.reasoning else model.content
        self.session.update_partial(run_id, self.session.turn, model.step, sanitize(content))
        if time - self.last_live_at.get(run_id, 0) >= 50:
            self.last_live_at[run_id] = time
            self.session.publish("assistant/stream/chunk")

    def model_end(self, run_id: str, message: Any = None, error: BaseException | None = None, *, status: str | None = None) -> None:
        """Commit every successful request or failed Attempt, including pre-token errors."""
        model = self.models.get(run_id)
        if not model or model.settled:
            return
        model.settled = True
        calls = list(getattr(message, "tool_calls", []) or []) if not error else []
        raw_calls = ((getattr(message, "additional_kwargs", None) or {}).get("tool_calls", []))
        raw_by_id = {c.get("id"): c.get("function", {}).get("arguments") for c in raw_calls}
        safe_calls = []
        for call in calls:
            raw_id = str(call["id"])
            call_id = f"{run_id}:{raw_id}"
            arguments = raw_by_id.get(raw_id) or json.dumps(call.get("args", {}), ensure_ascii=False)
            declaration = {"model_id": run_id, "name": call["name"], "arguments": arguments,
                           "tool_call_id": raw_id, "args": call.get("args", {}), "claimed": False, "settled": False}
            if not model.structured_output:
                self.declarations[call_id] = declaration
                model.pending.add(call_id)
            safe_calls.append({"id": call_id, "tool_call_id": raw_id, "name": call["name"], "arguments": sanitize(arguments),
                               **({"executable": False, "mode": "structured_output"} if model.structured_output else {})})
        output = getattr(message, "content", model.content)
        reasoning = (getattr(message, "additional_kwargs", {}) or {}).get("reasoning_content") or model.reasoning
        if reasoning:
            output = [{"type": "reasoning", "reasoning": reasoning}, {"type": "text", "text": output}]
        usage = getattr(message, "usage_metadata", None)
        response_metadata = getattr(message, "response_metadata", {}) or {}
        outcome = status or ("model_error" if error else "completed")
        self.session.append("assistant/attempt" if error else "assistant/message", {
            "turn": self.session.turn, "step": model.step, "attempt_id": run_id, "attempt": model.attempt,
            "started_at": model.started_at, "first_token_at": model.first_token_at, "status": outcome,
            "content": bounded(output, 262_144), "tool_calls": safe_calls, "stream": model.safe_stream(),
            **({"stream_truncated": True, "original_size": model.stream_bytes, "limit_bytes": self.stream_limit} if model.stream_truncated else {}),
            **({"usage": usage} if usage is not None else {}),
            **({"finish_reason": response_metadata["finish_reason"]} if "finish_reason" in response_metadata else {}),
            **({"error": error_data(error, outcome)} if error else {}),
        }, **model.context)
        self.session.publish("assistant/stream/end")
        if not model.pending:
            self.close_step(model, outcome)

    def close_step(self, model: ModelAttempt, reason: str) -> None:
        """Close only after model settlement and all known executions have settled."""
        if model.ended:
            return
        model.ended = True
        self.session.append("step/end", {"turn": self.session.turn, "step": model.step,
            "attempt_id": model.run_id, "reason": reason, "unresolved_calls": sorted(model.pending)}, **model.context)

    def tool_start(self, run_id: str, parent: str | None, name: str, arguments: Any,
                   metadata: dict, call_id: str | None = None) -> None:
        """Join by true call id/ancestry, or an unambiguous scoped declaration match."""
        context = self.context(run_id, parent, metadata)
        parent_tool = next((self.calls[p] for p in reversed(context["parent_ids"]) if p in self.calls), None)
        if not call_id and not parent_tool:
            call_id = metadata.get("tool_call_id")
        identity, declaration = self.declaration(str(call_id), context, unclaimed=True) if call_id else (None, None)
        resolution = "call_id" if declaration else None
        if not declaration and not parent_tool:
            candidates = []
            for key, item in self.declarations.items():
                owner = self.models[item["model_id"]]
                # Root/graph ancestry alone is insufficient. The enclosing
                # callback parent must be shared (same agent/invocation scope).
                if not item["claimed"] and not item["settled"] and item["name"] == name and item["args"] == arguments and self._same_scope(owner, context):
                    candidates.append((key, item))
            if len(candidates) == 1:
                identity, declaration = candidates[0]
                resolution = "scoped_declaration"
        owner = self.models[declaration["model_id"]] if declaration else None
        step = owner.step if owner else parent_tool.get("step") if parent_tool else None
        if declaration:
            declaration["claimed"] = True
        actual_id = str(identity or run_id)
        call = {"call_id": actual_id, "step": step, "turn": self.session.turn,
                "name": name, "arguments": bounded(declaration["arguments"] if declaration else arguments),
                "input": bounded(arguments), "tool_call_id": declaration["tool_call_id"] if declaration else call_id,
                "started_at": now_ms(),
                "attempt_id": owner.run_id if owner else parent_tool.get("attempt_id") if parent_tool else None,
                "parent_call_id": parent_tool["call_id"] if parent_tool else None,
                "parent_resolution": resolution or ("runtime_parent" if parent_tool else "unresolved"),
                "settled": False, "context": context}
        self.calls[run_id] = call
        self.session.append("tool/call", {k: v for k, v in call.items() if k not in {"context", "settled"}}, **context)

    def tool_end(self, run_id: str, output: Any = None, error: BaseException | None = None, *, status: str | None = None) -> None:
        """Pair actual tool results, preserving nested calls and distinct error categories."""
        call = self.calls.get(run_id)
        if not call or call["settled"]:
            return
        call["settled"] = True
        message_error = getattr(output, "status", "") == "error"
        self.session.append("tool/result", {"turn": call["turn"], "step": call["step"],
            "call_id": call["call_id"], "result": bounded(getattr(output, "content", output)),
            "is_error": bool(error or message_error), "status": status or ("tool_error" if error or message_error else "completed"),
            "duration_ms": max(0, now_ms() - call["started_at"]),
            **({"error": error_data(error, status or "tool_error")} if error else {}),
            **({"metadata": bounded(output.artifact)} if getattr(output, "artifact", None) is not None else {}),
        }, **call["context"])
        declaration = self.declarations.get(call["call_id"])
        if declaration:
            declaration["settled"] = True
            model = self.models[declaration["model_id"]]
            model.pending.discard(call["call_id"])
            # A wrapper result cannot close a step whose nested child still runs.
            active = any(not c["settled"] and c.get("attempt_id") == model.run_id for c in self.calls.values())
            if not model.pending and model.settled and not active:
                self.close_step(model, "completed")
        elif call.get("attempt_id") in self.models:
            model = self.models[call["attempt_id"]]
            if model.settled and not model.pending and not any(not c["settled"] and c.get("attempt_id") == model.run_id for c in self.calls.values()):
                self.close_step(model, "completed")

    def finish(self, status: str) -> None:
        """Settle active requests/calls before the enclosing user turn closes."""
        error = RuntimeError(status if status != "completed" else "Execution ended before this operation settled")
        for model in self.models.values():
            if not model.settled:
                self.model_end(model.run_id, error=error, status=status if status != "completed" else "interrupted")
        for run_id, call in self.calls.items():
            if not call["settled"]:
                self.tool_end(run_id, error=error, status=status if status != "completed" else "interrupted")
        for model in self.models.values():
            if not model.ended:
                self.close_step(model, status if status != "completed" else "unresolved_tools")
