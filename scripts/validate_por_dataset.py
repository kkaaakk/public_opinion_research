"""Validate the POR corpus and benchmark without retrieval, models, or indexes."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
from collections import Counter
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

import fitz
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
KNOWLEDGE = DATA / "knowledge/public_opinion"
CATEGORIES = {"product_docs", "historical_cases", "pr_playbooks", "compliance", "faq"}
TAGS = {
    "battery_safety", "driver_assistance", "quality_recall", "misleading_advertising",
    "after_sales", "privacy_leak", "delivery_delay", "price_adjustment",
    "executive_remarks", "misinformation",
}
TYPES = {
    "single_hop", "exact_match", "semantic_match", "multi_hop", "version_conflict",
    "table_image", "misleading", "unanswerable",
}
HEADING = re.compile(r"^(#{1,6}) (.+)$")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def folded(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)).casefold()


def valid_url(value):
    url = urlparse(value or "")
    return url.scheme == "https" and bool(url.hostname) and not url.username


def unique(values, label):
    require(len(set(values)) == len(values), f"Duplicate {label}")


def inspect_document(row, path):
    """Open the real file, and return searchable content for the keyword check."""
    doc_id = row["doc_id"]
    synthetic = row["data_origin"] == "synthetic"
    file_type = row["file_type"]
    if file_type == "pdf":
        with fitz.open(path) as pdf:
            require(pdf.is_pdf and not pdf.is_encrypted, f"{doc_id}: invalid PDF")
            require(pdf.page_count == row["page_count"] > 0, f"{doc_id}: PDF page count")
            pages = [page.get_text() for page in pdf]
            if synthetic:
                require("synthetic" in pdf.metadata["keywords"], f"{doc_id}: PDF provenance")
                require(bool(row.get("scan_only", False)) == (not any(p.strip() for p in pages)),
                        f"{doc_id}: unexpected PDF text layer")
            return "\n".join(pages)

    if file_type == "png":
        with Image.open(path) as image:
            require(image.format == "PNG", f"{doc_id}: invalid PNG")
            if synthetic:
                require(image.info.get("data_origin") == "synthetic"
                        and image.info.get("event_id") == row["event_id"], f"{doc_id}: PNG provenance")
            image.verify()
        return ""

    text = path.read_text(encoding="utf-8")
    require(text.strip() and "\x00" not in text, f"{doc_id}: invalid text")
    if file_type == "json":
        data = json.loads(text)
        require(isinstance(data, (dict, list)), f"{doc_id}: JSON root")
        if synthetic:
            require(data["data_origin"] == "synthetic" and data["event"]["id"] == row["event_id"]
                    and data["source_status"] == row["source_status"], f"{doc_id}: JSON provenance")
    elif synthetic:
        header = "\n".join(text.splitlines()[:5])
        require(all(value in header for value in ("synthetic", row["event_id"], row["source_status"])),
                f"{doc_id}: text provenance")
    return text


def read_documents():
    documents = jsonl(DATA / "metadata/documents.jsonl")
    unique([d["doc_id"] for d in documents], "document ID")
    unique([d["file_path"] for d in documents], "document path")
    unique([d["sha256"] for d in documents], "document SHA-256")
    unique([PurePosixPath(d["file_path"]).name.casefold() for d in documents], "retrieval filename")
    public_urls = [d["source_url"] for d in documents if d["data_origin"] == "public"]
    unique(public_urls, "primary public source URL")
    by_id = {row["doc_id"]: row for row in documents}
    scopes = set()
    searchable = {}
    for row in documents:
        doc_id = row["doc_id"]
        relative = PurePosixPath(row["file_path"])
        require(len(relative.parts) == 2 and relative.parts[0] == row["category"] in CATEGORIES
                and "\\" not in row["file_path"] and relative.name not in {".", ".."}, f"{doc_id}: path")
        require(row["file_type"] == relative.suffix[1:] in {"md", "txt", "json", "pdf", "png"},
                f"{doc_id}: unsupported file type")
        path = KNOWLEDGE / relative
        require(path.is_file() and not path.is_symlink(), f"{doc_id}: missing file")
        require(row["sha256"] == sha(path.read_bytes()), f"{doc_id}: SHA-256 mismatch")
        require(row["title"] and row["organization"] and row["source_group"], f"{doc_id}: identity")
        require(row["language"] in {"zh", "en", "mixed"}, f"{doc_id}: language")
        require(row["event_tags"] and set(row["event_tags"]) <= TAGS, f"{doc_id}: event tags")
        scopes.update(row["event_tags"])
        require(row["version"] is None or isinstance(row["version"], str), f"{doc_id}: version")
        if row["published_at"] is not None:
            date.fromisoformat(row["published_at"])
        rights = row["license"]
        require(rights["basis"] and (rights["url"] is None or valid_url(rights["url"])),
                f"{doc_id}: license basis")
        if row["data_origin"] == "public":
            require(valid_url(row["source_url"]), f"{doc_id}: primary source")
            require(all(valid_url(url) for url in row.get("supporting_source_urls", [])),
                    f"{doc_id}: supporting source")
            date.fromisoformat(row["accessed_at"])
        else:
            require(row["data_origin"] == "synthetic" and row["source_url"] is None
                    and row["organization"] == "砾舟智行（虚构新能源车企）"
                    and row["event_id"] == row["source_group"], f"{doc_id}: synthetic identity")
        status = row["source_status"]
        require(status in {"authoritative", "deprecated", "misleading"}, f"{doc_id}: status")
        if status == "deprecated":
            current = by_id[row["superseded_by"]]
            require(current["source_group"] == row["source_group"]
                    and current["source_status"] == "authoritative", f"{doc_id}: supersession")
            if row.get("series_id"):
                require(current["series_id"] == row["series_id"]
                        and date.fromisoformat(row["effective_from"]) <= date.fromisoformat(row["valid_until"])
                        < date.fromisoformat(current["effective_from"]), f"{doc_id}: validity windows")
        if status == "misleading":
            require(row["conflicts_with"] and all(
                by_id[other]["source_group"] == row["source_group"]
                and by_id[other]["source_status"] == "authoritative" for other in row["conflicts_with"]
            ), f"{doc_id}: conflict links")
        searchable[doc_id] = inspect_document(row, path)

    inventory = {p.relative_to(KNOWLEDGE).as_posix() for p in KNOWLEDGE.rglob("*") if p.is_file()}
    listed = {d["file_path"] for d in documents}
    require(inventory == listed, f"Inventory mismatch: {sorted(inventory ^ listed)}")
    require(scopes == TAGS, f"Missing event tags: {sorted(TAGS - scopes)}")
    normalized = [folded(text) for text in searchable.values() if text.strip()]
    unique(normalized, "normalized full document text")
    return by_id, searchable


def heading_text(text, target):
    current = []
    section = []
    for line in text.splitlines():
        match = HEADING.fullmatch(line)
        if match:
            depth = len(match[1])
            current[depth - 1:] = [match[2]]
        elif current == target:
            section.append(line)
    return "\n".join(section)


def json_value(text, pointer):
    require(pointer.startswith("/"), "JSON Pointer must start with /")
    value = json.loads(text)
    for element in pointer[1:].split("/"):
        key = element.replace("~1", "/").replace("~0", "~")
        value = value[int(key)] if isinstance(value, list) else value[key]
    return value


def validate_evidence(evidence, document, text, question_id):
    doc_id = document["doc_id"]
    prefix = f"{question_id} → {doc_id}"
    excerpt = evidence["evidence_text"]
    require(isinstance(excerpt, str) and excerpt.strip() and evidence["relevance"] in {1, 2, 3},
            f"{prefix}: invalid evidence")
    file_type = document["file_type"]
    if file_type == "md":
        headings = evidence["heading_path"]
        require(headings[-1] == evidence["section"] and excerpt in heading_text(text, headings),
                f"{prefix}: Markdown heading or excerpt")
    elif file_type == "json":
        value = json_value(text, evidence["json_pointer"])
        matches = excerpt in value if isinstance(value, str) else json.loads(excerpt) == value
        require(matches, f"{prefix}: JSON field or value")
    elif file_type == "txt":
        begin, end = evidence["line_start"], evidence["line_end"]
        lines = text.splitlines()
        require(1 <= begin <= end <= len(lines) and excerpt in "\n".join(lines[begin - 1:end]),
                f"{prefix}: text line or excerpt")
    else:
        path = KNOWLEDGE / document["file_path"]
        if file_type == "pdf":
            with fitz.open(path) as pdf:
                number = evidence["page"]
                require(type(number) is int and 1 <= number <= len(pdf), f"{prefix}: PDF page")
                page = pdf[number - 1]
                if evidence.get("visual_check"):
                    box = fitz.Rect(evidence["region"])
                    require(evidence["coordinate_system"] == "pdf_points_top_left"
                            and not box.is_empty and page.rect.contains(box), f"{prefix}: PDF region")
                    samples = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False, clip=box).samples
                    require(sha(samples) == evidence["region_sha256"], f"{prefix}: PDF region digest")
                else:
                    content = page.get_text()
                    if "region" in evidence:
                        box = fitz.Rect(evidence["region"])
                        require(evidence["coordinate_system"] == "pdf_points_top_left"
                                and not box.is_empty and page.rect.contains(box), f"{prefix}: PDF region")
                        content = page.get_textbox(box)
                    require(folded(excerpt) in folded(content), f"{prefix}: PDF excerpt")
        else:
            with Image.open(path) as image:
                x0, y0, x1, y1 = evidence["region"]
                require(evidence["coordinate_system"] == "image_pixels_top_left"
                        and 0 <= x0 < x1 <= image.width and 0 <= y0 < y1 <= image.height,
                        f"{prefix}: image region")
                samples = image.convert("RGB").crop((x0, y0, x1, y1)).tobytes()
                require(sha(samples) == evidence["region_sha256"], f"{prefix}: image region digest")


def question_terms(prompt):
    return "".join(char for char in folded(prompt) if char.isalnum())


def validate_questions(documents, searchable):
    splits = {name: jsonl(DATA / f"benchmark/{name}.jsonl") for name in ("dev", "test")}
    questions = splits["dev"] + splits["test"]
    unique([q["question_id"] for q in questions], "question ID")
    unique([question_terms(q["question"]) for q in questions], "normalized question")
    groups = {}
    for split, rows in splits.items():
        groups[split] = set()
        for q in rows:
            qid = q["question_id"]
            require(qid == q["id"] and q["query"] == q["question"] and q["category"] == q["question_type"],
                    f"{qid}: legacy aliases")
            require(q["question_type"] in TYPES and q["difficulty"] in {"easy", "medium", "hard"}
                    and q["language"] in {"zh", "en", "mixed"} and q["expected_answer"],
                    f"{qid}: question classification")
            require(q["event_tags"] and set(q["event_tags"]) <= TAGS, f"{qid}: event tags")
            gold = q["gold_evidence"]
            excluded = q["excluded_doc_ids"]
            gold_ids = list(dict.fromkeys(item["doc_id"] for item in gold))
            if q["question_type"] == "unanswerable":
                require(q["answer_type"] == "unanswerable" and q["answerable"] is False
                        and not gold and not q["expected_keywords"], f"{qid}: unanswerable contract")
                check = q["absence_check"]
                require(check["missing_fact"] and check["search_terms"] and check["nearby_doc_ids"],
                        f"{qid}: absent-fact review")
                nearby = check["nearby_doc_ids"]
            else:
                require(q["answer_type"] == "answerable" and q["answerable"] is True and gold,
                        f"{qid}: answerable contract")
                nearby = []
            refs = set(gold_ids) | set(excluded) | set(nearby)
            require(refs <= documents.keys() and not set(gold_ids) & set(excluded), f"{qid}: references")
            expected_groups = {documents[id_]["source_group"] for id_ in refs}
            require(expected_groups == set(q["source_groups"]), f"{qid}: source groups")
            groups[split].update(expected_groups)
            require([Path(documents[id_]["file_path"]).name for id_ in gold_ids] == q["expected_sources"],
                    f"{qid}: legacy source filenames")
            if q["question_type"] == "multi_hop":
                require(len(gold_ids) >= 2, f"{qid}: multi-hop source count")
            if q["question_type"] == "misleading":
                require(excluded, f"{qid}: missing excluded source")
            require(set(excluded) == set(q.get("exclusion_reasons", {})), f"{qid}: exclusion reasons")
            for item in gold:
                row = documents[item["doc_id"]]
                require(row["source_status"] == "authoritative"
                        or (row["source_status"] == "deprecated"
                            and item.get("evidence_role") == "historical_record"),
                        f"{qid}: inapplicable source used as gold")
                validate_evidence(item, row, searchable[row["doc_id"]], qid)
            candidate_text = folded(" ".join([searchable[d] for d in gold_ids]
                                             + [e["evidence_text"] for e in gold]))
            require(all(folded(term) and folded(term) in candidate_text for term in q["expected_keywords"]),
                    f"{qid}: keyword absent from gold sources")
    require(not groups["dev"] & groups["test"], f"Split leakage: {groups['dev'] & groups['test']}")
    for left in splits["dev"]:
        for right in splits["test"]:
            ratio = SequenceMatcher(None, question_terms(left["question"]), question_terms(right["question"])).ratio()
            require(ratio < 0.9, f"Near-duplicate across splits: {left['question_id']} and {right['question_id']}")
    return splits


def check_loader(documents):
    import yaml

    from open_deep_research.rag.loaders.knowledge import (
        fingerprint_knowledge_base_paths,
        load_documents_from_paths,
    )

    settings = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    roots = [str((ROOT / path).resolve()) for path in settings["rag_knowledge_base_paths"]]
    require(roots == [str(KNOWLEDGE.resolve())], "Repository config has a different knowledge root")
    enumerated = fingerprint_knowledge_base_paths(roots, include_multimodal=True)["files"]
    paths = {Path(entry["path"]).resolve() for entry in enumerated}
    require(paths == {(KNOWLEDGE / d["file_path"]).resolve() for d in documents.values()},
            "Loader enumeration differs from the manifest")
    loaded = load_documents_from_paths(roots, multimodal_enabled=False, vision_enabled=False)
    text_paths = {Path(item.source.split("#page=")[0]).resolve() for item in loaded}
    expected = [d for d in documents.values() if d["file_type"] != "png" and not d.get("scan_only")]
    require(text_paths == {(KNOWLEDGE / d["file_path"]).resolve() for d in expected},
            "Text-only loader missed a selectable-text file or loaded an unexpected file")
    print(f"Loader: {len(paths)} candidate files, {len(text_paths)} text-readable files, {len(loaded)} loaded records")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-loader", action="store_true", help="Exercise the current Loader with multimodal disabled")
    args = parser.parse_args()
    documents, searchable = read_documents()
    splits = validate_questions(documents, searchable)
    print(f"{len(documents)} documents, {sum(len(rows) for rows in splits.values())} questions")
    print("Formats:", dict(Counter(d["file_type"] for d in documents.values())))
    for split, rows in splits.items():
        print(f"{split}: {len(rows)}", dict(Counter(q["question_type"] for q in rows)))
    if args.check_loader:
        check_loader(documents)


if __name__ == "__main__":
    main()
