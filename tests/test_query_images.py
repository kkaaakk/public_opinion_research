"""Verify direct user-image Vision recognition and preserved RAG PDF routing."""

import asyncio
import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from langchain_core.messages import HumanMessage
from pydantic import ValidationError

from open_deep_research.configuration import Configuration
from open_deep_research.rag import loaders, query_images
from open_deep_research.rag.loaders import knowledge
from open_deep_research.rag.loaders import query_images as query_image_loader
from open_deep_research.rag.loaders.knowledge import extract_image_bytes_vision_text
from open_deep_research.rag.service import build_rag_pipeline_config
from open_deep_research.workflow import intake
from open_deep_research.workflow.python_state import copy_state

PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aZxkAAAAASUVORK5CYII="
)
VISION_SETTINGS = {
    "vision_model": "openai:vision-fixture",
    "vision_prompt": "Describe visible information.",
    "vision_max_tokens": 123,
    "max_images": 3,
    "max_bytes": 5_000_000,
}
RECOGNIZED_TEXT = (
    "Uploaded screenshot shows public complaints about delayed deliveries."
)


def _data_url(image_bytes=PNG_BYTES, mime_type="image/png"):
    return f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"


def _image_message(*refs):
    return HumanMessage(
        content=[{"type": "image_url", "image_url": {"url": ref}} for ref in refs]
    )


def _context(messages, **overrides):
    return query_images.build_query_image_context(
        messages, **{**VISION_SETTINGS, **overrides}
    )


@pytest.fixture
def vision_runtime(monkeypatch):
    generate = Mock(return_value=(RECOGNIZED_TEXT, {}))
    monkeypatch.setattr(knowledge, "generate_text_sync", generate)
    # Restore compatibility-hook assignments along with their test patches.
    for module in (knowledge, loaders, query_images, query_image_loader):
        monkeypatch.setattr(
            module, "extract_image_bytes_vision_text", extract_image_bytes_vision_text
        )
    return generate


@pytest.mark.parametrize("entrypoint", [query_images, query_image_loader, loaders])
def test_valid_image_uses_one_shared_vision_content_call(entrypoint, vision_runtime):
    generate = vision_runtime
    context = entrypoint.build_query_image_context(
        [_image_message(_data_url())], **VISION_SETTINGS
    )

    generate.assert_called_once()
    assert generate.call_args.args[0] == VISION_SETTINGS["vision_model"]
    assert generate.call_args.kwargs["max_tokens"] == 123
    assert generate.call_args.kwargs["component"] == "rag_vision"
    content = generate.call_args.args[1][0]["content"]
    assert content == [
        {"type": "input_text", "text": VISION_SETTINGS["vision_prompt"]},
        {"type": "input_image", "image_url": _data_url(), "detail": "auto"},
    ]
    assert RECOGNIZED_TEXT in context
    assert "User image 1\nSource: image/png" in context
    assert "Route:" not in context
    assert "Reason:" not in context


def test_user_images_never_use_features_ocr_or_classification(
    monkeypatch, vision_runtime
):
    forbidden_names = (
        "analyze_image_features",
        "analyze_image_bytes_features",
        "_try_opencv_edge_density",
        "quick_ocr_probe_image_text",
        "quick_ocr_probe_image_bytes_text",
        "extract_image_text",
        "extract_image_bytes_text",
        "extract_routed_image_text",
        "extract_routed_image_bytes_text",
        "route_image_for_rag",
        "classify_image_with_vision",
        "classify_image_bytes_with_vision",
    )
    forbidden = {
        name: Mock(side_effect=AssertionError(name)) for name in forbidden_names
    }
    for name, function in forbidden.items():
        monkeypatch.setattr(knowledge, name, function)

    assert RECOGNIZED_TEXT in _context([_image_message(_data_url())])
    vision_runtime.assert_called_once()
    for function in forbidden.values():
        function.assert_not_called()


@pytest.mark.parametrize("max_images", [0, 1, 2])
def test_multiple_images_respect_call_limit(max_images, vision_runtime):
    generate = vision_runtime
    context = _context([_image_message(*[_data_url()] * 4)], max_images=max_images)

    assert generate.call_count == max_images
    assert context.count("User image ") == max_images


@pytest.mark.parametrize(
    "result", [("  ", {}), RuntimeError("unavailable")]
)
def test_empty_and_failed_recognition_still_count_toward_limit(result, vision_runtime):
    generate = vision_runtime
    generate.side_effect = [result, ("Must not be requested", {})]

    assert _context([_image_message(_data_url(), _data_url())], max_images=1) == ""
    generate.assert_called_once()


def test_oversized_image_is_skipped_before_vision(vision_runtime):
    generate = vision_runtime
    assert _context([_image_message(_data_url())], max_bytes=len(PNG_BYTES) - 1) == ""
    generate.assert_not_called()


def test_oversized_file_is_not_read(monkeypatch, tmp_path, vision_runtime):
    path = tmp_path / "large.png"
    path.write_bytes(PNG_BYTES)
    read_bytes = Mock(side_effect=AssertionError("oversized file must not be read"))
    monkeypatch.setattr(type(path), "read_bytes", read_bytes)

    assert _context([{"content": [{"path": str(path)}]}], max_bytes=1) == ""
    read_bytes.assert_not_called()
    vision_runtime.assert_not_called()


def test_invalid_image_does_not_consume_valid_image_limit(vision_runtime):
    generate = vision_runtime
    messages = [_image_message(_data_url(PNG_BYTES * 2), _data_url())]

    assert RECOGNIZED_TEXT in _context(messages, max_images=1, max_bytes=len(PNG_BYTES))
    generate.assert_called_once()


@pytest.mark.parametrize(
    "image_ref",
    [
        "data:image/png;base64,a===",
        "data:image/png;base64,",
        "data:text/plain;base64,ZmFrZQ==",
        "https://example.invalid/image.png",
        "./missing.png",
        "./unsupported.svg",
    ],
)
def test_invalid_references_skip_safely(image_ref, vision_runtime):
    assert _context([_image_message(image_ref)]) == ""
    vision_runtime.assert_not_called()


@pytest.mark.parametrize("input_format", ["image_url", "input_image", "base64", "text"])
def test_existing_message_image_formats(input_format, vision_runtime):
    data_url = _data_url(mime_type="image/jpeg")
    if input_format == "text":
        content = f"Please analyze this image: {data_url}"
    elif input_format == "base64":
        content = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": base64.b64encode(PNG_BYTES).decode("ascii"),
                },
            }
        ]
    else:
        content = [{"type": input_format, "image_url": data_url}]

    assert RECOGNIZED_TEXT in _context([{"role": "user", "content": content}])
    sent_url = (
        vision_runtime.call_args.args[1][0]["content"][1]["image_url"]
    )
    assert sent_url == data_url


@pytest.mark.parametrize(
    ("extension", "mime_type"),
    [
        (".png", "image/png"),
        (".jpg", "image/jpeg"),
        (".webp", "image/webp"),
        (".bmp", "image/bmp"),
        (".tiff", "image/tiff"),
    ],
)
def test_local_image_mime_type_is_preserved(
    extension, mime_type, tmp_path, vision_runtime
):
    path = tmp_path / f"image{extension}"
    path.write_bytes(PNG_BYTES)

    assert RECOGNIZED_TEXT in _context([{"content": [{"image_path": str(path)}]}])
    sent_url = (
        vision_runtime.call_args.args[1][0]["content"][1]["image_url"]
    )
    assert sent_url == _data_url(mime_type=mime_type)


@pytest.mark.parametrize("input_format", ["path", "file_uri"])
def test_local_image_text_and_file_uri(input_format, tmp_path, vision_runtime):
    path = tmp_path / "screenshot.png"
    path.write_bytes(PNG_BYTES)
    if input_format == "path":
        message = HumanMessage(content=f"Analyze this screenshot: {path}")
    else:
        message = _image_message(path.as_uri())

    assert RECOGNIZED_TEXT in _context([message])
    vision_runtime.assert_called_once()


@pytest.mark.parametrize(
    "messages", [[], [HumanMessage(content="Research brand sentiment.")]]
)
def test_no_images_do_not_call_vision(messages, vision_runtime):
    assert asyncio.run(intake.enrich_query_images({"messages": messages}, {})) == {}
    vision_runtime.assert_not_called()


def test_vision_failure_keeps_workflow_running_and_processes_remaining_images(
    vision_runtime,
):
    generate = vision_runtime
    state = {"messages": [_image_message(_data_url(), _data_url())]}
    generate.side_effect = [
        RuntimeError("Vision unavailable"),
        (RECOGNIZED_TEXT, {}),
    ]

    result = asyncio.run(intake.enrich_query_images(state, {}))
    assert RECOGNIZED_TEXT in result["messages"][0].content
    assert generate.call_count == 2

    generate.side_effect = RuntimeError("Vision unavailable")
    result = asyncio.run(intake.enrich_query_images(state, {}))
    assert result == {}
    command = asyncio.run(
        intake.clarification_step(
            state, {"configurable": {"allow_clarification": False}}
        )
    )
    assert command.goto == "write_research_brief"


def test_query_image_switch_disables_recognition(vision_runtime):
    state = {"messages": [_image_message(_data_url())]}
    config = {"configurable": {"rag_query_image_enabled": False}}

    assert asyncio.run(intake.enrich_query_images(state, config)) == {}
    vision_runtime.assert_not_called()


def test_context_reaches_clarification_and_brief_without_rag_enabled(
    monkeypatch, vision_runtime
):
    config = {
        "configurable": {
            "rag_enabled": False,
            "rag_multimodal_enabled": False,
            "rag_multimodal_provider": "disabled",
            "rag_vision_enabled": False,
            "rag_query_image_enabled": True,
            "rag_vision_model": VISION_SETTINGS["vision_model"],
            "rag_query_image_prompt": VISION_SETTINGS["vision_prompt"],
            "rag_vision_max_tokens": 123,
            "allow_clarification": True,
        }
    }
    invoke = AsyncMock(side_effect=[
        (SimpleNamespace(need_clarification=False, verification="Clear"), {}),
        (SimpleNamespace(research_brief="Research delivery complaints"), {}),
    ])
    monkeypatch.setattr(intake, "generate_structured", invoke)
    original_messages = [_image_message(_data_url())]
    original_messages[0].id = "user-image-input"

    update = asyncio.run(
        intake.enrich_query_images({"messages": original_messages}, config)
    )
    assert update["messages"][0].additional_kwargs["rag_query_image_context"] is True
    state = {
        "messages": copy_state({"messages": [*original_messages, *update["messages"]]})[
            "messages"
        ]
    }
    assert asyncio.run(intake.enrich_query_images(state, config)) == {}
    clarification = asyncio.run(intake.clarification_step(state, config))
    brief = asyncio.run(intake.research_brief_step(state, config))

    assert clarification.goto == "write_research_brief"
    assert brief.update["workflow"]["brief"] == "Research delivery complaints"
    assert invoke.await_count == 2
    for call in invoke.await_args_list:
        assert RECOGNIZED_TEXT in call.args[1]
    vision_runtime.assert_called_once()
    assert vision_runtime.call_args.args[0] == VISION_SETTINGS["vision_model"]
    assert vision_runtime.call_args.kwargs["max_tokens"] == 123
    assert (
        vision_runtime.call_args.args[1][0]["content"][0]["text"]
        == VISION_SETTINGS["vision_prompt"]
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [("rag_vision_model", ""), ("rag_vision_max_tokens", 0)],
)
def test_query_images_validate_shared_vision_settings_with_rag_vision_disabled(
    field, value
):
    with pytest.raises(ValidationError, match=field):
        Configuration(
            rag_vision_enabled=False, rag_query_image_enabled=True, **{field: value}
        )


def test_disabled_query_and_rag_vision_allow_unused_vision_settings():
    Configuration(
        rag_vision_enabled=False,
        rag_query_image_enabled=False,
        rag_vision_model="",
        rag_vision_prompt="",
        rag_query_image_prompt="",
        rag_vision_max_tokens=0,
    )


@pytest.mark.parametrize(
    "query_prompt", [None, "Read all visible details for the user."]
)
@pytest.mark.parametrize("rag_prompt", [None, "Extract concise searchable knowledge."])
def test_user_and_rag_images_use_independent_prompts(
    query_prompt, rag_prompt, monkeypatch, vision_runtime
):
    overrides = {}
    if query_prompt is not None:
        overrides["rag_query_image_prompt"] = query_prompt
    if rag_prompt is not None:
        overrides["rag_vision_prompt"] = rag_prompt
    configurable = Configuration(**overrides)
    pipeline_config = build_rag_pipeline_config(configurable)
    assert configurable.rag_query_image_prompt != configurable.rag_vision_prompt
    assert pipeline_config.vision_prompt == (
        rag_prompt or knowledge.DEFAULT_RAG_VISION_PROMPT
    )

    result = asyncio.run(
        intake.enrich_query_images(
            {"messages": [_image_message(_data_url())]},
            {"configurable": configurable.model_dump()},
        )
    )
    assert RECOGNIZED_TEXT in result["messages"][0].content

    monkeypatch.setattr(
        knowledge,
        "analyze_image_bytes_features",
        Mock(
            return_value={
                "edge_density": 0.05,
                "color_count": 200,
                "entropy": 6.0,
            }
        ),
    )
    monkeypatch.setattr(
        knowledge, "quick_ocr_probe_image_bytes_text", Mock(return_value="")
    )
    rag_text, metadata = knowledge.extract_routed_image_bytes_text(
        PNG_BYTES,
        provider=pipeline_config.multimodal_provider,
        ocr_languages=pipeline_config.ocr_languages,
        vision_enabled=pipeline_config.vision_enabled,
        vision_model=pipeline_config.vision_model,
        vision_prompt=pipeline_config.vision_prompt,
        vision_max_tokens=pipeline_config.vision_max_tokens,
    )
    assert rag_text == RECOGNIZED_TEXT
    assert metadata["image_route"] == "vision"
    calls = vision_runtime.call_args_list
    assert len(calls) == 2
    assert calls[0].args[1][0]["content"][0]["text"] == configurable.rag_query_image_prompt
    assert calls[1].args[1][0]["content"][0]["text"] == configurable.rag_vision_prompt


def test_query_image_prompts_come_from_configuration_not_environment(
    monkeypatch, vision_runtime
):
    monkeypatch.setenv(
        "RAG_QUERY_IMAGE_PROMPT", "Describe the uploaded image in detail."
    )
    monkeypatch.setenv("RAG_VISION_PROMPT", "Extract searchable knowledge.")
    configurable = Configuration(
        rag_query_image_prompt="Configured query image prompt.",
        rag_vision_prompt="Configured RAG vision prompt.",
    )

    asyncio.run(
        intake.enrich_query_images(
            {"messages": [_image_message(_data_url())]},
            {"configurable": configurable.model_dump()},
        )
    )
    vision_runtime.assert_called_once()
    assert (
        vision_runtime.call_args.args[1][0]["content"][0]["text"]
        == configurable.rag_query_image_prompt
    )
    assert (
        build_rag_pipeline_config(configurable).vision_prompt
        == "Configured RAG vision prompt."
    )


@pytest.mark.parametrize("query_enabled", [False, True])
@pytest.mark.parametrize("rag_enabled", [False, True])
@pytest.mark.parametrize("field", ["rag_query_image_prompt", "rag_vision_prompt"])
@pytest.mark.parametrize("empty_prompt", ["", " \n "])
def test_each_prompt_is_validated_only_for_its_enabled_path(
    query_enabled, rag_enabled, field, empty_prompt
):
    settings = {
        "rag_query_image_enabled": query_enabled,
        "rag_vision_enabled": rag_enabled,
        field: empty_prompt,
    }
    enabled = query_enabled if field == "rag_query_image_prompt" else rag_enabled
    if enabled:
        with pytest.raises(ValidationError, match=field):
            Configuration(**settings)
    else:
        Configuration(**settings)


@pytest.mark.parametrize("route", ["ocr", "ocr_vision", "vision", "disabled"])
def test_rag_pdf_preserves_text_extraction_and_scanned_page_routing(
    route, monkeypatch, tmp_path
):
    fitz = pytest.importorskip("fitz")
    pdf_path = tmp_path / "knowledge.pdf"
    with fitz.open() as document:
        document.new_page().insert_text((72, 72), "Original PDF text")
        document.new_page()
        document.save(pdf_path)

    features = Mock(
        return_value={
            "edge_density": 0.25 if route == "ocr_vision" else 0.05,
            "color_count": 32 if route == "ocr_vision" else 200,
            "entropy": 6.0,
        }
    )
    probe = Mock(return_value="Scanned document text " * 8 if route == "ocr" else "")
    ocr = Mock(return_value="Scanned OCR text")
    vision = Mock(return_value="Scanned Vision description")
    classify = Mock(side_effect=AssertionError("rule route should suffice"))
    monkeypatch.setattr(knowledge, "analyze_image_bytes_features", features)
    monkeypatch.setattr(knowledge, "quick_ocr_probe_image_bytes_text", probe)
    monkeypatch.setattr(knowledge, "extract_image_bytes_text", ocr)
    monkeypatch.setattr(knowledge, "extract_image_bytes_vision_text", vision)
    monkeypatch.setattr(knowledge, "classify_image_bytes_with_vision", classify)

    documents = knowledge.load_documents_from_paths(
        [str(pdf_path)], multimodal_enabled=route != "disabled", vision_enabled=True
    )
    assert documents[0].content == "Original PDF text"
    assert documents[0].metadata["modality"] == "text"
    assert documents[0].metadata["page_number"] == 1
    classify.assert_not_called()
    if route == "disabled":
        assert len(documents) == 1
        for function in (features, probe, ocr, vision):
            function.assert_not_called()
        return

    assert len(documents) == 2
    assert documents[1].source.endswith("#page=2")
    assert documents[1].metadata["page_number"] == 2
    assert documents[1].metadata["page_count"] == 2
    assert documents[1].metadata["image_route"] == route
    assert documents[1].metadata["modality"] == f"pdf_page_{route}"
    features.assert_called_once()
    probe.assert_called_once()
    assert ocr.call_count == int("ocr" in route)
    assert vision.call_count == int("vision" in route)
    if "ocr" in route:
        assert "Scanned OCR text" in documents[1].content
    if "vision" in route:
        assert "Scanned Vision description" in documents[1].content
