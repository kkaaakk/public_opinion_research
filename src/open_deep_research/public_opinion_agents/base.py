# 定义业务角色的输入输出契约、工作步骤和工具权限。
"""Agent specifications for the public-opinion workflow."""

from dataclasses import dataclass


# 封装一个舆情角色的职责、工具领域及输出要求。
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
    report_policy: tuple[str, ...]
    execution_strategy: tuple[str, ...]
    handoff_policy: tuple[str, ...]

    # 将角色职责、权限、输入输出契约渲染为专用系统提示词。
    def format_system_prompt(
        self,
        *,
        retrieval_tool_prompt: str,
        mcp_prompt: str,
        date: str,
        organization_context: str,
        skill_prompt: str = "",
    ) -> str:
        """Render this agent's dedicated system prompt."""
        skills_section = (
            f"\n\n<Available Skills>\n{skill_prompt}\n</Available Skills>"
            if skill_prompt
            else ""
        )
        return AGENT_SYSTEM_PROMPT_TEMPLATE.format(
            date=date,
            role=self.role,
            display_name=self.display_name,
            responsibility=self.responsibility,
            expected_output=self.expected_output,
            organization_context=organization_context,
            retrieval_tool_prompt=retrieval_tool_prompt,
            mcp_prompt=mcp_prompt,
            skills_section=skills_section,
            input_contract=_bullet_lines(self.input_contract),
            output_schema=_bullet_lines(self.output_schema),
            agent_prompt=self.prompt,
            tool_policy=_bullet_lines(self.tool_policy),
            report_policy=_bullet_lines(self.report_policy),
            execution_strategy=_numbered_lines(self.execution_strategy),
            handoff_policy=_bullet_lines(self.handoff_policy),
        )


# 把条目列表排成项目符号文本。
def _bullet_lines(items: tuple[str, ...]) -> str:
    return "\n".join(f"- {item}" for item in items)


# 把步骤列表排成带序号的执行说明。
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

<Available Tools>
You have access to the configured research tools for this run:
{retrieval_tool_prompt}
{mcp_prompt}

Use the tools only when they add evidence. When the retrieved evidence is sufficient,
return your final role report as a normal assistant message; no completion tool exists.
</Available Tools>{skills_section}

<Input Contract>
{input_contract}
</Input Contract>

<Dedicated Agent Prompt>
{agent_prompt}
</Dedicated Agent Prompt>

<Tool Policy>
{tool_policy}
</Tool Policy>

<Report Policy>
{report_policy}
</Report Policy>

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
6. Cite only URLs, document paths and observations you actually retrieved. Never invent
   sources, identifiers or provenance metadata.
</Evidence Rules>"""
