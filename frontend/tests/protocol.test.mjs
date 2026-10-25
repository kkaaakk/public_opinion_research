import test from "node:test";
import assert from "node:assert/strict";
import { parseFrames, phaseForEvent, projectRun, TERMINAL } from "../src/protocol.js";

test("SSE handles split frames and does not turn a keepalive into an event", () => {
  const a = parseFrames(': keep-alive\n\nid: 1\ndata: {"seq":1,"type":"step_started",');
  assert.equal(a.frames.length, 0);
  const b = parseFrames(a.rest + '"step":"research_review"}\n\n');
  assert.equal(b.frames[0].seq, 1);
  assert.equal(phaseForEvent(b.frames[0]), "review");
});
test("producer branches and downstream role are not inferred completed", () => {
  const out = projectRun([
    { type: "step_started", step: "public_signal_agent" },
    { type: "step_completed", step: "public_signal_agent" },
    { type: "step_started", step: "risk_assessment_agent" }
  ], { status: "running", completed_steps: [] }, { rag_enabled: true });
  assert.equal(out.statuses.public_signal, "completed");
  assert.equal(out.statuses.internal_knowledge, "waiting");
  assert.equal(out.statuses.risk_assessment, "running");
  assert.equal(out.statuses.reporting, "waiting");
});
test("terminal native status does not imply final settlement", () => {
  assert.equal(TERMINAL.has("failed"), true);
  assert.equal(TERMINAL.has("cancelling"), false);
  const out = projectRun([], { status: "failed", current_step: "research_review" }, {});
  assert.equal(out.statuses.review, "error");
});
test("disabled RAG producer is visually distinct from completed", () => {
  const out = projectRun([], { status: "running" }, { rag_enabled: false });
  assert.equal(out.statuses.internal_knowledge, "disabled");
});
