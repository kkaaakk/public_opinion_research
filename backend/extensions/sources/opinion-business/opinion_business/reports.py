"""Report composition contract; the legacy report pipeline has not been migrated."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from .evidence import EvidenceItem
from .risk import RiskFinding


@dataclass(frozen=True, slots=True)
class OpinionReport:
    title: str
    markdown: str
    evidence_ids: tuple[str, ...]


class ReportWriter(Protocol):
    def write(self, evidence: Sequence[EvidenceItem], findings: Sequence[RiskFinding]) -> OpinionReport: ...
