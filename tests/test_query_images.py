"""Verify direct user-image Vision recognition and preserved RAG PDF routing."""

import asyncio
import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph.message import add_messages
from pydantic import ValidationError

from open_deep_research.configuration import Configuration
from open_deep_research.memory import writer as memory_writer
from open_deep_research.rag import loaders, query_images
from open_deep_research.rag.loaders import knowledge
from open_deep_research.rag.loaders import query_images as query_image_loader
from open_deep_research.rag.loaders.knowledge import extract_image_bytes_vision_text
from open_deep_research.workflow import intake

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
RECOGNIZED_TEXT = "Uploaded screenshot shows public complaints about delayed deliveries."


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
    model = Mock()
    model.invoke.return_value = AIMessage(content=RECOGNIZED_TEXT)
    factory = Mock(return_value=model)
    monkeypatch.setattr(knowledge, "init_chat_model", factory)
    # Restore compatibility-hook assignments along with their test patches.
    for module in (loaders, query_images, query_image_loader):
        monkeypatch.setattr(
            module, "extract_image_bytes_vision_text", extract_image_bytes_vision_text
        )
    return model, factory


@pytest.mark.parametrize("entrypoint", [query_images, query_image_loader, loaders])
def test_valid_image_uses_one_shared_vision_content_call(entrypoint, vision_runtime):
    model, factory = vision_runtime
    context = entrypoint.build_query_image_context(
        [_image_message(_data_url())], **VISION_SETTINGS
    )

    factory.assert_called_once_with(
        model=VISION_SETTINGS["vision_model"],
        max_tokens=123,
        tags=["langsmith:nostream"],
    )
    model.invoke.assert_called_once()
    content = model.invoke.call_args.args[0][0].content
    assert content == [
        {"type": "text", "text": VISION_SETTINGS["vision_prompt"]},
        {"type": "image_url", "image_url": {"url": _data_url()}},
    ]
    assert RECOGNIZED_TEXT in context
    assert "User image 1\nSource: image/png" in context
    assert "Route:" not in context
    assert "Reason:" not in context


def test_user_images_never_use_features_ocr_or_classification(monkeypatch, vision_runtime):
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
    forbidden = {name: Mock(side_effect=AssertionError(name)) for name in forbidden_names}
    for name, function in forbidden.items():
        monkeypatch.setattr(knowledge, name, function)

    assert RECOGNIZED_TEXT in _context([_image_message(_data_url())])
    vision_runtime[0].invoke.assert_called_once()
    for function in forbidden.values():
        function.assert_not_called()


@pytest.mark.parametrize("max_images", [0, 1, 2])
def test_multiple_images_respect_call_limit(max_images, vision_runtime):
    model, _ = vision_runtime
    context = _context([_image_message(*[_data_url()] * 4)], max_images=max_images)

    assert model.invoke.call_count == max_images
    assert context.count("User image ") == max_images


@pytest.mark.parametrize("result", [AIMessage(content="  "), RuntimeError("unavailable")])
def test_empty_and_failed_recognition_still_count_toward_limit(result, vision_runtime):
    model, _ = vision_runtime
    model.invoke.side_effect = [result, AIMessage(content="Must not be requested")]

    assert _context([_image_message(_data_url(), _data_url())], max_images=1) == ""
    model.invoke.assert_called_once()


def test_oversized_image_is_skipped_before_vision(vision_runtime):
    model, factory = vision_runtime
    assert _context([_image_message(_data_url())], max_bytes=len(PNG_BYTES) - 1) == ""
    factory.assert_not_called()
    model.invoke.assert_not_called()


def test_oversized_file_is_not_read(monkeypatch, tmp_path, vision_runtime):
    path = tmp_path / "large.png"
    path.write_bytes(PNG_BYTES)
    read_bytes = Mock(side_effect=AssertionError("oversized file must not be read"))
    monkeypatch.setattr(type(path), "read_bytes", read_bytes)

    assert _context([{"content": [{"path": str(path)}]}], max_bytes=1) == ""
    read_bytes.assert_not_called()
    vision_runtime[1].assert_not_called()


def test_invalid_image_does_not_consume_valid_image_limit(vision_runtime):
    model, _ = vision_runtime
    messages = [_image_message(_data_url(PNG_BYTES * 2), _data_url())]

    assert RECOGNIZED_TEXT in _context(messages, max_images=1, max_bytes=len(PNG_BYTES))
    model.invoke.assert_called_once()


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
    vision_runtime[1].assert_not_called()


@pytest.mark.parametrize("input_format", ["image_url", "input_image", "base64", "text"])
def test_existing_message_image_formats(input_format, vision_runtime):
    data_url = _data_url(mime_type="image/jpeg")
    if input_format == "text":
        content = f"Please analyze this image: {data_url}"
    elif input_format == "base64":
        content = [{
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": base64.b64encode(PNG_BYTES).decode("ascii"),
            },
        }]
    else:
        content = [{"type": input_format, "image_url": data_url}]

    assert RECOGNIZED_TEXT in _context([{"role": "user", "content": content}])
    sent_url = vision_runtime[0].invoke.call_args.args[0][0].content[1]["image_url"]["url"]
    assert sent_url == data_url


@pytest.mark.parametrize(
    ("extension", "mime_type"),
    [(".png", "image/png"), (".jpg", "image/jpeg"), (".webp", "image/webp"),
     (".bmp", "image/bmp"), (".tiff", "image/tiff")],
)
def test_local_image_mime_type_is_preserved(extension, mime_type, tmp_path, vision_runtime):
    path = tmp_path / f"image{extension}"
    path.write_bytes(PNG_BYTES)

    assert RECOGNIZED_TEXT in _context([{"content": [{"image_path": str(path)}]}])
    sent_url = vision_runtime[0].invoke.call_args.args[0][0].content[1]["image_url"]["url"]
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
    vision_runtime[0].invoke.assert_called_once()


@pytest.mark.parametrize("messages", [[], [HumanMessage(content="Research brand sentiment.")]])
def test_no_images_do_not_call_vision(messages, vision_runtime):
    assert asyncio.run(intake.enrich_query_images({"messages": messages}, {})) == {}
    vision_runtime[1].assert_not_called()


def test_vision_failure_keeps_workflow_running_and_processes_remaining_images(vision_runtime):
    model, _ = vision_runtime
    state = {"messages": [_image_message(_data_url(), _data_url())]}
    model.invoke.side_effect = [RuntimeError("Vision unavailable"), AIMessage(content=RECOGNIZED_TEXT)]

    result = asyncio.run(intake.enrich_query_images(state, {}))
    assert RECOGNIZED_TEXT in result["messages"][0].content
    assert model.invoke.call_count == 2

    model.invoke.side_effect = RuntimeError("Vision unavailable")
    result = asyncio.run(intake.enrich_query_images(state, {}))
    assert result == {}
    command = asyncio.run(intake.clarify_with_user(state, {"configurable": {"allow_clarification": False}}))
    assert command.goto == "write_research_brief"


def test_query_image_switch_disables_recognition(vision_runtime):
    state = {"messages": [_image_message(_data_url())]}
    config = {"configurable": {"rag_query_image_enabled": False}}

    assert asyncio.run(intake.enrich_query_images(state, config)) == {}
    vision_runtime[1].assert_not_called()


def test_context_reaches_clarification_and_brief_without_rag_enabled(monkeypatch, vision_runtime):
    config = {"configurable": {
        "rag_enabled": False,
        "rag_multimodal_enabled": False,
        "rag_multimodal_provider": "disabled",
        "rag_vision_enabled": False,
        "rag_query_image_enabled": True,
        "rag_vision_model": VISION_SETTINGS["vision_model"],
        "rag_vision_prompt": VISION_SETTINGS["vision_prompt"],
        "rag_vision_max_tokens": 123,
        "allow_clarification": True,
    }}
    persist = Mock()
    monkeypatch.setattr(memory_writer, "persist_conversation_memory", persist)
    monkeypatch.setattr(intake.shared, "configurable_model", Mock())
    monkeypatch.setattr(intake, "structured_output_chain", Mock())
    invoke = AsyncMock(side_effect=[
        ({"parsed": SimpleNamespace(need_clarification=False, verification="Clear")}, {}),
        ({"parsed": SimpleNamespace(research_brief="Research delivery complaints")}, {}),
    ])
    monkeypatch.setattr(intake, "ainvoke_model_with_budget", invoke)
    original_messages = [_image_message(_data_url())]

    update = asyncio.run(intake.enrich_query_images({"messages": original_messages}, config))
    assert update["messages"][0].additional_kwargs["rag_query_image_context"] is True
    state = {"messages": add_messages(original_messages, update["messages"])}
    assert asyncio.run(intake.enrich_query_images(state, config)) == {}
    clarification = asyncio.run(intake.clarify_with_user(state, config))
    brief = asyncio.run(intake.write_research_brief(state, config))

    assert clarification.goto == "write_research_brief"
    assert brief.update["workflow"]["brief"] == "Research delivery complaints"
    assert invoke.await_count == 2
    for call in invoke.await_args_list:
        assert RECOGNIZED_TEXT in call.args[1][0].content
    vision_runtime[0].invoke.assert_called_once()
    vision_runtime[1].assert_called_once_with(
        model=VISION_SETTINGS["vision_model"], max_tokens=123, tags=["langsmith:nostream"]
    )
    assert memory_writer._messages_without_query_image_context(state["messages"]) == original_messages
    persist.assert_not_called()


@pytest.mark.parametrize(
    ("field", "value"),
    [("rag_vision_model", ""), ("rag_vision_prompt", " "), ("rag_vision_max_tokens", 0)],
)
def test_query_images_validate_shared_vision_settings_with_rag_vision_disabled(field, value):
    with pytest.raises(ValidationError, match=field):
        Configuration(rag_vision_enabled=False, rag_query_image_enabled=True, **{field: value})


def test_disabled_query_and_rag_vision_allow_unused_vision_settings():
    Configuration(
        rag_vision_enabled=False,
        rag_query_image_enabled=False,
        rag_vision_model="",
        rag_vision_prompt="",
        rag_vision_max_tokens=0,
    )


@pytest.mark.parametrize("route", ["ocr", "ocr_vision", "vision", "disabled"])
def test_rag_pdf_preserves_text_extraction_and_scanned_page_routing(route, monkeypatch, tmp_path):
    fitz = pytest.importorskip("fitz")
    pdf_path = tmp_path / "knowledge.pdf"
    with fitz.open() as document:
        document.new_page().insert_text((72, 72), "Original PDF text")
        document.new_page()
        document.save(pdf_path)

    features = Mock(return_value={
        "edge_density": 0.25 if route == "ocr_vision" else 0.05,
        "color_count": 32 if route == "ocr_vision" else 200,
        "entropy": 6.0,
    })
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
