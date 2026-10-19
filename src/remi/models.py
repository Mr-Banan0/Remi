from __future__ import annotations

from typing import Literal

from pydantic import PrivateAttr, BaseModel, ConfigDict, Field, model_validator, field_validator


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


class EvidenceRef(StrictModel):
    source_id: str
    quote: str = Field(min_length=1, max_length=200000)


class Mention(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    kind: str = Field(min_length=1, max_length=60)


class Candidate(StrictModel):
    id: str
    content: str = Field(min_length=1, max_length=4000)
    evidence: list[EvidenceRef] = Field(min_length=1, max_length=8)
    entities: list[Mention] = Field(max_length=12)
    tags: list[MemoryKind] = Field(min_length=1, max_length=4)


class ProposalPlan(StrictModel):
    items: list[Candidate] = Field(max_length=24)
    reason: str


class EntityDecision(StrictModel):
    ref: str
    target_id: str | None
    name: str = Field(min_length=1, max_length=200)
    kind: str = Field(min_length=1, max_length=60)
    is_self: bool
    aliases: list[str] = Field(max_length=12)
    evidence: list[EvidenceRef] = Field(min_length=1, max_length=8)


    @field_validator("target_id", mode="before")
    @classmethod
    def optional_target(cls, value):
        return nullable(value)


class EntityRole(StrictModel):
    entity_ref: str
    role: str = Field(min_length=1, max_length=120)


class Assertion(StrictModel):
    subject_ref: str
    predicate: str = Field(min_length=1, max_length=160)
    object_ref: str
    negated: bool = False


class LiteralAttribute(StrictModel):
    subject_ref: str
    predicate: str
    value: str
    negated: bool = False


class MemoryLink(StrictModel):
    target_ref: str
    label: str = Field(min_length=1, max_length=160)


class BoundMemoryLink(MemoryLink):
    target_revision_id: str


class MemoryEdit(StrictModel):
    ref: str
    candidate_ids: list[str] = Field(min_length=1, max_length=24)
    operation: Literal["write", "keep", "retract"]
    target_id: str | None = Field(description="Select the same independently maintained claim or particular occurrence. Shared entity alone is insufficient. Use null for an independent claim, including a new ongoing state distinct from its establishing event.")
    content: str = Field(min_length=1, max_length=4000)
    change: Literal["confirm", "supplement", "replace"]
    tags: list[MemoryKind] = Field(min_length=1, max_length=4)
    evidence: list[EvidenceRef] = Field(min_length=1, max_length=12)
    roles: list[EntityRole] = Field(max_length=16)
    assertions: list[Assertion] = Field(max_length=16)
    links: list[MemoryLink] = Field(max_length=12)
    time_expression: str | None
    end_expression: str | None
    status: Literal["stated", "planned", "occurred", "cancelled", "desired", "unknown"]
    temporal_scope: Literal["ongoing", "episodic", "unknown"]
    corrects_occurrence: bool = Field(default=False, description="True only when the new source explicitly corrects the date/details of the same previously completed occurrence. A later independent occurrence uses false.")
    impact: Literal["high", "low", "unknown"]

    @field_validator("target_id", "time_expression", "end_expression", mode="before")
    @classmethod
    def optional_arguments(cls, value):
        return nullable(value)

    @model_validator(mode="after")
    def target_contract(self):
        if self.operation in {"keep", "retract"} and self.target_id is None:
            raise ValueError("keep_or_retract_selects_existing_history")
        return self


class DiscardedCandidate(StrictModel):
    candidate_id: str
    reason: str = Field(min_length=1)


class MaintenancePlan(StrictModel):
    _graph_dispositions: list[dict] = PrivateAttr(default_factory=list)
    _write_compositions: list[dict] = PrivateAttr(default_factory=list)
    entities: list[EntityDecision] = Field(max_length=40)
    edits: list[MemoryEdit] = Field(max_length=24)
    discarded: list[DiscardedCandidate] = Field(max_length=24)


class SourceCitation(StrictModel):
    source_id: str


class CandidateDraft(Candidate):
    evidence: list[SourceCitation] = Field(min_length=1, max_length=8)


class ProposalDraft(StrictModel):
    items: list[CandidateDraft] = Field(max_length=24)
    reason: str


class EntityDraft(EntityDecision):
    evidence: list[SourceCitation] = Field(min_length=1, max_length=8)


class MemoryEditDraft(MemoryEdit):
    evidence: list[SourceCitation] = Field(max_length=12)


class MaintenanceDraft(StrictModel):
    entities: list[EntityDraft] = Field(max_length=40)
    edits: list[MemoryEditDraft] = Field(max_length=24)
    discarded: list[DiscardedCandidate] = Field(max_length=24)


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
    tags: list[MemoryKind] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    roles: list[EntityRole] = Field(default_factory=list)
    assertions: list[Assertion] = Field(default_factory=list)
    attributes: list[LiteralAttribute] = Field(default_factory=list)
    links: list[BoundMemoryLink] = Field(default_factory=list)
    temporal_scope: Literal["ongoing", "episodic", "unknown"] = "unknown"
    replaces_revision_id: str | None = None
    change: str = "new"


class Memory(StrictModel):
    id: str
    owner: str
    revisions: list[Revision]
    impact: Literal["high", "low", "unknown"]
    pinned: bool = False
    last_accessed_at: str | None = None

    @property
    def current(self) -> Revision:
        from .clock import parse_time
        return max(self.revisions, key=lambda revision: (parse_time(revision.observed_at), parse_time(revision.created_at), revision.id))

    @property
    def tags(self) -> list[MemoryKind]:
        return self.current.tags

    @property
    def object_ids(self) -> list[str]:
        return list(dict.fromkeys(role.entity_ref for role in self.current.roles))


class AliasEvidence(StrictModel):
    name: str
    source_id: str
    observed_at: str


class GraphObject(StrictModel):
    id: str
    owner: str
    kind: str
    name: str
    is_self: bool = False
    aliases: list[AliasEvidence] = Field(default_factory=list)
    observed_at: str


class Edge(StrictModel):
    id: str
    owner: str
    origin: str
    target: str
    relation: Literal["involves", "assertion", "related", "supported_by", "revises"]
    label: str = ""
    revision_id: str
    memory_id: str
    source_id: str
    target_revision_id: str | None = None


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
    valid_from: str | None = None
    valid_to: str | None = None
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
