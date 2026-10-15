"""Evidence graph boundary; this does not migrate the legacy LangGraph workflow."""

from collections.abc import Sequence
from typing import Protocol

from .evidence import EvidenceItem


class ResearchGraphReader(Protocol):
    def evidence_for(self, topic: str, *, limit: int = 10) -> Sequence[EvidenceItem]: ...
