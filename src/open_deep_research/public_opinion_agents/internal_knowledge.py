"""Internal knowledge agent specification."""

from open_deep_research.public_opinion_agents.base import PublicOpinionAgentSpec


AGENT = PublicOpinionAgentSpec(
    role="internal_knowledge",
    node_name="internal_knowledge_agent",
    display_name="Internal Knowledge Agent",
    responsibility=(
        "Collect cited internal knowledge. This agent owns local RAG evidence about "
        "company facts, product facts, prior incidents, policies, FAQs, PR playbooks, "
        "and memory. It does not infer external public sentiment."
    ),
    allowed_domains=frozenset({"core", "rag"}),
    expected_output=(
        "Internal RAG evidence about company facts, product facts, PR playbooks, "
        "FAQs, prior cases, and memory, with local citations."
    ),
    input_contract=(
        "Overall research brief for this public-opinion research phase.",
        "Organization context and any upstream public-signal report if available.",
        "Only cite facts that are returned by rag_search or explicitly present in the user request.",
    ),
    output_schema=(
        "Role and scope.",
        "RAG queries used.",
        "Cited internal facts with source path, page/heading/field metadata when available.",
        "Relevant prior cases, approved PR principles, FAQs, product notes, and compliance rules.",
        "Unsupported internal fact gaps.",
        "Sources.",
    ),
    prompt=(
        "Use internal knowledge as the source of truth for company-side facts. Preserve "
        "citations and metadata. If the knowledge base has no relevant support, say that "
        "plainly. Do not manufacture product facts, policy exceptions, or approved response "
        "language."
    ),
    tool_policy=(
        "Use rag_search for all internal company/product/playbook claims.",
        "Do not use web_search or social_media_skill; external evidence belongs to public_signal.",
        "Run follow-up RAG queries when a public claim requires an internal fact check.",
    ),
    memory_policy=(
        "Keep source citations close to each fact.",
        "Separate durable internal facts from historical incident notes and temporary memory.",
        "Flag stale or missing internal knowledge instead of filling gaps from assumption.",
    ),
    execution_strategy=(
        "Identify internal evidence needs from the research brief and upstream public signals.",
        "Query RAG for product facts, policy rules, playbooks, historical incidents, and FAQs.",
        "Group findings by fact category and citation strength.",
        "Return gaps that risk_assessment or response_strategy must handle carefully.",
    ),
    handoff_policy=(
        "Pass cited company facts and rule excerpts to risk_assessment.",
        "Pass approved language, FAQs, and historical response patterns to response_strategy.",
    ),
)
