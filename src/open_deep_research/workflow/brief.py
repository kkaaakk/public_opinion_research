"""Markdown research specification and bounded human interaction contracts.

Markdown alone carries research semantics. Versions and decisions control execution;
headings are presentation, never tool permissions or executable instructions.
"""

from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from open_deep_research.budget import estimate_text_tokens

MAX_BRIEF_TOKENS = 2500


def validate_brief(value: str, *, check_size: bool = True) -> str:
    """Validate human-readable Markdown, using the application's approximate counter.

    This limit is an engineering bound, not a provider tokenizer measurement.
    Never truncate a specification to make it pass.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Research Brief must contain Markdown")
    value = value.strip()
    if not value.startswith("# ") or "\n## " not in value or len(value) < 40:
        raise ValueError(
            "Research Brief requires a title and substantive Markdown sections"
        )
    if check_size and estimate_text_tokens(value) > MAX_BRIEF_TOKENS:
        raise ValueError(
            f"Research Brief exceeds {MAX_BRIEF_TOKENS} estimated tokens; shorten it and resubmit"
        )
    return value


def brief_metadata(value: str, revision: int, run_id: str) -> dict:
    from open_deep_research.web.run_data import digest

    content_hash = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return dict(
        revision=revision,
        content_hash=content_hash,
        version=digest(
            dict(run_id=run_id, revision=revision, content_hash=content_hash)
        ),
    )


class PlanQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,40}$")
    text: str = Field(min_length=1, max_length=1000)
    kind: Literal["single", "multiple", "text"]
    options: list[str] = Field(max_length=8)

    @model_validator(mode="after")
    def displayable(self):
        if not self.text.strip() or any(
            not o.strip() or len(o) > 300 for o in self.options
        ):
            raise ValueError("Clarification text and options must be displayable")
        if len(set(self.options)) != len(self.options):
            raise ValueError("Clarification options must be unique")
        if (self.kind == "text" and self.options) or (
            self.kind != "text" and len(self.options) < 2
        ):
            raise ValueError(
                "Choice questions require at least two options; text questions have none"
            )
        return self


class PlanDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["ready", "ask_user"]
    questions: list[PlanQuestion] = Field(max_length=4)
    brief_md: str

    @model_validator(mode="after")
    def consistent(self):
        if self.action == "ask_user":
            if not self.questions or self.brief_md:
                raise ValueError("ask_user requires questions and an empty brief_md")
            if len({q.id for q in self.questions}) != len(self.questions):
                raise ValueError("Question IDs must be unique")
        else:
            if self.questions:
                raise ValueError("ready cannot contain pending questions")
            self.brief_md = validate_brief(self.brief_md, check_size=False)
        return self


def validate_answers(questions: list[dict], answers: dict, skip: bool) -> dict:
    if skip:
        if answers:
            raise ValueError("Skipped clarification cannot also contain answers")
        return {}
    known = {q["id"]: PlanQuestion.model_validate(q) for q in questions}
    if not answers or set(answers) - known.keys():
        raise ValueError("Answer at least one pending question or explicitly skip")
    for key, value in answers.items():
        question = known[key]
        if question.kind == "multiple":
            if (
                not isinstance(value, list)
                or not value
                or len(value) != len(set(value))
                or any(v not in question.options for v in value)
            ):
                raise ValueError("Invalid multiple-choice answer")
        elif not isinstance(value, str) or not value.strip() or len(value) > 4000:
            raise ValueError("Invalid text answer")
        elif question.kind == "single" and value not in question.options:
            raise ValueError("Invalid single-choice answer")
    return answers
