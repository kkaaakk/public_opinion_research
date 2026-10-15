# 验证检索评估指标、漏召诊断、真实模型约束和纯文本输出。
from pathlib import Path
from types import SimpleNamespace

import pytest

import tests.evaluate_rag_retrieval as evaluator
from tests.evaluate_rag_retrieval import (
    add_miss_diagnostics,
    average_metric,
    build_pipeline,
    collect_index_stats,
    configure_plain_output,
    evaluate_cases,
    normalize_source,
    parse_args,
    print_report,
    print_run_config,
    score_evidence_retrieval,
    score_retrieval,
    summarize_by_category,
)


# 验证来源归一化保留预期文件名。
def test_normalize_source_keeps_expected_file_name():
    assert normalize_source("C:/repo/data/knowledge/faq.json#item-2") == "faq.json"
    assert normalize_source(r"C:\repo\data\knowledge\team_handbook.md") == "team_handbook.md"


# 验证命中率、召回率、精确率及 MRR 的计算。
def test_score_retrieval_computes_hit_recall_precision_and_mrr():
    score = score_retrieval(
        expected_sources=["team_handbook.md", "faq.json"],
        retrieved_sources=["runbook.txt", "faq.json", "team_handbook.md"],
        k=3,
    )

    assert score is not None
    assert score["hit@3"] == 1.0
    assert score["recall@3"] == 1.0
    assert score["precision@3"] == 2 / 3
    assert score["mrr@3"] == 0.5


# 验证没有预期来源的不可回答案例被跳过。
def test_score_retrieval_skips_unanswerable_cases_without_sources():
    assert score_retrieval(expected_sources=[], retrieved_sources=["runbook.txt"], k=5) is None


# 验证证据级评分结合分块内容与来源状态。
def test_score_evidence_retrieval_uses_chunk_content_and_authority_status():
    details = [
        {
            "rank": 1,
            "source": "misleading_archive.md",
            "page_content": "Wrong claim. Correction: raw planning notes are retained for 30 days.",
            "metadata": {"source_status": "misleading"},
        },
        {
            "rank": 2,
            "source": "data_governance.md",
            "page_content": "Audit records are retained for 180 days. Raw planning notes are retained for 30 days.",
            "metadata": {"source_status": "authoritative"},
        },
        {
            "rank": 3,
            "source": "runbook.txt",
            "page_content": "Unrelated runbook text.",
            "metadata": {"source_status": "authoritative"},
        },
    ]

    score = score_evidence_retrieval(
        expected_sources=["data_governance.md"],
        expected_keywords=["audit records", "180 days", "raw planning notes", "30 days"],
        retrieved_details=details,
        k=3,
    )

    assert score is not None
    assert score["evidence_hit@3"] == 1.0
    assert score["evidence_recall@3"] == 1.0
    assert score["evidence_precision@3"] == 1 / 3
    assert score["misleading_rate@3"] == 1 / 3
    assert score["authoritative_rate@3"] == 2 / 3


# 验证按案例类别正确汇总指标。
def test_summarize_by_category_groups_scores():
    scores = [
        {
            "category": "single_hop",
            "hit@5": 1.0,
            "recall@5": 1.0,
            "precision@5": 0.5,
            "mrr@5": 1.0,
            "avg_latency_ms": 10.0,
        },
        {
            "category": "single_hop",
            "hit@5": 0.0,
            "recall@5": 0.0,
            "precision@5": 0.0,
            "mrr@5": 0.0,
            "avg_latency_ms": 30.0,
        },
        {
            "category": "multi_hop",
            "hit@5": 1.0,
            "recall@5": 0.5,
            "precision@5": 0.25,
            "mrr@5": 0.5,
            "avg_latency_ms": 20.0,
        },
    ]

    assert average_metric(scores, "hit@5") == 2 / 3
    assert average_metric(scores, "avg_latency_ms") == 20.0
    summary = summarize_by_category(scores, metric_keys=["hit@5", "recall@5", "avg_latency_ms"])

    assert summary["single_hop"] == {
        "count": 2,
        "hit@5": 0.5,
        "recall@5": 0.5,
        "avg_latency_ms": 20.0,
    }
    assert summary["multi_hop"] == {
        "count": 1,
        "hit@5": 1.0,
        "recall@5": 0.5,
        "avg_latency_ms": 20.0,
    }


# 验证已评分和跳过案例均保留实际耗时。
def test_evaluate_cases_records_latency_for_scored_and_skipped_cases():
    # 提供可记录配置或返回预设引用的管线替身。
    class FakePipeline:
        # 按问题返回预设来源引用，检查评估与耗时记录。
        def query(self, query):
            if query == "answerable":
                citations = [SimpleNamespace(source="C:/repo/data/knowledge/faq.json#item-1")]
            else:
                citations = [SimpleNamespace(source="C:/repo/data/knowledge/runbook.txt")]
            return SimpleNamespace(citations=citations)

    ticks = iter([1.0, 1.025, 2.0, 2.05])
    cases = [
        {
            "id": "case_1",
            "category": "single_hop",
            "answer_type": "answerable",
            "query": "answerable",
            "expected_sources": ["faq.json"],
        },
        {
            "id": "case_2",
            "category": "negative",
            "answer_type": "unanswerable",
            "query": "unanswerable",
            "expected_sources": [],
        },
    ]

    scores, skipped = evaluate_cases(cases, FakePipeline(), k=5, time_fn=lambda: next(ticks))

    assert scores[0]["avg_latency_ms"] == 25.0
    assert skipped[0]["avg_latency_ms"] == 50.0


# 验证预期来源位于截断范围之外时给出漏召诊断。
def test_add_miss_diagnostics_flags_expected_source_below_top_k():
    expected_chunk = SimpleNamespace(
        source="C:/repo/data/knowledge/expected.md",
        chunk_id="doc-2-chunk-1",
        content="Expected source content",
        metadata={"heading_path": ["Expected"]},
    )
    misleading_chunk = SimpleNamespace(
        source="C:/repo/data/knowledge/misleading.md",
        chunk_id="doc-1-chunk-1",
        content="Misleading source content",
        metadata={"heading_path": ["Misleading"]},
    )
    retrieval_results = [
        SimpleNamespace(
            chunk=misleading_chunk,
            score=0.9,
            vector_score=0.8,
            keyword_score=0.1,
            rerank_score=0.7,
        ),
        SimpleNamespace(
            chunk=expected_chunk,
            score=0.6,
            vector_score=0.4,
            keyword_score=0.0,
            rerank_score=0.2,
        ),
    ]

    # 提供预设候选结果，隔离真实检索后端。
    class FakeRetriever:
        # 返回预设检索候选，供查询改写或漏召诊断检查。
        def retrieve(self, **kwargs):
            return retrieval_results

    # 提供可控候选排序结果，隔离真实重排模型。
    class FakeReranker:
        # 返回测试所需的候选顺序和评分。
        def rerank(self, **kwargs):
            return kwargs["results"]

    pipeline = SimpleNamespace(
        config=SimpleNamespace(
            rerank_top_n=2,
            keyword_top_k=2,
            authority_rerank_enabled=False,
        ),
        indexer=SimpleNamespace(
            documents=[object()],
            chunks=[misleading_chunk, expected_chunk],
            embedding_backend=SimpleNamespace(embed_query=lambda query: [0.1]),
            retriever=FakeRetriever(),
            ensure_ready=lambda: None,
        ),
        reranker=FakeReranker(),
        _filter_results=lambda results: results,
    )
    record = {
        "id": "case_1",
        "category": "single_hop",
        "query": "why missed?",
        "expected": ["expected.md"],
        "retrieved": ["misleading.md"],
    }

    add_miss_diagnostics(record, pipeline, query="why missed?", k=1)

    assert record["expected_not_retrieved"] is False
    assert record["expected_retrieved_but_low_rank"] is True
    assert record["retrieved_details"][1]["rank"] == 2
    assert record["retrieved_details"][1]["source"] == "expected.md"


# 验证索引统计读取实际文档、分块和向量数。
def test_collect_index_stats_reads_ready_index_counts():
    pipeline = SimpleNamespace(
        indexer=SimpleNamespace(
            documents=[object(), object()],
            chunks=[object(), object(), object()],
            last_vector_count=3,
        )
    )

    assert collect_index_stats(pipeline) == {
        "num_documents": 2,
        "num_chunks": 3,
        "num_vectors": 3,
    }


# 验证显式真实模型参数正确传入管线。
def test_build_pipeline_wires_explicit_real_model_options(monkeypatch):
    captured = {}

    # 提供可记录配置或返回预设引用的管线替身。
    class FakePipeline:
        # 接收测试构造参数，不建立真实外部连接。
        def __init__(self, config):
            captured["config"] = config

    monkeypatch.setattr(evaluator, "RAGPipeline", FakePipeline)
    build_pipeline(
        SimpleNamespace(
            knowledge_base=["data/knowledge"],
            chunk_size=400,
            chunk_overlap=50,
            top_k=5,
            rerank_top_n=8,
            embedding_provider="sentence_transformers",
            embedding_model="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
            embedding_device="cpu",
            vectorstore_provider="milvus",
            vectorstore_path="data/indexes/rag",
            milvus_uri="C:/tmp/odr_milvus_eval.db",
            milvus_metric_type="COSINE",
            reranker_provider="cross_encoder",
            reranker_model="BAAI/bge-reranker-base",
            reranker_device="cpu",
        )
    )

    config = captured["config"]
    assert config.embedding_provider == "sentence_transformers"
    assert config.embedding_model == "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    assert config.embedding_device == "cpu"
    assert config.reranker_provider == "cross_encoder"
    assert config.reranker_model == "BAAI/bge-reranker-base"
    assert config.reranker_device == "cpu"


# 验证正式质量评估拒绝哈希嵌入。
def test_build_pipeline_rejects_hash_embedding_for_retrieval_metrics():
    with pytest.raises(ValueError, match="hash embeddings"):
        build_pipeline(
            SimpleNamespace(
                knowledge_base=["data/knowledge"],
                chunk_size=400,
                chunk_overlap=50,
                top_k=5,
                rerank_top_n=8,
                embedding_provider="hash",
                embedding_model="unused-model",
                embedding_device=None,
                vectorstore_provider="milvus",
                vectorstore_path="data/indexes/rag",
                milvus_uri="C:/tmp/odr_milvus_eval.db",
                milvus_metric_type="COSINE",
                reranker_provider="cross_encoder",
                reranker_model="BAAI/bge-reranker-base",
                reranker_device=None,
            )
        )


# 验证命令行默认选用真实语义模型栈。
def test_parse_args_defaults_to_real_model_stack(monkeypatch):
    monkeypatch.setattr("sys.argv", ["evaluate_rag_retrieval.py"])

    args = parse_args()

    assert args.embedding_provider == "sentence_transformers"
    assert "paraphrase-multilingual-MiniLM-L12-v2" in args.embedding_model
    assert args.vectorstore_provider == "milvus"
    assert args.vectorstore_path == "data/indexes/rag"
    assert str(Path(args.milvus_uri).parent).replace("\\", "/") == "data/indexes/rag"
    assert Path(args.milvus_uri).name.startswith("odr_milvus_eval_")
    assert Path(args.milvus_uri).suffix == ".db"
    assert args.milvus_metric_type == "COSINE"
    assert args.reranker_provider == "cross_encoder"
    assert args.reranker_model == ".rag_models/cross-encoder-ms-marco-MiniLM-L6-v2"


# 验证命令行参数校验拒绝非语义嵌入评估。
def test_parse_args_rejects_hash_embedding_for_retrieval_metrics(monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        ["evaluate_rag_retrieval.py", "--embedding-provider", "hash"],
    )

    with pytest.raises(SystemExit):
        parse_args()


# 验证运行配置输出为无终端控制字符的单行文本。
def test_print_run_config_uses_plain_single_line_output(capsys):
    print_run_config(
        SimpleNamespace(
            knowledge_base=["data/knowledge"],
            embedding_provider="sentence_transformers",
            embedding_model="local-model",
            embedding_device=None,
            vectorstore_provider="milvus",
            vectorstore_path="data/indexes/rag",
            milvus_uri="C:/tmp/odr_milvus_eval.db",
            milvus_metric_type="COSINE",
            reranker_provider="cross_encoder",
            reranker_model="local-reranker",
            reranker_device=None,
            top_k=5,
            rerank_top_n=8,
        )
    )

    output = capsys.readouterr().out
    assert "\x1b[" not in output
    assert "knowledge_base: ['data/knowledge']" in output
    assert "top_k: 5" in output


# 验证类别统计与漏召说明采用纯文本格式。
def test_print_report_uses_plain_single_line_category_and_miss_output(capsys):
    scores = [
        {
            "id": "case_1",
            "category": "single_hop",
            "query": "why did the expected source miss?",
            "expected": ["faq.json"],
            "retrieved": ["runbook.txt"],
            "expected_not_retrieved": True,
            "expected_retrieved_but_low_rank": False,
            "retrieved_details": [
                {
                    "rank": 1,
                    "source": "runbook.txt",
                    "chunk_id": "doc-1-chunk-1",
                    "score": 0.8,
                    "vector_score": 0.7,
                    "keyword_score": 0.2,
                    "rerank_score": None,
                    "page_content": "Misleading preview",
                    "metadata": {"heading_path": ["Runbook"]},
                }
            ],
            "hit@5": 0.0,
            "recall@5": 0.0,
            "precision@5": 0.0,
            "mrr@5": 0.0,
            "evidence_hit@5": 0.0,
            "evidence_recall@5": 0.0,
            "evidence_precision@5": 0.0,
            "misleading_rate@5": 1.0,
            "authoritative_rate@5": 0.0,
            "avg_latency_ms": 12.3456,
        }
    ]

    print_report(
        scores,
        skipped=[],
        k=5,
        show_misses=True,
        index_stats={"num_documents": 2, "num_chunks": 3, "num_vectors": 3},
    )

    output = capsys.readouterr().out
    assert "\x1b[" not in output
    assert "num_documents: 2" in output
    assert "num_chunks: 3" in output
    assert "num_vectors: 3" in output
    assert "evidence_hit@5: 0.0" in output
    assert "misleading_rate@5: 1.0" in output
    assert "single_hop: count=1 hit@5=0.0 recall@5=0.0" in output
    assert "case_1 expected=['faq.json'] retrieved=['runbook.txt']" in output
    assert "case_id: case_1" in output
    assert "query: why did the expected source miss?" in output
    assert "expected_not_retrieved=True" in output
    assert "expected_retrieved_but_low_rank=False" in output
    assert "retrieved_rank_list: 1:runbook.txt#doc-1-chunk-1" in output
    assert "rank=1 source=runbook.txt chunk_id=doc-1-chunk-1" in output
    assert "vector_score=0.7000" in output
    assert "rerank_score=n/a" in output
    assert "page_content: Misleading preview" in output
    assert 'metadata: {"heading_path": ["Runbook"]}' in output


# 验证纯文本模式关闭进度条。
def test_configure_plain_output_disables_progress_bars(monkeypatch):
    monkeypatch.delenv("HF_HUB_DISABLE_PROGRESS_BARS", raising=False)
    monkeypatch.delenv("TOKENIZERS_PARALLELISM", raising=False)
    monkeypatch.delenv("TRANSFORMERS_NO_ADVISORY_WARNINGS", raising=False)

    configure_plain_output()

    import os

    assert os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] == "1"
    assert os.environ["TOKENIZERS_PARALLELISM"] == "false"
    assert os.environ["TRANSFORMERS_NO_ADVISORY_WARNINGS"] == "1"
