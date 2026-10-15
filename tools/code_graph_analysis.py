# 静态分析代码符号、导入和调用关系，生成架构学习所用的图数据。
"""Build a best-effort static symbol call graph for interview architecture review."""

from __future__ import annotations

import ast
import json
from collections import defaultdict
from pathlib import Path

import networkx as nx

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "open_deep_research"
OUT = ROOT / "docs" / "interview-architecture"


# 将源文件相对路径转换为 Python 模块名。
def module_name(path: Path) -> str:
    rel = path.relative_to(ROOT / "src").with_suffix("")
    return ".".join(rel.parts)


# 保存单文件 AST、导入别名及顶层和类方法符号。
class FileIndex:
    # 解析文件 AST，索引导入、函数、类和方法定义。
    def __init__(self, path: Path) -> None:
        self.path = path
        self.module = module_name(path)
        self.tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        self.imports: dict[str, str] = {}
        self.symbols: dict[str, ast.AST] = {}
        self.classes: set[str] = set()
        for node in self.tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.imports[alias.asname or alias.name.split(".")[0]] = alias.name
            elif isinstance(node, ast.ImportFrom):
                prefix = node.module or ""
                for alias in node.names:
                    self.imports[alias.asname or alias.name] = f"{prefix}.{alias.name}".strip(".")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.symbols[node.name] = node
            elif isinstance(node, ast.ClassDef):
                self.classes.add(node.name)
                self.symbols[node.name] = node
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        self.symbols[f"{node.name}.{child.name}"] = child


# 递归还原名称或属性访问链，动态表达式无法解析时返回 None。
def attr_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = attr_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return None


# 结合导入别名、当前类和文件符号，尽力解析调用目标。
def resolve_call(index: FileIndex, owner: str, call: ast.Call) -> str:
    raw = attr_name(call.func) or "<dynamic>"
    if raw.startswith("self.") and "." in owner:
        cls = owner.split(".", 1)[0]
        return f"{index.module}::{cls}.{raw.split('.', 1)[1]}"
    first, *rest = raw.split(".")
    if first in index.imports:
        return index.imports[first] + ("." + ".".join(rest) if rest else "")
    if raw in index.symbols:
        return f"{index.module}::{raw}"
    if first in index.classes and rest:
        return f"{index.module}::{raw}"
    return raw


# 扫描源码构建有向调用图，并记录并发相关调用位置。
def build_graph() -> tuple[nx.DiGraph, list[dict], dict[str, FileIndex]]:
    indexes = {idx.module: idx for idx in (FileIndex(p) for p in SRC.rglob("*.py"))}
    graph = nx.DiGraph()
    concurrency: list[dict] = []
    concurrency_targets = {
        "asyncio.gather": "fan_out",
        "asyncio.to_thread": "thread_pool_offload",
        "threading.Thread": "background_thread",
        "Send": "langgraph_dynamic_send",
        "langgraph.types.Send": "langgraph_dynamic_send",
        "asyncio.Semaphore": "request_semaphore",
    }
    for index in indexes.values():
        for symbol, node in index.symbols.items():
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            source = f"{index.module}::{symbol}"
            graph.add_node(
                source,
                module=index.module,
                symbol=symbol,
                file=str(index.path.relative_to(ROOT)).replace("\\", "/"),
                lineno=node.lineno,
                async_fn=isinstance(node, ast.AsyncFunctionDef),
                internal=True,
            )
            for child in ast.walk(node):
                if not isinstance(child, ast.Call):
                    continue
                target = resolve_call(index, symbol, child)
                graph.add_node(target, internal="::" in target)
                if graph.has_edge(source, target):
                    graph[source][target]["count"] += 1
                    graph[source][target]["lines"].append(child.lineno)
                else:
                    graph.add_edge(source, target, count=1, lines=[child.lineno])
                normalized = target.split("::")[-1]
                for key, kind in concurrency_targets.items():
                    if normalized == key or normalized.endswith("." + key.split(".")[-1]):
                        concurrency.append(
                            {
                                "kind": kind,
                                "source": source,
                                "target": target,
                                "file": str(index.path.relative_to(ROOT)).replace("\\", "/"),
                                "line": child.lineno,
                            }
                        )
    return graph, concurrency, indexes


# 执行静态分析并写出架构图及统计产物。
def main() -> None:
    graph, concurrency, indexes = build_graph()
    internal = graph.subgraph([n for n, d in graph.nodes(data=True) if d.get("internal")]).copy()
    pagerank = nx.pagerank(internal) if internal else {}
    betweenness = nx.betweenness_centrality(internal) if internal else {}
    ranking = []
    for node in internal.nodes:
        ranking.append(
            {
                "node": node,
                "in_degree": internal.in_degree(node),
                "out_degree": internal.out_degree(node),
                "pagerank": pagerank.get(node, 0.0),
                "betweenness": betweenness.get(node, 0.0),
                **internal.nodes[node],
            }
        )
    ranking.sort(key=lambda item: (item["betweenness"], item["pagerank"], item["in_degree"]), reverse=True)
    components = [sorted(c) for c in nx.strongly_connected_components(internal) if len(c) > 1]
    module_edges: dict[tuple[str, str], int] = defaultdict(int)
    for left, right, data in internal.edges(data=True):
        lm = internal.nodes[left].get("module", str(left).split("::")[0])
        rm = internal.nodes[right].get("module", str(right).split("::")[0])
        if lm != rm:
            module_edges[(lm, rm)] += int(data.get("count", 1))
    payload = {
        "baseline": "origin/main@12e2f0e",
        "python_files": len(indexes),
        "symbol_nodes": internal.number_of_nodes(),
        "internal_call_edges": internal.number_of_edges(),
        "all_nodes": graph.number_of_nodes(),
        "all_edges": graph.number_of_edges(),
        "top_symbols": ranking[:40],
        "concurrency_points": concurrency,
        "strongly_connected_components": components,
        "module_dependencies": [
            {"from": a, "to": b, "calls": count}
            for (a, b), count in sorted(module_edges.items(), key=lambda item: item[1], reverse=True)
        ],
        "core_paths": {
            "request": [
                "open_deep_research.web.server::research",
                "open_deep_research.deep_researcher::write_research_brief",
                "open_deep_research.deep_researcher::plan_report_sections",
                "open_deep_research.deep_researcher::research_phase",
                "open_deep_research.runtime.business_agent::run_business_agent",
                "open_deep_research.runtime.agent_runtime::AgentRuntime.run",
                "open_deep_research.deep_researcher::compile_final_report",
            ],
            "tool": [
                "open_deep_research.utils::get_all_tools",
                "open_deep_research.mcp.tools::load_mcp_tools",
                "open_deep_research.runtime.business_agent::_business_agent_tools",
                "open_deep_research.runtime.agent_runtime::AgentRuntime.run",
                "open_deep_research.runtime.business_agent::_execute_tool_safely",
            ],
            "rag": [
                "open_deep_research.tools.rag_tool::rag_search",
                "open_deep_research.rag.service::RAGPipeline.query",
                "open_deep_research.rag.retriever::HybridChunkRetriever.retrieve",
                "open_deep_research.rag.retriever::reciprocal_rank_fusion",
                "open_deep_research.rag.reranker::CrossEncoderReranker.rerank",
            ],
        },
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "code-graph-analysis.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    graphml = nx.DiGraph()
    for node, data in internal.nodes(data=True):
        graphml.add_node(node, **{k: v for k, v in data.items() if isinstance(v, (str, int, float, bool))})
    for left, right, data in internal.edges(data=True):
        graphml.add_edge(
            left,
            right,
            count=int(data.get("count", 1)),
            lines=",".join(str(value) for value in data.get("lines", [])),
        )
    nx.write_graphml(graphml, OUT / "code-graph.graphml")
    print(json.dumps({k: payload[k] for k in ("python_files", "symbol_nodes", "internal_call_edges", "all_nodes", "all_edges")}, indent=2))
    print("TOP")
    for item in ranking[:15]:
        print(item["node"], round(item["betweenness"], 4), item["in_degree"], item["out_degree"])
    print("CONCURRENCY", len(concurrency))


if __name__ == "__main__":
    main()
