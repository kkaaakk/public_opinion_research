# 使用结构化模型评分评估报告的质量、相关性、结构、正确性和证据支撑。
from typing import cast

from langchain_anthropic import ChatAnthropic
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from open_deep_research.time_utils import get_today_str
from tests.prompts import (
    COMPLETENESS_PROMPT,
    CORRECTNESS_PROMPT,
    GROUNDEDNESS_PROMPT,
    OVERALL_QUALITY_PROMPT,
    RELEVANCE_PROMPT,
    STRUCTURE_PROMPT,
)

eval_model = ChatOpenAI(
    model="gpt-4.1",
)

# 把评估输入整理为统一研究问题文本。
def _format_input_query(inputs: dict) -> str:
    messages = inputs["messages"]
    if len(messages) == 1:
        return messages[0]["content"]

    role_to_string_format_map = {
        "user": "<user_input>\n{content}\n</user_input>",
        "assistant": "<assistant_follow_up>\n{content}\n</assistant_follow_up>",
    }

    return "\n\n".join([role_to_string_format_map[message["role"]].format(content=message["content"]) for message in messages])


# 定义报告整体质量评分的结构化结果。
class OverallQualityScore(BaseModel):
    """Score the overall quality of the report against specific criteria."""
    research_depth: int = Field(description="Integer score 1-5 showing whether the report meets the provided criteria (1 = doesn't meet at all, 5 = meets all criteria).")
    source_quality: int = Field(description="Integer score 1-5 showing whether the report meets the provided criteria (1 = doesn't meet at all, 5 = meets all criteria).")
    analytical_rigor: int = Field(description="Integer score 1-5 showing whether the report meets the provided criteria (1 = doesn't meet at all, 5 = meets all criteria).")
    practical_value: int = Field(description="Integer score 1-5 showing whether the report meets the provided criteria (1 = doesn't meet at all, 5 = meets all criteria).")
    balance_and_objectivity: int = Field(description="Integer score 1-5 showing whether the report meets the provided criteria (1 = doesn't meet at all, 5 = meets all criteria).")
    writing_quality: int = Field(description="Integer score 1-5 showing whether the report meets the provided criteria (1 = doesn't meet at all, 5 = meets all criteria).")

# 按整体质量准则调用评估模型，并返回评分。
def eval_overall_quality(inputs: dict, outputs: dict):
    query = _format_input_query(inputs)
    final_report = outputs["final_report"]
    user_input_content = f"""User input: {query}\n\nReport: \n\n{final_report}\n\nEvaluate whether the report meets the criteria and provide detailed justification for your evaluation."""
    if isinstance(eval_model, ChatAnthropic):
        user_input_content = [{
            "type": "text",
            "text": user_input_content,
            "cache_control": {"type": "ephemeral", "ttl": "1h"}
        }]
    eval_result = cast(OverallQualityScore, eval_model.with_structured_output(OverallQualityScore).invoke([
        {"role": "system", "content": OVERALL_QUALITY_PROMPT.format(today=get_today_str())},
        {"role": "user", "content": user_input_content}
    ]))
    return [
        {"key": "research_depth_score", "score": eval_result.research_depth / 5},
        {"key": "source_quality_score", "score": eval_result.source_quality / 5},
        {"key": "analytical_rigor_score", "score": eval_result.analytical_rigor / 5},
        {"key": "practical_value_score", "score": eval_result.practical_value / 5},
        {"key": "balance_and_objectivity_score", "score": eval_result.balance_and_objectivity / 5},
        {"key": "writing_quality_score", "score": eval_result.writing_quality / 5},
    ]


# 定义报告与研究问题相关性的评分结果。
class RelevanceScore(BaseModel):
    """Score the report relevance against specific criteria."""
    reasoning: str = Field(description="The reason for the score, including specific examples from the report.")
    score: int = Field(description="Integer score 1-5 showing whether the report meets the provided criteria for relevance (1 = doesn't meet at all, 5 = meets all criteria).")

# 评估报告是否围绕用户问题与指定范围展开。
def eval_relevance(inputs: dict, outputs: dict):
    query = _format_input_query(inputs)
    final_report = outputs["final_report"]
    user_input_content = f"""User input: {query}\n\nReport: \n\n{final_report}\n\nEvaluate whether the report meets the criteria and provide detailed justification for your evaluation."""
    if isinstance(eval_model, ChatAnthropic):
        user_input_content = [{
            "type": "text",
            "text": user_input_content,
            "cache_control": {"type": "ephemeral", "ttl": "1h"}
        }]

    eval_result = cast(RelevanceScore, eval_model.with_structured_output(RelevanceScore).invoke([
        {"role": "system", "content": RELEVANCE_PROMPT.format(today=get_today_str())},
        {"role": "user", "content": user_input_content}
    ]))
    return {"key": "relevance_score", "score": eval_result.score / 5, "comment": eval_result.reasoning}


# 定义报告组织结构的评分结果。
class StructureScore(BaseModel):
    """Score the report structure against specific criteria."""
    reasoning: str = Field(description="The reason for the score, including specific examples from the report.")
    score: int = Field(description="Integer score 1-5 showing whether the report meets the provided criteria for structure and flow (1 = doesn't meet at all, 5 = meets all criteria).")

# 评估报告层次、组织及表达结构。
def eval_structure(inputs: dict, outputs: dict):
    query = _format_input_query(inputs)
    final_report = outputs["final_report"]
    user_input_content = STRUCTURE_PROMPT.format(user_question=query, report=final_report, today=get_today_str())
    if isinstance(eval_model, ChatAnthropic):
        user_input_content = [{
            "type": "text",
            "text": user_input_content,
            "cache_control": {"type": "ephemeral", "ttl": "1h"}
        }]

    eval_result = cast(StructureScore, eval_model.with_structured_output(StructureScore).invoke([
        {"role": "user", "content": user_input_content}
    ]))
    return {"key": "structure_and_cohesiveness_score", "score": eval_result.score / 5, "comment": eval_result.reasoning}


# 定义报告事实正确性的评分结果。
class CorrectnessScore(BaseModel):
    """Score the report correctness against specific criteria."""
    reasoning: str = Field(description="The reason for the score, including specific examples from the report.")
    score: int = Field(description="Integer score 1-5 showing whether the report meets the provided criteria for correctness (1 = doesn't meet at all, 5 = meets all criteria).")

# 根据评估准则判断报告内容是否准确。
def eval_correctness(inputs: dict, outputs: dict, reference_outputs: dict):
    query = _format_input_query(inputs)
    final_report = outputs["final_report"]
    answer = reference_outputs["answer"]
    user_input_content = CORRECTNESS_PROMPT.format(user_question=query, report=final_report, answer=answer, today=get_today_str())
    if isinstance(eval_model, ChatAnthropic):
        user_input_content = [{
            "type": "text",
            "text": user_input_content,
            "cache_control": {"type": "ephemeral", "ttl": "1h"}
        }]

    eval_result = cast(CorrectnessScore, eval_model.with_structured_output(CorrectnessScore).invoke([
        {"role": "user", "content": user_input_content}
    ]))
    return {"key": "correctness_score", "score": eval_result.score / 5, "comment": eval_result.reasoning}

# 保存单个报告主张及是否有上下文证据支撑的判断。
class GroundednessClaim(BaseModel):
    """A claim from the report, and whether or not it is grounded in the context"""
    claim: str = Field(description="The claim extracted from the report.")
    grounded: bool = Field(description="Whether the claim is grounded in the context.")

# 汇总报告主张的证据支撑判定。
class GroundednessScore(BaseModel):
    """Extract the claims and whether they are grounded in the context"""
    claims: list[GroundednessClaim] = Field(description="All claims extracted from the report, and whether or not they are grounded in the context.")

# 抽取并核验报告主张与研究上下文的对应关系。
def eval_groundedness(inputs: dict, outputs: dict):
    final_report = outputs["final_report"]
    context = str(outputs["raw_notes"])

    user_input_content = GROUNDEDNESS_PROMPT.format(context=context, report=final_report, today=get_today_str())
    if isinstance(eval_model, ChatAnthropic):
        user_input_content = [{
            "type": "text",
            "text": user_input_content,
            "cache_control": {"type": "ephemeral", "ttl": "1h"}
        }]

    eval_result = cast(GroundednessScore, eval_model.with_structured_output(GroundednessScore).with_retry(stop_after_attempt=3).invoke([
        {"role": "user", "content": user_input_content},
    ]))
    # normalize to 0-1
    grounded_claims = [claim for claim in eval_result.claims if claim.grounded]
    return {"key": "groundedness_score", "score": len(grounded_claims) / len(eval_result.claims), "comment": str(eval_result.claims)}


# 定义报告覆盖完整性的评分结果。
class CompletenessScore(BaseModel):
    """Score the report completeness against specific criteria."""
    reasoning: str = Field(description="The reason for the score, including specific examples from the report.")
    score: int = Field(description="Integer score 1-5 showing whether the report meets the provided criteria for completeness (1 = doesn't meet at all, 5 = meets all criteria).")

# 检查报告是否覆盖问题要求的主要方面。
def eval_completeness(inputs: dict, outputs: dict):
    query = _format_input_query(inputs)
    final_report = outputs["final_report"]
    research_brief = outputs["research_brief"]
    user_input_content = COMPLETENESS_PROMPT.format(user_question=query, research_brief=research_brief, report=final_report, today=get_today_str())
    if isinstance(eval_model, ChatAnthropic):
        user_input_content = [{
            "type": "text",
            "text": user_input_content,
            "cache_control": {"type": "ephemeral", "ttl": "1h"}
        }]

    eval_result = cast(CompletenessScore, eval_model.with_structured_output(CompletenessScore).invoke([
        {"role": "user", "content": user_input_content}
    ]))
    return {"key": "completeness_score", "score": eval_result.score / 5, "comment": eval_result.reasoning}