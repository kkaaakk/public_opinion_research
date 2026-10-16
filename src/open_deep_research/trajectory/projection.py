"""One deterministic projection for committed/live history and cold replay."""

from __future__ import annotations

import copy
from typing import Any

from open_deep_research.trajectory.events import SessionEvent
from open_deep_research.trajectory.presentation import (
    blocks,
    request_config,
    text,
    usage,
)


class TrajectoryProjection:
    """Fold explicit backend identities into JSON-compatible DSH snapshots."""

    def __init__(self):
        """Initialize only derived presentation state; no runtime state or spans."""
        self.nodes: dict[int, dict] = {}
        self.requests: dict[str, dict] = {}
        self.tools: dict[str, dict] = {}
        self.headers: dict[str, dict] = {}
        self.systems: dict[str, str] = {}
        self.system_prompts: list[dict] = []
        self.turns: dict[int, dict] = {}
        self.steps: dict[tuple[int, int], dict] = {}
        self.coordinates: dict[int, tuple[int | None, int | None]] = {}
        self.schemas: dict[str, dict] = {}
        self.partials: dict[str, dict] = {}
        self.status = "running"
        self.budget_usage: dict | None = None

    def apply(self, event: SessionEvent) -> None:
        """Consume a durable fact; all identities, clocks and parent links are supplied."""
        d, kind, seq, time = event.data, event.type, event.seq, event.time
        turn, step = d.get("turn"), d.get("step")
        self.coordinates[seq] = (turn, step)
        base = {"seq": seq, "time": time}
        attempt = d.get("attempt_id", event.run_id or "")
        if kind == "turn/start":
            self.status = "running"
            self.turns[turn] = {"turn": turn, "start": event.to_dict(), "status": "open"}
        elif kind == "turn/end":
            self.turns[turn].update({"end": event.to_dict(), "status": "closed"})
            if d.get("reason") != "completed":
                self.nodes[seq] = {**base, "kind": "turn-error", "turn": turn, "step": step or 0,
                                   "message": d.get("reason", "interrupted")}
        elif kind == "step/start":
            self.steps[turn, step] = {"turn": turn, "step": step, "start": event.to_dict(), "status": "open"}
            self.requests[attempt] = {"purpose": "assistant", "turn": turn, "step": step,
                "startSeq": seq, "startedAt": d.get("started_at", time), "completedAt": None, "status": "running",
                "attemptId": attempt, "agentName": event.agent_name}
        elif kind == "step/end":
            self.steps[turn, step].update({"end": event.to_dict(), "status": "closed"})
            for call_id in d.get("unresolved_calls", []):
                call = self.tools.get(call_id)
                if call and not call.get("settled"):
                    call.update({"settled": True, "ended_at": time, "result_seq": seq,
                        "is_error": True, "status": "interrupted" if d.get("reason") in {"cancelled", "interrupted"} else "unresolved",
                        "result": "Tool requested but no execution outcome was observed",
                        "error": {"name": "UnresolvedToolError"}})
        elif kind == "user/message":
            self.nodes[seq] = {**base, "kind": "user", "content": [{"type": "text", "text": text(d["content"])}],
                               "source": {"kind": "user"}}
        elif kind == "system/message":
            self.systems[d["scope_id"]] = text(d["content"])
            self.system_prompts.append({**base, "turn": turn, "step": step, "text": text(d["content"]), "update": d["update"]})
        elif kind == "request/header":
            config = request_config(d["header"])
            tools = d["header"].get("tools", [])
            prompt = {"config": config, "tools": tools, "system": self.systems.get(d["scope_id"], "")}
            self.headers[attempt] = prompt
            request = self.requests[attempt]
            request.update({"prompt": prompt, "requestConfig": config, "providerMetadata": config})
        elif kind in {"assistant/message", "assistant/attempt"}:
            self.partials.pop(attempt, None)
            request = self.requests[attempt]
            failed = kind == "assistant/attempt"
            error = text(d.get("error")) if failed else ""
            content_blocks = blocks(d.get("content"), d.get("tool_calls"))
            if failed:
                content_blocks.append({"kind": "text", "text": f"Attempt {d.get('attempt', '?')}: {d.get('status', 'model_error')} — {error}"})
            prompt = self.headers.get(attempt, {})
            converted_usage = usage(d.get("usage"))
            request.update({"completedAt": time, "resultSeq": seq, "status": "error" if failed else "complete"})
            if failed:
                request["error"] = error
            if converted_usage is not None:
                request["usage"] = converted_usage
            self.nodes[seq] = {**base, "kind": "assistant", "turn": turn, "step": step,
                "messageId": attempt, "blocks": content_blocks, "providerMetadata": prompt.get("config", {}),
                "requestConfig": prompt.get("config", {}), "timing": {"stepStartTime": d["started_at"],
                "firstTokenTime": d.get("first_token_at"), "completedTime": time},
                **({"usage": converted_usage} if converted_usage is not None else {}),
                **({"interrupted": True} if d.get("status") in {"cancelled", "interrupted"} else {})}
            by_name = {t["name"]: t for t in prompt.get("tools", [])}
            for call in d.get("tool_calls", []):
                if call.get("executable") is False:
                    continue
                self.tools.setdefault(call["id"], {"call_id": call["id"], "name": call["name"],
                    "arguments": call["arguments"], "turn": turn, "step": step,
                    "seq": seq, "time": time, "started_at": None, "settled": False,
                    "attempt_id": attempt, "requested_only": True})
                if call["name"] in by_name:
                    self.schemas[call["id"]] = by_name[call["name"]]
        elif kind == "tool/call":
            self.tools[d["call_id"]] = {**base, **d, "settled": False}
            owner_prompt = self.headers.get(d.get("attempt_id", ""), {})
            for schema in owner_prompt.get("tools", []):
                if schema["name"] == d["name"]:
                    self.schemas[d["call_id"]] = schema
        elif kind == "tool/result":
            # Results observed in a later model context may have no execution start.
            call = self.tools.setdefault(d["call_id"], {**base, **d, "started_at": None})
            call.update({"settled": True, "result": d.get("result"), "is_error": d.get("is_error", False),
                         "error": d.get("error"), "ended_at": time, "result_seq": seq, "status": d.get("status")})
        elif kind in {"agent/context", "request/context", "workflow/error", "retry/scheduled", "retry/started"}:
            label = {"agent/context": "Agent", "request/context": "Request context", "workflow/error": "Workflow error", "retry/scheduled": "Retry", "retry/started": "Retry"}[kind]
            self.nodes[seq] = {**base, "kind": "context", "source": {"kind": "runtime", "name": label},
                "producer": {"kind": "runtime", "name": label}, "form": None,
                "content": [{"type": "text", "text": text(d)}]}
        elif kind == "compaction/summary":
            self.nodes[seq] = {**base, "kind": "compaction", "summary": text(d.get("summary")),
                "summaryEventSeq": seq, "shadowedItemCount": d.get("replaced_count"), "shadowedTokenCount": None}
        elif kind == "compaction/end" and d.get("error"):
            self.nodes[seq] = {**base, "kind": "context", "producer": {"kind": "runtime", "name": "Compaction"},
                "form": None, "source": {"kind": "runtime"}, "content": [{"type": "text", "text": text(d["error"])}]}
        elif kind == "session/end":
            self.status = d["status"]
            self.budget_usage = d.get("budget_usage")

    def live(self, attempt_id: str, turn: int, step: int, content: Any) -> None:
        """Update transient output from a recorder-owned stream, without allocating seq."""
        self.partials[attempt_id] = {"turn": turn, "step": step, "blocks": blocks(content)}

    def snapshot(self, *, active: bool = True, min_seq: int = 0) -> dict[str, Any]:
        """Materialize the same value for live or replay; recovery never mutates events."""
        requests = copy.deepcopy(list(self.requests.values()))
        nodes = [copy.deepcopy(n) for seq, n in self.nodes.items() if seq >= min_seq]
        interrupted = not active and (any(t["status"] == "open" for t in self.turns.values())
                                     or any(s["status"] == "open" for s in self.steps.values()))
        if interrupted:
            for request in requests:
                if request["status"] == "running":
                    request.update({"status": "error", "error": "interrupted — no durable model settlement"})
            for turn_state in self.turns.values():
                if turn_state["status"] == "open":
                    start = turn_state["start"]
                    nodes.append({"kind": "turn-error", "seq": start["seq"], "time": start["time"],
                                  "turn": turn_state["turn"], "step": 0, "message": "interrupted — writer is no longer live"})

        def tool_shape(call: dict) -> dict:
            sub = [tool_shape(c) for c in self.tools.values() if c.get("parent_call_id") == call["call_id"]]
            base = {"callId": call["call_id"], "subCalls": sub}
            if not call.get("settled") and active:
                return {**base, "phase": "preparing" if call.get("requested_only") else "start", "name": call["name"], "argsRaw": text(call.get("arguments")),
                        "time": call["time"], "turn": call.get("turn"), "step": call.get("step")}
            unobserved = call.get("status") == "execution_unobserved"
            return {**base, "kind": "tool-result", "seq": call.get("result_seq", call["seq"]),
                "time": call.get("ended_at") if call.get("settled") else None, "callTime": call.get("started_at"),
                "call": {"name": call.get("name", "unknown"), "argsRaw": text(call.get("arguments"))},
                "content": [{"type": "text", "text": text(call.get("result")) if call.get("settled") else "interrupted — tool outcome unknown"}],
                "isError": (call.get("is_error", False) or unobserved) if call.get("settled") else True,
                **({"error": {"name": (call.get("error") or {}).get("name", "InterruptedError"),
                              "code": call.get("status") or "interrupted",
                              "reason": "Only a model-visible result was observed" if unobserved else text(call.get("error"))}} if call.get("error") or not call.get("settled") or unobserved else {})}

        running = []
        for call in self.tools.values():
            if call.get("parent_call_id"):
                continue
            shape = tool_shape(call)
            if shape.get("seq", call["seq"]) < min_seq:
                continue
            if shape.get("kind") == "tool-result":
                nodes.append(shape)
            else:
                running.append(shape)
        locations = []
        location: dict[str, Any]
        for seq, (turn, step) in self.coordinates.items():
            if turn is None:
                location = {"kind": "unresolved"}
            else:
                t = copy.deepcopy(self.turns.get(turn, {"turn": turn, "status": "unknown"}))
                location = {"kind": "turn", "turn": t}
                if step is not None and (turn, step) in self.steps:
                    location.update({"kind": "step", "step": copy.deepcopy(self.steps[turn, step])})
            locations.append([seq, location])
        return {"systemPrompts": copy.deepcopy(self.system_prompts), "eventNodes": sorted(nodes, key=lambda n: n["seq"]),
            "requests": [r for r in requests if r.get("resultSeq", r["startSeq"]) >= min_seq],
            "eventLocations": locations, "callSchemas": copy.deepcopy([[k, v] for k, v in self.schemas.items()]),
            "partial": copy.deepcopy(next(reversed(self.partials.values()), None)) if active else None,
            "runningCalls": running, "status": "interrupted" if interrupted else self.status,
            "budgetUsage": self.budget_usage}


def replay(events: list[SessionEvent], *, active: bool = False, min_seq: int = 0) -> dict:
    """Reconstruct history using exactly the same fold as live publication."""
    projection = TrajectoryProjection()
    for event in events:
        projection.apply(event)
    return projection.snapshot(active=active, min_seq=min_seq)
