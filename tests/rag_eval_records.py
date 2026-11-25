"""Deterministic POR evaluation fingerprints and append-only run history."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from open_deep_research.models.providers import (
    parse_model_ref,
    resolve_api_key,
    resolve_base_url,
    sdk_api_for,
)
from open_deep_research.rag.loaders.knowledge import fingerprint_knowledge_base_paths

ROOT = Path(__file__).resolve().parents[1]
DATASET_VERSION = "dataset-v1"
FROZEN_DATASET_SHA256 = "b0017ecb387d3ba9ef918e1f05096e9211fde3d7352211467aeeab86b910f82d"
CORPUS = Path("data/knowledge/public_opinion")
MANIFEST = Path("data/metadata/documents.jsonl")
BENCHMARK = Path("data/benchmark")
HISTORY = Path("docs/evaluation/rag-retrieval-test-records.md")
RUNS = Path("docs/evaluation/runs")
SCORING_SOURCE = "tests/rag_eval_evidence.py"
EVALUATOR_SOURCES = (
    "tests/evaluate_rag_retrieval.py",
    SCORING_SOURCE,
    "tests/rag_eval_records.py",
)
RAG_DIR = "src/open_deep_research/rag"
RAG_SOURCES = (
    *(
        f"{RAG_DIR}/{name}.py"
        for name in (
            "citations",
            "code_languages",
            "config",
            "elasticsearch_bm25",
            "embeddings",
            "indexer",
            "metadata",
            "query_rewriter",
            "reranker",
            "retriever",
            "service",
            "splitter",
            "types",
            "vectorstore",
        )
    ),
    f"{RAG_DIR}/loaders/knowledge.py",
    "src/open_deep_research/tools/rag_tool.py",
    "src/open_deep_research/llm/generation.py",
    "src/open_deep_research/models/providers.py",
    "src/open_deep_research/models/sdk.py",
)
DEPENDENCIES = (
    "openai-agents",
    "openai",
    "httpx",
    "pydantic",
    "langchain-core",
    "langchain-text-splitters",
    "pymupdf",
    "pillow",
    "pytesseract",
    "numpy",
    "torch",
    "transformers",
    "tokenizers",
    "sentence-transformers",
    "sentencepiece",
    "huggingface-hub",
    "pymilvus",
    "milvus-lite",
    "opencv-python",
    "opencv-python-headless",
    "pyyaml",
    "faiss-cpu",
    "chromadb",
    "elasticsearch",
    "jieba",
)
SCHEMA_VERSION = 1
HISTORY_START = "<!-- POR RAG RUNS START -->"
HISTORY_END = "<!-- POR RAG RUNS END -->"
RUN_ID = re.compile(r"\d{8}T\d{6}Z-[0-9a-f]{32}")


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def file_digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def dataset_fingerprint(root: Path = ROOT, split: str | None = None) -> dict:
    """Hash real corpus bytes, manifest bytes, and the requested or both split files."""
    if split not in (None, "dev", "test"):
        raise ValueError("Expected dev or test split")
    paths = [
        *sorted(
            path.relative_to(root)
            for path in (root / CORPUS).rglob("*")
            if path.is_file()
        ),
        MANIFEST,
        *(
            BENCHMARK / f"{name}.jsonl"
            for name in ((split,) if split else ("dev", "test"))
        ),
    ]
    if len(paths) <= (2 if split else 3):
        raise ValueError("The POR corpus is empty")
    files = {path.as_posix(): file_digest(root / path) for path in paths}
    return {"sha256": digest(files), "files": files}


def _source_digests(root: Path, paths: tuple[str, ...]) -> dict[str, str]:
    return {
        name: hashlib.sha256(
            (root / name).read_text(encoding="utf-8").encode("utf-8")
        ).hexdigest()
        for name in paths
    }


def _path_name(path: str, root: Path) -> str:
    resolved = Path(path).expanduser().resolve()
    return (
        resolved.relative_to(root).as_posix()
        if resolved.is_relative_to(root)
        else resolved.as_posix()
    )


def _safe_endpoint(url: str | None) -> dict | None:
    """Keep endpoint identity without storing user info, query strings, or URL paths."""
    if not url:
        return None
    parsed = urlsplit(url)
    return {
        "scheme": parsed.scheme,
        "hostname": parsed.hostname,
        "port": parsed.port,
        "route_sha256": digest(parsed.path),
    }


def public_rag_config(rag_config: dict, root: Path = ROOT) -> dict:
    result = {
        key: value for key, value in rag_config.items() if key not in {"milvus_token"}
    }
    result["knowledge_base_paths"] = [
        _path_name(path, root) for path in result["knowledge_base_paths"]
    ]
    result["vectorstore_path"] = _path_name(result["vectorstore_path"], root)
    for field in ("milvus_uri", "elasticsearch_url"):
        value = result[field]
        if value and "://" in value:
            result[field] = _safe_endpoint(value)
        elif value and field == "milvus_uri":
            result[field] = _path_name(value, root)
    return result


def _dependency_version(name: str) -> str | None:
    try:
        return version(name)
    except PackageNotFoundError:
        return None


def _local_model(provider: str, model: str, device: str | None) -> dict:
    info = {
        "provider": provider,
        "model": model,
        "device": device or "auto",
        "revision": None,
    }
    if provider not in {"sentence_transformers", "cross_encoder"}:
        return {**info, "revision_status": "builtin"}
    path = Path(model).expanduser()
    if not path.exists():
        try:
            from huggingface_hub import try_to_load_from_cache

            cached = try_to_load_from_cache(model, "config.json")
        except (ImportError, ValueError):
            cached = None
        path = Path(cached) if isinstance(cached, str) else path
    for parent in [path, *path.parents]:
        if parent.parent.name == "snapshots" and re.fullmatch(
            r"[a-f0-9]{40}", parent.name
        ):
            return {
                **info,
                "revision": parent.name,
                "revision_status": "cached_huggingface_snapshot",
            }
    return {**info, "revision_status": "immutable_revision_unavailable"}


def _remote_model(model: str, config: dict | None) -> dict:
    ref = parse_model_ref(model)
    return {
        "model": model,
        "provider": ref.provider,
        "api": sdk_api_for(ref, config),
        "endpoint": _safe_endpoint(resolve_base_url(ref, config)),
        "credential_available": bool(resolve_api_key(ref, config)),
        "revision": None,
        "revision_status": "provider_revision_unavailable",
    }


def _ocr_runtime(enabled: bool) -> dict | None:
    if not enabled:
        return None
    try:
        import pytesseract

        return {
            "version": str(pytesseract.get_tesseract_version()),
            "languages": sorted(pytesseract.get_languages()),
        }
    except (ImportError, OSError, RuntimeError):
        return {"version": None, "languages": []}


def runtime_details(
    config: dict, tool_options: dict, scope: dict, runnable: dict
) -> dict:
    models = {
        "embedding": _local_model(
            config["embedding_provider"],
            config["embedding_model"],
            config["embedding_device"],
        ),
        "reranker": _local_model(
            config["reranker_provider"],
            config["reranker_model"],
            config["reranker_device"],
        ),
    }
    multimodal = config["multimodal_enabled"]
    if multimodal and config["vision_enabled"]:
        models["vision"] = _remote_model(config["vision_model"], None)
    if scope["mode"] == "tool" and tool_options["rag_query_rewrite_enabled"]:
        models["query_rewrite"] = _remote_model(
            tool_options["rag_query_rewrite_model"], runnable
        )
    return {
        "python": platform.python_version(),
        "os": platform.system(),
        "machine": platform.machine(),
        "dependencies": {name: _dependency_version(name) for name in DEPENDENCIES},
        "models": models,
        "ocr": _ocr_runtime(
            multimodal
            and config["multimodal_provider"] in {"ocr", "tesseract", "local_ocr"}
        ),
        "environment": {
            name: value
            for name in (
                "OMP_NUM_THREADS",
                "MKL_NUM_THREADS",
                "HF_HUB_OFFLINE",
                "TRANSFORMERS_OFFLINE",
            )
            if (value := os.environ.get(name)) is not None
            and re.fullmatch(r"[0-9]+|true|false", value, re.I)
        },
        "huggingface_endpoint": _safe_endpoint(os.environ.get("HF_ENDPOINT")),
    }


def build_fingerprint(
    scope: dict,
    rag_config: dict,
    tool_options: dict,
    runnable: dict,
    *,
    root: Path = ROOT,
) -> dict:
    """Describe the exact evaluation inputs without instantiating indexes or models."""
    root = root.resolve()
    data = dataset_fingerprint(root, scope["split"])
    settings = public_rag_config(rag_config, root)
    effective_files = fingerprint_knowledge_base_paths(
        rag_config["knowledge_base_paths"],
        include_multimodal=True,
    )["files"]
    indexed_files = {
        _path_name(entry["path"], root): entry.get("sha256")
        for entry in effective_files
    }
    environment = runtime_details(rag_config, tool_options, scope, runnable)
    rag = {
        "sources": _source_digests(root, RAG_SOURCES),
        "config": settings,
        "tool_options": tool_options,
        "evaluation_index": "fresh_namespace_per_execution",
        "models": environment["models"],
    }
    evaluator = _source_digests(root, EVALUATOR_SOURCES)
    parts = {
        "dataset_version": DATASET_VERSION,
        "dataset": digest(
            {"dataset": data["sha256"], "configured_corpus": indexed_files}
        ),
        "rag": digest(rag),
        "evaluator": digest(evaluator),
        "scoring": evaluator[SCORING_SOURCE],
        "scope": digest(scope),
        "environment": digest(environment),
    }
    return {
        "evaluation_fingerprint": digest(parts),
        "components": parts,
        "dataset_split_fingerprint": data["sha256"],
        "full_dataset_fingerprint": dataset_fingerprint(root)["sha256"],
        "configured_corpus": indexed_files,
        "rag_config": settings,
        "tool_options": tool_options,
        "runtime": environment,
        "source_digests": {"rag": rag["sources"], "evaluator": evaluator},
        "scope": scope,
    }


def git_commit(root: Path = ROOT) -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def start_record(fingerprint: dict, *, title: str, notes: str, git: str | None) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid4().hex}",
        "started_at": utc_now(),
        "finished_at": None,
        "status": "running",
        "dataset_version": DATASET_VERSION,
        "git_commit": git,
        **fingerprint,
        "title": title,
        "notes": notes,
        "questions": [],
    }


def _record_path(directory: Path, run_id: str) -> Path:
    if not RUN_ID.fullmatch(run_id):
        raise ValueError("Invalid RAG evaluation run ID")
    return directory / f"{run_id}.json"


def _atomic_write(path: Path, text: str) -> None:
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        delete=False,
        dir=path.parent,
    ) as temporary:
        temp_path = Path(temporary.name)
        temporary.write(text)
    try:
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def save_record(directory: Path, record: dict) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = _record_path(directory, record["run_id"])
    payload = {**record, "record_sha256": digest(record)}
    content = (
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False
        )
        + "\n"
    )
    if record["status"] == "running":
        with path.open("x", encoding="utf-8", newline="\n") as state:
            state.write(content)
        return path
    previous = load_record(path)
    if (
        previous["status"] != "running"
        or previous["started_at"] != record["started_at"]
    ):
        raise ValueError("A finished evaluation record cannot be replaced")
    _atomic_write(path, content)
    return path


def load_record(path: Path) -> dict:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        integrity = record.pop("record_sha256")
        if record["schema_version"] != SCHEMA_VERSION or digest(record) != integrity:
            raise ValueError("Invalid record integrity or schema")
        return record
    except (TypeError, KeyError, AttributeError) as exc:
        raise ValueError("Invalid evaluation record") from exc


def _complete(record: dict) -> bool:
    if record.get("status") != "succeeded" or not record.get("finished_at"):
        return False
    scope = record.get("scope", {})
    ids = scope.get("question_ids", [])
    rows = record.get("questions", [])
    return (
        scope.get("purpose") == "formal"
        and scope.get("selection") == "all"
        and bool(ids)
        and len(ids) == len(set(ids))
        and record.get("counts")
        == {"total": len(ids), "succeeded": len(ids), "failed": 0}
        and [row.get("question_id") for row in rows] == ids
        and all(row.get("status") == "succeeded" and "views" in row for row in rows)
        and record.get("index", {}).get("num_chunks", 0) > 0
        and bool(record.get("metrics", {}).get("overall"))
    )


def history_records(directory: Path) -> list[tuple[Path, dict]]:
    output = []
    for path in sorted(directory.glob("*.json"), reverse=True):
        if not RUN_ID.fullmatch(path.stem):
            continue
        try:
            record = load_record(path)
            if record["run_id"] == path.stem:
                output.append((path, record))
        except (ValueError, OSError, KeyError):
            continue
    return output


def find_reusable_record(
    directory: Path, fingerprint: dict
) -> tuple[Path, dict] | None:
    """Return the newest intact, completed full result for this exact evaluation."""
    for path, record in history_records(directory):
        if (
            record.get("evaluation_fingerprint")
            == fingerprint["evaluation_fingerprint"]
            and record.get("dataset_version")
            == fingerprint["components"]["dataset_version"]
            and record.get("scope") == fingerprint["scope"]
            and record.get("components") == fingerprint["components"]
            and record.get("full_dataset_fingerprint")
            == fingerprint["full_dataset_fingerprint"]
            and _complete(record)
        ):
            return path, record
    return None


def _deltas(before: dict, after: dict) -> dict:
    """Keep common numeric measurements, with the same nested keys."""
    output = {}
    for key in sorted(before.keys() & after.keys()):
        left, right = before[key], after[key]
        if isinstance(left, dict) and isinstance(right, dict):
            changes = _deltas(left, right)
            if changes:
                output[key] = changes
        elif type(left) in (int, float) and type(right) in (int, float):
            output[key] = {"before": left, "after": right, "delta": right - left}
    return output


def _failures(record: dict) -> dict:
    failures = {}
    for row in record["questions"]:
        delivered = row["views"]["delivered"]
        reasons = [
            {"doc_id": evidence["gold"]["doc_id"], "reason": evidence["miss_reason"]}
            for evidence in delivered["evidence"]
            if evidence["miss_reason"]
        ]
        if delivered["excluded_doc_ids"]:
            reasons.append({"excluded_doc_ids": delivered["excluded_doc_ids"]})
        if reasons:
            failures[row["question_id"]] = reasons
    return failures


def compare_records(before: dict, after: dict) -> dict:
    """Compare full successful records with compatible data, scope and scoring."""
    if not (_complete(before) and _complete(after)):
        raise ValueError(
            "Formal comparison requires two complete successful full-split records"
        )
    if (
        before["dataset_version"] != after["dataset_version"]
        or before["full_dataset_fingerprint"] != after["full_dataset_fingerprint"]
        or before["components"]["dataset"] != after["components"]["dataset"]
        or before["scope"] != after["scope"]
        or before["components"]["scoring"] != after["components"]["scoring"]
    ):
        raise ValueError("Incompatible dataset, split, scope, K, or scoring rules")
    if before["scope"]["mode"] == "pipeline" and (
        before["tool_options"].get("rag_query_rewrite_enabled")
        != after["tool_options"].get("rag_query_rewrite_enabled")
    ):
        raise ValueError("Query Rewrite comparisons require the tool scope")
    old_failures, new_failures = _failures(before), _failures(after)
    return {
        "before": before["run_id"],
        "after": after["run_id"],
        "metrics": _deltas(before["metrics"], after["metrics"]),
        "index": _deltas(before["index"], after["index"]),
        "post_first_query_latency": _deltas(
            before.get("post_first_query_latency", {}),
            after.get("post_first_query_latency", {}),
        ),
        "model_usage": _deltas(before["model_usage"], after["model_usage"]),
        "new_failures": {
            qid: new_failures[qid]
            for qid in sorted(new_failures.keys() - old_failures.keys())
        },
        "fixed_failures": {
            qid: old_failures[qid]
            for qid in sorted(old_failures.keys() - new_failures.keys())
        },
    }


def update_history(path: Path, directory: Path) -> None:
    """Regenerate only the current table, leaving historical experiments intact."""

    def render(value: float | None) -> str:
        return f"{value:.4f}" if value is not None else "—"

    header = "| 评测记录 | 技术版本及范围 | 数据版本 | Recall@K | MRR@K | 完整证据率 | P95 (ms) | 备注 |"
    divider = "| --- | --- | --- | ---: | ---: | ---: | ---: | --- |"
    rows = []
    for record_path, record in history_records(directory):
        if not _complete(record):
            continue
        overall = record["metrics"]["overall"]
        metrics = overall["views"]["delivered"]["evidence_metrics"] or {}
        scope = record["scope"]
        title = record["title"].replace("|", r"\|").replace("\n", " ")
        relative = os.path.relpath(record_path, path.parent).replace(os.sep, "/")
        notes = record["notes"].replace("|", r"\|").replace("\n", " ") or "—"
        rows.append(
            f"| [{record['run_id']}](<{relative}>) | {title} ({scope['split']}/{scope['mode']}, K={scope['top_k']}) "
            f"| {record['dataset_version']} | {render(metrics.get('recall'))} | {render(metrics.get('mrr'))} "
            f"| {render(metrics.get('complete_evidence_rate'))} | {render(overall['latency']['p95_ms'])} | {notes} |"
        )
    if not rows:
        rows.append(
            "| 待测 | V0 原始基线 | dataset-v1 | 待测 | 待测 | 待测 | 待测 | 待测 |"
        )
    existing = path.read_text(encoding="utf-8")
    before, start, tail = existing.partition(HISTORY_START)
    _, end, after = tail.partition(HISTORY_END)
    if not (start and end):
        raise ValueError("The RAG history table markers are missing")
    updated = before + "\n".join([start, "", header, divider, *rows, "", end]) + after
    if updated != existing:
        _atomic_write(path, updated)
