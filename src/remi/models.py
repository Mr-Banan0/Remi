from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator, field_validator


def nullable(value):
    """Canonicalize empty model arguments before source/identity/date validation."""
    return None if isinstance(value, str) and value.strip().casefold() in {"", "null", "none"} else value


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AddMessage(StrictModel):
    role: Literal["user", "assistant", "system"]
    content: str = Field(min_length=1, max_length=200000)
    timestamp: int | None = Field(default=None, strict=True, ge=-62135596800000, le=253402300799999)


class AddRequest(StrictModel):
    request_id: str = Field(min_length=1, max_length=256)
    user_id: str = Field(min_length=1, max_length=256)
    session_id: str = Field(min_length=1, max_length=256)
    messages: list[AddMessage] = Field(min_length=1, max_length=1000)


class AddResponse(StrictModel):
    success: Literal[True] = True
    request_id: str
    user_id: str
    session_id: str


class SearchRequest(StrictModel):
    query: str = Field(min_length=1, max_length=20000)
    user_id: str = Field(min_length=1, max_length=256)
    top_k: int = Field(ge=1, le=50)
    options: list[str] | None = None


class SearchItem(StrictModel):
    id: str
    content: str
    score: float
    created_at: str


class SearchResponse(StrictModel):
    data: list[SearchItem]


MemoryKind = Literal["preference", "fact", "event", "experience"]


class Source(StrictModel):
    id: str
    owner: str
    session_id: str
    request_id: str
    ordinal: int
    role: str
    content: str
    message_at: str | None
    created_at: str


class ObjectRef(StrictModel):
    kind: Literal["user", "person", "project", "client", "document", "topic"]
    name: str = Field(min_length=1, max_length=200)


class Command(StrictModel):
    operation: Literal["create", "update", "retract"]
    target_id: str | None
    kind: MemoryKind
    content: str = Field(min_length=1, max_length=4000)
    source_id: str
    context_source_ids: list[str] = Field(max_length=3)
    quote: str = Field(min_length=1, max_length=200000)
    objects: list[ObjectRef] = Field(max_length=8)
    time_expression: str | None
    end_expression: str | None
    status: Literal["stated", "planned", "occurred", "cancelled"]
    impact: Literal["high", "low", "unknown"]

    @field_validator("target_id", "time_expression", "end_expression", mode="before")
    @classmethod
    def optional_arguments(cls, value):
        return nullable(value)

    @model_validator(mode="after")
    def target_contract(self):
        if self.operation == "create" and self.target_id is not None:
            raise ValueError("create_target_must_be_null")
        if self.operation != "create" and not self.target_id:
            raise ValueError("update_or_retract_requires_target")
        if self.kind != "event" and self.status in {"planned", "cancelled"}:
            raise ValueError("plans_and_cancellations_use_event_kind")
        return self


class ContentItem(StrictModel):
    """Temporary processing text; provenance and source clocks belong to the program."""
    content: str = Field(min_length=1, max_length=200000)

    @field_validator("content")
    @classmethod
    def meaningful_content(cls, value):
        if not value.strip():
            raise ValueError("content_requires_text")
        return value


class SplitPlan(StrictModel):
    items: list[ContentItem] = Field(min_length=2, max_length=24)


class WrittenItem(StrictModel):
    item_id: str
    content: str = Field(min_length=1, max_length=4000)
    objects: list[ObjectRef] = Field(max_length=8)
    time_expression: str | None
    end_expression: str | None
    status: Literal["stated", "planned", "occurred", "cancelled"]
    impact: Literal["high", "low", "unknown"]

    @field_validator("time_expression", "end_expression", mode="before")
    @classmethod
    def optional_arguments(cls, value):
        return nullable(value)

    @field_validator("content")
    @classmethod
    def meaningful_content(cls, value):
        return ContentItem.meaningful_content(value)


class WrittenPlan(StrictModel):
    items: list[WrittenItem] = Field(max_length=24)


class Revision(StrictModel):
    id: str
    content: str
    source_ids: list[str]
    quote: str
    event_at: str | None
    valid_from: str | None
    valid_to: str | None
    observed_at: str
    created_at: str
    status: str
    time_resolution: Literal["rule", "unknown"]
    time_precision: Literal["day", "instant", "unknown"] = "unknown"
    operation: str


class Memory(StrictModel):
    id: str
    owner: str
    kind: MemoryKind
    object_ids: list[str]
    revisions: list[Revision]
    impact: Literal["high", "low", "unknown"]
    pinned: bool = False
    last_accessed_at: str | None = None

    @property
    def current(self) -> Revision:
        from .clock import parse_time
        return max(self.revisions, key=lambda revision: (parse_time(revision.observed_at), parse_time(revision.created_at), revision.id))


class GraphObject(StrictModel):
    id: str
    owner: str
    kind: str
    name: str


class Edge(StrictModel):
    id: str
    owner: str
    origin: str
    target: str
    relation: Literal["about", "supported_by", "revises"]
    source_id: str


class SourceWindow(StrictModel):
    source_id: str
    start: int = Field(ge=0)
    stop: int = Field(ge=0)


class MemoryEvidence(StrictModel):
    memory_id: str
    revision_id: str
    content: str
    quote: str
    source_ids: list[str]
    event_at: str | None
    status: str
    time_precision: Literal["day", "instant", "unknown"] = "unknown"


class Document(StrictModel):
    id: str
    owner: str
    text: str
    source_ids: list[str]
    memory_id: str | None
    revision_id: str | None
    message_at: str | None
    event_at: str | None
    created_at: str
    vector: list[float] | None = None
    source_windows: list[SourceWindow] = Field(default_factory=list)
    memory_evidence: list[MemoryEvidence] = Field(default_factory=list)


class RecallStep(StrictModel):
    query: str = Field(min_length=1, max_length=2000)
    time_expression: str | None
    anchor_source_id: str | None
    time_basis: Literal["message", "event"]
    relation: Literal["independent", "same_day", "before", "after"]
    select: Literal["all", "first", "latest"]
    window_days: int = Field(ge=0, le=3)
    anchor_step: int | None = Field(default=None, ge=0, le=0, description=(
        "Step 1 always uses null. Step 2 may use 0 to bind step 1's time; "
        "then anchor_source_id and time_expression both use null."))

    @field_validator("time_expression", "anchor_source_id", "anchor_step", mode="before")
    @classmethod
    def optional_arguments(cls, value):
        return nullable(value)

    @model_validator(mode="after")
    def anchor_contract(self):
        if self.anchor_step is not None and (self.anchor_source_id or self.time_expression):
            raise ValueError("step_anchor_uses_prior_result_time")
        return self


class Decision(StrictModel):
    steps: list[RecallStep] = Field(max_length=2)
    relevant_memory_ids: list[str] = Field(max_length=24)
