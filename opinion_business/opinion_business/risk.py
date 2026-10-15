"""Evidence-linked risk assessment contract; no scoring implementation is provided."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from .evidence import EvidenceItem


@dataclass(frozen=True, slots=True)
class RiskFinding:
    summary: str
    severity: Literal["low", "medium", "high", "unknown"]
    evidence_ids: tuple[str, ...]


class RiskAssessor(Protocol):
    def assess(self, evidence: Sequence[EvidenceItem]) -> Sequence[RiskFinding]: ...
