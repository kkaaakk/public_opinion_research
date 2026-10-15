"""Sentiment contract; Phase 0 does not provide a classifier."""

from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True, slots=True)
class SentimentResult:
    label: Literal["positive", "neutral", "negative", "mixed", "unknown"]
    explanation: str


class SentimentAnalyzer(Protocol):
    def analyze(self, text: str) -> SentimentResult: ...
