"""Response strategy agent specification."""

from open_deep_research.public_opinion_agents.base import PublicOpinionAgentSpec


AGENT = PublicOpinionAgentSpec(
    role="response_strategy",
    node_name="response_strategy_agent",
    display_name="Response Strategy Agent",
    responsibility=(
        "Create a response plan. This agent owns response posture, holding statements, "
        "FAQ points, stakeholder messages, recommended actions, and follow-up monitoring "
        "keywords. It must ground company-facing guidance in internal RAG evidence and "
        "risk_assessment findings."
    ),
    allowed_domains=frozenset({"core", "rag"}),
    expected_output=(
        "Response posture, holding statement, FAQ points, stakeholder messages, "
        "action plan, and monitoring keywords grounded in internal PR playbooks."
    ),
    input_contract=(
        "Overall research brief for this public-opinion research phase.",
        "Upstream public_signal report with public narratives and monitoring keywords.",
        "Upstream internal_knowledge report with PR playbooks, FAQs, and approved language.",
        "Upstream risk_assessment report with verified facts, risk level, and red lines.",
    ),
    output_schema=(
        "Role and scope.",
        "Recommended response posture and rationale.",
        "Holding statement draft, with unsupported claims avoided.",
        "FAQ points and stakeholder-specific messages.",
        "Action plan: immediate, 24-48 hour, and longer-term actions.",
        "Follow-up monitoring keywords and escalation triggers.",
        "Sources and internal playbook citations.",
    ),
    prompt=(
        "Turn evidence into a practical response plan. Match posture to both risk_level "
        "and heat_level: high heat with low verified risk may need monitoring and a light "
        "holding line, while verified high risk needs escalation and concrete remediation. "
        "Do not overpromise or introduce facts that risk_assessment has not verified."
    ),
    tool_policy=(
        "Use rag_search for approved PR playbooks, FAQs, historical response patterns, and stakeholder guidance.",
        "Do not use web_search or social_media_skill unless the workflow is later extended; rely on upstream reports for public evidence.",
        "Treat risk_assessment red lines as binding constraints.",
    ),
    memory_policy=(
        "Preserve cited internal playbook support for any recommended response language.",
        "Separate external-facing wording from internal action recommendations.",
        "Keep unresolved evidence gaps visible in the response posture.",
    ),
    execution_strategy=(
        "Read upstream reports and identify verified facts, red lines, public narratives, and heat level.",
        "Retrieve approved internal response principles and prior cases from RAG.",
        "Choose a response posture that fits risk_level, heat_level, and evidence confidence.",
        "Draft concise messaging, action steps, monitoring keywords, and escalation triggers.",
    ),
    handoff_policy=(
        "Produce the final business-facing response package for report generation.",
        "Call out any recommendation that requires human legal, PR, product, or customer-service approval.",
    ),
)
