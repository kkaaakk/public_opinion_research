"""Deterministic Temporal shell: all project I/O stays in the Activity."""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy


@workflow.defn
class ResearchRun:
    def __init__(self):
        self.version = ""
        self.approved = False

    @workflow.query
    def pending_plan(self) -> str:
        return self.version

    @workflow.update
    def approve(self, version: str) -> str:
        self.approved = True
        return "approved"

    @approve.validator
    def validate_approval(self, version: str) -> None:
        if not self.version or self.approved or version != self.version:
            raise ValueError("approval version is absent, stale or consumed")

    @workflow.run
    async def run(self, value: dict) -> dict:
        while value["cursor"] not in {"done", "interrupted", "cancelled"}:
            value = await workflow.execute_activity(
                "phase4_segment",
                value,
                start_to_close_timeout=timedelta(seconds=120),
                heartbeat_timeout=timedelta(seconds=15),
                retry_policy=RetryPolicy(
                    initial_interval=timedelta(seconds=1), maximum_attempts=3
                ),
                cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
            )
            if value["cursor"] == "approval":
                self.version = value["plan_version"]
                if value.get("require_approval"):
                    await workflow.wait_condition(
                        lambda: self.approved, timeout=timedelta(seconds=120)
                    )
                self.version = ""
                value = {**value, "cursor": "research_init"}
            if value["cursor"] == "research_review":
                await workflow.execute_activity(
                    "phase4_gate",
                    value["directory"],
                    start_to_close_timeout=timedelta(seconds=120),
                    heartbeat_timeout=timedelta(seconds=15),
                    retry_policy=RetryPolicy(
                        initial_interval=timedelta(seconds=1), maximum_attempts=3
                    ),
                )
        return value
