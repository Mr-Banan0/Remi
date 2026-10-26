from __future__ import annotations

from typing import Literal, Annotated

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


SCHEMA_VERSION = 13
MemoryType = Literal['fact', 'preference', 'experience', 'event']
RecordType = Literal['relation', 'fact', 'preference', 'experience', 'event']
SubjectKind = Literal['person', 'animal', 'organization']
ExpressionMode = Literal['asserted', 'desired', 'requested', 'hypothetical', 'document']
EventStatus = Literal['planned', 'occurred', 'cancelled', 'desired', 'unknown']
SubjectRef = Annotated[int, Field(strict=True, ge=0, le=95)]
class SourceCitation(StrictModel):
    source_id: str


class Subject(StrictModel):
    kind: SubjectKind
    ref: int = Field(ge=0, le=95)
    name: str
    is_user: bool
    aliases: list[str]
    evidence: list[EvidenceRef]


class ConnectionDraft(StrictModel):
    subject_refs: list[int] = Field(min_length=2, max_length=2)
    source_id: str
    quote: str = Field(min_length=1, max_length=2000)
    mode: ExpressionMode


class CandidateDraft(StrictModel):
    content: str = Field(min_length=1, max_length=4000)
    record_type: MemoryType
    subject_refs: list[SubjectRef] = Field(max_length=12)
    mode: ExpressionMode
    context: str | None = Field(max_length=300)
    status: EventStatus | None
    time_expression: str | None = Field(max_length=300)
    evidence: list[SourceCitation] = Field(min_length=1, max_length=8)


class Candidate(StrictModel):
    id: str
    content: str
    subject_refs: list[int]
    connection: ConnectionDraft | None
    record_type: MemoryType | None = None
    mode: ExpressionMode | None = None
    context: str | None = None
    status: EventStatus | None = None
    time_expression: str | None = None
    evidence: list[EvidenceRef]


class ProposalDraft(StrictModel):
    items: list[CandidateDraft] = Field(max_length=24)
    reason: str


class ProposalPlan(StrictModel):
    subjects: list[Subject]
    items: list[Candidate]
    reason: str


class SubjectNameDraft(SourceCitation):
    name: str = Field(min_length=1, max_length=200)


class SubjectNameEvidence(EvidenceRef):
    name: str = Field(min_length=1, max_length=200)


class SubjectDecision(StrictModel):
    subject_ref: int
    target_id: str | None
    alias_additions: list[SubjectNameEvidence] = Field(default_factory=list, max_length=24)
    kind: SubjectKind | None = None
    reason: str = Field(max_length=600)


class SubjectDraft(StrictModel):
    ref: SubjectRef
    operation: Literal['create', 'reuse', 'pending']
    target_id: str | None
    name: str = Field(min_length=1, max_length=200)
    kind: SubjectKind
    aliases: list[SubjectNameDraft] = Field(max_length=24)
    evidence: list[SourceCitation] = Field(min_length=1, max_length=8)


class ConnectionChangeDraft(StrictModel):
    ref: SubjectRef
    operation: Literal['create', 'update', 'keep', 'pending']
    target_id: str | None
    subject_refs: list[SubjectRef] = Field(min_length=2, max_length=2)
    source_id: str
    quote: str = Field(min_length=1, max_length=2000)
    mode: ExpressionMode
    previous_quote: str | None = Field(max_length=4000)


class SubjectMaintenanceDraft(StrictModel):
    subjects: list[SubjectDraft] = Field(max_length=96)
    connections: list[ConnectionChangeDraft] = Field(max_length=48)


class CardDecision(StrictModel):
    """Program write plan; relationship operations come from the subject writer."""
    candidate_id: str
    operation: Literal['create', 'patch_relation', 'keep', 'pending']
    target_id: str | None = None
    record_type: RecordType | None = None
    content: str | None = Field(default=None, max_length=4000)
    subject_refs: list[int] | None = Field(default=None,max_length=12)
    mode: ExpressionMode | None = None
    context: str | None = Field(default=None, max_length=300)
    status: EventStatus | None = None
    time_expression: str | None = Field(default=None, max_length=300)
    reason: str = Field(max_length=600)

    _null_arguments = field_validator('target_id', 'time_expression', mode='before')(nullable)


class EventTimeEvidence(StrictModel):
    """Source expression and anchor for an event date, including unresolved dates."""
    field: Literal['planned_at', 'event_at']
    expression: str | None
    source_id: str
    anchor: str | None
    value: str | None
    precision: Literal['day', 'instant', 'unknown']


class Revision(StrictModel):
    id: str
    content: str
    source_ids: list[str]
    quote: str
    evidence: list[EvidenceRef]
    entity_ids: list[str]
    user_involvement: Literal['involved', 'not_involved']
    mode: ExpressionMode
    context: str
    status: Literal['stated', 'planned', 'occurred', 'cancelled', 'desired', 'unknown']
    planned_at: str | None
    event_at: str | None
    observed_at: str
    created_at: str
    time_resolution: Literal['rule', 'unknown']
    time_precision: Literal['day', 'instant', 'unknown']
    operation: Literal['create', 'reschedule', 'complete', 'cancel', 'update']
    replaces_revision_id: str | None = None
    time_evidence: list[EventTimeEvidence] = Field(default_factory=list)
    maintenance_patch: dict | None = None


class Memory(StrictModel):
    schema_version: Literal[13] = SCHEMA_VERSION
    id: str
    owner: str
    record_type: RecordType
    endpoint_ids: list[str] = Field(default_factory=list, max_length=2)
    connection_evidence: EvidenceRef | None = None
    revisions: list[Revision] = Field(min_length=1)
    impact: Literal['high', 'low', 'unknown'] = 'unknown'
    pinned: bool = False
    last_accessed_at: str | None = None

    @model_validator(mode='after')
    def record_contract(self):
        from .identity import stable_id
        user_id = stable_id('obj_', self.owner, 'user')
        if any((r.user_involvement == 'involved') != (user_id in r.entity_ids) for r in self.revisions):
            raise ValueError('revision_user_binding_matches_involvement')
        if self.record_type == 'relation':
            if (len(set(self.endpoint_ids)) != 2
                    or self.connection_evidence is None
                    or not set(self.endpoint_ids) <= set(self.current.entity_ids)
                    or self.connection_evidence.source_id not in self.current.source_ids):
                raise ValueError('connection_requires_two_distinct_supported_subjects')
        elif self.endpoint_ids or self.connection_evidence is not None:
            raise ValueError('ordinary_memories_use_participants_and_source_evidence')
        if self.record_type != 'event' and any(r.status != 'stated' for r in self.revisions):
            raise ValueError('statements_use_stated_status')
        if self.record_type == 'event' and any(r.status == 'stated' for r in self.revisions):
            raise ValueError('events_use_event_status')
        return self

    @property
    def current(self) -> Revision:
        from .clock import parse_time
        return max(self.revisions, key=lambda r: (parse_time(r.observed_at), parse_time(r.created_at), r.id))

    @property
    def tags(self):
        return [self.record_type]

    @property
    def entity_ids(self):
        return list(self.current.entity_ids)


class AliasEvidence(StrictModel):
    name: str
    source_id: str
    quote: str = Field(min_length=1)
    observed_at: str


class GraphObject(StrictModel):
    schema_version: Literal[13] = SCHEMA_VERSION
    id: str
    owner: str
    name: str
    kind: SubjectKind
    is_user: bool = False
    aliases: list[AliasEvidence] = Field(default_factory=list)
    observed_at: str

    @model_validator(mode='after')
    def user_contract(self):
        from .identity import stable_id
        canonical = self.id == stable_id('obj_', self.owner, 'user')
        if canonical != self.is_user or (self.is_user and (self.name != 'user' or self.kind != 'person')):
            raise ValueError('user_entity_uses_canonical_owner_identity')
        return self


class Edge(StrictModel):
    id: str
    owner: str
    origin: str
    target: str
    relation: Literal['mentions', 'supported_by', 'revises', 'related']
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
