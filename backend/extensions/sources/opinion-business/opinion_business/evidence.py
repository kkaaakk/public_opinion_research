"""Shared evidence contract; storage implementations migrate in a later phase."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    """An identifiable source excerpt; source_uri may refer to a local document."""

    evidence_id: str
    text: str
    source_uri: str


class EvidenceRepository(Protocol):
    """Lookup preserves identifiers used by risk findings and reports."""

    def get(self, evidence_id: str) -> EvidenceItem | None: ...
