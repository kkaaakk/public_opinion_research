# 执行研究基准评估，将研究图输出交给报告质量评估器。
import asyncio
import os
import uuid

import httpx
from dotenv import load_dotenv
from langsmith import Client

from tests.evaluators import (
    eval_completeness,
    eval_correctness,
    eval_groundedness,
    eval_overall_quality,
    eval_relevance,
    eval_structure,
)

load_dotenv("../.env")


# NOTE: Configure the right dataset and evaluators
dataset_name = "Deep Research Bench"
evaluators = [
    eval_overall_quality,
    eval_relevance,
    eval_structure,
    eval_correctness,
    eval_groundedness,
    eval_completeness,
]
# The API admits the model/search profile; operator budgets remain server-owned.
search_api = "tavily"
research_model = os.environ.get("EVALUATION_MODEL", "openai:gpt-4.1")


# 运行单条评估输入并返回评估器需要的研究结果。
async def target(
    inputs: dict,
):
    token = os.environ.get("PUBLIC_OPINION_API_TOKEN") or os.environ.get(
        "WEB_API_TOKEN"
    )
    if not token:
        raise RuntimeError(
            "The durable evaluation runner requires PUBLIC_OPINION_API_TOKEN"
        )
    key = uuid.uuid4().hex
    body = {
        "topic": inputs["messages"][0]["content"],
        "model": research_model,
        "search_api": search_api,
    }
    async with httpx.AsyncClient(
        base_url=os.environ.get("RESEARCH_API_URL", "http://127.0.0.1:8000"),
        headers={"Authorization": "Bearer " + token},
        timeout=30,
    ) as api:
        created = await api.post(
            "/api/runs", json=body, headers={"Idempotency-Key": key}
        )
        created.raise_for_status()
        run_id = created.json()["run_id"]
        while True:
            response = await api.get("/api/runs/" + run_id)
            response.raise_for_status()
            run = response.json()
            if run["status"] in {
                "completed",
                "failed",
                "interrupted",
                "cancelled",
                "awaiting_approval",
            }:
                break
            await asyncio.sleep(1)
    if run["status"] != "completed":
        raise RuntimeError("Evaluation Run requires reconciliation: " + run_id)
    return {
        "final_report": run["report"],
        "raw_notes": [stage["content"] for stage in run["role_reports"]],
        "research_brief": run["plan"]["brief"] if run.get("plan") else body["topic"],
    }


# 配置数据集、目标函数和评估器后启动综合评估。
async def main():
    client = Client()
    return await client.aevaluate(
        target,
        data=dataset_name,
        evaluators=evaluators,
        experiment_prefix=f"DBOS {research_model}, Tavily Search",
        max_concurrency=10,
        metadata={
            "execution_engine": "dbos",
            "research_model": research_model,
            "search_api": search_api,
            "grounding_context": "settled role reports; not raw tool observations",
        },
    )


if __name__ == "__main__":
    results = asyncio.run(main())
    print(results)
