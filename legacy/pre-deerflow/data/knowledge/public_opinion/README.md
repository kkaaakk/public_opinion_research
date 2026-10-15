# Public Opinion — Internal Knowledge Base

Internal reference documents indexed for RAG retrieval by the `internal_knowledge` agent.

## Structure

```
public_opinion/
├── pr_playbooks/          # Crisis response procedures and holding statements
│   └── tesla_china_crisis_playbook.md
├── product_docs/          # Technical specifications and engineering facts
│   └── tesla_battery_technical_facts.md
├── historical_cases/      # Post-mortem reviews of past incidents
│   └── tesla_battery_crises_review.md
├── compliance/            # Regulatory requirements and exposure tracking
│   └── china_regulatory_landscape.md
└── faq/                   # Approved Q&A for customer-facing teams
    ├── tesla_battery_customer_faq.md
    └── media_response_faq.json

# Memory Records
data/memory/public_opinion/memories.jsonl  # Durable decisions and incident notes
```
