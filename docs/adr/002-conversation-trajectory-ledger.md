# ADR 002: Record conversation facts beside native tracing

## Status

Accepted — implements the phase 2 task's explicit architecture decisions.

## Context

PR #14 rendered transient LangChain event DTOs in the DSH view. Reload lost
history, the browser allocated Steps from Agents/tools, failed streamed attempts
were incomplete, and Stop added a browser-only terminal event. LangSmith already
provides native execution spans; `budget.py` already provides usage policy.

## Decision

Attach an official LangChain callback recorder to the original graph invocation.
Append sanitized, versioned conversation events to a leased local JSONL writer.
Project live facts and cold history with one deterministic backend fold. HTTP
subscribers do not own execution; explicit Stop waits for backend settlement.
The original root `astream_events(v2)` transport continues supplying reports and
the accumulated root budget. No model/tool/graph is invoked again for recording
or replay. Model starts allocate Steps, actual user submissions allocate Turns,
and native tool ids/hierarchy resolve executions. Ambiguous ownership remains
unresolved. Structured-output envelopes are protocol results, not fake tools.

Only centralized session/turn/step/agent context and genuine shared compaction
boundaries add semantics. The business agents contain no new emit calls.
LangGraph state/checkpoints, LangSmith spans and budget accounting retain their
authority. Narrow DSH compatibility edits honor explicit Locations and
LangChain's cache-inclusive input totals, preserving the original visual UI.

## Alternatives

| Alternative | Reason not selected |
| --- | --- |
| Pull LangSmith traces into the browser | Execution spans are not a local session event source; requires external trace access. |
| Manual LLM/tool spans in each agent | Duplicates native facts and instrumentation responsibilities. |
| Import the DSH runtime | Replaces established LangGraph/provider/tool/memory infrastructure unnecessarily. |
| Reuse ResearchTranscript/MySQL/RAG stores | Those stores own raw debug or business/memory data, not sequenced sanitized conversation history. |
| Add a new database/message service | Increases operational cost for a local first implementation. |

## Consequences and trade-offs

Replay and live rendering share stable identities, timestamps, Tool schemas and
Attempt outcomes. Writer leases prevent competing repair/live writers; cold
recovery is read-only. Persistence failures degrade trajectory without repeating
or failing business research. Redaction precedes storage and SSE, and large
payload/stream limits are explicit. Existing optional HTTP token authentication
also protects history and cancellation endpoints.

JSONL is easy to inspect and has no service dependency, but validates full prefixes
and sends coalesced snapshots. Very long sessions will need indexed storage and
incremental transport. The replaceable writer/read seam enables that later.
Leases coordinate writers on one filesystem; they do not route cross-process
SSE or implement distributed ownership. Hard crashes can lose an unsettled
stream buffer, so missing model/tool outcomes are shown as interrupted.
History can contain sensitive research content after credential redaction;
operators must protect/exclude the storage directory and configure retention.

## Evidence

The source audit uses DSH `639ed015397290b3745d163aafe02ffee4aa3f84`, including
Session envelopes/repair, Agent loop settlement, Tool lifecycle, JSONL ownership,
cold read and conversation/trajectory contracts. Exact source boundaries are in
`src/open_deep_research/web/frontend/src/trajectory/UPSTREAM.md`.
Real callback/graph fixtures test one Turn across four roles, parallel/nested
Tools, attempts/retries, persistence/serialization isolation, disk reload and
Stop. Official LangSmith tracing with a recording transport sees the same model
and Tool run ids as the ledger. Browser QA and real-provider smoke are reported
separately so offline fixtures cannot imply an external-provider pass.
