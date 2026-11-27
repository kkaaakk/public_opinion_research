"""Kill and replace real Executors. Models and the external provider are fixtures."""

import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest
from dbos import DBOSClient
from manager import BudgetExhausted, UnknownEffect
from postgres_lab import APP, DATABASE_ENV, VERSION, Lab

pytestmark = pytest.mark.postgres
EXPERIMENT = Path(__file__).resolve().parents[1]
EXPECTED_IDS = ["analysis:initial", "knowledge:initial", "public:initial"]
WORKFLOW = "manager-research"


class Executor:
    def __init__(self, lab, directory):
        self.lab = lab
        self.log = directory / "executor.log"
        self.process = None
        self.launches = 0
        self.client = DBOSClient(
            system_database_url=lab.url,
            application_name=APP,
            dbos_system_schema=lab.journal,
        )

    def launch(self, marker=""):
        assert self.process is None
        with self.log.open("a", encoding="utf-8") as output:
            self.process = subprocess.Popen(
                [
                    sys.executable,
                    "-B",
                    "-m",
                    "dbos_worker",
                    self.lab.schema,
                    "--pause-at",
                    marker,
                ],
                cwd=EXPERIMENT,
                stdout=output,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        self.until(
            lambda: self.log.read_text(encoding="utf-8").count("executor ready")
            > self.launches
        )
        self.launches += 1

    def stop(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.kill()
                self.process.wait(timeout=10)
            self.process = None

    def enqueue(self, workflow, *args):
        self.client.enqueue(
            {
                "workflow_name": workflow,
                "queue_name": "poc-research",
                "workflow_id": WORKFLOW,
                "app_version": VERSION,
            },
            self.lab.schema,
            *args,
        )

    def until(self, predicate):
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            assert self.process.poll() is None, self.log.read_text(encoding="utf-8")
            if predicate():
                return
            time.sleep(0.05)
        pytest.fail(self.log.read_text(encoding="utf-8"))

    def paused(self, marker):
        self.until(lambda: self.events("paused:" + marker) == 1)

    def events(self, kind):
        return self.lab.execute(
            "SELECT count(*) FROM observation WHERE kind = %s", kind
        )[0][0]

    def status(self, workflow_id=WORKFLOW):
        selected = self.client.list_workflows(workflow_ids=[workflow_id])
        return selected[0].status if selected else None

    def terminal(self, expected):
        self.until(lambda: self.status() in {"SUCCESS", "ERROR", "CANCELLED"})
        assert self.status() == expected, self.log.read_text(encoding="utf-8")

    def result(self):
        return self.client.retrieve_workflow(WORKFLOW).get_result()

    def steps(self, workflow_id=WORKFLOW):
        return [
            step["function_name"]
            for step in self.client.list_workflow_steps(workflow_id)
        ]

    def attempts(self):
        return self.lab.execute(
            "SELECT id, purpose, tokens FROM model_attempt ORDER BY id"
        )


@pytest.fixture
def experiment(tmp_path, request):
    if DATABASE_ENV not in os.environ:
        pytest.skip(f"Set {DATABASE_ENV} to a test-only PostgreSQL instance.")
    lab = Lab(f"manager_poc_{uuid4().hex[:12]}")
    lab.create(getattr(request, "param", 30))
    executor = Executor(lab, tmp_path)
    try:
        yield executor
    finally:
        executor.stop()
        executor.client.destroy()


@pytest.mark.parametrize(
    "marker", ["after-stage", "after-evidence", "after-receipt", "after-effect"]
)
def test_a_outer_stage_checkpoint_and_business_recovery(experiment, marker):
    worker = experiment
    worker.launch(marker)
    worker.enqueue("poc.outer")
    worker.paused(marker)
    worker.stop()
    before = worker.attempts()
    assert before and all(purpose == "research" for _, purpose, _ in before)
    assert worker.lab.execute("SELECT operation_id FROM external_effect") == [
        ("public:initial",)
    ]
    assert ("poc.research_stage" in worker.steps()) == (marker == "after-stage")
    assert "poc.nested_probe" not in worker.steps()

    worker.launch()
    if marker == "after-effect":
        worker.terminal("ERROR")
        with pytest.raises(UnknownEffect, match="public:initial"):
            worker.result()
        assert worker.attempts() == before
        assert worker.lab.execute("SELECT reference FROM operation") == [(None,)]
        assert "poc.report" not in worker.steps()
        assert worker.events("nested-step-body") == 1
    else:
        worker.terminal("SUCCESS")
        result = worker.result()
        assert result["report"] == "validated report"
        assert result["summary"]["evidence_ids"] == EXPECTED_IDS
        assert result["sdk_requests"] == 9
        after = worker.attempts()
        assert after[: len(before)] == before
        assert [tokens for _, _, tokens in after[len(before) :]] == (
            [14] if marker == "after-stage" else [14] * 10
        )
        assert after[-1][1:] == ("report", 14)
        assert worker.steps() == ["poc.research_stage", "poc.report"]
        if marker == "after-stage":
            assert len(before) == 9
            assert worker.events("research-entered") == 1
            assert worker.events("nested-step-body") == 1
        else:
            assert worker.events("research-entered") == 2
            assert worker.events("nested-step-body") == 2
        if marker == "after-evidence":
            assert len(before) == 5
            assert sum(tokens is None for _, _, tokens in before) == 1
            assert worker.events("evidence-reused:knowledge:initial") == 1
            assert worker.events("evidence-reused:public:initial") == 1
        if marker == "after-receipt":
            assert worker.events("external-lookup:public:initial") == 1
    assert worker.lab.execute("SELECT operation_id FROM external_effect") == [
        ("public:initial",)
    ]


@pytest.mark.parametrize("experiment", [10], indirect=True)
def test_a_persistent_model_budget_still_applies_after_kill(experiment):
    worker = experiment
    worker.launch("after-evidence")
    worker.enqueue("poc.outer")
    worker.paused("after-evidence")
    worker.stop()
    before = worker.attempts()
    worker.launch()
    worker.terminal("ERROR")
    with pytest.raises(BudgetExhausted, match="research"):
        worker.result()
    ledger = worker.attempts()
    assert ledger[: len(before)] == before
    assert len(ledger) == 9 and {row[1] for row in ledger} == {"research"}
    assert "poc.report" not in worker.steps()
    assert worker.lab.execute("SELECT operation_id FROM external_effect") == [
        ("public:initial",)
    ]
    # Show that the reserved slot is still admissible after the interrupted stage.
    assert worker.lab.admit("report") > ledger[-1][0]


def test_a_native_cancel_preempts_the_research_step(experiment):
    worker = experiment
    worker.launch("after-evidence")
    worker.enqueue("poc.outer")
    worker.paused("after-evidence")
    worker.client.cancel_workflow(WORKFLOW)
    worker.terminal("CANCELLED")
    worker.until(lambda: worker.events("cancelled:after-evidence") == 1)
    assert "poc.report" not in worker.steps()
    assert worker.events("research-entered") == 1
    assert worker.lab.execute("SELECT operation_id FROM external_effect") == [
        ("public:initial",)
    ]


def test_b_child_workflow_result_survives_parent_restart(experiment):
    worker = experiment
    worker.launch("after-child-workflow")
    worker.enqueue("poc.child_workflow")
    worker.paused("after-child-workflow")
    child_id = WORKFLOW + "/public"
    assert worker.status(child_id) == "SUCCESS"
    assert (
        len(
            [
                name
                for name in worker.steps(child_id)
                if name.endswith("_model_call_step")
            ]
        )
        == 1
    )
    worker.stop()
    worker.launch()
    worker.terminal("SUCCESS")
    result = worker.result()
    assert "verified: Find public sources (child requests=1)" in result["output"]
    assert result["parent_requests"] == 2
    assert worker.events("physical:public") == worker.events("hook:public") == 1
    assert worker.events("physical:manager") == 3


@pytest.mark.parametrize("mode", ["explicit", "named"])
def test_c_closed_over_model_and_inherited_provider_have_different_journals(
    experiment, mode
):
    worker = experiment
    worker.launch("after-child-steps")
    worker.enqueue("poc.runner_variant", mode)
    worker.paused("after-child-steps")
    assert worker.events("physical:manager") == worker.events("physical:public") == 1
    assert len(
        [name for name in worker.steps() if name.endswith("_model_call_step")]
    ) == (2 if mode == "named" else 1)
    worker.stop()
    worker.launch()
    worker.terminal("SUCCESS")
    assert worker.result() == {
        "output": "public-call=public evidence",
        "sdk_requests": 3,
    }
    assert worker.events("physical:manager") == 2
    assert worker.events("physical:public") == (1 if mode == "named" else 2)
    assert worker.events("hook:manager") == 4
    assert worker.events("hook:public") == 2


def test_c_child_response_can_replace_parents_turnstile(experiment):
    worker = experiment
    worker.launch()
    worker.enqueue("poc.runner_variant", "turnstile")
    worker.terminal("ERROR")
    with pytest.raises(RuntimeError, match="Turnstile missing call 'knowledge-call'"):
        worker.result()
    assert worker.events("physical:public") == 1
    assert worker.events("physical:knowledge") == 0
    assert worker.events("physical:manager") == 1


@pytest.mark.parametrize("mode", ["model-order", "tool-order"])
def test_c_reordered_sibling_steps_can_replay_another_roles_result(experiment, mode):
    worker = experiment
    worker.launch("after-child-steps")
    worker.enqueue("poc.runner_variant", mode)
    worker.paused("after-child-steps")
    worker.stop()
    worker.launch()
    worker.terminal("SUCCESS")
    assert (
        worker.result()["output"]
        == "knowledge-call=public evidence; public-call=knowledge evidence"
    )
    if mode == "model-order":
        assert (
            worker.events("physical:public") == worker.events("physical:knowledge") == 1
        )
    else:
        assert (
            worker.events("tool-body:public")
            == worker.events("tool-body:knowledge")
            == 1
        )
