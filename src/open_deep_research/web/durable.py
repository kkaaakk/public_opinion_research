"""Run API admission and projection over native DBOS status/messages/streams."""

from __future__ import annotations

from functools import lru_cache
from uuid import uuid4

from sqlalchemy import text

from open_deep_research.budget import merge_budget_usage
from open_deep_research.configuration import Configuration
from open_deep_research.observability.telemetry import (
    business_span,
    correlation,
    enqueue_context,
)
from open_deep_research.runtime.sdk_runtime import SDK_RUNTIME_VERSION
from open_deep_research.web.run_data import (
    RunConflict,
    RunNotFound,
    digest,
    dumps,
    loads,
    public_payload,
    timestamp,
)
from open_deep_research.workflow.business_steps import SAFE_STEPS
from open_deep_research.workflow.dbos_config import (
    ARCHIVE,
    BUSINESS,
    CONTROL,
    RECOVERY,
    SDK_VERSION,
    VERSION,
)
from open_deep_research.workflow.python_state import copy_state
from open_deep_research.workflow.run_facts import facts


class RunClient:
    """Validate business controls and project execution facts; never schedule/claim work."""

    def __init__(self, repository=None):
        self.facts = repository or facts()
        self.facts.verify_policy()
        self.client = self.facts.client

    def create(self, owner, key, request, config, state, *, session_id=None):
        if not owner or len(owner) > 128 or not key or len(key) > 128:
            raise RunConflict("Owner and idempotency key are required")
        fingerprint = digest({"request": request, "session_id": session_id})
        repo = self.facts
        with repo.engine.begin() as db:
            repo.lock(db)
            old = db.execute(
                text(
                    "SELECT id,value FROM por_domain.requests WHERE owner=:o AND request_key=:k"
                ),
                {"o": owner, "k": key},
            ).first()
            if old:
                if loads(old[1])["fingerprint"] != fingerprint:
                    raise RunConflict("Idempotency key was used for another request")
                run_id = old[0]
                created = False
            else:
                if session_id:
                    owners = (
                        db.execute(
                            text(
                                "SELECT DISTINCT owner FROM por_domain.requests WHERE session=:s"
                            ),
                            {"s": session_id},
                        )
                        .scalars()
                        .all()
                    )
                    if not owners or any(o != owner for o in owners):
                        raise RunNotFound("Session not found")
                pending = self.client.list_workflows(
                    name="por.run",
                    status=["ENQUEUED", "PENDING"],
                    load_input=False,
                    load_output=False,
                    limit=repo.settings.queue_capacity + 1,
                )
                if len(pending) >= repo.settings.queue_capacity:
                    raise RunConflict("Research queue is full")
                cfg = loads(dumps(config))
                operator_approval = Configuration.from_runnable_config(
                    cfg
                ).allow_plan_feedback
                cfg.setdefault("configurable", {})["allow_plan_feedback"] = bool(
                    cfg["configurable"].get("allow_plan_feedback") or operator_approval
                )
                cfg.setdefault("metadata", {})["accepted_run"] = True
                effective = Configuration.from_runnable_config(cfg).model_dump(
                    mode="json"
                )
                run_id = uuid4().hex
                session_id = session_id or uuid4().hex
                cfg["configurable"].update(
                    research_run_id=run_id, thread_id=session_id, user_id=owner
                )
                # Assign missing message identities before entering a Workflow.
                state = copy_state(state)
                state.setdefault("research", {})["run_id"] = run_id
                data = {
                    "request": request,
                    "fingerprint": fingerprint,
                    "config": cfg,
                    "initial": state,
                    "configuration": digest(effective),
                    "version": VERSION,
                    "sdk": SDK_VERSION,
                    "runtime": SDK_RUNTIME_VERSION,
                    "created": repo.now(db),
                    "approval_seconds": repo.settings.approval_seconds,
                    "run_seconds": repo.settings.run_seconds,
                }
                db.execute(
                    text(
                        "INSERT INTO por_domain.requests(id,owner,request_key,session,value) VALUES (:id,:o,:k,:s,:v)"
                    ),
                    {
                        "id": run_id,
                        "o": owner,
                        "k": key,
                        "s": session_id,
                        "v": dumps(data),
                    },
                )
                with correlation(run_id=run_id), business_span("run_submit"):
                    for suffix, name, queue in [
                        (":events", "por.events", CONTROL),
                        ("", "por.run", CONTROL),
                        (":finalize", "por.finalize", ARCHIVE),
                    ]:
                        self.client.enqueue_in_transaction(
                            db,
                            {
                                "workflow_name": name,
                                "queue_name": queue,
                                "workflow_id": run_id + suffix,
                                "app_version": VERSION,
                                "authenticated_user": owner,
                                **enqueue_context(),
                            },
                            run_id,
                        )
                repo.publish(db, run_id, "run_status", status="queued")
                created = True
        return self.get(run_id, owner), created

    def native(self, run_id):
        values = self.client.list_workflows(
            workflow_ids=[run_id], load_input=False, load_output=False
        )
        if not values:
            raise RunNotFound("Native workflow not found")
        return values[0]

    def get(self, run_id, owner):
        repo = self.facts
        req = repo.request(run_id, owner)
        status = self.native(run_id)
        units = repo.units(run_id)
        with repo.engine.connect() as db:
            generation = int(
                db.execute(
                    text(
                        "SELECT coalesce(max(generation),0) FROM por_domain.retries WHERE run_id=:id"
                    ),
                    {"id": run_id},
                ).scalar_one()
            )
        plan = self.client.get_event(run_id, "plan", timeout_seconds=0)
        phase = self.client.get_event(run_id, "phase", timeout_seconds=0)
        cursor = (
            self.client.get_event(run_id + ":events", "cursor", timeout_seconds=0) or 0
        )
        history = self.client.get_event(
            run_id + ":events", "history", timeout_seconds=0
        )
        if isinstance(history, dict):
            history = history["id"] if history["generation"] == generation else None
        error = {}
        stages = []
        budget = req["data"]["initial"].get("runtime", {}).get("budget", {})
        report = ""
        for unit in units:
            budget = merge_budget_usage(budget, unit["usage"])
            if unit["error"]:
                error = unit["error"]
            patch = (unit["artifact"] or {}).get("patch", {})
            report = patch.get("report", {}).get("final", report)
            for role, data in patch.get("agents", {}).items():
                if isinstance(data, dict) and data.get("report"):
                    stages.append(
                        {
                            "role": role,
                            "content": data["report"].get("value", "")
                            if isinstance(data["report"], dict)
                            else data["report"],
                            "stage_id": unit["id"],
                            "completed_at": "",
                        }
                    )
        unsettled = repo.unresolved(run_id)
        pending_cleanup = any(u["settled"] is None for u in units)
        terminal = status.status in {
            "SUCCESS",
            "ERROR",
            "CANCELLED",
            "MAX_RECOVERY_ATTEMPTS_EXCEEDED",
        }
        settlement = (
            "pending"
            if pending_cleanup and not terminal
            else "unresolved"
            if unsettled
            else "settled"
        )
        native_state = {
            "ENQUEUED": "queued",
            "PENDING": "running",
            "SUCCESS": "completed",
            "ERROR": "failed",
            "CANCELLED": "cancelled",
            "MAX_RECOVERY_ATTEMPTS_EXCEEDED": "interrupted",
        }.get(status.status, "interrupted")
        approval = self.client.get_event(run_id, "approval_end", timeout_seconds=0)
        if status.status == "SUCCESS" and approval in {"reject", "expired"}:
            native_state = "cancelled"
        if not terminal and phase == "awaiting_approval":
            native_state = "awaiting_approval"
        if req["cancel_requested"] and not terminal:
            native_state = "cancelling"
        if terminal and unsettled:
            native_state = "interrupted"
            error = error or {"kind": "unconfirmed_effect", "usage_complete": False}
        if unsettled:
            error["usage_complete"] = False
        if status.status == "ERROR" and "kind" not in error:
            error = {"kind": "execution_error", "usage_complete": not unsettled}
        if terminal and pending_cleanup:
            settlement = "pending"
            if req["cancel_requested"]:
                native_state = "cancelling"
        delivered = self.client.get_event(run_id + ":events", "done", timeout_seconds=0)
        delivered_phase = self.client.get_event(
            run_id + ":events", "phase", timeout_seconds=0
        )
        applied = (
            self.client.get_event(
                run_id + ":finalize", "retry_applied", timeout_seconds=0
            )
            or 0
        )
        if isinstance(applied, dict):
            applied = applied["generation"]
        current = next(
            (u["name"] for u in reversed(units) if u["artifact"] is None), ""
        )
        # Native receipts are published after their stream cursor. Reading done
        # or history may cross a relay commit after our first cursor read. Fence
        # the projection after those receipts so SSE cannot acknowledge an unseen
        # terminal event using an older cursor. This remains a native read.
        cursor = max(
            cursor,
            self.client.get_event(run_id + ":events", "cursor", timeout_seconds=0) or 0,
        )
        return {
            "approval_required": bool(
                req["data"]["config"]["configurable"].get("allow_plan_feedback")
            ),
            "request": {
                key: value
                for key, value in req["data"]["request"].items()
                if key in {"topic", "model", "mode", "rag_enabled", "require_approval"}
            },
            "generation": generation,
            "retry_requested": generation > applied,
            "cancel_requested": req["cancel_requested"],
            "events_complete": bool(
                delivered and delivered.get("generation", 0) == generation
            ),
            "events_paused": delivered_phase == "awaiting_approval",
            "run_id": run_id,
            "owner": req["owner"],
            "research_session_id": req["session"],
            "attempt_id": run_id,
            "status": native_state,
            "native_status": status.status,
            "state_version": max(1, cursor),
            "event_cursor": cursor,
            "seq": cursor,
            "current_step": current,
            "completed_steps": [u["name"] for u in units if u["artifact"]],
            "workflow_backend": "dbos",
            "business_agent_runtime": "openai_agents",
            "execution_engine": "dbos",
            "workflow_version": VERSION,
            "sdk_version": SDK_VERSION,
            "runtime_version": SDK_RUNTIME_VERSION,
            "usage": budget,
            "settlement": settlement,
            "error": error,
            "report": report,
            "role_reports": stages,
            "archive_status": "saved" if history else "pending" if terminal else "none",
            "history_id": history,
            "plan": dict(
                version=plan["version"],
                brief=plan["brief"],
                sections=[
                    s.model_dump(mode="json") if hasattr(s, "model_dump") else s
                    for s in plan["sections"]
                ],
                expires_at=timestamp(plan["expires_at"]),
            )
            if plan
            else None,
            "retry_safe": status.status == "ERROR"
            and not req["cancel_requested"]
            and generation == applied
            and not unsettled
            and any(u["error"] and u["name"] in SAFE_STEPS for u in units),
            "created_at": timestamp(req["data"]["created"]),
            "updated_at": timestamp(status.updated_at / 1000),
            "terminated_at": timestamp(status.updated_at / 1000) if terminal else None,
        }

    def read_events(self, run_id, owner, after, limit=100):
        self.facts.request(run_id, owner)
        cursor = (
            self.client.get_event(run_id + ":events", "cursor", timeout_seconds=0) or 0
        )
        if not isinstance(after, int) or after < 0 or after > cursor:
            raise RunConflict("Invalid event cursor")
        result = []
        for offset in range(after, min(cursor, after + limit)):
            item = self.client.read_stream_offset(
                run_id + ":events", "events", offset, timeout_seconds=0
            )
            if item is not None:
                result.append(public_payload(item))
            else:
                raise RunConflict(
                    "Durable event prefix is not available; retain the current cursor"
                )
        return result

    def approve(self, run_id, owner, plan_version, decision):
        req = self.facts.request(run_id, owner)
        current = self.get(run_id, owner)
        plan = self.client.get_event(run_id, "plan", timeout_seconds=0)
        if (
            current["status"] != "awaiting_approval"
            or not plan
            or plan["version"] != plan_version
            or decision not in {"approve", "reject"}
        ):
            raise RunConflict("Approval is no longer pending or plan version changed")
        with self.facts.engine.begin() as db:
            locked = self.facts.request(run_id, owner, db=db, lock=True)
            if locked["cancel_requested"] or self.facts.now(db) >= plan["expires_at"]:
                raise RunConflict("Approval expired or cancellation requested")
            if (
                db.execute(
                    text(
                        "INSERT INTO por_domain.approvals VALUES (:id,:v,:d) ON CONFLICT DO NOTHING"
                    ),
                    {"id": run_id, "v": plan_version, "d": decision},
                ).rowcount
                != 1
            ):
                raise RunConflict("Approval already consumed")
            self.client.send_in_transaction(
                db,
                run_id,
                {"owner": req["owner"], "version": plan_version, "decision": decision},
                topic="approval",
                idempotency_key="approval:" + plan_version,
            )
        return self.get(run_id, owner)

    def cancel(self, run_id, owner):
        current = self.get(run_id, owner)
        if not current["retry_requested"] and current["native_status"] in {
            "SUCCESS",
            "ERROR",
            "CANCELLED",
            "MAX_RECOVERY_ATTEMPTS_EXCEEDED",
        }:
            return current
        with self.facts.engine.begin() as db:
            self.facts.request(run_id, owner, db=db, lock=True)
            db.execute(
                text(
                    "UPDATE por_domain.requests SET cancel_requested=true WHERE id=:id"
                ),
                {"id": run_id},
            )
        # Restart-safe retry of this public command; admission is already fenced.
        self.client.cancel_workflow(run_id, cancel_children=True)
        return self.get(run_id, owner)

    def _failure_target(self, status, queue):
        """Identify one native failed execution, never a reusable Run-level grant."""
        steps = self.client.list_workflow_steps(status.workflow_id)
        errors = [s["function_id"] for s in steps if s.get("error") is not None]
        return {
            "workflow_id": status.workflow_id,
            "queue": queue,
            "start_step": min(errors)
            if errors
            else max((s["function_id"] for s in steps), default=0) + 1,
            "failure": digest(
                {
                    "version": status.app_version,
                    "updated": status.updated_at,
                    "completed": status.completed_at,
                    "steps": [
                        {
                            k: s.get(k)
                            for k in (
                                "function_id",
                                "function_name",
                                "child_workflow_id",
                                "started_at_epoch_ms",
                                "completed_at_epoch_ms",
                            )
                        }
                        for s in steps
                    ],
                }
            ),
        }

    def _rewind_safe(self, run_id, command):
        # Only the durable Finalizer calls this. Replaying its Step after a lost
        # ACK may confirm the original command, never authorize a later ERROR.
        with self.facts.engine.begin() as db:
            req = self.facts.request(run_id, db=db, lock=True)
            row = db.execute(
                text(
                    "SELECT version,generation,outcome FROM por_domain.retries WHERE run_id=:id AND key=:k"
                ),
                {"id": run_id, "k": command.get("key")},
            ).first()
            if (
                not row
                or not row[2]
                or command
                != {
                    "key": command.get("key"),
                    "generation": row[1],
                    "version": row[0],
                    "outcome": digest(loads(row[2])),
                }
            ):
                return None
            outcome = loads(row[2])
            if (
                outcome["source_generation"] + 1 != row[1]
                or outcome["version"] != VERSION
            ):
                return None
            self.facts.validate(req)
            if req["cancel_requested"]:
                self.client.cancel_workflow(run_id, cancel_children=True)
                return {"generation": row[1], "outcome": "cancelled"}
            # The captured native status/journal timestamps are execution
            # versions. Rewind clears/replaces them before another execution.
            # A crash after native commit but before our Step ACK therefore
            # skips an advanced target, even if it has already failed again.
            root_target = outcome["targets"][-1]
            root_status = self.native(run_id)
            if (
                root_status.status != "ERROR"
                or self._failure_target(root_status, root_target["queue"])
                != root_target
            ):
                return {"generation": row[1], "outcome": "superseded"}
            for target in outcome["targets"]:
                status = self.native(target["workflow_id"])
                if (
                    status.status != "ERROR"
                    or self._failure_target(status, target["queue"]) != target
                ):
                    continue
                if target["queue"] == BUSINESS and self.facts.unresolved(run_id, db):
                    return {"generation": row[1], "outcome": "blocked"}
                self.client.rewind_workflow(
                    target["workflow_id"],
                    start_step=target["start_step"],
                    queue_name=target["queue"],
                    queue_partition_key=req["owner"],
                )
            return {"generation": row[1], "outcome": "applied"}

    def retry(self, run_id, owner, key, expected_version):
        repo = self.facts
        with repo.engine.begin() as db:
            req = repo.request(run_id, owner, db=db, lock=True)
            existing = db.execute(
                text(
                    "SELECT version FROM por_domain.retries WHERE run_id=:id AND key=:k"
                ),
                {"id": run_id, "k": key},
            ).first()
            if existing:
                if existing[0] != expected_version:
                    raise RunConflict("Retry key was used for another observed version")
                # An idempotent acknowledgement is never an execution grant.
                # The original native message survives a submitter crash.
            else:
                repo.validate(req)
                result = self.get(run_id, owner)
                if (
                    result["state_version"] != expected_version
                    or not result["retry_safe"]
                    or not result["events_complete"]
                    or result["retry_requested"]
                    or req["cancel_requested"]
                    or not key
                ):
                    raise RunConflict(
                        "Run requires reconciliation; no safe automatic retry"
                    )
                children = self.client.list_workflows(
                    workflow_id_prefix=run_id + ":",
                    status="ERROR",
                    load_input=False,
                    load_output=False,
                )
                targets = [
                    self._failure_target(c, BUSINESS)
                    for c in children
                    if c.name in {"por.plan", "por.research"}
                ]
                root = self.native(run_id)
                if root.status != "ERROR" or root.app_version != VERSION or not targets:
                    raise RunConflict("The observed failed execution changed")
                targets.append(self._failure_target(root, RECOVERY))
                generation = result["generation"] + 1
                outcome = {
                    "source_generation": result["generation"],
                    "version": VERSION,
                    "targets": targets,
                }
                db.execute(
                    text(
                        "INSERT INTO por_domain.retries(run_id,key,version,generation,outcome) VALUES (:id,:k,:v,:g,:o)"
                    ),
                    {
                        "id": run_id,
                        "k": key,
                        "v": expected_version,
                        "g": generation,
                        "o": dumps(outcome),
                    },
                )
                repo.publish(db, run_id, "retry_started", generation=generation)
                self.client.send_in_transaction(
                    db,
                    run_id + ":finalize",
                    {
                        "key": key,
                        "generation": generation,
                        "version": expected_version,
                        "outcome": digest(outcome),
                    },
                    topic="retry",
                    idempotency_key=key,
                )
        return self.get(run_id, owner)

    def reconcile(self, run_id, owner, expected_version, *, worker_stopped, note):
        result = self.get(run_id, owner)
        if (
            not worker_stopped
            or not note.strip()
            or len(note) > 2000
            or result["state_version"] != expected_version
            or result["status"] != "interrupted"
        ):
            raise RunConflict(
                "Verify executor death and authoritative effects for the observed interrupted Run"
            )
        with self.facts.engine.begin() as db:
            self.facts.lock(db)
            observed = self.facts.request(run_id, owner, db=db, lock=True)
            if observed["released"]:
                raise RunConflict("Quarantine was already reconciled")
            db.execute(
                text("UPDATE por_domain.units SET permit=:p WHERE run_id=:id"),
                {"id": run_id, "p": uuid4().hex},
            )
            db.execute(
                text(
                    "UPDATE por_domain.requests SET occupied=false,released=:n WHERE id=:id"
                ),
                {
                    "id": run_id,
                    "n": dumps({"owner": owner, "note": note, "worker_stopped": True}),
                },
            )
            self.facts.publish(db, run_id, "reconciled", retry_safe=False)
        return self.get(run_id, owner)


@lru_cache(maxsize=1)
def get_run_client():
    return RunClient()


def close_run_client():
    if facts.cache_info().currsize:
        facts().close()
    facts.cache_clear()
    get_run_client.cache_clear()
