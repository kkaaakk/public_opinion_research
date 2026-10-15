"""Retrieval boundary, independent of any vector database or agent runtime."""

from collections.abc import Sequence
from typing import Protocol

from .evidence import EvidenceItem


class EvidenceRetriever(Protocol):
    def search(self, query: str, *, limit: int = 10) -> Sequence[EvidenceItem]: ...
