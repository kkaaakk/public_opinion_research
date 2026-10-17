# 提供直接执行研究图的调试脚本，便于查看流程输出。
import asyncio, uuid
from dotenv import load_dotenv
from langgraph.checkpoint.memory import MemorySaver
from open_deep_research.deep_researcher import deep_researcher_builder

load_dotenv()

graph = deep_researcher_builder.compile(checkpointer=MemorySaver())
config = {
    "configurable": {
        "thread_id": str(uuid.uuid4()),
        "allow_clarification": False,
        "search_api": "tavily",
    }
}

asyncio.run(
    graph.ainvoke(
        {"messages": [{"role": "user", "content": "帮我研究一下 OpenAI 和 Anthropic 的最新模型差异"}]},
        config,
    )
)
