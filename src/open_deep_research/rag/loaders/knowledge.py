"""本地 RAG 文档加载器.

这个模块负责把用户配置的 `rag_knowledge_base_paths` 转成 `RAGDocument`。
它只做“读取和元数据提取”，不负责切块、embedding 或检索。

设计要点：

- 支持文件和目录。目录会递归扫描受支持扩展名的文件。
- 文件缺失或单个文件解析失败时记录 warning，但不中断整个知识库加载。
- 尽量保留后续引用需要的 metadata，例如 PDF 页码、JSON 字段路径、
  Markdown 标题等。
- 提供 fingerprint 计算，用于让 cache key 感知文件内容变化。
"""

import base64
import hashlib
import json
import logging
from io import BytesIO
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from langchain.chat_models import init_chat_model
from langchain_core.messages import HumanMessage

from open_deep_research.observability import observe_model_invoke
from open_deep_research.rag.code_languages import (
    CODE_EXTENSION_LANGUAGE_MAP,
    language_for_extension,
)
from open_deep_research.rag.types import RAGDocument

LOGGER = logging.getLogger(__name__)
SUPPORTED_TEXT_EXTENSIONS = {".txt", ".md", ".json", ".pdf"}
SUPPORTED_CODE_EXTENSIONS = frozenset(CODE_EXTENSION_LANGUAGE_MAP)
SUPPORTED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
SUPPORTED_EXTENSIONS = (
    SUPPORTED_TEXT_EXTENSIONS | SUPPORTED_CODE_EXTENSIONS | SUPPORTED_IMAGE_EXTENSIONS
)
DEFAULT_RAG_VISION_MODEL = "openai:gpt-4.1-mini"
DEFAULT_RAG_VISION_PROMPT = (
    "Describe only the visible information in this image for RAG indexing. "
    "Include chart meaning, UI structure, diagrams, relationships, objects, and scene context. "
    "Do not invent details; say when something is unclear. Keep it concise and searchable."
)
DEFAULT_RAG_VISION_CLASSIFICATION_PROMPT = (
    "Classify this image for RAG ingestion. Return exactly one label: "
    "text, diagram, ui, flowchart, architecture, chart, photo, low_info, or uncertain."
)
TEXT_HEAVY_OCR_CHARS = 80
SOME_OCR_TEXT_CHARS = 8
LOW_INFO_EDGE_DENSITY = 0.01
LOW_INFO_ENTROPY = 0.2
DIAGRAM_EDGE_DENSITY = 0.18
DIAGRAM_MAX_COLOR_COUNT = 64
PHOTO_MIN_COLOR_COUNT = 128
PHOTO_MIN_ENTROPY = 5.5


def load_documents_from_paths(
    knowledge_base_paths: Sequence[str],
    json_text_fields: Sequence[str] | None = None,
    multimodal_enabled: bool = True,
    multimodal_provider: str = "ocr",
    ocr_languages: str = "eng+chi_sim",
    vision_enabled: bool = True,
    vision_model: str = DEFAULT_RAG_VISION_MODEL,
    vision_prompt: str = DEFAULT_RAG_VISION_PROMPT,
    vision_max_tokens: int = 512,
) -> list[RAGDocument]:
    """从配置路径加载所有受支持文档.

    `knowledge_base_paths` 中每个元素既可以是单个文件，也可以是目录。
    目录会通过 `rglob("*")` 递归查找 `.txt/.md/.json/.pdf`。

    返回的是 `RAGDocument` 列表，而不是 chunk 列表；切块交给 splitter。
    """
    loaded_documents: list[RAGDocument] = []
    for configured_path in knowledge_base_paths:
        path = Path(configured_path).expanduser()
        if not path.exists():
            LOGGER.warning("Skipping missing RAG path: %s", configured_path)
            continue
        for candidate_path in _iter_supported_paths(
            path,
            include_multimodal=multimodal_enabled,
        ):
            try:
                # 单个文件可能因为编码、JSON 格式、PDF 解析等原因失败。
                # 这里吞掉单文件错误并继续加载其它文件，避免一个坏文件拖垮整批知识库。
                loaded_documents.extend(
                    _load_document(
                        candidate_path,
                        json_text_fields=json_text_fields,
                        multimodal_enabled=multimodal_enabled,
                        multimodal_provider=multimodal_provider,
                        ocr_languages=ocr_languages,
                        vision_enabled=vision_enabled,
                        vision_model=vision_model,
                        vision_prompt=vision_prompt,
                        vision_max_tokens=vision_max_tokens,
                    )
                )
            except Exception as exc:  # pragma: no cover - defensive logging path
                LOGGER.warning("Failed to load RAG document %s: %s", candidate_path, exc)
    return loaded_documents


def fingerprint_knowledge_base_paths(
    knowledge_base_paths: Sequence[str],
    include_multimodal: bool = False,
) -> dict[str, Any]:
    """根据文件内容和 mtime 生成知识库 fingerprint.

    fingerprint 会参与 `build_rag_index_id()`。只要文件内容、大小或 mtime 变化，
    index id 就会变化，从而避免误用旧向量索引。

    这里同时保存 sha256 和 mtime：
    - sha256 能准确感知内容变化。
    - mtime/size 便于调试，也能反映文件系统层面的变化。
    """
    files = []
    for configured_path in knowledge_base_paths:
        path = Path(configured_path).expanduser()
        if not path.exists():
            files.append({"path": str(path), "missing": True})
            continue
        for candidate_path in _iter_supported_paths(
            path,
            include_multimodal=include_multimodal,
        ):
            stat = candidate_path.stat()
            files.append(
                {
                    "path": candidate_path.as_posix(),
                    "mtime_ns": stat.st_mtime_ns,
                    "size": stat.st_size,
                    "sha256": _file_sha256(candidate_path),
                }
            )
    return {"files": sorted(files, key=lambda item: item["path"])}


def _iter_supported_paths(path: Path, include_multimodal: bool = False) -> Iterable[Path]:
    """枚举一个文件或目录下所有受支持文件.

    传入文件时只检查扩展名；传入目录时递归扫描。排序保证同一组文件生成
    文档顺序稳定，从而 chunk id 更稳定。
    """
    if path.is_file():
        if _is_supported_suffix(path.suffix.lower(), include_multimodal=include_multimodal):
            yield path.resolve()
        return

    for candidate in sorted(path.rglob("*")):
        if candidate.is_file() and _is_supported_suffix(
            candidate.suffix.lower(),
            include_multimodal=include_multimodal,
        ):
            yield candidate.resolve()


def _is_supported_suffix(suffix: str, include_multimodal: bool = False) -> bool:
    if suffix in SUPPORTED_TEXT_EXTENSIONS:
        return True
    if suffix in SUPPORTED_CODE_EXTENSIONS:
        return True
    return include_multimodal and suffix in SUPPORTED_IMAGE_EXTENSIONS


def _load_document(
    path: Path,
    json_text_fields: Sequence[str] | None = None,
    multimodal_enabled: bool = True,
    multimodal_provider: str = "ocr",
    ocr_languages: str = "eng+chi_sim",
    vision_enabled: bool = True,
    vision_model: str = DEFAULT_RAG_VISION_MODEL,
    vision_prompt: str = DEFAULT_RAG_VISION_PROMPT,
    vision_max_tokens: int = 512,
) -> list[RAGDocument]:
    """按扩展名分派到具体 loader.

    返回 list 是因为一个物理文件可能展开成多个 `RAGDocument`：
    - JSON 数组会展开为多个 item document。
    - PDF 会按页展开为多个 page document。
    """
    suffix = path.suffix.lower()
    if suffix in {".txt", ".md"}:
        text = path.read_text(encoding="utf-8", errors="ignore").strip()
        if not text:
            return []
        return [
            RAGDocument(
                content=text,
                source=path.as_posix(),
                title=_guess_title(text=text, path=path),
                metadata={
                    "extension": suffix,
                    "path": path.as_posix(),
                    "title_hierarchy": _document_heading_hierarchy(text),
                },
            )
        ]

    if suffix in SUPPORTED_CODE_EXTENSIONS:
        return _load_code_document(path)

    if suffix == ".json":
        return _load_json_documents(path, json_text_fields=json_text_fields)

    if suffix == ".pdf":
        return _load_pdf_document(
            path,
            multimodal_enabled=multimodal_enabled,
            multimodal_provider=multimodal_provider,
            ocr_languages=ocr_languages,
            vision_enabled=vision_enabled,
            vision_model=vision_model,
            vision_prompt=vision_prompt,
            vision_max_tokens=vision_max_tokens,
        )

    if suffix in SUPPORTED_IMAGE_EXTENSIONS and multimodal_enabled:
        return _load_image_document(
            path,
            multimodal_provider=multimodal_provider,
            ocr_languages=ocr_languages,
            vision_enabled=vision_enabled,
            vision_model=vision_model,
            vision_prompt=vision_prompt,
            vision_max_tokens=vision_max_tokens,
        )

    return []


def _load_code_document(path: Path) -> list[RAGDocument]:
    text = path.read_text(encoding="utf-8", errors="ignore").strip()
    if not text:
        return []

    language = language_for_extension(path.suffix)
    return [
        RAGDocument(
            content=text,
            source=path.as_posix(),
            title=path.name,
            metadata={
                "extension": path.suffix.lower(),
                "file_type": "code",
                "language": language,
                "path": path.as_posix(),
            },
        )
    ]


def _load_json_documents(
    path: Path,
    json_text_fields: Sequence[str] | None = None,
) -> list[RAGDocument]:
    """加载 JSON 原文，让 splitter 负责结构化 json_path 切分."""
    raw_text = path.read_text(encoding="utf-8", errors="ignore").strip()
    if not raw_text:
        return []
    raw_data = json.loads(raw_text)

    return [
        RAGDocument(
            content=raw_text,
            source=path.as_posix(),
            title=_extract_json_title(raw_data) or path.stem,
            metadata={
                "extension": ".json",
                "file_type": "json",
                "path": path.as_posix(),
                **(
                    {"json_text_fields": list(json_text_fields)}
                    if json_text_fields
                    else {}
                ),
            },
        )
    ]


def _load_pdf_document(
    path: Path,
    multimodal_enabled: bool = True,
    multimodal_provider: str = "ocr",
    ocr_languages: str = "eng+chi_sim",
    vision_enabled: bool = True,
    vision_model: str = DEFAULT_RAG_VISION_MODEL,
    vision_prompt: str = DEFAULT_RAG_VISION_PROMPT,
    vision_max_tokens: int = 512,
) -> list[RAGDocument]:
    """使用 PyMuPDF 加载 PDF 文本.

    PDF 按页生成 `RAGDocument`，而不是整本 PDF 生成一个 document。
    这样后续 citation 能带 `#page=N` 和 `page_number`，引用颗粒度更好。

    注意：这里只处理可抽取文本的 PDF；扫描件如果没有 OCR 文本，会被视为无内容。
    """
    try:
        import fitz
    except ImportError:  # pragma: no cover - dependency exists in this repository
        LOGGER.warning("PyMuPDF is unavailable; skipping PDF file %s", path)
        return []

    pdf_title: str | None = None
    page_count = 0
    loaded_pages: list[RAGDocument] = []

    with fitz.open(path) as pdf_document:
        metadata = pdf_document.metadata or {}
        pdf_title = metadata.get("title") or None
        page_count = pdf_document.page_count
        for page_index in range(pdf_document.page_count):
            page = pdf_document.load_page(page_index)
            page_text = page.get_text("text").strip()
            modality = "text"
            route_metadata: dict[str, Any] = {}
            if not page_text and multimodal_enabled:
                pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                routed_text, route_metadata = extract_routed_image_bytes_text(
                    pixmap.tobytes("png"),
                    provider=multimodal_provider,
                    ocr_languages=ocr_languages,
                    vision_enabled=vision_enabled,
                    vision_model=vision_model,
                    vision_prompt=vision_prompt,
                    vision_max_tokens=vision_max_tokens,
                )
                page_text = routed_text.strip()
                modality = f"pdf_page_{route_metadata.get('image_route', 'image')}"
            if page_text:
                page_number = page_index + 1
                metadata = {
                    "extension": ".pdf",
                    "path": path.as_posix(),
                    "page_number": page_number,
                    "page_count": page_count,
                    "modality": modality,
                    "ocr_provider": (
                        multimodal_provider
                        if "ocr" in str(route_metadata.get("image_route", modality))
                        else None
                    ),
                    "ocr_languages": (
                        ocr_languages
                        if "ocr" in str(route_metadata.get("image_route", modality))
                        else None
                    ),
                }
                metadata.update(route_metadata)
                loaded_pages.append(
                    RAGDocument(
                        content=page_text,
                        source=f"{path.as_posix()}#page={page_number}",
                        title=f"{pdf_title or path.stem} p.{page_number}",
                        metadata=metadata,
                    )
                )

    return loaded_pages


def _load_image_document(
    path: Path,
    multimodal_provider: str,
    ocr_languages: str,
    vision_enabled: bool,
    vision_model: str,
    vision_prompt: str,
    vision_max_tokens: int,
) -> list[RAGDocument]:
    """Load an image file by routing it to OCR, Vision, both, or skip."""
    recognized_text, route_metadata = extract_routed_image_text(
        path,
        provider=multimodal_provider,
        ocr_languages=ocr_languages,
        vision_enabled=vision_enabled,
        vision_model=vision_model,
        vision_prompt=vision_prompt,
        vision_max_tokens=vision_max_tokens,
    )
    recognized_text = recognized_text.strip()
    if not recognized_text:
        return []
    metadata = {
        "extension": path.suffix.lower(),
        "path": path.as_posix(),
        "source_type": "image",
        "modality": "image",
        "ocr_provider": multimodal_provider if "ocr" in route_metadata["image_route"] else None,
        "ocr_languages": ocr_languages if "ocr" in route_metadata["image_route"] else None,
    }
    metadata.update(route_metadata)
    return [
        RAGDocument(
            content=recognized_text,
            source=path.as_posix(),
            title=path.stem,
            metadata=metadata,
        )
    ]


def extract_routed_image_text(
    path: Path,
    provider: str,
    ocr_languages: str,
    vision_enabled: bool,
    vision_model: str,
    vision_prompt: str,
    vision_max_tokens: int,
) -> tuple[str, dict[str, Any]]:
    """Route an image through OCR, Vision, both, or skip before indexing."""
    features = analyze_image_features(path)
    try:
        ocr_probe_text = quick_ocr_probe_image_text(path, provider, ocr_languages)
    except Exception as exc:  # pragma: no cover - depends on local OCR runtime
        LOGGER.warning("Failed quick OCR probe for %s: %s", path, exc)
        ocr_probe_text = ""

    route, reason, classification = route_image_for_rag(
        features=features,
        ocr_probe_text=ocr_probe_text,
        vision_enabled=vision_enabled,
        classify=lambda: classify_image_with_vision(
            path,
            vision_model,
            DEFAULT_RAG_VISION_CLASSIFICATION_PROMPT,
            min(vision_max_tokens, 64),
        ),
    )
    text, metadata = _extract_text_for_image_route(
        route=route,
        reason=reason,
        features=features,
        ocr_probe_text=ocr_probe_text,
        classification=classification,
        ocr_factory=lambda: extract_image_text(path, provider, ocr_languages),
        vision_factory=lambda: extract_image_vision_text(
            path,
            vision_model,
            vision_prompt,
            vision_max_tokens,
        ),
        vision_enabled=vision_enabled,
        vision_model=vision_model,
    )
    return text, metadata


def extract_routed_image_bytes_text(
    image_bytes: bytes,
    provider: str,
    ocr_languages: str,
    vision_enabled: bool,
    vision_model: str,
    vision_prompt: str,
    vision_max_tokens: int,
) -> tuple[str, dict[str, Any]]:
    """Route rendered image bytes, used for image-only PDF pages."""
    features = analyze_image_bytes_features(image_bytes)
    try:
        ocr_probe_text = quick_ocr_probe_image_bytes_text(image_bytes, provider, ocr_languages)
    except Exception as exc:  # pragma: no cover - depends on local OCR runtime
        LOGGER.warning("Failed quick OCR probe for rendered PDF page: %s", exc)
        ocr_probe_text = ""

    route, reason, classification = route_image_for_rag(
        features=features,
        ocr_probe_text=ocr_probe_text,
        vision_enabled=vision_enabled,
        classify=lambda: classify_image_bytes_with_vision(
            image_bytes,
            vision_model,
            DEFAULT_RAG_VISION_CLASSIFICATION_PROMPT,
            min(vision_max_tokens, 64),
        ),
    )
    return _extract_text_for_image_route(
        route=route,
        reason=reason,
        features=features,
        ocr_probe_text=ocr_probe_text,
        classification=classification,
        ocr_factory=lambda: extract_image_bytes_text(image_bytes, provider, ocr_languages),
        vision_factory=lambda: extract_image_bytes_vision_text(
            image_bytes,
            vision_model,
            vision_prompt,
            vision_max_tokens,
        ),
        vision_enabled=vision_enabled,
        vision_model=vision_model,
    )


def route_image_for_rag(
    *,
    features: Mapping[str, Any],
    ocr_probe_text: str,
    vision_enabled: bool,
    classify,
) -> tuple[str, str, str | None]:
    """Choose OCR, Vision, both, or skip from cheap local signals first."""
    text_chars = _text_signal_length(ocr_probe_text)
    edge_density = _feature_float(features, "edge_density")
    color_count = _feature_float(features, "color_count")
    entropy = _feature_float(features, "entropy")

    if features.get("feature_error"):
        return "ocr", "feature_error_ocr_fallback", None

    if text_chars >= TEXT_HEAVY_OCR_CHARS:
        return "ocr", "text_heavy", None

    if (
        text_chars == 0
        and edge_density <= LOW_INFO_EDGE_DENSITY
        and color_count <= 2
        and entropy <= LOW_INFO_ENTROPY
    ):
        return "skip", "low_information", None

    if edge_density >= DIAGRAM_EDGE_DENSITY and color_count <= DIAGRAM_MAX_COLOR_COUNT:
        return ("ocr_vision" if vision_enabled else "ocr"), "diagram_like", None

    if text_chars == 0 and color_count >= PHOTO_MIN_COLOR_COUNT and entropy >= PHOTO_MIN_ENTROPY:
        return ("vision" if vision_enabled else "skip"), "photo_like", None

    if not vision_enabled:
        if text_chars >= SOME_OCR_TEXT_CHARS:
            return "ocr", "ocr_probe_has_text", None
        return "skip", "uncertain_without_vision", None

    try:
        classification = normalize_image_classification_label(classify())
    except Exception as exc:  # pragma: no cover - defensive fallback
        LOGGER.warning("Failed to classify image route with Vision: %s", exc)
        classification = "uncertain"

    if classification == "text":
        return "ocr", "vision_classified_text", classification
    if classification in {"diagram", "ui", "flowchart", "architecture", "chart"}:
        return "ocr_vision", f"vision_classified_{classification}", classification
    if classification == "photo":
        return "vision", "vision_classified_photo", classification
    if classification == "low_info":
        return "skip", "vision_classified_low_info", classification
    if text_chars >= SOME_OCR_TEXT_CHARS:
        return "ocr_vision", "uncertain_with_ocr_probe_text", classification
    return "vision", "uncertain_vision_fallback", classification


def _extract_text_for_image_route(
    *,
    route: str,
    reason: str,
    features: Mapping[str, Any],
    ocr_probe_text: str,
    classification: str | None,
    ocr_factory,
    vision_factory,
    vision_enabled: bool,
    vision_model: str,
) -> tuple[str, dict[str, Any]]:
    metadata: dict[str, Any] = {
        "image_route": route,
        "image_route_reason": reason,
        "image_features": dict(features),
        "ocr_probe_chars": _text_signal_length(ocr_probe_text),
        "vision_enabled": vision_enabled,
    }
    if classification:
        metadata["image_classification"] = classification
    if vision_enabled:
        metadata["vision_model"] = vision_model

    if route == "skip":
        return "", metadata

    ocr_text = ""
    vision_text = ""
    if "ocr" in route:
        try:
            ocr_text = ocr_factory().strip()
        except Exception as exc:  # pragma: no cover - depends on local OCR runtime
            LOGGER.warning("Failed to OCR routed RAG image: %s", exc)
            metadata["ocr_error"] = str(exc)

    if "vision" in route:
        try:
            vision_text = vision_factory().strip()
            if vision_text:
                metadata["vision_provider"] = "llm"
        except Exception as exc:
            LOGGER.warning("Failed Vision extraction for routed RAG image: %s", exc)
            metadata["vision_error"] = str(exc)

    return _combine_routed_image_text(ocr_text, vision_text), metadata


def _combine_routed_image_text(ocr_text: str, vision_text: str) -> str:
    if ocr_text and vision_text:
        return f"OCR text:\n{ocr_text}\n\nVision description:\n{vision_text}"
    return ocr_text or vision_text


def analyze_image_features(path: Path) -> dict[str, Any]:
    """Extract cheap local image features with Pillow and optional OpenCV."""
    return analyze_image_bytes_features(path.read_bytes())


def analyze_image_bytes_features(image_bytes: bytes) -> dict[str, Any]:
    """Extract cheap local image features from in-memory image bytes."""
    try:
        from PIL import Image, ImageFilter, ImageStat
    except ImportError as exc:  # pragma: no cover - dependency exists in this repository
        raise ImportError("Install pillow to use multimodal RAG image routing.") from exc

    try:
        with Image.open(BytesIO(image_bytes)) as image:
            rgb_image = image.convert("RGB")
            width, height = rgb_image.size
            thumbnail = rgb_image.copy()
            thumbnail.thumbnail((256, 256))
            gray = thumbnail.convert("L")
            gray_stat = ImageStat.Stat(gray)
            edges = gray.filter(ImageFilter.FIND_EDGES)
            edge_density = ImageStat.Stat(edges).mean[0] / 255
            color_sample = thumbnail.copy()
            color_sample.thumbnail((64, 64))
            colors = color_sample.getcolors(maxcolors=4096)
            color_count = len(colors or [])
            entropy = float(gray.entropy())
            features = {
                "width": width,
                "height": height,
                "aspect_ratio": round(width / height, 4) if height else 0,
                "edge_density": round(edge_density, 6),
                "color_count": color_count,
                "entropy": round(entropy, 6),
                "brightness": round(gray_stat.mean[0], 4),
                "contrast": round(gray_stat.stddev[0], 4),
            }
            opencv_edge_density = _try_opencv_edge_density(image_bytes)
            if opencv_edge_density is not None:
                features["opencv_edge_density"] = opencv_edge_density
            return features
    except Exception as exc:
        LOGGER.warning("Failed to analyze RAG image features: %s", exc)
        return {
            "width": 0,
            "height": 0,
            "aspect_ratio": 0,
            "edge_density": 0.0,
            "color_count": 0,
            "entropy": 0.0,
            "feature_error": str(exc),
        }


def _try_opencv_edge_density(image_bytes: bytes) -> float | None:
    try:
        import cv2  # type: ignore
        import numpy as np
    except ImportError:
        return None
    try:
        image_array = np.frombuffer(image_bytes, dtype=np.uint8)
        decoded = cv2.imdecode(image_array, cv2.IMREAD_GRAYSCALE)
        if decoded is None:
            return None
        resized = cv2.resize(decoded, (256, 256), interpolation=cv2.INTER_AREA)
        edges = cv2.Canny(resized, 80, 160)
        return round(float((edges > 0).mean()), 6)
    except Exception:
        return None


def quick_ocr_probe_image_text(path: Path, provider: str, ocr_languages: str) -> str:
    """Run a smaller OCR pass to estimate text volume for routing."""
    normalized_provider = provider.lower().strip()
    if normalized_provider in {"none", "disabled"}:
        return ""
    if normalized_provider not in {"ocr", "tesseract", "local_ocr"}:
        raise ValueError(
            f"Unsupported RAG multimodal provider '{provider}'. "
            "Use ocr, tesseract, local_ocr, none, or disabled."
        )
    try:
        import pytesseract
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise ImportError(
            "Install pillow and pytesseract to use local OCR multimodal RAG loading."
        ) from exc

    with Image.open(path) as image:
        image.thumbnail((768, 768))
        return pytesseract.image_to_string(image, lang=ocr_languages).strip()


def quick_ocr_probe_image_bytes_text(
    image_bytes: bytes,
    provider: str,
    ocr_languages: str,
) -> str:
    """Run a smaller OCR probe against in-memory image bytes."""
    normalized_provider = provider.lower().strip()
    if normalized_provider in {"none", "disabled"}:
        return ""
    if normalized_provider not in {"ocr", "tesseract", "local_ocr"}:
        raise ValueError(
            f"Unsupported RAG multimodal provider '{provider}'. "
            "Use ocr, tesseract, local_ocr, none, or disabled."
        )
    try:
        import pytesseract
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise ImportError(
            "Install pillow and pytesseract to use PDF page OCR for multimodal RAG."
        ) from exc

    with Image.open(BytesIO(image_bytes)) as image:
        image.thumbnail((768, 768))
        return pytesseract.image_to_string(image, lang=ocr_languages).strip()


def extract_image_text(path: Path, provider: str, ocr_languages: str) -> str:
    """Extract text from an image path using the configured multimodal provider."""
    normalized_provider = provider.lower().strip()
    if normalized_provider in {"none", "disabled"}:
        return ""
    if normalized_provider not in {"ocr", "tesseract", "local_ocr"}:
        raise ValueError(
            f"Unsupported RAG multimodal provider '{provider}'. "
            "Use ocr, tesseract, local_ocr, none, or disabled."
        )
    try:
        import pytesseract
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise ImportError(
            "Install pillow and pytesseract to use local OCR multimodal RAG loading."
        ) from exc

    with Image.open(path) as image:
        return pytesseract.image_to_string(image, lang=ocr_languages).strip()


def extract_image_vision_text(
    path: Path,
    model: str,
    prompt: str,
    max_tokens: int,
) -> str:
    """Use a vision-capable chat model to describe image semantics."""
    return extract_image_bytes_vision_text(
        path.read_bytes(),
        model=model,
        prompt=prompt,
        max_tokens=max_tokens,
        mime_type=_image_mime_type(path),
    )


def extract_image_bytes_text(
    image_bytes: bytes,
    provider: str,
    ocr_languages: str,
) -> str:
    """Extract text from rendered image bytes."""
    normalized_provider = provider.lower().strip()
    if normalized_provider in {"none", "disabled"}:
        return ""
    if normalized_provider not in {"ocr", "tesseract", "local_ocr"}:
        raise ValueError(
            f"Unsupported RAG multimodal provider '{provider}'. "
            "Use ocr, tesseract, local_ocr, none, or disabled."
        )
    try:
        import pytesseract
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise ImportError(
            "Install pillow and pytesseract to use PDF page OCR for multimodal RAG."
        ) from exc

    with Image.open(BytesIO(image_bytes)) as image:
        return pytesseract.image_to_string(image, lang=ocr_languages).strip()


def extract_image_bytes_vision_text(
    image_bytes: bytes,
    model: str,
    prompt: str,
    max_tokens: int,
    mime_type: str = "image/png",
) -> str:
    """Use a vision-capable chat model to describe rendered image bytes."""
    return _invoke_vision_model(
        image_bytes=image_bytes,
        mime_type=mime_type,
        model=model,
        prompt=prompt,
        max_tokens=max_tokens,
    )


def classify_image_with_vision(
    path: Path,
    model: str,
    prompt: str,
    max_tokens: int,
) -> str:
    """Classify an image path with a Vision LLM for routing."""
    return classify_image_bytes_with_vision(
        path.read_bytes(),
        model=model,
        prompt=prompt,
        max_tokens=max_tokens,
        mime_type=_image_mime_type(path),
    )


def classify_image_bytes_with_vision(
    image_bytes: bytes,
    model: str,
    prompt: str,
    max_tokens: int,
    mime_type: str = "image/png",
) -> str:
    """Classify in-memory image bytes with a Vision LLM for routing."""
    return _invoke_vision_model(
        image_bytes=image_bytes,
        mime_type=mime_type,
        model=model,
        prompt=prompt,
        max_tokens=max_tokens,
    )


def normalize_image_classification_label(raw_label: Any) -> str:
    """Normalize free-form Vision classification output to a routing label."""
    label = str(raw_label or "").strip().lower()
    for candidate in (
        "architecture",
        "flowchart",
        "diagram",
        "chart",
        "photo",
        "low_info",
        "uncertain",
        "text",
        "ui",
    ):
        if candidate in label:
            return candidate
    if "low information" in label or "blank" in label:
        return "low_info"
    if "screenshot" in label or "dashboard" in label:
        return "ui"
    if "document" in label:
        return "text"
    return "uncertain"


def _invoke_vision_model(
    *,
    image_bytes: bytes,
    mime_type: str,
    model: str,
    prompt: str,
    max_tokens: int,
) -> str:
    encoded_image = base64.b64encode(image_bytes).decode("ascii")
    image_url = f"data:{mime_type};base64,{encoded_image}"
    vision_model = init_chat_model(
        model=model,
        max_tokens=max_tokens,
        tags=["langsmith:nostream"],
    )
    response = observe_model_invoke(
        vision_model,
        [
            HumanMessage(
                content=[
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ]
            )
        ],
        observer_model=model,
        observer_component="rag_vision",
    )
    return _message_content_to_text(response.content).strip()


def _message_content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if text:
                    parts.append(str(text))
        return "\n".join(parts)
    return str(content or "")


def _image_mime_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".webp":
        return "image/webp"
    if suffix in {".tif", ".tiff"}:
        return "image/tiff"
    if suffix == ".bmp":
        return "image/bmp"
    return "image/png"


def _feature_float(features: Mapping[str, Any], key: str) -> float:
    try:
        return float(features.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _text_signal_length(text: str) -> int:
    return len("".join(str(text or "").split()))


def _extract_json_text(
    data: Any,
    json_text_fields: Sequence[str] | None = None,
) -> tuple[str, list[str]]:
    """从任意 JSON 结构中提取可索引文本和字段路径.

    返回 `(text, field_paths)`：
    - `text` 是用于后续切块和 embedding 的拼接文本。
    - `field_paths` 是每段文本来源字段，最终会进入 citation metadata。
    """
    fragments: list[tuple[str, str]] = []

    if json_text_fields:
        for field_path in json_text_fields:
            extracted_value = _extract_field_path(data, field_path)
            fragments.extend(_collect_text_fragments(extracted_value, prefix=field_path))
    else:
        fragments.extend(_collect_text_fragments(data))

    deduplicated_fragments = []
    field_paths = []
    seen_fragments = set()
    for field_path, fragment in fragments:
        normalized_fragment = fragment.strip()
        if not normalized_fragment or normalized_fragment in seen_fragments:
            continue
        seen_fragments.add(normalized_fragment)
        deduplicated_fragments.append(normalized_fragment)
        field_paths.append(field_path)

    return "\n\n".join(deduplicated_fragments), field_paths


def _extract_json_title(data: Any) -> str | None:
    """从常见字段名推断 JSON document 标题."""
    if not isinstance(data, dict):
        return None
    for title_key in ("title", "name", "headline", "subject"):
        title_value = data.get(title_key)
        if isinstance(title_value, str) and title_value.strip():
            return title_value.strip()
    return None


def _extract_field_path(data: Any, field_path: str) -> Any:
    """解析 JSON dotted path.

    例如 `items.0.title` 会依次访问 dict 字段和 list 索引。
    如果路径不存在或类型不匹配，返回 None。
    """
    current_value = data
    for part in field_path.split("."):
        if isinstance(current_value, list):
            if part.isdigit():
                index = int(part)
                if 0 <= index < len(current_value):
                    current_value = current_value[index]
                    continue
            return None
        if not isinstance(current_value, dict):
            return None
        current_value = current_value.get(part)
        if current_value is None:
            return None
    return current_value


def _collect_text_fragments(value: Any, prefix: str = "$") -> list[tuple[str, str]]:
    """递归收集 JSON 中可索引的标量片段.

    返回值是 `(field_path, text)` 列表。数字和布尔值也会转成字符串，因为
    很多配置、政策、指标类知识会以数字或布尔值形式存在。
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [(prefix, value)]
    if isinstance(value, (int, float, bool)):
        return [(prefix, str(value))]
    if isinstance(value, list):
        fragments: list[tuple[str, str]] = []
        for index, item in enumerate(value):
            fragments.extend(_collect_text_fragments(item, prefix=f"{prefix}.{index}"))
        return fragments
    if isinstance(value, dict):
        fragments = []
        for key, nested_value in value.items():
            fragments.extend(_collect_text_fragments(nested_value, prefix=f"{prefix}.{key}"))
        return fragments
    return []


def _guess_title(text: str, path: Path) -> str:
    """从文本内容推断标题.

    优先级：
    1. 第一行 Markdown heading。
    2. 第一行短文本。
    3. 文件名 stem。
    """
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    if first_line.startswith("#"):
        stripped_heading = first_line.lstrip("#").strip()
        if stripped_heading:
            return stripped_heading
    if 0 < len(first_line) <= 120:
        return first_line
    return path.stem


def _document_heading_hierarchy(text: str) -> list[str]:
    """提取文档级 Markdown 标题列表.

    这里保留前 10 个标题，作为文档整体 metadata。chunk 级更精确的标题路径
    会在 splitter 中根据 chunk 位置重新计算。
    """
    headings = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("#"):
            continue
        heading = stripped.lstrip("#").strip()
        if heading:
            headings.append(heading)
    return headings[:10]


def _file_sha256(path: Path) -> str:
    """流式计算文件 sha256，避免一次性读入大文件."""
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
