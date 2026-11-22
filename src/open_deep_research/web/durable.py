"""Run API admission and projection over native DBOS status/messages/streams."""

from __future__ import annotations

from contextlib import closing, suppress
from functools import lru_cache
from importlib.metadata import version as installed_version
from itertools import islice
from uuid import uuid4

from dbos import error as dbos_error
from sqlalchemy import text

from open_deep_research.budget import (
    compose_run_usage,
    merge_budget_usage,
    model_bill,
    tool_ledger,
)
from open_deep_research.config_file import effective_config, non_sensitive_config
from open_deep_research.configuration import Configuration
from open_deep_research.observability.telemetry import (
    business_span,
    correlation,
    enqueue_context,
)
from open_deep_research.runtime.sdk_runtime import SDK_RUNTIME_VERSION
from open_deep_research.web.observations import stage_errors, stage_results
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
HUMAN_PHASES = {"awaiting_approval", "awaiting_clarification"}
NATIVE_RESULT_VERSIONS = {
    VERSION,
    "por-agent-context-v1",
    "por-research-agents-v1",
    "por-sdk-models-v1",
}
DOMAIN_LEDGER_VERSIONS = NATIVE_RESULT_VERSIONS | {"dbos-domain-v1"}


def validate_request(request):
    data = request["data"]
    try:
        effective = Configuration.from_runnable_config(data["config"]).model_dump(
            mode="json"
        )
    except ValueError as exc:
        raise RunConflict(
            "Accepted configuration is no longer valid; drain using the original release"
        ) from exc
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


def request_fingerprint(request, configuration, session_id=None):
    """Hash one request with its non-sensitive effective configuration digest."""
    return digest(
        {
            "request": request,
            "session_id": session_id,
            "configuration": configuration,
        }
    )


def matches_stored_request(
    stored,
    *,
    request,
    fingerprint_configuration,
    configuration=None,
    session_id=None,
    config_source=None,
):
    """Return whether a stored admission accepts this replay.

    Pre-upgrade records used the request, session and config path only. They are
    still replayable while their frozen configuration digest matches the current
    effective configuration; a changed YAML always requires a new key.
    """
    if stored.get("fingerprint") == request_fingerprint(
        request, fingerprint_configuration, session_id
    ):
        return True
    legacy = digest(
        {
            "request": request,
            "session_id": session_id,
            "configuration_source": config_source or "",
        }
    )
    return (
        configuration is not None
        and stored.get("fingerprint") == legacy
        and stored.get("configuration") == configuration
    )


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
            statuses = (
                {
                    wf.workflow_id: wf
                    for wf in self.client.list_workflows(
                        workflow_ids=ids, load_input=False, load_output=False
                    )
                }
                if ids
                else {}
            )
            if any(wf.status in ACTIVE for wf in statuses.values()):
                raise RunConflict("Session already has an active research Run")
            for run_id, _, reconciliation in requests:
                if run_id == excluding or reconciliation:
                    continue
                if (
                    retry_generation(db, run_id)
                    > self._retry_receipt(run_id)["generation"]
                    or any(external_effects(db, run_id))
                    or model_bill(db, run_id)[1]
                    or self._unconfirmed_stage(statuses.get(run_id))
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

    def create(
        self, owner, key, request, config, state, *, session_id=None, config_source=None
    ):
        if not owner or len(owner) > 128 or not key or len(key) > 128:
            raise RunConflict("Owner and idempotency key are required")
        # Freeze one complete, non-sensitive business snapshot before admission.
        # Every Executor, Agent and child Workflow re-reads this snapshot and
        # never the mutable config.yaml or ambient environment.
        effective = effective_config(config)
        configuration = digest(effective)
        fingerprint_configuration = digest(non_sensitive_config(effective))
        fingerprint = request_fingerprint(
            request, fingerprint_configuration, session_id
        )
        with self.engine.begin() as db:
            admission_lock(db)
            old = db.execute(
                text(
                    "SELECT id,value FROM por_domain.requests WHERE owner=:o AND request_key=:k"
                ),
                {"o": owner, "k": key},
            ).first()
            if old:
                if not matches_stored_request(
                    loads(old[1]),
                    request=request,
                    fingerprint_configuration=fingerprint_configuration,
                    configuration=configuration,
                    session_id=session_id,
                    config_source=config_source,
                ):
                    raise RunConflict(
                        "Idempotency key was used for another request or "
                        "configuration; use a new key"
                    )
                run_id = old[0]
                created = False
            else:
                require_matching_journals()
                self._admit(db, owner, session_id)
                cfg = {"configurable": effective}
                run_id = uuid4().hex
                session_id = session_id or uuid4().hex
                cfg["configurable"].update(
                    research_run_id=run_id, thread_id=session_id, user_id=owner
                )
                # Assign missing message identities before entering a Workflow.
                state = copy_state(state)
                data = {
                    "request": request,
                    "fingerprint": fingerprint,
                    "config": cfg,
                    "initial": state,
                    "configuration": configuration,
                    "configuration_source": config_source or "",
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

    def _unconfirmed_stage(self, workflow):
        if workflow is None or workflow.status == "SUCCESS":
            return False
        if workflow.app_version not in NATIVE_RESULT_VERSIONS:
            old = self.client.get_event(
                workflow.workflow_id + ":events", "projection", 0
            )
            return bool(old and old.get("error", {}).get("unknown_outcome"))
        return any(
            error.get("unknown_outcome")
            for error in stage_errors(self.client, workflow.workflow_id)
        )

    def _old_results(self, run_id, view):
        """Read retained relay indexes and artifacts from drained releases."""
        report = view.get("legacy_report", "")
        if seq := view.get("report"):
            report = self.client.read_stream_offset(
                run_id + ":events", "events", seq - 1, timeout_seconds=0
            )["content"]
        roles = list(view.get("legacy_roles", []))
        for stage, seq in view.get("roles", {}).items():
            item = self.client.read_stream_offset(
                run_id + ":events", "events", seq - 1, timeout_seconds=0
            )
            roles.append(
                dict(
                    role=item["role"],
                    content=item["content"],
                    stage_id=stage,
                    completed_at="",
                )
            )
        active = view.get("active", {})
        return dict(
            completed_steps=view.get(
                "completed_steps", list(view.get("completed", {}).values())
            ),
            current_step=view.get("current_step")
            or next(reversed(active.values()), ""),
            role_reports=roles,
            report=report,
            stage_usage={
                key: view[key]
                for key in (
                    "tool_calls",
                    "search_calls",
                    "budget_warnings",
                    "degradation_reasons",
                )
                if key in view
            },
        )

    def outcome(self, run_id):
        """Read business output and settlement independently of Stream delivery."""
        return self._outcome(read_request(run_id), self.native(run_id))

    def _outcome(self, req, native):
        run_id = req["id"]
        data = req["data"]
        version = data["version"]
        terminal = native.status in NATIVE_TERMINAL
        old = {}
        if version not in NATIVE_RESULT_VERSIONS:
            old = self.client.get_event(run_id + ":events", "projection", 0) or {}
        with self.engine.connect() as db:
            generation = retry_generation(db, run_id)
            tool_counts: dict[str, int] = {}
            if version not in DOMAIN_LEDGER_VERSIONS:
                historic, charges, unknown = legacy_projection(db, run_id)
                old |= historic
                open_effect, complete = False, not unknown
            else:
                charges, pending_model, complete = model_bill(db, run_id)
                tool_counts = tool_ledger(db, run_id)
                open_effect, unknown = external_effects(db, run_id)
                open_effect = bool(open_effect or pending_model)
            expired = (
                terminal
                and native.completed_at is not None
                and open_effect
                and (
                    db_time(db) - native.completed_at / 1000 >= SETTLEMENT_GRACE_SECONDS
                )
            )

        applied = self._retry_receipt(run_id) if generation else {"generation": 0}
        errors = (
            stage_errors(self.client, run_id)
            if version in NATIVE_RESULT_VERSIONS and terminal
            else [old["error"]]
            if old.get("error")
            else []
        )
        error = dict(next(iter(errors), {}))
        error.pop("unknown_outcome", None)
        missing_checkpoint = any(err.get("unknown_outcome") for err in errors)
        pending = open_effect and not (
            missing_checkpoint or req["reconciliation"] or expired
        )
        unknown = bool(unknown or missing_checkpoint or open_effect)
        settlement = "pending" if pending else "unresolved" if unknown else "settled"
        status = {
            "ENQUEUED": "queued",
            "DELAYED": "queued",
            "PENDING": "running",
            "SUCCESS": "completed",
            "ERROR": "failed",
            "CANCELLED": "cancelled",
        }.get(native.status, "interrupted")
        if native.status == "SUCCESS" and self.client.get_event(
            run_id, "approval_end", 0
        ) in {"reject", "expired"}:
            status = "cancelled"
        if req["cancel_requested"]:
            if not terminal or pending:
                status = "cancelling"
            elif applied.get("outcome") == "cancelled":
                status = "cancelled"
        if terminal and unknown and not pending:
            status = "interrupted"
            error.setdefault("kind", "unconfirmed_effect")
        elif status == "failed":
            error.setdefault("kind", "execution_error")
        if error or unknown:
            error["usage_complete"] = complete and not unknown

        view = (
            stage_results(self.client, run_id)
            if version in NATIVE_RESULT_VERSIONS
            else self._old_results(run_id, old)
        )
        if not view["report"] and native.status == "SUCCESS":
            outcome = self.client.retrieve_workflow(run_id).get_result()
            if isinstance(outcome, dict):
                view["report"] = outcome.get("report", {}).get("final", "")
        notes = view.pop("stage_usage")
        initial_usage = data["initial"].get("runtime", {}).get("budget", {})
        if version not in DOMAIN_LEDGER_VERSIONS:
            # Drained releases keep their historical projection, counters included.
            usage = merge_budget_usage(
                merge_budget_usage(initial_usage, charges), notes
            )
        else:
            usage = compose_run_usage(initial_usage, charges, tool_counts, notes)
        if not complete:
            usage["usage_complete"] = False
        settled_at = native.completed_at or native.updated_at or 0
        return dict(
            view,
            status=status,
            native_status=native.status,
            settlement=settlement,
            usage=usage,
            error=error,
            current_step=""
            if status == "completed"
            else error.get("step") or view["current_step"],
            owner=req["owner"],
            research_session_id=req["session"],
            archive_request=data["request"],
            generation=generation,
            retry_requested=generation > applied["generation"],
            retry_safe=(
                native.status == "ERROR"
                and native.app_version == VERSION
                and generation == applied["generation"]
                and not req["cancel_requested"]
                and not unknown
                and error.get("retry_safe", False)
            ),
            created_at=timestamp(data["created"]),
            updated_at=timestamp((native.updated_at or 0) / 1000),
            terminated_at=timestamp(settled_at / 1000) if terminal else None,
        )

    def _delivery(self, run_id, version):
        relay = run_id + ":events"
        if version in NATIVE_RESULT_VERSIONS:
            return self.client.get_event(relay, "delivery", 0) or {
                "offset": 0,
                "generation": 0,
            }
        history = self.client.get_event(relay, "history", 0)
        done = self.client.get_event(relay, "done", 0)
        return {
            "offset": self.client.get_event(relay, "cursor", 0) or 0,
            "generation": (done or {}).get("generation", 0),
            "terminal": bool(done),
            "pause": {"phase": self.client.get_event(relay, "phase", 0)},
            "history_id": history.get("id")
            if isinstance(history, dict)
            and history.get("generation", 0) == (done or {}).get("generation", 0)
            else history
            if isinstance(history, str)
            else None,
        }

    def get(self, run_id, owner):
        req = read_request(run_id, owner)
        native = self.native(run_id)
        phase = self.client.get_event(run_id, "phase", 0)
        plan = self.client.get_event(run_id, "plan", 0)
        result = self._outcome(req, native)
        version = req["data"]["version"]
        delivery = self._delivery(run_id, version)
        same_generation = delivery["generation"] == result["generation"]
        cursor = delivery["offset"]
        history_id = delivery.get("history_id") if same_generation else None
        if result["status"] == "running" and phase in HUMAN_PHASES:
            result["status"] = phase
        clarification = None
        if result["status"] == "awaiting_clarification":
            clarification = self.client.get_event(run_id, "clarification", 0)
            if clarification:
                with self.engine.connect() as db:
                    if db_time(db) >= clarification["expires_at"]:
                        clarification = None
        request = result.pop("archive_request")
        terminal = native.status in NATIVE_TERMINAL
        return dict(
            result,
            approval_required=bool(
                req["data"]["config"]["configurable"].get("allow_plan_feedback")
            ),
            request={
                key: request[key]
                for key in ("topic", "model", "mode", "rag_enabled", "require_approval")
                if key in request
            },
            run_id=run_id,
            attempt_id=run_id,
            cancel_requested=req["cancel_requested"],
            state_version=max(1, cursor),
            event_cursor=cursor,
            seq=cursor,
            events_complete=same_generation and bool(delivery.get("terminal")),
            events_paused=self._pause_delivered(
                delivery,
                result["status"],
                clarification or plan,
                version,
            ),
            clarification=dict(
                clarification, expires_at=timestamp(clarification["expires_at"])
            )
            if clarification
            else None,
            history_id=history_id,
            archive_status="saved" if history_id else "pending" if terminal else "none",
            plan=self._public_plan(
                run_id,
                plan,
                phase == "revising_brief" and result["status"] == "running",
            ),
            execution_engine="dbos",
            workflow_backend="dbos",
            workflow_version=version,
            business_agent_runtime="openai_agents",
            sdk_version=SDK_VERSION,
            runtime_version=SDK_RUNTIME_VERSION,
            phase=phase,
            configuration_source=req["data"].get("configuration_source", ""),
        )

    def _public_plan(self, run_id, plan, revision_pending):
        if plan is None:
            return None
        revision = plan.get("ai_revision")
        return dict(
            version=plan["version"],
            revision=plan.get("revision", 1),
            content_hash=plan.get("content_hash", ""),
            locked=self.client.get_event(run_id, "brief_locked", 0) == plan["version"],
            brief=plan["brief"],
            sections=[
                section.model_dump(mode="json")
                if hasattr(section, "model_dump")
                else section
                for section in plan.get("sections", ())
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

    def read_events(self, run_id, owner, after, limit=100):
        read_request(run_id, owner)
        if not isinstance(after, int) or after < 0:
            raise RunConflict("Invalid event cursor")
        relay = run_id + ":events"
        items = []
        # DBOS batches consecutive offsets. An immediate timeout means the
        # active writer has reached the current tail.
        with closing(
            self.client.read_stream(relay, "events", offset=after, timeout_seconds=0)
        ) as stream:
            with suppress(
                dbos_error.DBOSStreamTimeoutError,
                dbos_error.DBOSNonExistentWorkflowError,
            ):
                for seq, item in enumerate(islice(stream, limit), after + 1):
                    items.append(
                        public_payload(
                            dict(
                                item,
                                seq=seq,
                                protocol_version=1,
                                run_id=run_id,
                                attempt_id=run_id,
                            )
                        )
                    )
        if not items and after:
            # Distinguish a valid resume at the tail from a cursor beyond it.
            try:
                self.client.read_stream_offset(
                    relay, "events", after - 1, timeout_seconds=0
                )
            except (
                dbos_error.DBOSStreamTimeoutError,
                dbos_error.DBOSNonExistentWorkflowError,
            ):
                raise RunConflict("Invalid event cursor") from None
        return items

    @staticmethod
    def _pause_delivered(delivery, phase, interaction, version):
        pause = delivery.get("pause") or {}
        return (
            phase in HUMAN_PHASES
            and pause.get("phase") == phase
            and (
                version not in NATIVE_RESULT_VERSIONS
                or interaction is not None
                and pause.get("version") == interaction["version"]
            )
        )

    def stream_finished(self, run_id, owner, after):
        """Confirm EOF from native delivery facts without building a Run view."""
        req = read_request(run_id, owner)
        delivery = self._delivery(run_id, req["data"]["version"])
        if after < delivery["offset"]:
            return False
        if delivery.get("terminal"):
            with self.engine.connect() as db:
                return delivery["generation"] == retry_generation(db, run_id)
        phase = (delivery.get("pause") or {}).get("phase")
        if phase not in HUMAN_PHASES or req["cancel_requested"]:
            return False
        if (
            self.native(run_id).status != "PENDING"
            or self.client.get_event(run_id, "phase", 0) != phase
        ):
            return False
        key = "plan" if phase == "awaiting_approval" else "clarification"
        interaction = self.client.get_event(run_id, key, 0)
        return self._pause_delivered(
            delivery, phase, interaction, req["data"]["version"]
        )

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
