"""Public signal collection boundary; no collector or credentials are configured here."""

from collections.abc import Sequence
from typing import Protocol

from .evidence import EvidenceItem


class PublicSignalCollector(Protocol):
    def collect(self, query: str, *, limit: int = 50) -> Sequence[EvidenceItem]: ...
