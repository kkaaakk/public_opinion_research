"""Offline scoring, provenance, and run history contracts for POR retrieval."""

import asyncio
import json
import math
from copy import deepcopy

import pytest

from open_deep_research.config_file import frozen_config, load_configuration
from open_deep_research.rag.citations import build_answer_ready_context
from open_deep_research.rag.service import build_rag_pipeline_config
from open_deep_research.rag.types import RAGChunk, RetrievalResult
from open_deep_research.tools import rag_tool
from tests import evaluate_rag_retrieval as evaluator
from tests.rag_eval_evidence import (
    DocumentCatalog,
    latency_summary,
    score_question,
    summarize,
)
from tests.rag_eval_records import (
    BENCHMARK,
    CORPUS,
    EVALUATOR_SOURCES,
    FROZEN_DATASET_SHA256,
    HISTORY_END,
    HISTORY_START,
    MANIFEST,
    RAG_SOURCES,
    build_fingerprint,
    compare_records,
    dataset_fingerprint,
    find_reusable_record,
    load_record,
    save_record,
    start_record,
    update_history,
    utc_now,
)

HEADING = {"heading_path": ["手册", "适用范围"]}


@pytest.fixture
def catalog(tmp_path):
    return DocumentCatalog(
        tmp_path,
        [
            {
                "doc_id": doc_id,
                "file_path": path,
                "file_type": path.split(".")[-1],
                "source_status": status,
            }
            for doc_id, path, status in [
                ("gold", "a/manual.md", "authoritative"),
                ("old", "b/manual.md", "deprecated"),
                ("trap", "cases/archive.md", "misleading"),
                ("pdf", "cases/table.pdf", "authoritative"),
                ("json", "cases/export.json", "authoritative"),
                ("image", "cases/chart.png", "authoritative"),
                ("txt", "cases/record.txt", "authoritative"),
            ]
        ],
    )


def chunk(catalog, doc_id, text, *, chunk_id="x", page=None, **metadata):
    path = catalog.directory / catalog.by_id[doc_id]["file_path"]
    source = path.as_posix() + (f"#page={page}" if page is not None else "")
    return RetrievalResult(
        chunk=RAGChunk(
            content=text,
            source=source,
            title="example",
            chunk_id=chunk_id,
            metadata=metadata,
        ),
        score=0.8,
        vector_score=0.6,
        keyword_score=0.2,
        rerank_score=0.5,
    )


def gold(doc_id, text, *, relevance=3, **locators):
    return {"doc_id": doc_id, "evidence_text": text, "relevance": relevance, **locators}


def evaluate(catalog, results, evidence, *, excluded=(), k=8, context_fragment=None):
    answer = build_answer_ready_context("question", results)
    if context_fragment:
        answer.context = answer.context.split(context_fragment, 1)[0]
    case = {
        "answerable": bool(evidence),
        "gold_evidence": evidence,
        "excluded_doc_ids": list(excluded),
    }
    return score_question(case, answer, catalog, k)


def test_source_id_mapping_uses_paths_and_pdf_suffix(catalog):
    assert catalog.resolve(r"a\manual.md")["doc_id"] == "gold"
    assert catalog.resolve("b/manual.md")["doc_id"] == "old"
    assert catalog.resolve("manual.md") is None
    assert catalog.resolve("another/manual.md") is None
    source = catalog.directory / "cases/table.pdf"
    assert catalog.resolve(f"{source}#page=12")["doc_id"] == "pdf"


def test_correct_document_does_not_credit_an_incorrect_section(catalog):
    result = evaluate(
        catalog,
        [
            chunk(
                catalog,
                "gold",
                "右侧门把手可免于更换",
                heading_path=["手册", "维修"],
                chunk_id="wrong_section",
            ),
            chunk(catalog, "gold", "左侧门把手", **HEADING, chunk_id="wrong_text"),
        ],
        [gold("gold", "右侧门把手可免于更换", **HEADING)],
    )
    for view in result["views"].values():
        assert view["document_metrics"]["recall"] == 1
        assert view["evidence_metrics"]["recall"] == 0
        assert view["evidence"][0]["miss_reason"] == "evidence_text_not_retrieved"


def test_unicode_multiple_entries_dedup_and_graded_metrics(catalog):
    results = [
        chunk(catalog, "trap", "其他资料", **HEADING),
        chunk(catalog, "gold", "Ａ１７\u00a0首检\n 退款", **HEADING),
        chunk(catalog, "gold", "Ａ１７\u00a0首检\n 退款", chunk_id="repeat", **HEADING),
        chunk(catalog, "old", "明确留存历史记录", **HEADING),
    ]
    first = gold("gold", "a17 首检退款", relevance=2, **HEADING)
    history = gold(
        "old", "明确留存历史记录", evidence_role="historical_record", **HEADING
    )
    result = evaluate(
        catalog, results, [first, first, history], excluded=("trap",), k=4
    )
    for view in result["views"].values():
        assert view["excluded_doc_ids"] == ["trap"]
        assert view["document_metrics"]["precision"] == 3 / 4
        assert all(
            view["results"][3][key] == expected
            for key, expected in (
                ("doc_id", "old"),
                ("source_status", "deprecated"),
            )
        )
        assert view["evidence_metrics"] == pytest.approx(
            {
                "hit_rate": 1,
                "recall": 1,
                "precision": 3 / 4,
                "mrr": 1 / 2,
                "ndcg": (3 / math.log2(3) + 7 / math.log2(5)) / (7 + 3 / math.log2(3)),
                "complete_evidence_rate": 1,
            }
        )
        assert len(view["evidence"]) == 2
    limited = evaluate(catalog, results, [first, history], excluded=("trap",), k=3)
    assert limited["views"]["internal"]["evidence_metrics"]["recall"] == 0.5
    assert (
        limited["views"]["internal"]["evidence_metrics"]["complete_evidence_rate"] == 0
    )


def test_span_coverage_requires_contiguous_returned_chars_and_uses_completion_rank(
    catalog,
):
    right = chunk(
        catalog,
        "gold",
        "gamma delta",
        chunk_id="right",
        char_start=11,
        char_end=22,
        **HEADING,
    )
    left = chunk(
        catalog,
        "gold",
        "alpha beta gamma",
        chunk_id="left",
        char_start=0,
        char_end=16,
        **HEADING,
    )
    evidence = gold("gold", "beta gamma delta", **HEADING)
    result = evaluate(catalog, [right, left], [evidence], k=2)
    for view in result["views"].values():
        assert view["evidence_metrics"]["mrr"] == 0.5
        assert view["evidence_metrics"]["precision"] == 1
        assert view["evidence"][0]["supporting_ranks"] == [1, 2]
    outside_top_k = evaluate(catalog, [right, left], [evidence], k=1)
    assert outside_top_k["views"]["delivered"]["evidence_metrics"]["hit_rate"] == 0
    right.chunk.metadata.update(char_start=20, char_end=31)
    gap = evaluate(catalog, [right, left], [evidence], k=2)
    assert gap["views"]["internal"]["evidence_metrics"]["recall"] == 0


def test_final_citations_use_only_actual_visible_excerpt(catalog):
    long_body = "开头。" + ("背景信息。" * 120) + "额外保修"
    results = [chunk(catalog, "gold", long_body, **HEADING)]
    evidence = [gold("gold", "额外保修", **HEADING)]
    scored = evaluate(catalog, results, evidence)
    assert scored["views"]["internal"]["evidence_metrics"]["recall"] == 1
    delivered = scored["views"]["delivered"]
    assert delivered["evidence_metrics"]["recall"] == 0
    assert (
        delivered["evidence"][0]["miss_reason"] == "evidence_missing_from_tool_context"
    )
    assert len(delivered["results"][0]["content"]) == 500

    cut = evaluate(
        catalog,
        [chunk(catalog, "gold", "可见的事实。后面的证据", **HEADING)],
        [gold("gold", "后面的证据", **HEADING)],
        context_fragment="后面的证据",
    )
    assert cut["views"]["delivered"]["results"][0]["content"] == "可见的事实。"
    assert cut["views"]["delivered"]["evidence"][0]["completion_rank"] is None


def test_json_pointer_is_bound_to_value_and_array_requires_every_leaf(catalog):
    evidence = [
        gold("json", "42", json_pointer="/payload/count"),
        gold("json", '[ "A", "B" ]', json_pointer="/payload/keys"),
    ]
    rows = [
        chunk(
            catalog,
            "json",
            "sibling: 42\ncount: 420",
            chunk_id="numbers",
            field_paths=["$.payload.sibling", "$.payload.count"],
        ),
        chunk(
            catalog, "json", "[0]: A", chunk_id="a", field_paths=["$.payload.keys[0]"]
        ),
        chunk(
            catalog, "json", "[1]: B", chunk_id="b", field_paths=["$.payload.keys[1]"]
        ),
    ]
    result = evaluate(catalog, rows, evidence)
    for view in result["views"].values():
        assert view["evidence_metrics"]["recall"] == 0.5
        assert view["evidence"][0]["miss_reason"] == "evidence_text_not_retrieved"
        assert view["evidence"][1]["locator_precision"] == "json_leaf_values"
        assert view["evidence"][1]["completion_rank"] == 3
    partial = evaluate(catalog, rows[:2], evidence)
    assert partial["views"]["internal"]["evidence"][1]["missing_json_pointers"] == [
        "/payload/keys/1"
    ]
    escaped = evaluate(
        catalog,
        [chunk(catalog, "json", "a/b~c: available", field_paths=["$['a/b~c']"])],
        [
            gold("json", "available", json_pointer="/a~1b~0c"),
        ],
    )
    assert escaped["views"]["delivered"]["evidence_metrics"]["recall"] == 1


def test_page_line_and_region_precision(catalog):
    results = [
        chunk(catalog, "pdf", "回执 R42", page=2),
        chunk(catalog, "txt", "登录日期 4 月 5 日", line_start=4, line_end=6),
        chunk(catalog, "image", "回写订单 权益运营席"),
    ]
    evidence = [
        gold("pdf", "回执 R42", page=1),
        gold("txt", "登录日期 4 月 5 日", line_start=5, line_end=5),
        gold(
            "image",
            "回写订单 权益运营席",
            region=[0, 5, 9, 10],
            coordinate_system="image_pixels_top_left",
        ),
    ]
    scored = evaluate(catalog, results, evidence)["views"]["delivered"]
    assert [item["completion_rank"] for item in scored["evidence"]] == [None, 2, 3]
    assert scored["evidence"][0]["miss_reason"] == "locator_not_retrieved"
    assert scored["evidence"][2]["locator_precision"] == "text_only_region"


def test_unanswerable_metrics_are_separate_and_empty_queries_keep_the_mechanism(
    catalog,
):
    negative = evaluate(
        catalog, [chunk(catalog, "trap", "诱导资料")], [], excluded=("trap",)
    )
    empty = evaluate(catalog, [], [])
    missed = evaluate(catalog, [], [gold("gold", "已批准", **HEADING)])

    def make_row(state, answerable):
        return {
            **state,
            "status": "succeeded",
            "question_type": "single_hop" if answerable else "unanswerable",
            "answerable": answerable,
            "difficulty": "hard",
            "latency_ms": 10.0,
        }

    result = summarize(
        [make_row(negative, False), make_row(empty, False), make_row(missed, True)]
    )
    assert negative["views"]["internal"]["results"][0]["scores"]["score"] == 0.8
    assert empty["no_result_reason"] == "no_relevant_results"
    assert negative["views"]["internal"]["document_metrics"] is None
    assert result["overall"]["answerable_scored"] == 1
    assert result["overall"]["unanswerable"]["no_result_mechanism_rate"] == 0.5
    assert (
        result["overall"]["unanswerable"]["views"]["delivered"]["returned_rate"] == 0.5
    )
    assert (
        result["overall"]["unanswerable"]["views"]["delivered"]["excluded_source_rate"]
        == 0.5
    )
    assert (
        result["by_question_type"]["unanswerable"]["views"]["internal"][
            "evidence_metrics"
        ]
        is None
    )
    assert result["by_difficulty"]["hard"]["count"] == 3
    assert set(missed["views"]["delivered"]["evidence_metrics"].values()) == {0}
    assert latency_summary(list(range(1, 21)))["p95_ms"] == 19
    assert latency_summary([])["mean_ms"] is None


def test_config_uses_yaml_inheritance_and_isolates_every_backend(tmp_path):
    original = tmp_path / "base.yaml"
    original.write_text(
        "rag_knowledge_base_paths: [source]\nrag_top_k: 5\nrag_chunk_size: 900\n",
        encoding="utf-8",
    )
    child = tmp_path / "run.yaml"
    child.write_text(
        "extends: base.yaml\nrag_milvus_uri: https://milvus.invalid\nrag_keyword_backend: elasticsearch\n",
        encoding="utf-8",
    )
    args = evaluator.parse_args(
        [
            "fingerprint",
            "--config",
            str(child),
            "--top-k",
            "7",
            "--set",
            "rag_query_rewrite_enabled=false",
        ]
    )
    actual = evaluator.resolve_eval_config(args)
    expected = load_configuration(child)
    isolated = evaluator._isolated_config(actual, "20260101T000000Z-" + "a" * 32)
    two = evaluator._isolated_config(actual, "20260101T000000Z-" + "b" * 32)
    assert actual.rag_knowledge_base_paths == [str(tmp_path / "source")]
    assert actual.rag_chunk_size == expected.rag_chunk_size == 900
    assert actual.rag_top_k == 7 and actual.rag_query_rewrite_enabled is False
    assert isolated.rag_milvus_uri == actual.rag_milvus_uri
    assert isolated.rag_vectorstore_path != actual.rag_vectorstore_path
    assert isolated.rag_collection_name != two.rag_collection_name
    assert isolated.rag_elasticsearch_index != two.rag_elasticsearch_index
    with pytest.raises(ValueError, match="non-credential"):
        evaluator.resolve_eval_config(
            evaluator.parse_args(["run", "--set", "rag_milvus_token=secret"])
        )


@pytest.fixture
def fingerprint(tmp_path, monkeypatch):
    for name in [
        *RAG_SOURCES,
        *EVALUATOR_SOURCES,
        MANIFEST,
        BENCHMARK / "dev.jsonl",
        BENCHMARK / "test.jsonl",
        CORPUS / "source.md",
    ]:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("input\n", encoding="utf-8")
    default = evaluator.resolve_eval_config(
        evaluator.parse_args(
            [
                "fingerprint",
                "--set",
                "rag_embedding_provider=hash",
                "--set",
                "rag_reranker_provider=simple",
                "--set",
                "rag_multimodal_enabled=false",
            ]
        )
    )
    monkeypatch.setenv("RAG_MILVUS_TOKEN", "sensitive-milvus")
    settings = build_rag_pipeline_config(default).model_dump()
    settings.update(
        knowledge_base_paths=[str(tmp_path / CORPUS)],
        vectorstore_path=str(tmp_path / ".tmp/isolated"),
        elasticsearch_url="https://someone:secret-pass@search.invalid:9200/private-path?token=secret-token",
    )
    scope = {
        "mode": "pipeline",
        "split": "dev",
        "purpose": "formal",
        "selection": "all",
        "question_ids": ["D101"],
        "top_k": 4,
        "timing_protocol": "fresh_index_include_first_query",
    }
    options = {"rag_query_rewrite_enabled": True}

    def current():
        return build_fingerprint(
            scope, settings, options, frozen_config(default), root=tmp_path
        )

    return current


def test_sha_changes_with_effective_config_and_rag_code_only(tmp_path, fingerprint):
    first = fingerprint()
    assert first == fingerprint()
    assert len(first["evaluation_fingerprint"]) == 64
    assert not any(
        secret in json.dumps(first)
        for secret in (
            "sensitive-milvus",
            "secret-pass",
            "private-path",
            "secret-token",
            "someone",
        )
    )
    assert dataset_fingerprint(tmp_path)["sha256"] == first["full_dataset_fingerprint"]
    path = tmp_path / "src/open_deep_research/web/unrelated.py"
    path.parent.mkdir(parents=True)
    path.write_text("changed", encoding="utf-8")
    assert fingerprint() == first
    (tmp_path / RAG_SOURCES[0]).write_text("change retrieval", encoding="utf-8")
    changed = fingerprint()
    assert changed["components"]["rag"] != first["components"]["rag"]
    assert changed["components"]["dataset"] == first["components"]["dataset"]
    (tmp_path / CORPUS / "source.md").write_text(
        "different installed corpus bytes", encoding="utf-8"
    )
    assert fingerprint()["components"]["dataset"] != changed["components"]["dataset"]
    (tmp_path / EVALUATOR_SOURCES[1]).write_text("change scoring", encoding="utf-8")
    assert fingerprint()["components"]["scoring"] != changed["components"]["scoring"]


def test_checkout_matches_frozen_dataset():
    assert dataset_fingerprint()["sha256"] == FROZEN_DATASET_SHA256


def finish(fingerprint, directory, *, status="succeeded", evidence_miss=False):
    record = start_record(fingerprint, title="test version", notes="note", git="a" * 40)
    path = save_record(directory, record)
    view = {
        "document_metrics": {"recall": 1.0},
        "evidence_metrics": {
            "recall": float(not evidence_miss),
            "mrr": float(not evidence_miss),
            "complete_evidence_rate": float(not evidence_miss),
        },
        "excluded_doc_ids": [],
        "evidence": [
            {
                "gold": {"doc_id": "sample"},
                "miss_reason": "missing" if evidence_miss else None,
            }
        ],
    }
    row = {
        "question_id": "D101",
        "question_type": "single_hop",
        "difficulty": "easy",
        "answerable": True,
        "status": "succeeded",
        "latency_ms": 25.0,
        "no_result_reason": None,
        "views": {"internal": view, "delivered": view},
    }
    record.update(
        status=status,
        finished_at=utc_now(),
        questions=[row],
        counts={"total": 1, "succeeded": 1, "failed": 0},
        metrics=summarize([row]),
        index={"num_chunks": 1, "build_ms": 1.0},
        model_usage={"retrieval": {"model_calls": 0, "cost": None}},
    )
    save_record(directory, record)
    assert load_record(path) == record
    return path, record


def test_run_history_requires_complete_intact_matching_records(tmp_path, fingerprint):
    directory = tmp_path / "runs"
    current = fingerprint()
    failed_path, _ = finish(current, directory, status="failed")
    assert failed_path.exists()
    assert find_reusable_record(directory, current) is None
    running = start_record(current, title="unfinished", notes="", git=None)
    save_record(directory, running)
    path, complete = finish(current, directory)
    assert find_reusable_record(directory, current) == (path, complete)
    with pytest.raises(ValueError, match="cannot be replaced"):
        save_record(directory, complete)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["questions"] = []
    path.write_text(json.dumps(data), encoding="utf-8")
    assert find_reusable_record(directory, current) is None


def test_comparison_before_and_after_and_history_preserve_earlier_records(
    tmp_path, fingerprint
):
    current = fingerprint()
    runs = tmp_path / "runs"
    old_file, old = finish(current, runs, evidence_miss=True)
    new_file, new = finish(current, runs)
    comparison = compare_records(old, new)
    assert comparison["fixed_failures"] == {
        "D101": [{"doc_id": "sample", "reason": "missing"}]
    }
    assert (
        comparison["metrics"]["overall"]["views"]["delivered"]["evidence_metrics"][
            "recall"
        ]["delta"]
        == 1
    )
    assert compare_records(new, old)["new_failures"] == comparison["fixed_failures"]
    incompatible = deepcopy(new)
    incompatible["components"]["scoring"] = "new method"
    with pytest.raises(ValueError, match="Incompatible"):
        compare_records(old, incompatible)
    pipeline_rewrite = deepcopy(new)
    pipeline_rewrite["tool_options"]["rag_query_rewrite_enabled"] = False
    with pytest.raises(ValueError, match="tool scope"):
        compare_records(old, pipeline_rewrite)
    partial = deepcopy(new)
    partial["scope"]["question_ids"].append("D999")
    with pytest.raises(ValueError, match="complete"):
        compare_records(old, partial)
    history = tmp_path / "history.md"
    history.write_text(
        f"existing records\n{HISTORY_START}\nempty\n{HISTORY_END}\nexisting footer\n",
        encoding="utf-8",
    )
    update_history(history, runs)
    table = history.read_text(encoding="utf-8")
    assert table.startswith("existing records") and table.endswith("existing footer\n")
    assert old_file.stem in table and new_file.stem in table


def test_tool_scope_routes_through_production_rewrite(monkeypatch, catalog):
    seen = []

    def rewrite(query, **kwargs):
        assert kwargs["enabled"] is True
        return f"{query} rewritten"

    class Pipeline:
        def query(self, query, *, original_query):
            seen.append((query, original_query))
            return build_answer_ready_context(
                original_query, [chunk(catalog, "gold", "事实", **HEADING)]
            )

    monkeypatch.setattr(rag_tool, "maybe_rewrite_query_with_model", rewrite)
    monkeypatch.setattr(
        rag_tool, "get_or_create_rag_pipeline", lambda config: Pipeline()
    )
    config = {
        "configurable": {
            "rag_enabled": True,
            "retrieval_mode": "hybrid",
            "rag_query_rewrite_enabled": True,
            "rag_knowledge_base_paths": ["source"],
        },
    }
    structured = asyncio.run(evaluator.search_rag_context("问题", config=config))
    wrapped = asyncio.run(rag_tool._rag_search("问题", config=config))
    assert seen == [("问题 rewritten", "问题"), ("问题 rewritten", "问题")]
    assert wrapped == structured.context
    assert structured.matched_chunks[0].chunk.content == "事实"
