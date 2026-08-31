"""Agent specifications for the public-opinion workflow."""

from dataclasses import dataclass


@dataclass(frozen=True)
class PublicOpinionAgentSpec:
    """Encapsulates the contract and policy for one business agent."""

    role: str
    node_name: str
    display_name: str
    responsibility: str
    allowed_domains: frozenset[str]
    expected_output: str
    input_contract: tuple[str, ...]
    output_schema: tuple[str, ...]
    prompt: str
    tool_policy: tuple[str, ...]
    memory_policy: tuple[str, ...]
    execution_strategy: tuple[str, ...]
    handoff_policy: tuple[str, ...]

    def format_system_prompt(
        self,
        *,
        retrieval_tool_prompt: str,
        mcp_prompt: str,
        date: str,
        organization_context: str,
        private_memory_context: str = "No private memory has been recorded for this agent yet.",
    ) -> str:
        """Render this agent's dedicated system prompt."""
        return AGENT_SYSTEM_PROMPT_TEMPLATE.format(
            date=date,
            role=self.role,
            display_name=self.display_name,
            responsibility=self.responsibility,
            expected_output=self.expected_output,
            organization_context=organization_context,
            private_memory_context=private_memory_context,
            retrieval_tool_prompt=retrieval_tool_prompt,
            mcp_prompt=mcp_prompt,
            input_contract=_bullet_lines(self.input_contract),
            output_schema=_bullet_lines(self.output_schema),
            agent_prompt=self.prompt,
            tool_policy=_bullet_lines(self.tool_policy),
            memory_policy=_bullet_lines(self.memory_policy),
            execution_strategy=_numbered_lines(self.execution_strategy),
            handoff_policy=_bullet_lines(self.handoff_policy),
        )


def _bullet_lines(items: tuple[str, ...]) -> str:
    return "\n".join(f"- {item}" for item in items)


def _numbered_lines(items: tuple[str, ...]) -> str:
    return "\n".join(f"{index}. {item}" for index, item in enumerate(items, start=1))


AGENT_SYSTEM_PROMPT_TEMPLATE = """You are {display_name}, a specialized enterprise public-opinion and brand-risk monitoring agent. For context, today's date is {date}.

<Role>
{role}
</Role>

<Responsibility Boundary>
{responsibility}
</Responsibility Boundary>

<Expected Output>
{expected_output}
</Expected Output>

<Business Context>
{organization_context}
</Business Context>

<Private Agent Memory>
{private_memory_context}
</Private Agent Memory>

<Available Tools>
You have access to the configured research tools for this run:
{retrieval_tool_prompt}
{mcp_prompt}

Use think_tool after each retrieval step to reflect on evidence quality and decide whether to continue.
</Available Tools>

<Input Contract>
{input_contract}
</Input Contract>

<Dedicated Agent Prompt>
{agent_prompt}
</Dedicated Agent Prompt>

<Tool Policy>
{tool_policy}
</Tool Policy>

<Memory Policy>
{memory_policy}
</Memory Policy>

<Execution Strategy>
{execution_strategy}
</Execution Strategy>

<Output Schema>
Return a concise role report following this schema:
{output_schema}
</Output Schema>

<Handoff Policy>
{handoff_policy}
</Handoff Policy>

<Evidence Rules>
1. Do not overstate public sentiment from thin evidence.
2. For local RAG findings, preserve source paths, page/heading/field metadata, and citations.
3. If rag_search does not contain enough internal evidence, say so explicitly.
4. Distinguish facts, allegations, rumors, interpretations, and recommendations.
5. Keep dates concrete and absolute when available.
</Evidence Rules>"""
