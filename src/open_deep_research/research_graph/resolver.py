"""Thin claim/entity resolution boundary for future graph deployments.

Exact deterministic IDs handle identical claims in the current batch adapter.
This module provides the small candidate/judge interface needed for semantic
claim canonicalization without doing an all-pairs LLM comparison.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from typing import Any, Literal

from pydantic import BaseModel, Field

from open_deep_research.research_graph.models import Claim
from open_deep_research.research_graph.schema import normalize_text


class ClaimResolutionDecision(BaseModel):
    """Decision returned by the thin candidate/judge resolver."""

    decision: Literal["SAME_PROPOSITION", "DIFFERENT", "UNKNOWN"]
    candidate_claim_id: str = ""
    similarity: float = Field(default=0.0, ge=0, le=1)
    rationale: str = ""


ClaimJudge = Callable[[Claim, Claim], Awaitable[ClaimResolutionDecision | dict[str, Any]]]


class ClaimResolver:
    """Resolve only high-similarity candidates, never every claim pair."""

    def __init__(
        self,
        *,
        similarity_threshold: float = 0.86,
        max_candidates: int = 5,
        judge: ClaimJudge | None = None,
    ) -> None:
        """Configure candidate retrieval and the optional semantic judge."""
        self.similarity_threshold = max(0.0, min(1.0, similarity_threshold))
        self.max_candidates = max(1, int(max_candidates))
        self.judge = judge

    async def resolve(
        self,
        claim: Claim,
        candidates: Iterable[Claim],
    ) -> ClaimResolutionDecision:
        """Return SAME/DIFFERENT/UNKNOWN for the strongest cheap candidates."""
        ranked = sorted(
            (
                (_similarity(claim.statement, candidate.statement), candidate)
                for candidate in candidates
                if candidate.run_id == claim.run_id and candidate.claim_id != claim.claim_id
            ),
            key=lambda item: item[0],
            reverse=True,
        )[: self.max_candidates]
        if not ranked or ranked[0][0] < self.similarity_threshold:
            return ClaimResolutionDecision(decision="UNKNOWN")
        similarity, candidate = ranked[0]
        if self.judge is None:
            if normalize_text(claim.statement) == normalize_text(candidate.statement):
                return ClaimResolutionDecision(
                    decision="SAME_PROPOSITION",
                    candidate_claim_id=candidate.claim_id,
                    similarity=similarity,
                    rationale="Exact normalized proposition match.",
                )
            return ClaimResolutionDecision(
                decision="UNKNOWN",
                candidate_claim_id=candidate.claim_id,
                similarity=similarity,
                rationale="Candidate is similar but no semantic judge was configured.",
            )
        decision = await self.judge(claim, candidate)
        if isinstance(decision, ClaimResolutionDecision):
            return decision
        return ClaimResolutionDecision.model_validate(decision)


def _similarity(left: str, right: str) -> float:
    left_terms = set(normalize_text(left).split())
    right_terms = set(normalize_text(right).split())
    if not left_terms or not right_terms:
        return 0.0
    return len(left_terms & right_terms) / len(left_terms | right_terms)


__all__ = ["ClaimResolutionDecision", "ClaimResolver"]
