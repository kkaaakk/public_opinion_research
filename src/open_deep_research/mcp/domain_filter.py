"""Scene-based tool domain filtering and role-based tool routing.

All domain metadata lives in :data:`DOMAIN_REGISTRY` — the single source of
truth.  Keyword lists, prompt labels, and classifier descriptions are all
derived from it, so adding a new domain only requires one update.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field

from langchain_core.tools import BaseTool

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Domain Registry — single source of truth
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DomainDef:
    """Definition of one tool domain.

    Attributes
    ----------
    name:
        Unique key used in metadata and code (e.g. ``"database"``).
    label:
        Human-readable heading for system prompts
        (e.g. ``"Database Tools (DBHub)"``).
    description:
        One-line summary for LLM domain classification in
        ``write_research_brief``.
    always_active:
        If ``True``, this domain is never filtered out (always included).
    keywords:
        Trigger words for keyword-based fallback detection.  Case-insensitive
        match against the research topic.
    """

    name: str
    label: str
    description: str
    always_active: bool = False
    keywords: list[str] = field(default_factory=list)


# fmt: off
DOMAIN_REGISTRY: list[DomainDef] = [
    DomainDef(
        name="core",
        label="Core Tools (always available)",
        description="Strategic thinking and research completion — always active, never filtered.",
        always_active=True,
    ),
    DomainDef(
        name="web_search",
        label="Web Search",
        description="Live web search for external or up-to-date information — always active by default.",
        always_active=True,
    ),
    DomainDef(
        name="rag",
        label="Local Knowledge Base (RAG)",
        description="Search internal company documents, chat memory, or configured knowledge bases.",
        keywords=[
            "rag", "knowledge base", "internal", "company document",
            "chat memory", "local document", "知识库", "内部文档", "本地检索",
        ],
    ),
    DomainDef(
        name="database",
        label="Database Tools (DBHub)",
        description="SQL queries, schema exploration, table/index inspection for PostgreSQL, MySQL, SQLite, SQL Server, MariaDB.",
        keywords=[
            "sql", "query", "select", "insert", "update", "delete",
            "database", "db", "table", "schema", "postgres", "mysql",
            "sqlite", "mariadb", "column", "index", "ddl", "dml",
            "join", "transaction", "stored procedure", "view",
            "数据库", "查询", "表", "字段", "索引", "SQL",
        ],
    ),
    DomainDef(
        name="document",
        label="Document Conversion (MarkItDown)",
        description="Convert PDFs, Word documents, Excel files, PowerPoints, images, audio, HTML, and other formats to Markdown.",
        keywords=[
            "convert", "markdown", "pdf", "word", "excel", "ppt",
            "docx", "xlsx", "pptx", "file", "document", "extract",
            "ocr", "epub", "html to",
            "转换", "文件", "文档", "导出", "提取",
        ],
    ),
    DomainDef(
        name="feishu",
        label="Feishu / Lark",
        description="Send messages, create/edit documents, search knowledge bases, manage calendars, manipulate Bitable (多维表格) — all via Feishu/Lark Open API.",
        keywords=[
            "feishu", "lark", "飞书",
            "发送消息", "创建文档", "多维表格", "bitable",
            "日历", "消息", "云文档", "知识库", "wiki",
            "会议", "审批", "通讯录",
        ],
    ),
    DomainDef(
        name="social_media",
        label="Social Media & Public Opinion",
        description="Search social media posts, aggregate sentiment, track complaints, and monitor brand risk across platforms.",
        keywords=[
            "social media", "twitter", "x post", "complaint", "sentiment",
            "public opinion", "brand",
            "舆情", "投诉", "社交媒体", "品牌",
            "舆论", "负面", "好评",
        ],
    ),
    DomainDef(
        name="external_mcp",
        label="External MCP Tools",
        description="Additional tools from legacy or custom MCP server configurations.",
    ),
]
# fmt: on

# Fast lookups derived from the registry (kept in sync automatically)
_DOMAIN_BY_NAME: dict[str, DomainDef] = {d.name: d for d in DOMAIN_REGISTRY}
_DOMAIN_PROMPT_ORDER: list[tuple[str, str]] = [
    (d.name, d.label) for d in DOMAIN_REGISTRY
]

# Built-in (non-MCP) tool name → domain mapping
_BUILTIN_NAME_TO_DOMAIN: dict[str, str] = {
    "think_tool": "core",
    "ResearchComplete": "core",
    "web_search": "web_search",
    "rag_search": "rag",
}


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def get_domain(name: str) -> DomainDef | None:
    """Return the :class:`DomainDef` for *name*, or ``None``."""
    return _DOMAIN_BY_NAME.get(name)


def get_domain_description(name: str) -> str:
    """Return the one-line description for *name*."""
    dom = _DOMAIN_BY_NAME.get(name)
    return dom.description if dom else ""


def get_domain_label(name: str) -> str:
    """Return the human-readable label for *name*."""
    dom = _DOMAIN_BY_NAME.get(name)
    return dom.label if dom else name


def iter_domain_labels(domains: set[str]) -> list[tuple[str, str]]:
    """Return ordered ``(name, label)`` pairs for the given *domains*."""
    seen = {name for name, _ in _DOMAIN_PROMPT_ORDER if name in domains}
    # Also include any domains not in the order list
    for name in sorted(domains - seen):
        seen.add(name)
        _DOMAIN_PROMPT_ORDER.append((name, get_domain_label(name)))
    return [(name, label) for name, label in _DOMAIN_PROMPT_ORDER if name in domains]


# ---------------------------------------------------------------------------
# Tool domain resolution
# ---------------------------------------------------------------------------


_NATIVE_WEB_SEARCH_TYPES = frozenset(
    {"web_search_20250305", "web_search_preview"}
)


def get_tool_domain(tool) -> str | None:
    """Resolve a tool's explicit domain, or ``None`` when it is unclassified.

    MCP tools are classified from their ``tool_domain`` metadata. Known built-in
    tools and provider-native web-search declarations are also resolved here so
    business-agent filtering can apply one consistent domain policy.
    """
    metadata = tool.get("metadata") if isinstance(tool, Mapping) else getattr(tool, "metadata", None)
    if isinstance(metadata, Mapping) and "tool_domain" in metadata:
        return str(metadata["tool_domain"])

    if isinstance(tool, Mapping):
        name = tool.get("name", "")
        function = tool.get("function")
        if not name and isinstance(function, Mapping):
            name = function.get("name", "")
        tool_type = tool.get("type")
    else:
        name = getattr(tool, "name", "")
        tool_type = None

    if name in _BUILTIN_NAME_TO_DOMAIN:
        return _BUILTIN_NAME_TO_DOMAIN[name]
    if tool_type in _NATIVE_WEB_SEARCH_TYPES:
        return "web_search"
    return None


def _tool_domain(tool) -> str:
    """Resolve a tool's domain for grouping and prompt display."""
    return get_tool_domain(tool) or "external_mcp"


def tag_tools_with_domain(tools: list, domain: str) -> list:
    """Attach a validated ``tool_domain`` to LangChain tools.

    Provider-native tool dictionaries are intentionally left unchanged because
    their schema is provider-owned; ``get_tool_domain`` resolves known native
    web-search declarations without treating arbitrary dictionaries as safe.
    """
    if domain not in _DOMAIN_BY_NAME:
        raise ValueError(f"Unknown tool domain: {domain}")
    for tool in tools:
        if isinstance(tool, Mapping):
            continue
        metadata = getattr(tool, "metadata", None) or {}
        tool.metadata = {**metadata, "tool_domain": domain}
    return tools


def classify_tools(tools: list[BaseTool]) -> dict[str, list[BaseTool]]:
    """Group *tools* by their domain category."""
    buckets: dict[str, list[BaseTool]] = {}
    for tool in tools:
        domain = _tool_domain(tool)
        buckets.setdefault(domain, []).append(tool)
    return buckets


def tool_domain_summary(tools: list[BaseTool]) -> str:
    """Return a human-readable summary of tools grouped by domain."""
    buckets = classify_tools(tools)
    lines: list[str] = []
    for domain in sorted(buckets):
        names = sorted(t.name for t in buckets[domain])
        lines.append(f"  [{domain}] {', '.join(names)}")
    return "\n".join(lines)
