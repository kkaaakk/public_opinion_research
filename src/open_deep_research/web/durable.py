"""Run API admission and projection over native DBOS status/messages/streams."""

from __future__ import annotations

from functools import lru_cache
from importlib.metadata import version as installed_version
from uuid import uuid4

from sqlalchemy import text

from open_deep_research.budget import merge_budget_usage, model_bill
from open_deep_research.configuration import Configuration
from open_deep_research.observability.telemetry import (
    business_span,
    correlation,
    enqueue_context,
)
from open_deep_research.runtime.sdk_runtime import SDK_RUNTIME_VERSION
from open_deep_research.web.run_data import (
    IDEMPOTENCY_KEY_PATTERN,
    RunConflict,
    RunNotFound,
    db_time,
    digest,
    dumps,
    legacy_projection,
    loads,
    public_payload,
    publish_event,
    read_request,
    retry_generation,
    role_deliverables,
    timestamp,
)
from open_deep_research.workflow.brief import (
    MAX_BRIEF_FEEDBACK_CHARS,
    MAX_BRIEF_REVISIONS,
)
from open_deep_research.workflow.dbos_config import (
    ACTIVE,
    ARCHIVE,
    CONTROL,
    SDK_VERSION,
    SETTLEMENT_GRACE_SECONDS,
    VERSION,
    close_connections,
    domain_engine,
    native_client,
    settings,
)
from open_deep_research.workflow.domain_schema import (
    admission_lock,
    require_matching_journals,
    verify_policy,
)
from open_deep_research.workflow.python_state import copy_state

NATIVE_TERMINAL = {"SUCCESS", "ERROR", "CANCELLED", "MAX_RECOVERY_ATTEMPTS_EXCEEDED"}


def validate_request(request):
    data = request["data"]
    effective = Configuration.from_runnable_config(data["config"]).model_dump(
        mode="json"
    )
    if (
        data["version"] != VERSION
        or data["sdk"] != SDK_VERSION
        or installed_version("openai-agents") != SDK_VERSION
        or data["runtime"] != SDK_RUNTIME_VERSION
        or digest(effective) != data["configuration"]
    ):
        raise RunConflict(
            "Accepted configuration/version changed; drain using the original release"
        )


def external_effects(db, run_id):
    return db.execute(
        text("""
        SELECT EXISTS (SELECT 1 FROM por_domain.operations WHERE run_id=:r AND outcome IS NULL),
               EXISTS (SELECT 1 FROM por_domain.operations WHERE run_id=:r AND outcome='unknown')
        """),
        {"r": run_id},
    ).one()


class RunClient:
    """Own request admission and user commands over the native DBOS Run."""

    def __init__(self):
        self.engine = domain_engine()
        self.client = native_client()

    def _admit(self, db, owner, session_id, *, excluding=None):
        if session_id:
            requests = db.execute(
                text("""
                SELECT id, owner, reconciliation FROM por_domain.requests WHERE session=:s
                """),
                {"s": session_id},
            ).all()
            if not requests or any(row.owner != owner for row in requests):
                raise RunNotFound("Session not found")
            ids = [row.id for row in requests if row.id != excluding]
            if ids and self.client.list_workflows(
                workflow_ids=ids,
                status=list(ACTIVE),
                load_input=False,
                load_output=False,
                limit=1,
            ):
                raise RunConflict("Session already has an active research Run")
            for run_id, _, reconciliation in requests:
                if run_id == excluding or reconciliation:
                    continue
                if (
                    retry_generation(db, run_id)
                    > self._retry_receipt(run_id)["generation"]
                    or any(external_effects(db, run_id))
                    or model_bill(db, run_id)[1]
                    or (
                        self.client.get_event(
                            run_id + ":events", "projection", timeout_seconds=0
                        )
                        or {}
                    )
                    .get("error", {})
                    .get("unknown_outcome")
                ):
                    raise RunConflict(
                        "Session has a retry or external effect awaiting settlement"
                    )
        if (
            len(
                self.client.list_workflows(
                    name="por.run",
                    status=list(ACTIVE),
                    load_input=False,
                    load_output=False,
                    limit=settings().queue_capacity,
                )
            )
            >= settings().queue_capacity
        ):
            raise RunConflict("Research queue is full")

    def _retry_receipt(self, run_id):
        receipt = self.client.get_event(
            run_id + ":finalize", "retry_applied", timeout_seconds=0
        )
        return receipt if isinstance(receipt, dict) else {"generation": receipt or 0}

    def create(self, owner, key, request, config, state, *, session_id=None):
        if not owner or len(owner) > 128 or not key or len(key) > 128:
            raise RunConflict("Owner and idempotency key are required")
        fingerprint = digest({"request": request, "session_id": session_id})
        with self.engine.begin() as db:
            admission_lock(db)
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
                require_matching_journals()
                self._admit(db, owner, session_id)
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
                # Freeze the accepted report topology before any journal exists.
                cfg["configurable"]["report_mode"] = effective["report_mode"]
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
                    "created": db_time(db),
                    "approval_seconds": settings().approval_seconds,
                    "run_seconds": settings().run_seconds,
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
                publish_event(db, run_id, "run_status", status="queued")
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
        req = read_request(run_id, owner)
        status = self.native(run_id)
        relay = run_id + ":events"
        event = self.client.get_event
        # Read phase before the payloads the workflow publishes first.
        phase = event(run_id, "phase", timeout_seconds=0)
        plan = event(run_id, "plan", timeout_seconds=0)
        clarification = (
            event(run_id, "clarification", timeout_seconds=0)
            if phase == "awaiting_clarification"
            and status.status == "PENDING"
            and not req["cancel_requested"]
            else None
        )
        before_receipts = event(relay, "cursor", timeout_seconds=0) or 0
        history = event(relay, "history", timeout_seconds=0)
        delivered = event(relay, "done", timeout_seconds=0)
        view = event(relay, "projection", timeout_seconds=0) or {}
        terminal = status.status in NATIVE_TERMINAL
        legacy = req["data"]["version"] != VERSION
        with self.engine.connect() as db:
            generation = retry_generation(db, run_id)
            if clarification and db_time(db) >= clarification["expires_at"]:
                clarification = None
            if legacy:
                historic, charges, unknown = legacy_projection(db, run_id)
                view = view | historic
                open_effect, complete = False, not unknown
            else:
                charges, pending_model, complete = model_bill(db, run_id)
                open_effect, unknown = external_effects(db, run_id)
                open_effect = bool(open_effect or pending_model)
            expired = bool(
                open_effect
                and terminal
                and status.completed_at is not None
                and db_time(db) - status.completed_at / 1000 >= SETTLEMENT_GRACE_SECONDS
            )
        retry_receipt = self._retry_receipt(run_id)
        applied = retry_receipt["generation"]
        error = dict(view.get("error", {}))
        missing_checkpoint = error.pop("unknown_outcome", False)
        unknown = bool(unknown or missing_checkpoint)
        if open_effect:
            unknown = True
        pending = (
            open_effect
            and not missing_checkpoint
            and not req["reconciliation"]
            and not expired
        )
        settlement = "pending" if pending else "unresolved" if unknown else "settled"
        native_state = {
            "ENQUEUED": "queued",
            "DELAYED": "queued",
            "PENDING": "running",
            "SUCCESS": "completed",
            "ERROR": "failed",
            "CANCELLED": "cancelled",
        }.get(status.status, "interrupted")
        approval = event(run_id, "approval_end", timeout_seconds=0)
        if status.status == "SUCCESS" and approval in {"reject", "expired"}:
            native_state = "cancelled"
        if not terminal and phase in {"awaiting_approval", "awaiting_clarification"}:
            native_state = phase
        if (
            status.status == "ERROR"
            and req["cancel_requested"]
            and retry_receipt.get("outcome") == "cancelled"
        ):
            native_state = "cancelled"
        if terminal and unknown and not pending:
            native_state = "interrupted"
            error.setdefault("kind", "unconfirmed_effect")
        elif status.status == "ERROR":
            error.setdefault("kind", "execution_error")
        if req["cancel_requested"] and (not terminal or pending):
            native_state = "cancelling"
        if error or unknown:
            error["usage_complete"] = complete and not unknown
        usage = merge_budget_usage(
            req["data"]["initial"].get("runtime", {}).get("budget", {}), charges
        )
        if not legacy:
            usage = merge_budget_usage(
                usage,
                {
                    k: view[k]
                    for k in (
                        "tool_calls",
                        "search_calls",
                        "budget_warnings",
                        "degradation_reasons",
                    )
                    if k in view
                },
            )
        if not complete:
            usage["usage_complete"] = False
        report = view.get("legacy_report", "")
        if seq := view.get("report"):
            report = self._stream_item(relay, seq)["content"]
        if not report and native_state == "completed":
            # The Workflow can commit before the separate SSE relay publishes
            # its public index. Read the completed report from its native result.
            outcome = self.client.retrieve_workflow(run_id).get_result()
            report = (
                outcome.get("report", {}).get("final", "")
                if isinstance(outcome, dict)
                else ""
            )
        roles = list(view.get("legacy_roles", []))
        for stage, seq in view.get("roles", {}).items():
            item = self._stream_item(relay, seq)
            roles.append(
                dict(
                    role=item["role"],
                    content=item["content"],
                    stage_id=stage,
                    completed_at="",
                )
            )
        if (
            terminal
            and not legacy
            and (not delivered or delivered.get("generation", 0) != generation)
        ):
            # The native child result already exists while the public relay
            # catches up. Extract only the report from each missing child.
            published = view.get("roles", {})
            for child in self.client.list_workflows(
                name="por.role",
                workflow_id_prefix=run_id + ":",
                status="SUCCESS",
                load_input=False,
                load_output=True,
            ):
                if child.workflow_id in published or not child.output:
                    continue
                roles.extend(
                    dict(
                        role=role,
                        content=content,
                        stage_id=child.workflow_id,
                        completed_at="",
                    )
                    for role, content in role_deliverables(child.output).items()
                )
        current = (
            error.get("step")
            or view.get("current_step")
            or next(iter(reversed(view.get("active", {}))), None)
        )
        current = view.get("active", {}).get(current, current) or ""
        if native_state == "completed":
            current = ""
        delivered_phase = event(relay, "phase", timeout_seconds=0)
        # The relay commits cursor before its terminal receipts.
        cursor = max(before_receipts, event(relay, "cursor", timeout_seconds=0) or 0)
        if isinstance(history, dict):
            history = history["id"] if history["generation"] == generation else None
        locked_brief = event(run_id, "brief_locked", timeout_seconds=0)
        revision = plan.get("ai_revision") if plan else None
        revision_pending = (
            phase == "revising_brief"
            and status.status == "PENDING"
            and not req["cancel_requested"]
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
            "events_paused": delivered_phase
            in {"awaiting_approval", "awaiting_clarification"},
            "clarification": dict(
                clarification, expires_at=timestamp(clarification["expires_at"])
            )
            if clarification
            else None,
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
            "completed_steps": view.get(
                "completed_steps", list(view.get("completed", {}).values())
            ),
            "workflow_backend": "dbos",
            "business_agent_runtime": "openai_agents",
            "execution_engine": "dbos",
            "workflow_version": req["data"]["version"],
            "sdk_version": SDK_VERSION,
            "runtime_version": SDK_RUNTIME_VERSION,
            "usage": usage,
            "settlement": settlement,
            "error": error,
            "report": report,
            "role_reports": roles,
            "archive_status": "saved" if history else "pending" if terminal else "none",
            "history_id": history,
            "plan": dict(
                version=plan["version"],
                revision=plan.get("revision", 1),
                content_hash=plan.get("content_hash", ""),
                locked=locked_brief == plan["version"],
                brief=plan["brief"],
                sections=[
                    s.model_dump(mode="json") if hasattr(s, "model_dump") else s
                    for s in plan["sections"]
                ],
                expires_at=timestamp(plan["expires_at"]),
                ai_revision=dict(
                    used=revision["used"],
                    error=revision.get("error"),
                    limit=MAX_BRIEF_REVISIONS,
                    pending=revision_pending,
                )
                if revision is not None
                else None,
            )
            if plan
            else None,
            "retry_safe": status.status == "ERROR"
            and status.app_version == VERSION
            and not req["cancel_requested"]
            and generation == applied
            and not unknown
            and error.get("retry_safe", False),
            "created_at": timestamp(req["data"]["created"]),
            "updated_at": timestamp((status.updated_at or 0) / 1000),
            "terminated_at": timestamp((status.updated_at or 0) / 1000)
            if terminal
            else None,
        }

    def _stream_item(self, relay, seq):
        item = self.client.read_stream_offset(
            relay, "events", seq - 1, timeout_seconds=0
        )
        if item is None:
            raise RunConflict(
                "Durable event prefix is not available; retain the current cursor"
            )
        return item

    def read_events(self, run_id, owner, after, limit=100):
        read_request(run_id, owner)
        cursor = (
            self.client.get_event(run_id + ":events", "cursor", timeout_seconds=0) or 0
        )
        if not isinstance(after, int) or after < 0 or after > cursor:
            raise RunConflict("Invalid event cursor")
        return [
            public_payload(self._stream_item(run_id + ":events", seq))
            for seq in range(after + 1, min(cursor, after + limit) + 1)
        ]

    def _human_command(self, run_id, owner, version, topic, payload, *, key=None):
        req = read_request(run_id, owner)
        validate_request(req)
        native = self.native(run_id)
        phase, event = (
            ("awaiting_approval", "plan")
            if topic == "approval"
            else ("awaiting_clarification", "clarification")
        )
        if (
            req["cancel_requested"]
            or native.status != "PENDING"
            or native.app_version != VERSION
            or self.client.get_event(run_id, "phase", timeout_seconds=0) != phase
        ):
            raise RunConflict("Run is no longer waiting for this interaction")
        pending = self.client.get_event(run_id, event, timeout_seconds=0)
        if not pending or pending["version"] != version:
            raise RunConflict("Interaction version changed")
        with self.engine.connect() as db:
            if db_time(db) >= pending["expires_at"]:
                raise RunConflict("Interaction expired")

        command = dict(owner=owner, version=version, **payload)
        if key is not None:
            revision = pending.get("ai_revision")
            if not revision or revision["used"] >= MAX_BRIEF_REVISIONS:
                raise RunConflict("Brief AI revision limit reached")
            key_hash = digest(dict(run_id=run_id, owner=owner, key=key))
            command["revision_key_hash"] = key_hash
            message_key = f"{topic}:revise:{key_hash}"
        else:
            if topic == "clarification":
                from open_deep_research.workflow.brief import validate_answers

                try:
                    validate_answers(
                        pending["questions"], payload["answers"], payload["skip"]
                    )
                except ValueError as exc:
                    raise RunConflict(str(exc)) from exc
            message_key = f"{topic}:{digest(command)}"

        # The workflow rechecks version and deadline when consuming a message.
        self.client.send(run_id, command, topic=topic, idempotency_key=message_key)

    def approve(
        self,
        run_id,
        owner,
        plan_version,
        decision,
        brief_md=None,
        *,
        feedback=None,
        key=None,
    ):
        if decision not in {"approve", "reject", "edit", "revise"}:
            raise RunConflict("Invalid approval decision")
        payload = dict(decision=decision)
        if decision == "revise":
            if not isinstance(key, str) or not IDEMPOTENCY_KEY_PATTERN.fullmatch(key):
                raise RunConflict(
                    "A bounded Idempotency-Key is required for Brief revision"
                )
            if (
                not isinstance(feedback, str)
                or not feedback.strip()
                or len(feedback) > MAX_BRIEF_FEEDBACK_CHARS
            ):
                raise RunConflict("Brief revision requires bounded feedback")
            payload["feedback"] = feedback.strip()
        elif feedback is not None or key is not None:
            raise RunConflict("Feedback and Idempotency-Key require a revise decision")
        if decision == "edit":
            from open_deep_research.workflow.brief import validate_brief

            try:
                payload["brief_md"] = validate_brief(brief_md)
            except ValueError as exc:
                raise RunConflict(str(exc)) from exc
        elif brief_md is not None:
            raise RunConflict(
                "Brief edits must be saved before approving their new version"
            )
        self._human_command(run_id, owner, plan_version, "approval", payload, key=key)
        return self.get(run_id, owner)

    def clarify(self, run_id, owner, version, answers, skip=False):
        self._human_command(
            run_id,
            owner,
            version,
            "clarification",
            dict(answers=answers, skip=skip),
        )
        return self.get(run_id, owner)

    def cancel(self, run_id, owner):
        with self.engine.begin() as db:
            req = read_request(run_id, owner, db=db, lock=True)
            if not req["cancel_requested"] and (
                self.native(run_id).status not in NATIVE_TERMINAL
                or retry_generation(db, run_id)
                > self._retry_receipt(run_id)["generation"]
            ):
                if req["data"]["version"] != VERSION:
                    raise RunConflict("Cancel using the original application release")
                db.execute(
                    text(
                        "UPDATE por_domain.requests SET cancel_requested=true WHERE id=:id"
                    ),
                    {"id": run_id},
                )
                self.client.enqueue_in_transaction(
                    db,
                    {
                        "workflow_name": "por.cancel",
                        "queue_name": CONTROL,
                        "workflow_id": run_id + ":cancel",
                        "app_version": VERSION,
                        "authenticated_user": owner,
                    },
                    run_id,
                )
        return self.get(run_id, owner)

    def retry(self, run_id, owner, key, expected_version):
        from open_deep_research.workflow.dbos_workflow import retry_targets

        if not isinstance(key, str) or not IDEMPOTENCY_KEY_PATTERN.fullmatch(key):
            raise RunConflict("Retry requires a bounded Idempotency-Key")
        with self.engine.begin() as db:
            admission_lock(db)
            req = read_request(run_id, owner, db=db, lock=True)
            version = db.scalar(
                text(
                    "SELECT version FROM por_domain.retries WHERE run_id=:id AND key=:k"
                ),
                {"id": run_id, "k": key},
            )
            if version is not None:
                if version != expected_version:
                    raise RunConflict("Retry key was used for another observed version")
            else:
                result = self.get(run_id, owner)
                if (
                    result["state_version"] != expected_version
                    or not result["retry_safe"]
                    or not result["events_complete"]
                ):
                    raise RunConflict("Run requires reconciliation; no safe retry")
                validate_request(req)
                self._admit(db, owner, req["session"], excluding=run_id)
                targets = retry_targets(run_id)
                generation = result["generation"] + 1
                grant = {
                    "source_generation": generation - 1,
                    "version": VERSION,
                    "targets": targets,
                }
                db.execute(
                    text("""
                    INSERT INTO por_domain.retries(run_id,key,version,generation,outcome)
                    VALUES (:id,:k,:v,:g,:o)
                    """),
                    dict(
                        id=run_id,
                        k=key,
                        v=expected_version,
                        g=generation,
                        o=dumps(grant),
                    ),
                )
                publish_event(db, run_id, "retry_started", generation=generation)
                self.client.send_in_transaction(
                    db,
                    run_id + ":finalize",
                    {
                        "key": key,
                        "generation": generation,
                        "version": expected_version,
                        "outcome": digest(grant),
                    },
                    topic="retry",
                    idempotency_key=key,
                )
        return self.get(run_id, owner)

    def reconcile(self, run_id, owner, expected_version, *, worker_stopped, note):
        with self.engine.begin() as db:
            admission_lock(db)
            req = read_request(run_id, owner, db=db, lock=True)
            result = self.get(run_id, owner)
            if (
                req["reconciliation"]
                or not worker_stopped
                or not note.strip()
                or len(note) > 2000
                or result["state_version"] != expected_version
                or result["status"] != "interrupted"
                or result["retry_requested"]
            ):
                raise RunConflict(
                    "Verify executor death and authoritative effects for the observed interrupted Run"
                )
            db.execute(
                text(
                    "UPDATE por_domain.requests SET reconciliation=:receipt WHERE id=:id"
                ),
                dict(
                    id=run_id,
                    receipt=dumps(
                        {"owner": owner, "note": note, "worker_stopped": True}
                    ),
                ),
            )
            # The note releases Session admission. Original external evidence remains available.
            if not result["events_complete"] and result["workflow_version"] == VERSION:
                publish_event(db, run_id, "reconciled", retry_safe=False)
        return self.get(run_id, owner)


@lru_cache(maxsize=1)
def get_run_client():
    verify_policy()
    return RunClient()


def close_run_client():
    get_run_client.cache_clear()
    close_connections()
