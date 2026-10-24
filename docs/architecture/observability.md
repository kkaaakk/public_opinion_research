# Execution observability and usage

The current execution authority is DBOS, with SDK hooks and existing LangChain
business-service tracing. See [Phase 5 audit](../audits/openai-agents-phase5.md).

| Fact | Owner and correlation |
| --- | --- |
| Native execution / retries / elapsed time / errors | DBOS Workflow/Step journal; root Workflow ID equals public Run ID |
| Role invocation | Native child/unit ID plus SDK Harness `invocation_id`, role and round |
| Model usage | Actual SDK response/provider usage, plus captured single-call business-service attempts |
| Tool effects | `por_domain.operations` stable operation ID, role/unit/native call/name and argument fingerprint; no public raw arguments |
| Evidence | Run/role/task/round and graph call/receipt IDs link Source → Evidence → Claim → Finding |
| Public progress | DBOS Durable Stream seq, generation and Run ID; SSE is a read projection |
| Report archive | Stable History ID, owner, Run ID and generation-derived `run_state_version` |
| RAG / report / model business spans | Optional existing LangSmith and LangChain callbacks; bounded correlation metadata |

SDK tracing is currently explicitly disabled in `SDKAgentRuntime` RunConfig,
with `trace_include_sensitive_data=False`. There is therefore no external SDK
Trace ID to claim as a deployed correlation source. Native events carry SDK
invocation identity, while DBOS journals, receipts and evidence retain business
correlation. Enabling external SDK trace upload and validating its operational
privacy/credential policy requires separate deployment work; Phase 5 adds no
telemetry infrastructure and makes no external trace-export claim.

`budget.py` remains the budget/usage authority. SDK Runtime captures SDK usage and
nested Graph/tool/report model attempts, and business Steps retain failed and
cancelled charges. Unknown provider token usage is not estimated as billed usage.
Estimates for context admission are explicitly separate from actual usage.
Parallel deltas merge once; request redelivery and SSE replay do not add charges.

History intentionally stores the four numeric public counters: model calls,
input tokens, output tokens and total tokens. Run status retains the full budget
including warnings/cache details/tool counts. Compare those four counters across
History, Run and SSE; their schemas are not identical. Unknown opaque effects
project as Run `interrupted`/unresolved and legacy History `incomplete`.

Research terminal delivery and History completion are separate facts. A stream
can end after `done` while History is pending; reconnect/read Run for eventual
`history_saved`, and do not treat an old generation's receipt as current success.
Database errors close admission/control writes; SSE EOF is never proof of success.

LangSmith remains opt-in (`LANGSMITH_TRACING`, `LANGSMITH_API_KEY`,
`LANGSMITH_PROJECT`). Existing RAG stage spans are retained without wrapping a
model/tool twice. It no longer supplies native Graph root/node execution. Trace
failure does not authorize repeating research or suppress a business exception.
Avoid raw prompts, tool payloads, private memory, provider responses and secrets
in public events or correlation metadata; egress redaction and owner checks stay
active. Historical LangSmith reports can still be imported into owner-scoped
History without executing research.
