"""Score actual RAG chunks and tool excerpts against the POR gold locators."""

from __future__ import annotations

import json
import math
import os
import re
import statistics
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from open_deep_research.rag.types import AnswerReadyContext

VIEWS = ("internal", "delivered")
LOCATORS = (
    "heading_path",
    "page",
    "json_pointer",
    "line_start",
    "line_end",
    "region",
    "coordinate_system",
)
CHUNK_LOCATORS = (
    "heading_path",
    "page_number",
    "json_pointer",
    "field_paths",
    "primary_json_path",
    "char_start",
    "char_end",
    "line_start",
    "line_end",
    "region",
    "coordinate_system",
)
PAGE_SUFFIX = re.compile(r"#page=(\d+)$")
JSON_PATH_STEP = re.compile(
    r"\.([A-Za-z_][A-Za-z0-9_]*)|\[(\d+)\]|\['((?:\\.|[^'\\])*)'\]"
)
CONTEXT_HEADER = re.compile(r"^--- SOURCE \d+: .* ---$", re.MULTILINE)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class DocumentCatalog:
    """Match the loader's source paths without guessing from a basename."""

    def __init__(self, directory: Path, rows: list[dict[str, Any]]):
        self.directory = directory.resolve()
        self.by_id = {row["doc_id"]: row for row in rows}
        self.by_path = {
            self._path_key(self.directory / row["file_path"]): row for row in rows
        }
        if len(self.by_id) != len(rows) or len(self.by_path) != len(rows):
            raise ValueError("Duplicate document ID or path in the manifest")

    @staticmethod
    def _path_key(path: Path) -> str:
        return os.path.normcase(str(path.resolve()))

    def resolve(self, source: str) -> dict[str, Any] | None:
        path = Path(PAGE_SUFFIX.sub("", source).replace("\\", "/"))
        if path.is_absolute():
            return self.by_path.get(self._path_key(path))
        return self.by_path.get(self._path_key(path)) or self.by_path.get(
            self._path_key(self.directory / path)
        )


def fold(text: str) -> str:
    return "".join(unicodedata.normalize("NFKC", text).casefold().split())


def _visible_text(excerpt: str, body: str) -> tuple[str, bool, bool]:
    """Use the actual excerpt and recover only its whitespace/offset alignment."""
    flattened = " ".join(body.split())
    if excerpt == flattened:
        return body, True, False
    # A formatter ellipsis is not evidence. Keep a genuine ellipsis in the source.
    if excerpt.endswith("...") and not flattened.startswith(excerpt):
        excerpt = excerpt[:-3].rstrip()
    if not excerpt or not flattened.startswith(excerpt):
        return excerpt, False, excerpt != flattened
    remaining = len(excerpt)
    for word in re.finditer(r"\S+", body):
        if remaining <= len(word[0]):
            return body[: word.start() + remaining], True, True
        remaining -= len(word[0]) + 1
    return excerpt, False, True


def result_views(
    result: AnswerReadyContext, catalog: DocumentCatalog
) -> dict[str, list[dict]]:
    """Keep separate views of matched chunks and source blocks actually emitted."""
    internal = []
    for rank, match in enumerate(result.matched_chunks, 1):
        chunk = match.chunk
        document = catalog.resolve(chunk.source)
        internal.append(
            {
                "rank": rank,
                "source": chunk.source,
                "doc_id": document["doc_id"] if document else None,
                "source_status": document["source_status"] if document else None,
                "chunk_id": chunk.chunk_id,
                "content": chunk.content,
                "locator": {
                    key: chunk.metadata[key]
                    for key in CHUNK_LOCATORS
                    if key in chunk.metadata
                },
                "scores": match.model_dump(exclude={"chunk"}),
            }
        )

    blocks = CONTEXT_HEADER.split(result.context)
    headers = CONTEXT_HEADER.findall(result.context)
    emitted = dict(zip(headers, blocks[1:], strict=True))
    by_chunk = {(row["source"], row["chunk_id"]): row for row in internal}
    delivered = []
    for citation in result.citations:
        header = f"--- SOURCE {citation.citation_id}: {citation.title} ---"
        source_lines = f"\nSOURCE: {citation.source}\nCHUNK ID: {citation.chunk_id}\n"
        block = emitted.get(header, "")
        if not block.startswith(source_lines):
            continue
        _, marker, after = block[len(source_lines) :].partition("EXCERPT:\n")
        excerpt = after.partition("\n")[0]
        if not marker or not excerpt or not citation.excerpt.startswith(excerpt):
            continue
        match = by_chunk.get((citation.source, citation.chunk_id))
        if match is None:
            continue
        delivered.append(
            {
                **match,
                "rank": citation.citation_id,
                "content": excerpt,
            }
        )
    return {"internal": internal, "delivered": delivered}


def _pointer_tokens(path: str) -> tuple[str, ...] | None:
    """Translate the splitter's JSONPath leaf syntax to JSON Pointer segments."""
    if path.startswith("/"):
        return tuple(
            part.replace("~1", "/").replace("~0", "~") for part in path[1:].split("/")
        )
    if not path.startswith("$"):
        return None
    tokens = []
    end = 1
    for match in JSON_PATH_STEP.finditer(path, 1):
        if match.start() != end:
            return None
        name, index, quoted = match.groups()
        tokens.append(name or index or re.sub(r"\\(.)", r"\1", quoted or ""))
        end = match.end()
    return tuple(tokens) if end == len(path) else None


def _json_field(
    body: str,
    paths: dict[tuple[str, ...], str],
    target: tuple[str, ...],
) -> tuple[str, bool]:
    """Isolate the returned value, so a sibling field cannot supply the evidence."""

    def label(tokens: tuple[str, ...]) -> str:
        key = tokens[-1]
        return (
            f"[{key}]"
            if key.isdecimal() and paths[tokens].endswith(f"[{key}]")
            else key
        )

    target_label = label(target)
    labels = [label(path) for path in paths if path]
    if labels.count(target_label) != 1:
        return "", False
    fields = list(
        re.finditer(
            r"^(" + "|".join(map(re.escape, labels)) + r"):[ \t]+", body, re.MULTILINE
        )
    )
    matches = [index for index, field in enumerate(fields) if field[1] == target_label]
    if len(matches) != 1:
        return "", False
    index = matches[0]
    next_field = fields[index + 1].start() if index + 1 < len(fields) else None
    return body[fields[index].end() : next_field].rstrip("\r\n"), next_field is not None


@dataclass(frozen=True)
class Part:
    rank: int
    body: str
    start: int | None
    scalar: bool = False
    clipped: bool = False
    text_only: bool = False


def _part(
    evidence: dict,
    file_type: str,
    row: dict,
    original: dict | None,
) -> Part | None:
    metadata = row["locator"]
    body = row["content"]
    aligned, clipped = True, False
    if original is not None:
        body, aligned, clipped = _visible_text(body, original["content"])
    start, end = metadata.get("char_start"), metadata.get("char_end")
    span_body = original["content"] if original is not None else body
    span_start = (
        start
        if aligned and isinstance(start, int) and end == start + len(span_body)
        else None
    )
    scalar = False
    text_only = False

    if file_type == "md":
        if list(map(fold, metadata.get("heading_path", []))) != list(
            map(fold, evidence["heading_path"])
        ):
            return None
    elif file_type == "pdf":
        suffix = PAGE_SUFFIX.search(row["source"])
        page = int(suffix[1]) if suffix else metadata.get("page_number")
        if page != evidence["page"] or metadata.get("page_number", page) != page:
            return None
    elif file_type == "txt":
        lo, hi = metadata.get("line_start"), metadata.get("line_end")
        if not (
            isinstance(lo, int)
            and isinstance(hi, int)
            and hi >= evidence["line_start"]
            and lo <= evidence["line_end"]
        ):
            return None
    elif file_type == "json":
        target = _pointer_tokens(evidence["json_pointer"])
        paths = {
            parsed: path
            for path in [
                metadata.get("json_pointer", ""),
                *metadata.get("field_paths", []),
            ]
            if (parsed := _pointer_tokens(path))
        }
        if not aligned or not target or target not in paths:
            return None
        body, field_has_boundary = _json_field(body, paths, target)
        clipped = clipped and not field_has_boundary
        scalar = evidence.get("exact_value", False) or bool(
            re.fullmatch(
                r"(?:-?\d+(?:\.\d+)?(?:e[+-]?\d+)?|true|false|null)",
                fold(evidence["evidence_text"]),
            )
        )
        # The service records field-relative spans for long, split scalar values.
        if (
            _pointer_tokens(metadata.get("primary_json_path", "")) == target
            and isinstance(start, int)
            and isinstance(end, int)
            and (
                (not clipped and end == start + len(body))
                or (clipped and start + len(body) <= end)
            )
        ):
            span_start = start
        else:
            span_start = None

    if "region" in evidence:
        region = metadata.get("region")
        if region is None:
            text_only = True
        elif (
            metadata.get("coordinate_system") != evidence["coordinate_system"]
            or list(region) != evidence["region"]
        ):
            return None
    return Part(row["rank"], body, span_start, scalar, clipped, text_only)


def _contains(needle: str, text: str) -> bool:
    body = fold(text)
    if re.fullmatch(r"[+-]?\d+(?:[.,]\d+)*", needle):
        return bool(
            re.search(r"(?<![\d.,])" + re.escape(needle) + r"(?!\d|[.,]\d)", body)
        )
    return needle in body


def _cross_chunk_cover(needle: str, parts: list[Part]) -> tuple[int, ...]:
    """Join contiguous source spans, checking overlaps against returned characters."""
    located = sorted(
        (
            part
            for part in parts
            if part.start is not None and part.body and not part.scalar
        ),
        key=lambda part: (part.start, -len(part.body), part.rank),
    )
    for index, first in enumerate(located):
        joined = first.body
        spans = [(first.rank, first.body)]
        start = first.start
        for part in located[index + 1 :]:
            end = start + len(joined)
            if part.start > end:
                break
            overlap = end - part.start
            if overlap >= len(part.body) or (
                overlap and joined[-overlap:] != part.body[:overlap]
            ):
                continue
            suffix = part.body[overlap:]
            joined += suffix
            spans.append((part.rank, suffix))
            if _contains(needle, joined):
                while len(spans) > 1 and _contains(
                    needle, "".join(text for _, text in spans[1:])
                ):
                    spans.pop(0)
                return tuple(rank for rank, _ in spans)
    return ()


def _evidence_key(evidence: dict) -> str:
    return json.dumps(
        [
            evidence["doc_id"],
            [evidence.get(key) for key in LOCATORS],
            fold(evidence["evidence_text"]),
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _evidence_elements(evidence: dict) -> list[dict]:
    """Represent JSON arrays/objects by the leaf pointers their loader emits."""
    excerpt = evidence["evidence_text"]
    if not ("json_pointer" in evidence and excerpt.startswith(("[", "{"))):
        return [evidence]
    try:
        value = json.loads(excerpt)
    except json.JSONDecodeError:
        return [evidence]

    def elements(value: Any, pointer: str):
        if isinstance(value, (dict, list)):
            children = value.items() if isinstance(value, dict) else enumerate(value)
            for key, child in children:
                escaped = str(key).replace("~", "~0").replace("/", "~1")
                yield from elements(child, f"{pointer}/{escaped}")
        else:
            yield {
                **evidence,
                "json_pointer": pointer,
                "evidence_text": value if isinstance(value, str) else json.dumps(value),
                "exact_value": True,
            }

    return list(elements(value, evidence["json_pointer"]))


def _match_element(needle: str, parts: list[Part]) -> tuple[tuple[int, ...], set[int]]:
    direct = {
        part.rank
        for part in parts
        if (
            fold(part.body) == needle and not part.clipped
            if part.scalar
            else _contains(needle, part.body)
        )
    }
    for index, part in enumerate(parts):
        if part.rank in direct:
            return (part.rank,), direct
        if cover := _cross_chunk_cover(needle, parts[: index + 1]):
            return cover, direct | set(cover)
    return (), set()


def _score_ranking(
    gold_grades: list[int],
    first_ranks: dict[int, int],
    relevant: set[int],
    count: int,
    k: int,
) -> dict:
    gains = {}
    for index, rank in first_ranks.items():
        gains[rank] = max(gains.get(rank, 0), 2 ** gold_grades[index] - 1)
    dcg = sum(gain / math.log2(rank + 1) for rank, gain in gains.items())
    ideal = sum(
        (2**grade - 1) / math.log2(rank + 1)
        for rank, grade in enumerate(sorted(gold_grades, reverse=True)[:k], 1)
    )
    return {
        "hit_rate": float(bool(first_ranks)),
        "recall": len(first_ranks) / len(gold_grades),
        "precision": len(relevant) / count if count else 0.0,
        "mrr": 1.0 / min(first_ranks.values()) if first_ranks else 0.0,
        "ndcg": dcg / ideal,
        "complete_evidence_rate": float(len(first_ranks) == len(gold_grades)),
    }


def score_view(
    case: dict,
    rows: list[dict],
    catalog: DocumentCatalog,
    k: int,
    *,
    internal: list[dict] | None = None,
) -> dict:
    rows = [row for row in rows if row["rank"] <= k]
    excluded = sorted({row["doc_id"] for row in rows} & set(case["excluded_doc_ids"]))
    output = {"results": rows, "result_count": len(rows), "excluded_doc_ids": excluded}
    if not case["answerable"]:
        return {
            **output,
            "document_metrics": None,
            "evidence_metrics": None,
            "evidence": [],
        }

    original_by_chunk = {
        (row["source"], row["chunk_id"]): row for row in internal or []
    }
    expected = {}
    for evidence in case["gold_evidence"]:
        key = _evidence_key(evidence)
        if key not in expected or evidence["relevance"] > expected[key]["relevance"]:
            expected[key] = evidence
    gold_docs = list(
        dict.fromkeys(evidence["doc_id"] for evidence in expected.values())
    )
    document_ranks = {}
    document_relevant = set()
    for row in rows:
        if row["doc_id"] in gold_docs:
            document_ranks.setdefault(gold_docs.index(row["doc_id"]), row["rank"])
            document_relevant.add(row["rank"])

    evidence_results, first_ranks, related = [], {}, set()
    for index, evidence in enumerate(expected.values()):
        same_doc = [row for row in rows if row["doc_id"] == evidence["doc_id"]]
        file_type = catalog.by_id[evidence["doc_id"]]["file_type"]
        elements = _evidence_elements(evidence)
        supporting_ranks, relevant_ranks = set(), set()
        part_locations = {}
        missing_locators, missing_text = [], []
        for element in elements:
            parts = [
                part
                for row in same_doc
                if (
                    part := _part(
                        element,
                        file_type,
                        row,
                        original_by_chunk.get((row["source"], row["chunk_id"])),
                    )
                )
                is not None
            ]
            part_locations.update({part.rank: part.text_only for part in parts})
            matched, relevant = _match_element(fold(element["evidence_text"]), parts)
            if not parts:
                missing_locators.append(element.get("json_pointer"))
            elif not matched:
                missing_text.append(element.get("json_pointer"))
            supporting_ranks.update(matched)
            relevant_ranks.update(relevant)
        cover = (
            supporting_ranks
            if elements and not (missing_locators or missing_text)
            else set()
        )
        if cover:
            first_ranks[index] = max(cover)
            related.update(relevant_ranks)
        reason = None
        if not cover:
            if not same_doc:
                reason = "document_not_retrieved"
            elif not elements:
                reason = "structured_value_not_emitted"
            elif missing_locators:
                reason = "locator_not_retrieved"
            else:
                reason = "evidence_text_not_retrieved"
        evidence_results.append(
            {
                "gold": evidence,
                "completion_rank": max(cover) if cover else None,
                "supporting_ranks": sorted(cover),
                "locator_precision": "text_only_region"
                if any(part_locations[rank] for rank in cover)
                else "json_leaf_values"
                if cover and elements != [evidence]
                else "locator_and_text"
                if cover
                else None,
                "miss_reason": reason,
                "missing_json_pointers": [
                    pointer for pointer in [*missing_locators, *missing_text] if pointer
                ],
            }
        )

    return {
        **output,
        "document_metrics": _score_ranking(
            [1] * len(gold_docs),
            document_ranks,
            document_relevant,
            len(rows),
            k,
        ),
        "evidence_metrics": _score_ranking(
            [e["relevance"] for e in expected.values()],
            first_ranks,
            related,
            len(rows),
            k,
        ),
        "evidence": evidence_results,
    }


def score_question(
    case: dict, result: AnswerReadyContext, catalog: DocumentCatalog, k: int
) -> dict:
    views = result_views(result, catalog)
    scored = {
        name: score_view(
            case,
            results,
            catalog,
            k,
            internal=views["internal"] if name == "delivered" else None,
        )
        for name, results in views.items()
    }
    for hidden, shown in zip(
        scored["internal"]["evidence"], scored["delivered"]["evidence"], strict=True
    ):
        if hidden["completion_rank"] is not None and shown["completion_rank"] is None:
            shown["miss_reason"] = "evidence_missing_from_tool_context"
    no_result = next(
        (
            reason
            for prefix, reason in (
                ("No relevant local knowledge", "no_relevant_results"),
                ("No local RAG documents", "no_documents"),
                ("Local RAG index is not ready", "index_not_ready"),
                ("Local RAG retrieval is disabled", "rag_disabled"),
                ("No local RAG document paths", "no_paths"),
            )
            if result.context.startswith(prefix)
        ),
        None,
    )
    return {"views": scored, "context": result.context, "no_result_reason": no_result}


def latency_summary(values: list[float]) -> dict:
    if not values:
        return {"count": 0, "mean_ms": None, "p95_ms": None}
    return {
        "count": len(values),
        "mean_ms": statistics.mean(values),
        "p95_ms": sorted(values)[math.ceil(0.95 * len(values)) - 1],
    }


def _summarize(rows: list[dict]) -> dict:
    successful = [row for row in rows if row["status"] == "succeeded"]
    answerable = [row for row in successful if row["answerable"]]
    unanswerable = [row for row in successful if not row["answerable"]]
    no_results = sum(bool(row["no_result_reason"]) for row in unanswerable)
    summary = {
        "count": len(rows),
        "succeeded": len(successful),
        "answerable_scored": len(answerable),
        "latency": latency_summary([row["latency_ms"] for row in successful]),
        "unanswerable": {
            "count": len(unanswerable),
            "no_result_mechanism_count": no_results,
            "no_result_mechanism_rate": no_results / len(unanswerable)
            if unanswerable
            else None,
            "views": {},
        },
        "views": {},
    }
    for name in VIEWS:
        view = {}
        for level in ("document_metrics", "evidence_metrics"):
            values = [row["views"][name][level] for row in answerable]
            view[level] = (
                {key: statistics.mean(v[key] for v in values) for key in values[0]}
                if values
                else None
            )
        view["excluded_source_rate"] = (
            sum(bool(row["views"][name]["excluded_doc_ids"]) for row in successful)
            / len(successful)
            if successful
            else None
        )
        summary["views"][name] = view
        if unanswerable:
            negatives = [row["views"][name] for row in unanswerable]
            summary["unanswerable"]["views"][name] = {
                "returned_rate": statistics.mean(
                    bool(row["result_count"]) for row in negatives
                ),
                "mean_result_count": statistics.mean(
                    row["result_count"] for row in negatives
                ),
                "excluded_source_rate": statistics.mean(
                    bool(row["excluded_doc_ids"]) for row in negatives
                ),
            }
    return summary


def summarize(rows: list[dict]) -> dict:
    output = {"overall": _summarize(rows)}
    for field in ("question_type", "difficulty"):
        grouped = defaultdict(list)
        for row in rows:
            grouped[row[field]].append(row)
        output[f"by_{field}"] = {
            key: _summarize(group) for key, group in sorted(grouped.items())
        }
    return output
