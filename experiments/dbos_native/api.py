"""Minimal authenticated FastAPI backend using native status and stream offsets."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

from codec import NativeJSON, dumps
from dbos import DBOSClient
from env import APP_NAME, DB_URL, VERSION
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError


class Submission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    topic: str = Field(min_length=1, max_length=4000)
    approval: bool = False
    approval_seconds: float = Field(default=120, gt=0, le=86400)
    scenario: str = "normal"
    route: str = "A"


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str = Field(pattern="^[a-f0-9]{64}$")
    decision: str = Field(pattern="^(approve|reject)$")


class Gateway:
    def __init__(self, url=DB_URL, artifact_root=None):
        self.artifact_root = Path(
            artifact_root or Path.cwd() / ".tmp/native-api"
        ).resolve()
        self.sql = create_engine(
            url, pool_pre_ping=True, pool_timeout=2, connect_args={"connect_timeout": 2}
        )
        self.client = DBOSClient(
            system_database_engine=self.sql,
            application_name=APP_NAME,
            serializer=NativeJSON(),
            retry_connection_errors=False,
            observability_query_timeout_sec=2,
        )
        with self.sql.begin() as db:
            db.execute(text("CREATE SCHEMA IF NOT EXISTS native_domain"))
            db.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS native_domain.decisions (run_id text PRIMARY KEY, version text NOT NULL, owner text NOT NULL, decision text NOT NULL)"
                )
            )

    def owned(self, run_id, owner):
        statuses = self.client.list_workflows(workflow_ids=[run_id], load_output=False)
        if not statuses or statuses[0].authenticated_user != owner:
            raise HTTPException(404, "Run not found")
        return statuses[0]

    def submit(self, owner, key, body):
        run_id = hmac.new(
            b"poc-server-identity", f"{owner}:{key}".encode(), "sha256"
        ).hexdigest()
        fingerprint = hashlib.sha256(dumps(body).encode()).hexdigest()
        request = {
            **body,
            "run_id": run_id,
            "owner": owner,
            "directory": str(self.artifact_root / run_id[:12]),
            "fingerprint": fingerprint,
        }
        self.client.enqueue(
            {
                "workflow_name": "native_control",
                "queue_name": "control",
                "workflow_id": run_id,
                "app_version": VERSION,
                "authenticated_user": owner,
            },
            request,
        )
        status = self.owned(run_id, owner)
        if status.input["args"][0].get("fingerprint") != fingerprint:
            raise HTTPException(409, "Idempotency key body conflict")
        return {"run_id": run_id, "status": status.status}

    def approve(self, run_id, owner, version, decision):
        status = self.owned(run_id, owner)
        plan = self.client.get_event(run_id, "plan", timeout_seconds=0)
        if status.status != "PENDING" or not plan or plan["version"] != version:
            raise HTTPException(409, "No matching pending plan")
        try:
            with self.sql.begin() as db:
                now = float(
                    db.execute(
                        text("SELECT extract(epoch from clock_timestamp())")
                    ).scalar_one()
                )
                if now >= plan["expires_at"]:
                    raise HTTPException(409, "Approval expired")
                db.execute(
                    text("INSERT INTO native_domain.decisions VALUES (:id,:v,:o,:d)"),
                    {"id": run_id, "v": version, "o": owner, "d": decision},
                )
                self.client.send_in_transaction(
                    db,
                    run_id,
                    {"version": version, "owner": owner, "decision": decision},
                    topic="approval",
                    idempotency_key=f"approval:{version}",
                )
        except HTTPException:
            raise
        except Exception as exc:
            if type(exc).__name__ == "IntegrityError":
                raise HTTPException(409, "Approval already consumed") from None
            raise
        return {"accepted": True}


def make_app(gateway):
    app = FastAPI()

    @app.exception_handler(SQLAlchemyError)
    async def unavailable(request, exc):
        return JSONResponse(
            {"detail": "Execution database unavailable"}, status_code=503
        )

    def owner(authorization):
        value = {"Bearer native-alice": "alice", "Bearer native-bob": "bob"}.get(
            authorization
        )
        if not value:
            raise HTTPException(401, "Trusted operator token required")
        return value

    @app.post("/runs")
    def create(
        body: Submission,
        authorization: str = Header(default=""),
        idempotency_key: str = Header(),
    ):
        return gateway.submit(owner(authorization), idempotency_key, body.model_dump())

    @app.get("/runs/{run_id}")
    def status(run_id: str, authorization: str = Header(default="")):
        value = gateway.owned(run_id, owner(authorization))
        report = gateway.client.get_event(run_id, "report", timeout_seconds=0)
        return {
            "run_id": run_id,
            "status": value.status,
            "phase": gateway.client.get_event(run_id, "phase", timeout_seconds=0),
            "plan": gateway.client.get_event(run_id, "plan", timeout_seconds=0),
            "report": report,
        }

    @app.post("/runs/{run_id}/approval")
    def approve(run_id: str, body: Decision, authorization: str = Header(default="")):
        return gateway.approve(
            run_id, owner(authorization), body.version, body.decision
        )

    @app.post("/runs/{run_id}/cancel")
    def cancel(run_id: str, authorization: str = Header(default="")):
        gateway.owned(run_id, owner(authorization))
        gateway.client.cancel_workflow(run_id, cancel_children=True)
        return {"requested": True}

    @app.get("/runs/{run_id}/events")
    def events(
        run_id: str,
        authorization: str = Header(default=""),
        last_event_id: int = Header(default=0),
    ):
        gateway.owned(run_id, owner(authorization))
        if last_event_id < 0:
            raise HTTPException(400, "Invalid cursor")

        def stream():
            for seq, value in enumerate(
                gateway.client.read_stream(
                    run_id, "events", offset=last_event_id, timeout_seconds=2
                ),
                start=last_event_id + 1,
            ):
                yield f"id: {seq}\ndata: {json.dumps(value)}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    return app
