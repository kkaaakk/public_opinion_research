# Native DBOS POC design (before implementation)

Baseline application source is Phase 4 5ca71c12. The branch also retains the
previous evaluation (31764e64) unchanged. No production entry point is replaced.

Authority: DBOS WorkflowStatus and step/child results are the ONLY execution
state. No por_runs, SQL task queue, lease, heartbeat, Attempt table, Workflow
cursor, or SQL event outbox is used by this executable POC.

FastAPI uses DBOSClient with authenticated_user, stable workflow IDs and stored
input fingerprints. Native Streams provide ordered backend cursors. Events hold
plan, phase, report and settlement facts, not a second lifecycle state machine.
The control workflow is not capacity-limited while awaiting approval. Planning
and research are child workflows on the SAME capacity-limited work queue;
research starts two independent role workflows, joins their patches, reviews
and repeats the genuine domain gap assignment, then runs Risk and Strategy.

Each SDK role uses the real SDKAgentRuntime and ResearchWorkspace. Route A runs
one business role as a preemptible DBOS Step with the original Runner. Route B
uses an injected DBOSRunner and scoped clone admission through SDK public Hook
and input-filter APIs. Clone admission must retain context/run/role/task binding,
canonical prompt/tool policy and no-handoff rules. Normal operation and native
turn-replay are separate claims and must be tested separately.

Application SQL holds only domain facts, externally meaningful tool operations,
report artifacts and a monotonic per-role execution permit. The permit fences
stale model/tool starts when recovery is explicitly authorized; it has no timer,
queue or lease. Unknown tool effects stop re-entry. External idempotency is
verified with a separate authority table and does not imply global exactly-once.
Budget and Graph settlement remain owned by the real Runtime/Harness.

PostgreSQL and Neo4j are disposable labelled containers on loopback. All inference
is scripted. Tests kill real processes, replay native Streams, exercise approvals,
keep a zombie executor alive during public recovery, stop/restart the POC database,
and measure native global/partition queue limits. Conductor automatic failure
detection is a different deployment claim from public manual resume/recovery.

The production-equivalence inventory must separately count omitted session
quotas/quarantined capacity, full API/SSE protocol, archive compensation, SDK
Sessions and deployment operations. A short POC is not credited as complete
Phase 4 replacement without those contracts.
