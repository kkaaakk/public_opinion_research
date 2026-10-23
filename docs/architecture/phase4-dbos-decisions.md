# Phase 4 DBOS implementation decisions

Status: implemented; local functional and fault acceptance passed; PR checks required. Base: 5ca71c12. User explicitly selects DBOS.

- DBOS journal/status is the only execution authority. Control waits for approval;
  planning/research share one native business queue; roles have durable children.
- Existing domain steps, reducers, SDK Runtime and Harness remain. No DBOSRunner.
- PostgreSQL domain facts hold immutable admission identity/configuration, revocable
  business permissions, operation receipts, settled artifacts and report/archive facts.
  They hold no task queue, cursor, lease or lifecycle transition graph.
- Native global/owner queue limits plus small transactional resource reservations
  enforce session exclusion and retain capacity for unknown effects. Reservations
  never claim tasks or infer process death. Native sleep handles admission waits.
- Streams are public events, native messages consume versioned approvals in the
  same PostgreSQL transaction as the approval decision. Native archive workflows
  independently retry Neo4j, without repeating successful research.
- Old runs drain with the old release before cutover. No cross-engine resubmission.
  Multihost automatic failover requires Conductor or trusted external coordination.

Acceptance must include actual PostgreSQL, kill/restart, SDK settlement, unknown
side effects, joint capacity and old API/SSE contracts. No claim of cross-store
exactly-once or arbitrary SDK turn recovery. See final audit for measured results.
