"""Whitelist JSON codec shared with DBOS's PUBLIC Serializer extension."""

from __future__ import annotations

import json

from agents.items import ModelResponse, TResponseOutputItem
from agents.usage import Usage
from dbos import Serializer
from langchain_core.messages import BaseMessage, message_to_dict, messages_from_dict
from pydantic import TypeAdapter

from open_deep_research.state import ResearchReview, ResearchTask, Section

MODELS = {c.__name__: c for c in (Section, ResearchReview, ResearchTask)}


class DomainFailure(RuntimeError):
    def __init__(self, message, kind="DomainFailure"):
        self.kind = kind
        super().__init__(message)


def encode(value):
    if isinstance(value, BaseMessage):
        return {"_native_message": message_to_dict(value)}
    if type(value).__name__ in MODELS and type(value) is MODELS[type(value).__name__]:
        return {
            "_native_type": type(value).__name__,
            "value": value.model_dump(mode="json"),
        }
    if isinstance(value, ModelResponse):
        return {
            "_native_response": {
                "output": TypeAdapter(list[TResponseOutputItem]).dump_python(
                    value.output, mode="json"
                ),
                "usage": TypeAdapter(Usage).dump_python(value.usage, mode="json"),
                "response_id": value.response_id,
                "request_id": value.request_id,
                "raw_usage": value.raw_usage,
            }
        }
    if isinstance(value, BaseException):
        return {
            "_native_error": str(value),
            "kind": getattr(value, "kind", type(value).__name__),
        }
    raise TypeError(f"Unsupported persisted type {type(value).__name__}")


def decode(value):
    if "_native_message" in value:
        return messages_from_dict([value["_native_message"]])[0]
    if value.get("_native_type") in MODELS:
        return MODELS[value["_native_type"]].model_validate(value["value"])
    if "_native_response" in value:
        d = value["_native_response"]
        return ModelResponse(
            output=TypeAdapter(list[TResponseOutputItem]).validate_python(d["output"]),
            usage=TypeAdapter(Usage).validate_python(d["usage"]),
            response_id=d["response_id"],
            request_id=d["request_id"],
            raw_usage=d["raw_usage"],
        )
    if "_native_error" in value:
        return DomainFailure(value["_native_error"], value["kind"])
    return value


def dumps(value):
    return json.dumps(value, default=encode, sort_keys=True, ensure_ascii=False)


def loads(text):
    return json.loads(text, object_hook=decode)


class NativeJSON(Serializer):
    def name(self):
        return "por_native_json_v1"

    def serialize(self, value):
        return dumps(value)

    def deserialize(self, value):
        return loads(value)
