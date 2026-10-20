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
    query: str = Field(min_length=1)
    user_id: str = Field(min_length=1, max_length=256)
    top_k: int = Field(ge=1, le=100)
    options: list[str] | None = None


class SearchItem(StrictModel):
    id: str
    content: str
    score: float
    created_at: str


class SearchResponse(StrictModel):
    data: list[SearchItem]


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


class EvidenceRef(StrictModel):
    source_id: str
    quote: str = Field(min_length=1, max_length=200000)


RecordType = Literal['relation', 'event']
ExpressionMode = Literal['asserted', 'desired', 'requested', 'hypothetical', 'document']


class Mention(StrictModel):
    name: str = Field(min_length=1, max_length=200)


class Candidate(StrictModel):
    id: str = Field(min_length=1, max_length=100)
    content: str = Field(min_length=1, max_length=4000)
    mentions: list[Mention] = Field(max_length=12)
    evidence: list[EvidenceRef] = Field(min_length=1, max_length=8)


class ProposalPlan(StrictModel):
    items: list[Candidate] = Field(max_length=24)
    reason: str


class SourceCitation(StrictModel):
    source_id: str


class CandidateDraft(StrictModel):
    content: str = Field(min_length=1, max_length=4000)
    mentions: list[Mention] = Field(max_length=12)
    evidence: list[SourceCitation] = Field(min_length=1, max_length=8)


class ProposalDraft(StrictModel):
    items: list[CandidateDraft] = Field(max_length=24)
    reason: str


EventStatus = Literal['planned', 'occurred', 'cancelled', 'desired', 'unknown']


class CardDecision(StrictModel):
    """Canonical internal decision; the provider wire shapes expose each operation's own fields."""
    candidate_id: str
    operation: Literal['create', 'patch_event', 'keep', 'pending']
    target_id: str | None = None
    record_type: RecordType | None = None
    content: str | None = Field(default=None, max_length=4000)
    mode: ExpressionMode | None = None
    context: str | None = Field(default=None, max_length=300)
    status: EventStatus | None = None
    event_change: Literal['reschedule', 'complete', 'cancel'] | None = None
    time_expression: str | None = Field(default=None, max_length=300)
    reason: str = Field(max_length=600)

    _null_arguments = field_validator('target_id', 'time_expression', mode='before')(nullable)


class CreateDraft(StrictModel):
    candidate_id: str
    operation: Literal['create']
    record_type: RecordType
    content: str = Field(min_length=1, max_length=4000)
    mode: ExpressionMode
    context: str | None = Field(max_length=300)
    status: EventStatus | None
    time_expression: str | None = Field(max_length=300)
    reason: str = Field(max_length=600)


class EventPatchDraft(StrictModel):
    candidate_id: str
    operation: Literal['patch_event']
    target_id: str
    event_change: Literal['reschedule', 'complete', 'cancel']
    time_expression: str | None = Field(max_length=300)
    reason: str = Field(max_length=600)


class KeepDraft(StrictModel):
    candidate_id: str
    operation: Literal['keep']
    target_id: str
    reason: str = Field(max_length=600)


class PendingDraft(StrictModel):
    candidate_id: str
    operation: Literal['pending']
    reason: str = Field(max_length=600)


class MaintenanceDraft(StrictModel):
    decisions: list[CreateDraft | EventPatchDraft | KeepDraft | PendingDraft] = Field(max_length=24)


class Revision(StrictModel):
    id: str
    content: str
    source_ids: list[str]
    quote: str
    evidence: list[EvidenceRef]
    entity_ids: list[str]
    mode: ExpressionMode
    context: str
    status: Literal['stated', 'planned', 'occurred', 'cancelled', 'desired', 'unknown']
    planned_at: str | None
    event_at: str | None
    observed_at: str
    created_at: str
    time_resolution: Literal['rule', 'unknown']
    time_precision: Literal['day', 'instant', 'unknown']
    operation: Literal['create', 'reschedule', 'complete', 'cancel']
    replaces_revision_id: str | None = None


class Memory(StrictModel):
    schema_version: Literal[10] = 10
    id: str
    owner: str
    record_type: RecordType
    revisions: list[Revision] = Field(min_length=1)
    impact: Literal['high', 'low', 'unknown'] = 'unknown'
    pinned: bool = False
    last_accessed_at: str | None = None

    @model_validator(mode='after')
    def record_contract(self):
        if self.record_type == 'relation' and (len(self.revisions) != 1 or self.current.status != 'stated'):
            raise ValueError('relation_cards_are_append_only')
        if self.record_type == 'event' and any(r.status == 'stated' for r in self.revisions):
            raise ValueError('events_use_event_status')
        return self

    @property
    def current(self) -> Revision:
        from .clock import parse_time
        return max(self.revisions, key=lambda r: (parse_time(r.observed_at), parse_time(r.created_at), r.id))

    @property
    def tags(self):
        return ['event'] if self.record_type == 'event' else ['fact']

    @property
    def entity_ids(self):
        return list(self.current.entity_ids)


class AliasEvidence(StrictModel):
    name: str
    source_id: str
    observed_at: str


class GraphObject(StrictModel):
    schema_version: Literal[10] = 10
    id: str
    owner: str
    name: str
    kind: str = 'entity'
    is_self: bool = False
    aliases: list[AliasEvidence] = Field(default_factory=list)
    observed_at: str


class Edge(StrictModel):
    id: str
    owner: str
    origin: str
    target: str
    relation: Literal['mentions', 'supported_by', 'revises']
    revision_id: str
    memory_id: str
    source_id: str


class SourceWindow(StrictModel):
    source_id: str
    start: int = Field(ge=0)
    stop: int = Field(ge=0)


class MemoryEvidence(StrictModel):
    record_type: RecordType
    mode: ExpressionMode
    context: str
    structure: str
    memory_id: str
    revision_id: str
    content: str
    quote: str
    source_ids: list[str]
    event_at: str | None
    planned_at: str | None
    status: str
    time_precision: Literal["day", "instant", "unknown"] = "unknown"
    observed_at: str | None = None
    temporal_scope: str = "unknown"


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
    purpose: Literal["history", "scheduled"] = "history"
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
    action_time_expression: str | None = None
    steps: list[RecallStep] = Field(max_length=2)
    relevant_memory_ids: list[str] = Field(max_length=24)
