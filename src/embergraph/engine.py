"""One Add path and one Search path, with record-scoped commits and bounded inference."""
from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from threading import Lock, RLock
from time import monotonic

from pydantic import ValidationError

from .clock import absolute_content, in_view, message_time, parse_time, resolve_time, utcnow
from .heat import hot_view, visible_memory
from .models import (AddResponse, Command, Decision, Document, Edge, GraphObject, Memory, Revision, RecallStep,
                     SearchItem, SearchResponse, Source, SourceWindow, MemoryEvidence)
from .providers import OutputInvalid, ProviderUnavailable, RankResult
from .retrieval import clip_bytes, hybrid, tokenize, validate_vector
from .settings import Settings
from .storage import MemoryStore
from .add_pipeline import AddPipeline


class DuplicateRequestConflict(ValueError):
    pass


def stable_id(prefix, *parts):
    return prefix + hashlib.sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]


def source_visible(source, as_of):
    return parse_time(source.message_at or source.created_at) <= parse_time(as_of)


def source_quote(text, quote):
    """Copy the unique source span after whitespace and typographic-quote equivalence."""
    if quote in text:
        return quote
    table = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"'})
    pattern = r"\s+".join(re.escape(part) for part in quote.translate(table).split())
    if not pattern:
        return None
    matches = list(re.finditer(pattern, text.translate(table)))
    if len(matches) == 1:
        return text[matches[0].start():matches[0].end()]
    return None


def source_excerpt(text, query, budget, windows=()):
    """Select a contiguous original span around a retrieved window or a lexical hit."""
    if len(text.encode()) <= budget:
        return text
    terms = set(tokenize(query))
    spans = [(max(0, start), min(len(text), stop)) for start, stop in windows if stop > start]
    if spans:
        start, stop = max(spans, key=lambda span: len(terms & set(tokenize(text[span[0]:span[1]]))))
    else:
        start, stop = 0, len(text)
    segment = text[start:stop]
    matches = [match.start() for term in terms for match in re.finditer(re.escape(term), segment, re.IGNORECASE)]
    focus = start + min(matches) if matches else start
    start = max(start, focus - max(0, budget // 5))
    return clip_bytes(text[start:stop], budget)


class MemoryEngine:
    def __init__(self, store=None, model=None, embedder=None, reranker=None, settings=None, judger=None):
        self.store = store or MemoryStore()
        self.model, self.embedder, self.reranker = model, embedder, reranker
        self.judger = judger if judger is not None else getattr(model, "judger", None)
        self.settings = settings or Settings()
        self._locks, self._lock_guard = {}, Lock()
        self.last_search = {}

    @contextmanager
    def _scope(self, owner, deadline):
        with self._lock_guard:
            lock = self._locks.setdefault(owner, RLock())
        if not lock.acquire(timeout=max(0, deadline - monotonic())):
            raise ProviderUnavailable("owner_processing_busy")
        try:
            yield
        finally:
            lock.release()

    def add(self, request):
        deadline = monotonic() + self.settings.add_timeout
        with self._scope(request.user_id, deadline):
            payload = request.model_dump()
            fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            progress = self.store.get("requests", request.user_id, request.request_id)
            if progress and progress["fingerprint"] != fingerprint:
                raise DuplicateRequestConflict("request_id_payload_conflict")
            if progress and progress["status"] == "complete":
                return AddResponse(request_id=request.request_id, user_id=request.user_id, session_id=request.session_id)
            if not progress:
                created = utcnow()
                sources = [Source(id=stable_id("src_", request.user_id, request.request_id, index), owner=request.user_id,
                    session_id=request.session_id, request_id=request.request_id, ordinal=index,
                    role=message.role, content=message.content, message_at=message_time(message.timestamp), created_at=created)
                    for index, message in enumerate(request.messages)]
                progress = {"fingerprint": fingerprint, "status": "pending", "source_ids": [source.id for source in sources],
                            "units": {}, "created_at": created}
                with self.store.transaction():
                    for source in sources:
                        self.store.put("sources", source.owner, source.id, source.model_dump())
                    self.store.put("requests", request.user_id, request.request_id, progress)
            sources = self.store.sources(request.user_id, progress["source_ids"])
            units = self._units(sources)
            for number, unit in enumerate(units):
                if monotonic() >= deadline:
                    raise ProviderUnavailable("add_deadline")
                key = str(number)
                saved = progress["units"].get(key)
                if not saved:
                    source = next((item for item in unit if item.role == "user"), None)
                    if source and self.model:
                        state = self._add_state(source, unit, sources, deadline)
                        cache = progress.setdefault("add_stages", {}).setdefault(key, {})
                        commands, rejected, validation = self._commands(state, deadline, cache,
                            lambda: self.store.put("requests", request.user_id, request.request_id, progress))
                    else:
                        commands, rejected, validation = [], [], []
                    saved = {"phase": "planned", "commands": [command.model_dump() for command in commands],
                             "rejected": rejected, "memory_ids": [],
                             "validation": validation}
                    progress["units"][key] = saved
                    self.store.put("requests", request.user_id, request.request_id, progress)
                if saved["phase"] == "planned":
                    source_map = {source.id: source for source in sources}
                    for command in saved["commands"]:
                        try:
                            memory_id = self._apply(request.user_id, Command.model_validate(command), source_map)
                        except ValueError as error:
                            saved["rejected"].append({"position": -1, "error": str(error)[:200]})
                            continue
                        if memory_id not in saved["memory_ids"]:
                            saved["memory_ids"].append(memory_id)
                    saved["phase"] = "applied"
                    self.store.put("requests", request.user_id, request.request_id, progress)
                if saved["phase"] == "applied":
                    documents = self._documents(unit, self.store.memories(request.user_id, saved["memory_ids"]))
                    self._index(documents, deadline)
                    saved["phase"] = "indexed"
                    self.store.put("requests", request.user_id, request.request_id, progress)
            progress["status"] = "complete"
            progress["completed_at"] = utcnow()
            self.store.put("requests", request.user_id, request.request_id, progress)
        return AddResponse(request_id=request.request_id, user_id=request.user_id, session_id=request.session_id)

    @staticmethod
    def _units(sources):
        units = []
        for source in sources:
            if source.role == "user" or not units:
                units.append([])
            units[-1].append(source)
        return units

    def _add_state(self, source, unit, sources, deadline):
        previous = self.store.context_before(source.owner, source.session_id, source, limit=2)
        context_map = {item.id: item for item in [*previous, *unit]
                       if item.id != source.id and item.role in {"user", "assistant"}}
        context = list(context_map.values())[:3]
        return {"owner": source.owner,
                "new_user_source": {"id": source.id, "role": source.role,
                    "content": source.content, "message_at": source.message_at,
                    "created_at": source.created_at},
                "context_sources": [{"id": item.id, "role": item.role,
                    "content": clip_bytes(item.content, 4000), "message_at": item.message_at} for item in context],
                "candidates": []}

    def _typed_candidates(self, state, item, kind, deadline):
        owner = state["owner"]
        as_of = state["new_user_source"]["message_at"] or state["new_user_source"]["created_at"]
        memories = {memory.id: memory for original in self.store.memories(owner)
                    if original.kind == kind and (memory := visible_memory(original, as_of)) is not None}
        documents = [document for document in self._visible_documents(owner, as_of)
                     if document.memory_id in memories]
        if not documents:
            return []
        vector = self._embed([item.content], deadline)[0] if self.embedder else None
        hits = self._hybrid(owner, item.content, documents, vector, limit=len(documents))
        identifiers = list(dict.fromkeys(document.memory_id for document, _ in hits))[:4]
        candidates = []
        for key in identifiers:
            memory = memories[key]
            evidence = []
            for source in self.store.sources(owner, memory.current.source_ids):
                position = source.content.find(memory.current.quote)
                windows = [(position, position + len(memory.current.quote))] if position >= 0 else []
                evidence.append({"id": source.id, "role": source.role,
                    "content": source_excerpt(source.content, item.content, 1800, windows), "message_at": source.message_at})
            candidates.append({"handle": f"c{len(candidates)+1}", "id": memory.id, "kind": memory.kind,
                "content": memory.current.content, "quote": clip_bytes(memory.current.quote, 1800),
                "source_ids": memory.current.source_ids, "sources": evidence,
                "event_at": memory.current.event_at, "status": memory.current.status,
                "valid_from": memory.current.valid_from, "valid_to": memory.current.valid_to})
        return candidates

    def _validate_command(self, raw, state):
        command = Command.model_validate(raw)
        primary = state["new_user_source"]
        if command.source_id != primary["id"]:
            raise ValueError("new_user_source_id_required")
        quote = source_quote(primary["content"], command.quote)
        if quote is None:
            raise ValueError("new_user_source_quote_required")
        command = Command.model_validate({**command.model_dump(), "quote": quote})
        available_context = {item["id"] for item in state["context_sources"]}
        if set(command.context_source_ids) - available_context:
            raise ValueError("context_source_unknown")
        target = next((item for item in state["candidates"] if item["id"] == command.target_id), None)
        if command.operation != "create" and (target is None or target["kind"] != command.kind):
            raise ValueError("same_kind_recalled_target_required")
        for obj in command.objects:
            if obj.kind == "user":
                if obj.name != "self":
                    raise ValueError("user_object_name_is_self")
            elif obj.name not in primary["content"] and not any(obj.name in item["content"] for item in state["context_sources"]):
                raise ValueError("object_name_source_required")
        # Per-item text assigns temporal meaning; the original separately establishes the date evidence.
        time_text = state.get("item_content", command.quote)
        if command.time_expression and (resolve_time(command.time_expression, time_text, primary["message_at"]) is None
                or resolve_time(command.time_expression, primary["content"], primary["message_at"]) is None):
            raise ValueError("time_unresolved_choose_supported_expression_or_null")
        if command.end_expression and (resolve_time(command.end_expression, time_text, primary["message_at"]) is None
                or resolve_time(command.end_expression, primary["content"], primary["message_at"]) is None):
            raise ValueError("end_time_unresolved_choose_supported_expression_or_null")
        begin = resolve_time(command.time_expression, command.quote, primary["message_at"])
        end = resolve_time(command.end_expression, command.quote, primary["message_at"])
        if command.kind in {"fact", "preference"}:
            begin = begin or (target.get("valid_from") if target else primary["message_at"])
            end = end or (target.get("valid_to") if target else None)
        if begin and end and parse_time(begin) >= parse_time(end):
            raise ValueError("validity_interval_inverted")
        return command

    def _commands(self, state, deadline, cache=None, save_checkpoint=None):
        return AddPipeline(self).run(state, deadline, cache, save_checkpoint)

    def _apply(self, owner, command, source_map):
        source = source_map[command.source_id]
        resolved = resolve_time(command.time_expression, command.quote, source.message_at)
        end = resolve_time(command.end_expression, command.quote, source.message_at)
        identity = stable_id("mem_", owner, source.id, command.kind, command.content, command.quote, resolved,
                             [obj.model_dump() for obj in command.objects])
        memory_id = command.target_id or identity
        original = self.store.get("memories", owner, memory_id)
        memory = Memory.model_validate(original) if original else None
        revision_id = stable_id("rev_", owner, memory_id, command.model_dump())
        if memory and any(revision.id == revision_id for revision in memory.revisions):
            return memory_id
        if command.operation != "create" and memory is None:
            raise ValueError("target_disappeared")
        content = absolute_content(command.content, command.quote, source.message_at)
        objects = [GraphObject(id=stable_id("obj_", owner, obj.kind, obj.name), owner=owner, kind=obj.kind, name=obj.name)
                   for obj in command.objects]
        source_ids = list(dict.fromkeys([source.id, *command.context_source_ids]))
        prior_view = visible_memory(memory, source.message_at or source.created_at) if memory else None
        previous = prior_view.current if prior_view else None
        event_at = resolved if command.kind == "event" else None
        if previous and command.kind == "event" and not command.time_expression and command.status == previous.status:
            event_at = previous.event_at
        valid_from = (resolved or source.message_at) if command.kind in {"preference", "fact"} else None
        if previous and command.kind in {"preference", "fact"} and not command.time_expression:
            valid_from = previous.valid_from
        valid_to = previous.valid_to if previous and not command.end_expression else end
        if valid_from and valid_to and parse_time(valid_from) >= parse_time(valid_to):
            raise ValueError("resolved_validity_interval_inverted")
        revision = Revision(id=revision_id, content=content, source_ids=source_ids, quote=command.quote,
            event_at=event_at, valid_from=valid_from,
            valid_to=valid_to, observed_at=source.message_at or source.created_at, created_at=utcnow(),
            status="retracted" if command.operation == "retract" else command.status,
            time_resolution="rule" if event_at or valid_from else "unknown",
            time_precision=("instant" if "T" in command.time_expression and command.time_expression in command.quote else "day")
                if resolved and command.time_expression else previous.time_precision
                if previous and (event_at or valid_from) else "instant" if valid_from else "unknown", operation=command.operation)
        edges = [Edge(id=stable_id("edge_", revision_id, key, "supported_by"), owner=owner, origin=revision_id,
                      target=key, relation="supported_by", source_id=source.id) for key in source_ids]
        if memory:
            if command.operation == "create":
                return memory.id
            previous = previous or memory.current
            edges.append(Edge(id=stable_id("edge_", revision_id, previous.id, "revises"), owner=owner,
                              origin=revision_id, target=previous.id, relation="revises", source_id=source.id))
            memory.revisions.append(revision)
            memory.object_ids = list(dict.fromkeys([*memory.object_ids, *(obj.id for obj in objects)]))
            memory.impact = command.impact
        else:
            memory = Memory(id=memory_id, owner=owner, kind=command.kind, object_ids=[obj.id for obj in objects],
                            revisions=[revision], impact=command.impact)
        edges.extend(Edge(id=stable_id("edge_", revision_id, obj.id, "about"), owner=owner, origin=memory_id,
                          target=obj.id, relation="about", source_id=source.id) for obj in objects)
        self.store.save_memory(memory, objects, edges)
        return memory_id

    def _documents(self, unit, memories):
        documents = []
        if unit:
            combined = "\n".join(source.role + ": " + source.content for source in unit)
            locations, cursor = [], 0
            for source in unit:
                begin = cursor + len(source.role) + 2
                locations.append((source.id, begin, begin + len(source.content)))
                cursor = begin + len(source.content) + 1
            # Retrieval windows share the original whole-message IDs; persisted sources stay complete.
            for offset in range(0, len(combined), 2000):
                documents.append(Document(id=stable_id("doc_", unit[0].id, offset), owner=unit[0].owner,
                    text=combined[offset:offset + 2240], source_ids=[source.id for source in unit], memory_id=None,
                    revision_id=None, message_at=unit[0].message_at, event_at=None, created_at=unit[0].created_at,
                    source_windows=[SourceWindow(source_id=key, start=max(offset, begin) - begin,
                        stop=min(offset + 2240, stop) - begin) for key, begin, stop in locations
                        if max(offset, begin) < min(offset + 2240, stop)]))
        for memory in memories:
            for revision in memory.revisions:
                sources = self.store.sources(memory.owner, revision.source_ids[:1])
                position = sources[0].content.find(revision.quote) if sources else -1
                documents.append(Document(id=stable_id("doc_", revision.id), owner=memory.owner,
                    text=revision.content + "\nSource quote: " + revision.quote, source_ids=revision.source_ids,
                    memory_id=memory.id, revision_id=revision.id, message_at=revision.observed_at,
                    event_at=revision.event_at, created_at=revision.created_at,
                    source_windows=[SourceWindow(source_id=revision.source_ids[0], start=position,
                        stop=position + len(revision.quote))] if position >= 0 else [],
                    memory_evidence=[MemoryEvidence(memory_id=memory.id, revision_id=revision.id,
                        content=revision.content, quote=revision.quote, source_ids=revision.source_ids,
                        event_at=revision.event_at, status=revision.status, time_precision=revision.time_precision)]))
        return documents

    def _embed(self, texts, deadline):
        vectors = self.embedder.embed(texts, deadline=deadline)
        if len(vectors) != len(texts):
            raise ProviderUnavailable("embedding_count_invalid")
        dimensions = None
        for vector in vectors:
            try:
                validate_vector(vector, dimensions)
            except ValueError as error:
                raise ProviderUnavailable("embedding_vector_invalid") from error
            dimensions = len(vector)
        return vectors

    def _index(self, documents, deadline):
        pending = []
        for document in documents:
            previous = self.store.get("search_documents", document.owner, document.id)
            if previous and previous["text"] == document.text and (previous.get("vector") is not None or not self.embedder):
                continue
            self.store.put("search_documents", document.owner, document.id, document.model_dump())
            pending.append(document)
        if pending and self.embedder:
            vectors = self._embed([document.text for document in pending], deadline)
            for document, vector in zip(pending, vectors):
                document.vector = vector
                self.store.put("search_documents", document.owner, document.id, document.model_dump())

    def _visible_documents(self, owner, as_of):
        documents = []
        for document in self.store.documents(owner):
            if parse_time(document.message_at or document.created_at) <= parse_time(as_of):
                sources = self.store.sources(owner, document.source_ids)
                if all(source_visible(source, as_of) for source in sources):
                    documents.append(document)
        return documents

    def _hybrid(self, owner, query, documents, vector=None, limit=40):
        semantic = self.store.vector_scores(owner, vector, [document.id for document in documents]) if vector is not None else None
        return hybrid(query, documents, vector, limit=limit, semantic_scores=semantic)

    def _graph_candidates(self, owner, query, seed_documents, documents, vector):
        ids = {document.memory_id for document in seed_documents if document.memory_id}
        # Raw hits link back to memories through provenance, even when the memory document missed the base top N.
        sources = {key for document in seed_documents for key in document.source_ids}
        visible_sources = {key for document in documents for key in document.source_ids}
        support = [edge for edge in self.store.edges_for(owner, targets=sources, limit=200) if edge.source_id in visible_sources]
        revisions = {edge.origin for edge in support if edge.relation == "supported_by"}
        ids.update(document.memory_id for document in documents if document.revision_id in revisions and document.memory_id)
        object_ids = {edge.target for edge in self.store.edges_for(owner, origins=ids, limit=200)
                      if edge.relation == "about" and edge.source_id in visible_sources}
        # Broad shared-self edges provide no useful bounded expansion. Project/person/document edges remain eligible.
        self_objects = {stable_id("obj_", owner, "user", "self")}
        object_ids -= self_objects
        neighbors = {edge.origin for edge in self.store.edges_for(owner, targets=object_ids, limit=200)
                     if edge.relation == "about" and edge.source_id in visible_sources}
        pool = [document for document in documents if document.memory_id in neighbors or document.revision_id in revisions]
        return self._hybrid(owner, query, pool, vector, limit=self.settings.graph_limit)

    def _card(self, owner, document, query, budget):
        sources = self.store.sources(owner, document.source_ids)
        sources = sorted(sources, key=lambda source: (source.message_at or source.created_at, source.ordinal))
        headers = [f"[{source.id}] {source.role} at {source.message_at or 'unknown'}:\n" for source in sources]
        overhead = sum(len(header.encode()) + 2 for header in headers)
        allowance = max(0, (budget - overhead) // max(1, len(sources)))
        blocks = []
        for source, header in zip(sources, headers):
            windows = [(span.start, span.stop) for span in document.source_windows if span.source_id == source.id]
            excerpt = source_excerpt(source.content, query, allowance, windows)
            if excerpt:
                blocks.append({"source_id": source.id, "role": source.role, "message_at": source.message_at,
                               "text": excerpt, "rendered": header + excerpt})
        raw = "\n\n".join(block["rendered"] for block in blocks)
        return {"id": document.id, "source_ids": document.source_ids, "memory_id": document.memory_id,
                "event_at": document.event_at, "message_at": document.message_at,
                "original": raw, "summary": clip_bytes(document.text, 400),
                "memories": [{"id": memory.memory_id, "revision_id": memory.revision_id,
                    "content": clip_bytes(memory.content, 300), "event_at": memory.event_at, "status": memory.status,
                    "time_precision": memory.time_precision}
                    for memory in document.memory_evidence]}

    def _decision_state(self, owner, query, hits, current, as_of):
        state = {"query": clip_bytes(query, min(12000, self.settings.decision_bytes // 3)),
                 "reference_time": as_of, "evidence": [], "current_conditions": []}
        for memory in current:
            entry = {"id": memory.id, "kind": memory.kind, "content": clip_bytes(memory.current.content, 800),
                     "valid_from": memory.current.valid_from, "valid_to": memory.current.valid_to,
                     "observed_at": memory.current.observed_at, "source_ids": memory.current.source_ids}
            entry["status"] = memory.current.status
            trial = [*state["current_conditions"], entry]
            whole = {**state, "current_conditions": trial}
            if (len(json.dumps(trial, ensure_ascii=False).encode()) <= self.settings.context_bytes
                    and len(json.dumps(whole, ensure_ascii=False).encode()) <= self.settings.decision_bytes):
                state["current_conditions"] = trial
        for document, _ in hits:
            card = self._card(owner, document, query, 700)
            trial = {**state, "evidence": [*state["evidence"], card]}
            if len(json.dumps(trial, ensure_ascii=False).encode()) <= self.settings.decision_bytes:
                state = trial
        return state

    @staticmethod
    def _decode_decision(raw):
        """Validate independent decision fields and isolate each unusable tool step."""
        if not isinstance(raw, dict):
            raise OutputInvalid("decision_requires_object")
        errors = []
        identifiers = raw.get("relevant_memory_ids", [])
        if not isinstance(identifiers, list) or len(identifiers) > 24 or any(not isinstance(item, str) for item in identifiers):
            identifiers = []
            errors.append({"field": "relevant_memory_ids", "error": "condition_ids_invalid"})
        proposed = raw.get("steps", [])
        if not isinstance(proposed, list):
            proposed = []
            errors.append({"field": "steps", "error": "steps_invalid"})
        if len(proposed) > 2:
            errors.append({"field": "steps", "error": "extra_steps_disposed"})
        steps, positions = [], {}
        for position, item in enumerate(proposed[:2]):
            try:
                step = RecallStep.model_validate(item)
                if step.anchor_step is not None:
                    if step.anchor_step >= position or step.anchor_step not in positions:
                        raise ValueError("anchor_step_not_a_valid_prior_step")
                    step = step.model_copy(update={"anchor_step": positions[step.anchor_step]})
                positions[position] = len(steps)
                steps.append(step)
            except (ValidationError, ValueError):
                errors.append({"position": position, "error": "tool_step_invalid"})
        if set(raw) - {"steps", "relevant_memory_ids"}:
            errors.append({"field": "decision", "error": "extra_fields_disposed"})
        return Decision(steps=steps, relevant_memory_ids=identifiers), errors

    def _recall_step(self, owner, question, step, allowed_sources, documents, as_of, deadline, prior_results=()):
        scope = None
        if step.anchor_step is not None:
            if step.anchor_step >= len(prior_results):
                return [], "anchor_step_unavailable"
            prior_hits = prior_results[step.anchor_step]
            times = {document.message_at if step.time_basis == "message" else document.event_at
                     for document, _ in prior_hits}
            times.discard(None)
            if len(times) != 1:
                return [], "anchor_step_ambiguous"
            scope = next(iter(times))
        anchor = self.store.sources(owner, [step.anchor_source_id]) if step.anchor_source_id in allowed_sources else []
        if step.anchor_source_id and not anchor:
            return [], "anchor_unknown"
        if anchor:
            if step.time_basis == "message":
                scope = anchor[0].message_at
            else:
                # A contextual source supports interpretation; its date belongs to its own primary claim.
                times = {document.event_at for document in documents
                         if document.source_ids and document.source_ids[0] == anchor[0].id and document.event_at}
                scope = next(iter(times)) if len(times) == 1 else None
            if step.time_expression:
                scope = resolve_time(step.time_expression, anchor[0].content, anchor[0].message_at)
                if scope is None and step.time_basis == "message" and anchor[0].message_at:
                    try:
                        supplied, actual = parse_time(step.time_expression), parse_time(anchor[0].message_at)
                        if supplied == actual or re.fullmatch(r"\d{4}-\d{2}-\d{2}", step.time_expression) and supplied.date() == actual.date():
                            scope = anchor[0].message_at
                    except ValueError:
                        pass
        elif step.time_expression:
            scope = resolve_time(step.time_expression, question, as_of)
        restricted = bool(step.time_expression or step.anchor_source_id or step.anchor_step is not None
                          or step.relation != "independent")
        if restricted and not scope:
            return [], "time_scope_unresolved"
        pool, unknown = [], 0
        for document in documents:
            stamp = document.message_at if step.time_basis == "message" else document.event_at
            if scope:
                if not stamp:
                    unknown += 1
                    continue
                clock, target = parse_time(stamp), parse_time(scope)
                difference = (clock.date() - target.date()).days
                if step.relation in {"independent", "same_day"} and abs(difference) > step.window_days:
                    continue
                if step.relation == "after" and clock <= target or step.relation == "before" and clock >= target:
                    continue
            pool.append(document)
        vector = self._embed([step.query], deadline)[0] if self.embedder else None
        hits = self._hybrid(owner, step.query, pool, vector, limit=self.settings.candidate_limit)
        coverage = "bounded_candidates" + ("_unknown_times" if unknown else "")
        return hits, coverage

    @staticmethod
    def _merge(hits, documents=()):
        merged = {}
        unit_sources = {}
        for document in documents:
            if document.memory_id is None and document.source_ids:
                unit_sources.setdefault(document.source_ids[0], set()).update(document.source_ids)
        for document, score in hits:
            # Distinct memory kinds and graph paths still produce one original source-turn evidence item.
            key = document.source_ids[0] if document.source_ids else document.id
            previous = merged.get(key)
            if previous is None:
                copy = document.model_copy(deep=True)
                copy.source_ids = list(dict.fromkeys([*copy.source_ids, *sorted(unit_sources.get(key, set()))]))
                merged[key] = copy, score
            else:
                best, other = (document, previous[0]) if score > previous[1] else (previous[0], document)
                copy = best.model_copy(deep=True)
                copy.source_ids = list(dict.fromkeys([*best.source_ids, *other.source_ids, *sorted(unit_sources.get(key, set()))]))
                copy.source_windows = list({(span.source_id, span.start, span.stop): span
                                           for span in [*best.source_windows, *other.source_windows]}.values())
                copy.memory_evidence = list({memory.revision_id: memory for memory in
                                            [*best.memory_evidence, *other.memory_evidence]}.values())
                merged[key] = copy, max(score, previous[1])
        return sorted(merged.values(), key=lambda item: (-item[1], item[0].id))

    def search(self, request, reference_time=None):
        deadline = monotonic() + self.settings.search_timeout
        as_of = parse_time(reference_time or utcnow()).isoformat()
        with self._scope(request.user_id, deadline):
            documents = self._visible_documents(request.user_id, as_of)
            vector = None
            degradation = []
            if self.embedder:
                try:
                    vector = self._embed([request.query], deadline)[0]
                except ProviderUnavailable:
                    degradation.append("query_embedding_unavailable")
            base = self._hybrid(request.user_id, request.query, documents, vector, self.settings.candidate_limit)
            graph = self._graph_candidates(request.user_id, request.query, [document for document, _ in base[:8]], documents, vector) \
                if self.settings.graph_enabled else []
            merged = self._merge([*base, *graph], documents)
            baseline_order = {doc.source_ids[0] if doc.source_ids else doc.id: index
                              for index, (doc, _) in enumerate(merged)}
            channels = [{"channel": "base", "source_ids": sorted({key for doc, _ in base for key in doc.source_ids})},
                        {"channel": "graph", "source_ids": sorted({key for doc, _ in graph for key in doc.source_ids})}]
            current = hot_view(self.store.memories(request.user_id), as_of, self.settings.hot_limit,
                               kinds={"preference", "fact"}) if self.settings.hot_enabled else []
            state = self._decision_state(request.user_id, request.query, merged, current, as_of)
            decision = Decision(steps=[], relevant_memory_ids=[])
            decision_errors = []
            if self.model:
                try:
                    decision, decision_errors = self._decode_decision(self.model.decide(state, deadline=deadline))
                except (ProviderUnavailable, OutputInvalid, ValidationError):
                    degradation.append("decision_unavailable")
            shown = {item["id"] for item in state["current_conditions"]}
            selected = [memory for memory in current if memory.id in shown and memory.id in decision.relevant_memory_ids]
            unavailable_condition_count = len(set(decision.relevant_memory_ids) - shown)
            if "decision_unavailable" in degradation:
                selected = [memory for memory in current if memory.id in shown]
            source_ids = {source_id for card in state["evidence"] for source_id in card["source_ids"]}
            source_ids.update(source_id for item in state["current_conditions"] for source_id in item["source_ids"])
            tool_results, step_hits = [], []
            for index, step in enumerate(decision.steps):
                try:
                    hits, coverage = self._recall_step(request.user_id, request.query, step, source_ids, documents,
                                                      as_of, deadline, step_hits)
                    step_hits.append(hits)
                    scoped_new = len({document.id for document, _ in hits} - {document.id for document, _ in merged})
                    result = {"coverage": coverage, "scoped_new_documents": scoped_new, "fallback_executed": False}
                    tool_results.append(result)
                    if not hits and coverage in {"time_scope_unresolved", "anchor_unknown", "anchor_step_unavailable", "anchor_step_ambiguous"}:
                        # An invalid temporal constraint still permits a clearly diagnosed unrestricted supplemental search.
                        supplemental = self._hybrid(request.user_id, step.query, documents, vector=None, limit=self.settings.candidate_limit)
                        hits = supplemental
                        result["fallback_executed"] = True
                    result["new_documents"] = len({document.id for document, _ in hits} - {document.id for document, _ in merged})
                    result["new_source_turns"] = len({doc.source_ids[0] for doc, _ in hits if doc.source_ids}
                                                    - {doc.source_ids[0] for doc, _ in merged if doc.source_ids})
                    channels.append({"channel": f"tool_{index}", "source_ids": sorted({key for doc, _ in hits for key in doc.source_ids})})
                    extra_graph = self._graph_candidates(request.user_id, request.query, [document for document, _ in hits[:4]], documents, vector) \
                        if self.settings.graph_enabled else []
                    merged = self._merge([*merged, *hits, *extra_graph], documents)
                    channels.append({"channel": f"tool_graph_{index}", "source_ids": sorted({key for doc, _ in extra_graph for key in doc.source_ids})})
                    source_ids.update(source_id for document, _ in hits for source_id in document.source_ids)
                except ProviderUnavailable:
                    step_hits.append([])
                    tool_results.append({"coverage": "supplement_unavailable", "new_documents": 0})
                    degradation.append("supplement_unavailable")
            # Each channel is already bounded. Rank the whole deduplicated union so one query's
            # incomparable hybrid scores cannot erase another query's answer candidates.
            cards = [self._card(request.user_id, document, request.query, self.settings.evidence_bytes) for document, _ in merged]
            cards_by_source = {document.source_ids[0]: card for (document, _), card in zip(merged, cards) if document.source_ids}
            temporal = {}
            for index, (step, hits) in enumerate(zip(decision.steps, step_hits)):
                if step.select == "all":
                    continue
                for document, _ in hits:
                    stamp = document.message_at if step.time_basis == "message" else document.event_at
                    card = cards_by_source.get(document.source_ids[0]) if document.source_ids else None
                    if not stamp or card is None:
                        continue
                    target_id = stable_id("temporal_", index, document.id)
                    target = self._card(request.user_id, document, step.query, self.settings.evidence_bytes)
                    card.setdefault("temporal_targets", []).append({"id": target_id, "query": step.query,
                        "original": target["original"], "memories": target["memories"], "time": stamp})
                    temporal[target_id] = (index, document, stamp, card["id"])
            try:
                ranking = self.reranker.rank(request.query, cards, deadline=deadline) if self.reranker else RankResult()
            except (ProviderUnavailable, OutputInvalid):
                ranking = RankResult()
            scores = ranking.scores
            # Complete scores compare all channels. Partial scores retain the original question's
            # baseline order, with supplemental-only turns following it.
            ranked = (sorted(merged, key=lambda item: (-scores[item[0].id], item[0].id))
                      if len(scores) == len(merged) and merged else sorted(merged, key=lambda item:
                          baseline_order.get(item[0].source_ids[0] if item[0].source_ids else item[0].id,
                                             len(baseline_order))))
            if self.reranker and len(scores) < len(merged):
                degradation.append("rerank_partial_or_unavailable")
            mandatory = []
            for index in reversed(range(len(decision.steps))):
                step = decision.steps[index]
                if step.select == "all" or not step_hits[index]:
                    continue
                candidates = [(parse_time(stamp), card_id) for key, (number, document, stamp, card_id) in temporal.items()
                              if number == index and ranking.temporal_scores.get(key, 0) >= .8]
                expected = {key for key, value in temporal.items() if value[0] == index}
                if not expected or expected - ranking.temporal_scores.keys():
                    tool_results[index]["coverage"] += "_activity_unverified"
                    continue
                if not candidates:
                    tool_results[index]["coverage"] += "_activity_unconfirmed"
                    continue
                chosen = (min if step.select == "first" else max)(stamp for stamp, _ in candidates)
                mandatory.extend(card_id for stamp, card_id in candidates if stamp == chosen)
                tool_results[index]["selected_time"] = chosen.isoformat()
                tool_results[index]["selected_document_ids"] = list(dict.fromkeys(card_id for stamp, card_id in candidates if stamp == chosen))
            order = list(dict.fromkeys(mandatory))
            forced = {key: index for index, key in enumerate(order)}
            ranked.sort(key=lambda item: (0, forced[item[0].id]) if item[0].id in forced else (1, 0))
            prefix_lines = [f"[{memory.id}] {memory.current.content} (valid_from={memory.current.valid_from or 'unknown'}, "
                            f"valid_to={memory.current.valid_to or 'open'}, observed_at={memory.current.observed_at}, "
                            f"sources={','.join(memory.current.source_ids)})" for memory in selected]
            prefix = clip_bytes("Current relevant conditions:\n" + "\n".join(prefix_lines), self.settings.context_bytes) if prefix_lines else ""
            items = []
            cards_by_id = {card["id"]: card for card in cards}
            for document, base_score in ranked[:request.top_k]:
                card = cards_by_id[document.id]
                text = (prefix + "\n\n" if prefix else "") + "Original evidence:\n" + card["original"]
                if document.memory_id:
                    text += "\nMemory: " + card["summary"] + "\nEvent time: " + (document.event_at or "unknown")
                if card["memories"]:
                    text += "\nMemory revisions: " + json.dumps(card["memories"], ensure_ascii=False)
                if tool_results:
                    text += "\nRetrieval coverage: " + ", ".join(result["coverage"] for result in tool_results)
                items.append(SearchItem(id=document.id, content=text, score=scores.get(document.id, base_score), created_at=document.created_at))
            if not items and prefix:
                items = [SearchItem(id="current_context", content=prefix, score=0, created_at=as_of)]
            # Reading a record updates access heat only; its confirmation/validity fields remain unchanged.
            touched = {document.memory_id for document, _ in ranked[:request.top_k] if document.memory_id}
            touched.update(memory.id for memory in selected)
            for memory in self.store.memories(request.user_id, touched):
                if parse_time(memory.current.observed_at) <= parse_time(as_of):
                    if not memory.last_accessed_at or parse_time(memory.last_accessed_at) < parse_time(as_of):
                        memory.last_accessed_at = as_of
                    self.store.put("memories", memory.owner, memory.id, memory.model_dump())
            self.last_search = {"base_documents": len(base), "graph_documents": len(graph), "hot_conditions": len(current),
                "decision_cards": len(state["evidence"]), "decision_bytes": len(json.dumps(state, ensure_ascii=False).encode()),
                "selected_conditions": len(selected), "tool_results": tool_results, "rerank_documents": len(cards),
                "rerank_scored_documents": len(scores),
                "degradation": degradation, "coverage": "bounded_retrieval_not_exhaustive", "as_of": as_of,
                "decision": decision.model_dump(), "selected_condition_ids": [memory.id for memory in selected],
                "decision_errors": decision_errors,
                "candidate_channels": channels,
                "decision_source_ids": sorted({key for card in state["evidence"] for key in card["source_ids"]}),
                "candidate_union_limit": (1 + len(decision.steps)) * (self.settings.candidate_limit + self.settings.graph_limit),
                "temporal_selected_ids": order,
                "context_omitted_count": len(current) - len(shown),
                "unavailable_condition_selection_count": unavailable_condition_count,
                "candidates": [{"document_id": document.id, "source_ids": document.source_ids,
                    "memory_id": document.memory_id, "base_score": score, "rerank_score": scores.get(document.id),
                    "selected": document.id in {item.id for item in items}} for document, score in merged]}
            return SearchResponse(data=items)

    def close(self):
        for resource in (self.model, self.embedder, self.reranker, self.judger, self.store):
            if resource is not None and hasattr(resource, "close"):
                resource.close()
