# Public Opinion Agent Design

The `public_opinion_risk` workflow uses a research phase containing four compact business agents.

```text
research_phase
  ├─ public_signal_agent
  ├─ internal_knowledge_agent
  ├─ risk_assessment_agent
  └─ response_strategy_agent
```

The phase is a state-conversion wrapper around the subgraph, not a Supervisor
Agent. Its fixed execution topology is:

```text
public_signal_agent + internal_knowledge_agent
  -> risk_assessment_agent
  -> response_strategy_agent
```

Each business agent is encapsulated as:

```text
Agent = role responsibility + prompt + tool policy + input/output contract + private memory + execution strategy
```

The agent definitions live in:

```text
src/open_deep_research/public_opinion_agents/
```

## Agents

| Agent | Owns | Tools |
| --- | --- | --- |
| `public_signal_agent` | Public news, official notices, social discussion, complaints, heat, negative signals, competitor/category context | `web_search`, `social_media_skill` |
| `internal_knowledge_agent` | Internal RAG evidence, company/product facts, PR playbooks, FAQs, prior cases, approved principles | `rag_search` |
| `risk_assessment_agent` | Claim verification, risk register, regulatory/consumer-rights/product-quality/privacy/advertising/contract risk | `web_search`, `rag_search`, `social_media_skill` |
| `response_strategy_agent` | Response posture, holding statement, FAQ, stakeholder messages, action plan, monitoring keywords | `rag_search` |

## Private Agent Memory

Each business agent has its own private short-term memory slot in graph state:

```text
agent_memories["public_signal"]
agent_memories["internal_knowledge"]
agent_memories["risk_assessment"]
agent_memories["response_strategy"]
```

During execution:

```text
1. The agent reads only its own private memory slot.
2. The agent receives upstream shared role_reports separately.
3. After completing its role report, the agent writes a compact memory entry back to its own slot.
4. `research_phase` returns the updated `agent_memories` to the main graph state, so checkpointer-backed runs can carry it forward.
```

`role_reports` are shared handoff artifacts. `agent_memories` are private role-scoped memories.

Older seven-role names remain compatible and are mapped into the compact four-agent workflow:

```text
news_intelligence + social_sentiment + competitor_impact -> public_signal
fact_verification + compliance_risk -> risk_assessment
pr_strategy -> response_strategy
internal_knowledge -> internal_knowledge
```
