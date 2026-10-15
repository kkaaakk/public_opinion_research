# GitHub RAG Test Sets

This directory contains compact samples from public GitHub datasets for local RAG testing. The files are intentionally sampled and normalized instead of storing the full upstream datasets.

## Included Samples

| File                      | Upstream dataset | License | Local use                                                                |
| ------------------------- | ---------------- | ------- | ------------------------------------------------------------------------ |
| `retrievalqa_sample.json` | RetrievalQA      | MIT     | Open-domain QA examples with retrieved evidence context.                 |
| `qulac_sample.json`       | Qulac            | MIT     | Clarifying-question examples for ambiguous or faceted information needs. |

## Sources

- RetrievalQA repository: <https://github.com/hyintell/RetrievalQA>
- RetrievalQA sampled raw file: <https://raw.githubusercontent.com/hyintell/RetrievalQA/main/data/retrievalqa_gpt4.jsonl>
- Qulac repository: <https://github.com/aliannejadi/qulac>
- Qulac sampled raw file: <https://raw.githubusercontent.com/aliannejadi/qulac/master/data/qulac/qulac.json>

## Sampling Notes

- `retrievalqa_sample.json` keeps up to 8 records per RetrievalQA `data_source` from `retrievalqa_gpt4.jsonl` and truncates each retained evidence context to keep the local knowledge base small.
- `qulac_sample.json` keeps the first 40 unique `topic_id` rows from `qulac.json`.
- These files are meant for retrieval smoke tests and pipeline behavior checks, not benchmark reporting against the full upstream datasets.
