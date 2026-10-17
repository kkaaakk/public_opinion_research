# 通过分词、可选命名实体识别和 IDF 过滤提取适合建图的术语。
"""Improved graph term extraction for GraphRAG.

This module provides ``TermExtractor``, which replaces the original regex-only
``extract_graph_terms()`` with:

- Optional NER integration (spaCy) for English entity extraction.
- Optional Chinese word segmentation (jieba).
- IDF-weighted filtering to remove high-frequency low-information terms.
- Extended stopword lists for English and Chinese.

All external dependencies (spaCy, jieba) are lazy-loaded. When unavailable,
the extractor falls back to the original regex tokenization with the extended
stopword list.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

TOKEN_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_-]{1,}|[\u4e00-\u9fff]+")
CJK_PATTERN = re.compile(r"[\u4e00-\u9fff]+")

# Extended English stopwords (~300 words)
ENGLISH_STOPWORDS = {
    # Core function words
    "a", "an", "the", "and", "or", "but", "not", "nor", "so", "yet",
    "if", "then", "than", "as", "at", "by", "in", "on", "of", "to",
    "for", "from", "into", "out", "off", "up", "down", "over", "under",
    "about", "above", "after", "again", "also", "am", "are", "been",
    "before", "being", "between", "both", "can", "could", "did", "do",
    "does", "doing", "done", "each", "few", "get", "got", "had", "has",
    "have", "having", "he", "her", "here", "hers", "herself", "him",
    "himself", "his", "how", "i", "is", "it", "its", "itself", "just",
    "let", "like", "ll", "may", "me", "might", "more", "most", "must",
    "my", "myself", "need", "no", "nor", "now", "only", "or", "other",
    "our", "ours", "ourselves", "own", "re", "s", "same", "shall", "she",
    "should", "some", "such", "t", "that", "the", "their", "theirs",
    "them", "themselves", "then", "there", "these", "they", "this", "those",
    "through", "too", "under", "until", "upon", "us", "ve", "very",
    "was", "we", "were", "what", "when", "where", "which", "while", "who",
    "whom", "why", "will", "with", "would", "you", "your", "yours",
    "yourself", "yourselves",
    # Common verbs / auxiliaries
    "make", "made", "go", "going", "come", "take", "took", "give", "gave",
    "say", "said", "see", "saw", "know", "knew", "think", "thought",
    "find", "found", "tell", "told", "ask", "asked", "use", "used",
    "try", "tried", "work", "worked", "call", "called", "need", "needed",
    "keep", "kept", "put", "set", "seem", "seemed", "show", "showed",
    "turn", "turned", "run", "ran", "move", "moved", "live", "lived",
    "believe", "bring", "happen", "happened", "write", "written",
    # Common nouns / generic terms
    "all", "also", "another", "any", "around", "away", "back", "because",
    "begin", "best", "better", "big", "came", "case", "cases", "change",
    "data", "day", "days", "different", "end", "even", "every", "example",
    "fact", "few", "file", "files", "first", "follow", "following", "form",
    "found", "general", "given", "good", "great", "group", "hand", "help",
    "high", "home", "however", "important", "include", "including",
    "information", "issue", "issues", "item", "items", "just", "keep",
    "kind", "large", "last", "later", "least", "left", "level", "life",
    "like", "line", "list", "little", "local", "long", "look", "main",
    "major", "many", "might", "more", "most", "much", "must", "name",
    "need", "never", "new", "next", "nothing", "now", "number", "often",
    "old", "once", "only", "open", "order", "other", "others", "part",
    "place", "point", "possible", "power", "process", "product", "program",
    "provide", "public", "question", "quite", "rather", "really", "record",
    "report", "result", "right", "same", "second", "section", "see", "set",
    "several", "show", "side", "since", "small", "something", "source",
    "start", "state", "still", "such", "support", "system", "take", "team",
    "test", "than", "that", "their", "them", "then", "there", "these",
    "they", "thing", "things", "this", "those", "through", "time",
    "together", "too", "type", "under", "until", "upon", "user", "using",
    "value", "version", "very", "way", "well", "while", "whole", "world",
    "year", "years", "yet",
}

# Chinese stopwords (~100 words)
CHINESE_STOPWORDS = {
    "的", "了", "在", "是", "我", "有", "和", "就", "不", "人", "都",
    "一", "一个", "上", "也", "很", "到", "说", "要", "去", "你", "会",
    "着", "没有", "看", "好", "自己", "这", "他", "她", "它", "们",
    "那", "些", "么", "什么", "为", "之", "与", "以", "及", "而",
    "但", "对", "从", "把", "被", "让", "给", "向", "比", "最", "更",
    "还", "又", "再", "已", "已经", "曾", "曾经", "正", "正在",
    "能", "可以", "可能", "应", "应该", "应当", "必须", "需要", "须",
    "该", "得", "想", "将", "会", "可", "所", "其", "此", "彼",
    "等", "等等", "如下", "以上", "以下", "之间", "之后", "之后",
    "之前", "以后", "以前", "由于", "因为",
}


# 管理语料术语统计及中英文候选提取，过滤低信息量词语。
class TermExtractor:
    """Extract and filter graph terms from text with optional NER/segmentation.

    Args:
        chunks: Optional iterable of chunk texts to build IDF weights from.
        ner_enabled: Whether to attempt spaCy NER for English entity extraction.
        idf_enabled: Whether to compute and apply IDF-based filtering.
        idf_threshold_percentile: Minimum IDF percentile for a term to be kept.
            Terms below this percentile are considered too common. Default 85.0.
    """

    # 保存 NER、分词和 IDF 选项，必要时根据语料建立词频统计。
    def __init__(
        self,
        *,
        chunks: Iterable[str] | None = None,
        ner_enabled: bool = True,
        idf_enabled: bool = True,
        idf_threshold_percentile: float = 85.0,
    ) -> None:
        self.ner_enabled = ner_enabled
        self.idf_enabled = idf_enabled
        self.idf_threshold_percentile = idf_threshold_percentile
        self._idf: dict[str, float] | None = None
        self._idf_threshold: float = 0.0
        self._nlp: Any | None = None
        self._ner_available: bool | None = None
        self._jieba_available: bool | None = None

        if chunks is not None:
            self._build_idf(list(chunks))

    # -- Lazy dependency loading ------------------------------------------------

    # 检查可选命名实体识别依赖是否可用。
    def _ner_is_available(self) -> bool:
        if self._ner_available is not None:
            return self._ner_available
        try:
            import spacy  # noqa: F401
            self._ner_available = True
        except ImportError:
            self._ner_available = False
        return self._ner_available

    # 检查可选中文分词依赖是否可用。
    def _jieba_is_available(self) -> bool:
        if self._jieba_available is not None:
            return self._jieba_available
        try:
            import jieba  # noqa: F401
            self._jieba_available = True
        except ImportError:
            self._jieba_available = False
        return self._jieba_available

    # 首次需要时加载 spaCy 英文管线，避免启动阶段强制加载模型。
    def _load_nlp(self) -> Any:
        """Lazy-load spaCy pipeline (en_core_web_sm)."""
        if self._nlp is not None:
            return self._nlp
        import spacy

        try:
            self._nlp = spacy.load("en_core_web_sm")
        except OSError:
            # Try downloading if not present
            spacy.cli.download("en_core_web_sm")  # type: ignore[union-attr]
            self._nlp = spacy.load("en_core_web_sm")
        return self._nlp

    # -- IDF computation -------------------------------------------------------

    # 遍历语料统计文档频率，并计算术语的逆文档频率。
    def _build_idf(self, chunk_texts: list[str]) -> None:
        """Build IDF scores from the corpus."""
        if not chunk_texts:
            self._idf = {}
            return

        doc_freq: dict[str, int] = defaultdict(int)
        total = len(chunk_texts)

        for text in chunk_texts:
            terms = self._extract_raw_terms(text)
            for term in set(terms):
                doc_freq[term] += 1

        # Use smooth IDF: log(1 + (N - df + 0.5) / (df + 0.5))
        self._idf = {
            term: math.log(1 + (total - freq + 0.5) / (freq + 0.5))
            for term, freq in doc_freq.items()
        }

        if self.idf_enabled and self._idf:
            sorted_idf = sorted(self._idf.values())
            idx = int(len(sorted_idf) * self.idf_threshold_percentile / 100.0)
            idx = min(idx, len(sorted_idf) - 1)
            self._idf_threshold = sorted_idf[idx]
        else:
            self._idf_threshold = 0.0

    # -- Core extraction ---------------------------------------------------------

    # 提取未经过 IDF 筛选的原始候选术语。
    def _extract_raw_terms(self, text: str) -> set[str]:
        """Extract raw candidate terms from text (no IDF filter)."""
        terms: set[str] = set()

        # English / general token extraction
        for token in TOKEN_PATTERN.findall(text):
            normalized = token.lower().strip("_-")
            if len(normalized) < 2 or normalized in ENGLISH_STOPWORDS:
                continue
            terms.add(normalized)

        # spaCy NER for English entities (if enabled and available)
        if self.ner_enabled and self._ner_is_available():
            try:
                nlp = self._load_nlp()
                doc = nlp(text)
                for ent in doc.ents:
                    if ent.label_ in (
                        "PERSON", "ORG", "GPE", "PRODUCT", "EVENT",
                        "LAW", "WORK_OF_ART", "FAC",
                    ):
                        entity = ent.text.lower().strip()
                        if len(entity) >= 2:
                            terms.add(entity)
            except Exception:
                # Graceful fallback if NER fails for any reason
                pass

        # Chinese jieba segmentation (if available)
        if self._jieba_is_available():
            try:
                import jieba

                for sentence in text.split("\n"):
                    # Segment CJK sentences
                    for char in sentence:
                        if "\u4e00" <= char <= "\u9fff":
                            break
                    else:
                        continue
                    # This line contains CJK, segment it
                    tokens = jieba.lcut(sentence)
                    for word in tokens:
                        word = word.strip()
                        if (
                            len(word) >= 2
                            and word not in CHINESE_STOPWORDS
                            and not all("\u4e00" <= c <= "\u9fff" and c in CHINESE_STOPWORDS for c in word)
                        ):
                            terms.add(word)
                    break
            except Exception:
                pass

        return terms

    # 提取候选词并按配置应用 IDF 等过滤规则。
    def extract(self, text: str) -> set[str]:
        """Extract graph terms from text with optional IDF filtering."""
        terms = self._extract_raw_terms(text)
        if not self.idf_enabled or self._idf is None:
            return terms
        # Filter by IDF threshold
        return {
            term for term in terms
            if self._idf.get(term, self._idf_threshold + 1.0) >= self._idf_threshold
        }

    # 返回某术语的 IDF；尚未建立统计时返回 None。
    def get_idf(self, term: str) -> float | None:
        """Return the IDF score for a term, or None if not computed."""
        if self._idf is None:
            return None
        return self._idf.get(term)


# 提供兼容旧签名的默认术语提取，不要求调用方传入语料。
def extract_graph_terms(text: str) -> set[str]:
    """Backward-compatible wrapper: extract terms using default TermExtractor.

    This function mirrors the original signature so callers don't need to
    change their interface. It uses a TermExtractor without IDF (since no
    corpus is provided in this single-call context).
    """
    extractor = TermExtractor(
        ner_enabled=False,
        idf_enabled=False,
    )
    return extractor.extract(text)
