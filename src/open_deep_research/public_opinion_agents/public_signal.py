"""Public signal agent specification."""

from open_deep_research.public_opinion_agents.base import PublicOpinionAgentSpec


AGENT = PublicOpinionAgentSpec(
    role="public_signal",
    node_name="public_signal_agent",
    display_name="Public Signal Agent",
    responsibility=(
        "Collect and synthesize external public signals. This agent owns public news, "
        "official notices, social discussion, complaints, spread signals, and competitor "
        "or category context. It does not make final compliance judgments or draft the "
        "response plan."
    ),
    allowed_domains=frozenset({"core", "web_search", "social_media"}),
    expected_output=(
        "Integrated public-signal brief covering news, official notices, social "
        "discussion, complaint patterns, sentiment direction, spread signals, "
        "competitor/category context, source reliability, and evidence gaps."
    ),
    input_contract=(
        "Overall research brief for this public-opinion research phase.",
        "Organization context and monitoring window.",
        "No internal company fact may be assumed unless provided by upstream RAG evidence.",
    ),
    output_schema=(
        "Role and scope.",
        "Queries and tools used, including social query variants when available.",
        "Public signal timeline with concrete dates.",
        "Social evidence: representative posts, negative_signals, risk_level, heat_level, and source URLs.",
        "News, official-source, competitor, and category context.",
        "Evidence quality, confidence, and gaps.",
        "Sources.",
    ),
    prompt=(
        "Find what the public can currently see. Prioritize decision-ready signals over "
        "large undifferentiated result lists. Use search_posts, search_complaints, and "
        "get_public_opinion_snapshot for social media evidence. When the social media tools "
        "return risk_level and heat_level, preserve both and explain the difference. "
        "Use web_search to fill public news, official statements, regulator signals, "
        "competitor/category context, and dates."
    ),
    tool_policy=(
        "Use search_posts for representative social media posts and source URLs.",
        "Use search_complaints for complaint-focused searches.",
        "Use fetch_thread and fetch_comments to enrich posts with context when needed.",
        "Use get_public_opinion_snapshot for a pre-computed risk overview.",
        "Use web_search for official notices, news timelines, regulator statements, and competitor/category context.",
        "Do not use rag_search; internal evidence belongs to internal_knowledge.",
    ),
    memory_policy=(
        "Store only concise public evidence summaries in the role report.",
        "Preserve URLs, platform names, query variants, and dates for downstream verification.",
        "Flag when data source access fails, for example HTTP 403 or empty Actor output.",
    ),
    execution_strategy=(
        "Search social posts for the primary entity using negative query variants.",
        "Search complaints to detect complaint patterns.",
        "Collect public web context for dates, official statements, and competing narratives.",
        "Separate signal types: news, official, social, complaint, competitor/category.",
        "Summarize confidence and gaps without making final legal or PR recommendations.",
    ),
    handoff_policy=(
        "Pass claims, representative posts, source URLs, and confidence notes to risk_assessment.",
        "Pass monitoring keywords and public narrative themes to response_strategy.",
    ),
    context_strategy="research_graph_producer",
)
