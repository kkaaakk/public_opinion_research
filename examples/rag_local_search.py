# 展示如何建立内存 RAG 索引并执行单次本地知识查询。
"""Minimal local RAG example for Open Deep Research."""

from pathlib import Path

from open_deep_research.rag.service import (
    RAGPipelineConfig,
    get_or_create_rag_pipeline,
    reset_rag_pipeline_cache,
)


# 创建本地内存检索管线，准备索引后执行示例查询并显示引用结果。
def main() -> None:
    """Build a local in-memory index and run a single RAG query."""
    example_data_dir = Path(__file__).resolve().parents[1] / "data" / "knowledge"
    reset_rag_pipeline_cache()

    pipeline = get_or_create_rag_pipeline(
        RAGPipelineConfig(
            knowledge_base_paths=[str(example_data_dir)],
            chunk_size=400,
            chunk_overlap=50,
            top_k=3,
            rerank_top_n=4,
        )
    )

    result = pipeline.query("What release controls and risk review rules apply to Atlas Launch?")
    print(result.context)


if __name__ == "__main__":
    main()
