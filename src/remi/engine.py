"""One Add path and one Search path, with record-scoped commits and bounded inference."""
from __future__ import annotations

import json
import re
from contextlib import contextmanager
from threading import Lock, RLock
from time import monotonic

from pydantic import ValidationError

from .clock import in_view, parse_time, resolve_time, utcnow
from .heat import hot_view, visible_memory
from .models import (AddResponse, Decision, Document, Edge, GraphObject, Memory, Revision, PlannedSearch, RecallStep,
                     SearchItem, SearchResponse, Source, SourceWindow, MemoryEvidence)
from .providers import OutputInvalid, ProviderUnavailable, RankResult
from .retrieval import clip_bytes, hybrid, tokenize, validate_vector
from .settings import Settings
from .storage import MemoryStore
from .add_pipeline import AddPipeline, DuplicateRequestConflict


from .identity import stable_id

def source_visible(source, as_of):
    return parse_time(source.message_at or source.created_at) <= parse_time(as_of)


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
    def __init__(self, store=None, model=None, embedder=None, reranker=None, settings=None):
        self.store = store or MemoryStore()
        self.model, self.embedder, self.reranker = model, embedder, reranker
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
        return AddPipeline(self).add(request)

    def _add_state(self, source, unit, sources, deadline):
        """Build the single production Add state for validation and replay tools."""
        return AddPipeline(self)._add_state(source, unit, sources, deadline)

    def _shared_recall(self, state, deadline):
        """Run the single production subject recall step."""
        return AddPipeline(self)._shared_recall(state, deadline)

    def _memory_after_identity(self, cache, result, save, deadline):
        """Run the single production memory stage for validation tooling."""
        return AddPipeline(self)._memory_after_identity(cache, result, save, deadline)

    @staticmethod
    def _units(sources):
        units = []
        for source in sources:
            if source.role == "user" or not units:
                units.append([])
            units[-1].append(source)
        return units


    def _documents(self, unit, memories, entities=(), *, pending_candidates=(), owner=None):
        documents = []
        if unit:
            combined = "\n".join(source.role + ": " + source.content for source in unit)
            locations, cursor = [], 0
            for source in unit:
                begin = cursor + len(source.role) + 2
                locations.append((source.id, begin, begin + len(source.content)))
                cursor = begin + len(source.content) + 1
            for offset in range(0, len(combined), 2000):
                documents.append(Document(id=stable_id("doc_", unit[0].id, offset), owner=unit[0].owner,
                    text=combined[offset:offset + 2240], source_ids=[source.id for source in unit], memory_id=None,
                    revision_id=None, message_at=unit[0].message_at, event_at=None, created_at=unit[0].created_at,
                    source_windows=[SourceWindow(source_id=key, start=max(offset, begin) - begin,
                        stop=min(offset + 2240, stop) - begin) for key, begin, stop in locations
                        if max(offset, begin) < min(offset + 2240, stop)]))
        owner = owner or (unit[0].owner if unit else memories[0].owner if memories else None)
        names = {raw['id']: raw.get('name') or raw['id'] for raw in self.store.list('objects', owner)} if owner else {}
        names.update({e['id']: e.get('name') or e['id'] for e in entities})
        for memory in memories:
            for revision in memory.revisions:
                sources = self.store.sources(memory.owner, revision.source_ids[:1])
                position = sources[0].content.find(revision.quote) if sources else -1
                structure = (f"Card {memory.record_type}; mentions=" + ", ".join(names.get(eid, eid) for eid in revision.entity_ids)
                    + f"; mode={revision.mode}; context={revision.context}; status={revision.status}"
                    + ("; connects="+", ".join(names.get(e,e) for e in memory.endpoint_ids)+"; quote="+memory.connection_evidence.quote if memory.endpoint_ids else "")
                    + f"; planned_at={revision.planned_at or 'unknown'}; actual_at={revision.event_at or 'unknown'}")
                scope = 'episodic' if memory.record_type == 'event' else 'source_reported'
                documents.append(Document(id=stable_id("doc_", revision.id), owner=memory.owner,
                    text=revision.content + "\n" + structure + "\nSource quote: " + revision.quote,
                    source_ids=revision.source_ids, memory_id=memory.id, revision_id=revision.id,
                    message_at=revision.observed_at, event_at=revision.event_at or revision.planned_at, created_at=revision.created_at,
                    source_windows=[SourceWindow(source_id=revision.source_ids[0], start=position, stop=position + len(revision.quote))] if position >= 0 else [],
                    memory_evidence=[MemoryEvidence(record_type=memory.record_type, mode=revision.mode, context=revision.context,
                        structure=structure, memory_id=memory.id, revision_id=revision.id, content=revision.content,
                        quote=revision.quote, source_ids=revision.source_ids, event_at=revision.event_at, planned_at=revision.planned_at,
                        status=revision.status, time_precision=revision.time_precision, observed_at=revision.observed_at, temporal_scope=scope)]))
        for candidate in pending_candidates:
            primary = self.store.sources(owner, [candidate.evidence[0].source_id])[0]
            documents.append(Document(id=stable_id('doc_pending_', primary.id, candidate.id, candidate.content), owner=owner,
                text='Pending candidate retrieval hint: ' + candidate.content + "\nOriginal evidence: " + primary.content,
                source_ids=[e.source_id for e in candidate.evidence], memory_id=None, revision_id=None,
                message_at=primary.message_at, event_at=None, created_at=primary.created_at))
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

    def _visible_documents(self, owner, as_of):
        revisions = {}
        for original in self.store.memories(owner):
            memory = visible_memory(original, as_of)
            if memory:
                revisions.update({r.id: r for r in memory.revisions})
        documents = []
        for document in self.store.documents(owner):
            if parse_time(document.message_at or document.created_at) <= parse_time(as_of):
                if document.revision_id and document.revision_id not in revisions:
                    continue
                sources = self.store.sources(owner, document.source_ids)
                if len(sources) != len(set(document.source_ids)) or not all(source_visible(s, as_of) for s in sources):
                    continue
                documents.append(document)
        return documents

    def _expand_revision_chains(self, owner, hits, documents, as_of):
        """Activate a logical Memory from any visible version/source hit and include its path to current."""
        by_memory, by_revision = {}, {}
        for document in documents:
            if document.memory_id:
                by_memory.setdefault(document.memory_id, []).append(document)
            if document.revision_id:
                by_revision[document.revision_id] = document
        scores = {document.id: score for document, score in hits}
        activated, matched = {}, {}
        for document, score in hits:
            memory_ids = {document.memory_id} if document.memory_id else {
                candidate.memory_id for candidate in documents if candidate.memory_id
                and set(candidate.source_ids).intersection(document.source_ids)}
            for memory_id in memory_ids - {None}:
                activated[memory_id] = max(score, activated.get(memory_id, 0))
                if document.memory_id == memory_id and document.revision_id:
                    matched.setdefault(memory_id, set()).add(document.revision_id)
                else:
                    matched.setdefault(memory_id, set()).update(candidate.revision_id for candidate in by_memory.get(memory_id, [])
                        if candidate.revision_id and set(candidate.source_ids).intersection(document.source_ids))
        paths = []
        for memory_id, score in activated.items():
            originals = self.store.memories(owner, [memory_id])
            memory = visible_memory(originals[0], as_of) if originals else None
            if memory is None:
                continue
            revisions = {revision.id: revision for revision in memory.revisions}
            current_id = memory.current.id
            for matched_id in matched.get(memory_id) or {current_id}:
                path, cursor, seen = [], current_id, set()
                while cursor and cursor not in seen:
                    seen.add(cursor);path.append(cursor)
                    if cursor == matched_id:
                        break
                    cursor = revisions.get(cursor).replaces_revision_id if cursor in revisions else None
                if matched_id not in path:
                    path.append(matched_id)
                path.reverse()
                paths.append(dict(memory_id=memory_id, matched_revision_id=matched_id,
                                  current_revision_id=current_id, revision_ids=path))
                for revision_id in path:
                    document = by_revision.get(revision_id)
                    if document:
                        scores[document.id] = max(score, scores.get(document.id, 0))
        expanded = [(next(document for document in documents if document.id == key), score)
                    for key, score in scores.items()]
        expanded.sort(key=lambda item: (-item[1], item[0].id))
        return expanded, paths

    @staticmethod
    def _retrieval_coverage(query, hits):
        """Measure direct lexical coverage without interpreting query intent."""
        terms = set(tokenize(query))
        if not terms or not hits:
            return dict(sufficient=False, token_coverage=0.0, direct_documents=len(hits))
        evidence = set()
        for document, _ in hits[:3]:
            evidence.update(tokenize(document.text))
        coverage = len(terms & evidence) / len(terms)
        return dict(sufficient=coverage >= .6, token_coverage=coverage, direct_documents=len(hits))

    def _hybrid(self, owner, query, documents, vector=None, limit=40):
        semantic = self.store.vector_scores(owner, vector, [document.id for document in documents]) if vector is not None else None
        return hybrid(query, documents, vector, limit=limit, semantic_scores=semantic)

    def _graph_candidates(self,owner,query,seed_documents,documents,vector,as_of):
        from .graph import active_edges, active_connections
        from .subject_identity import visible_subject, matching_subjects
        edges=active_edges(self.store,owner,as_of);mids={d.memory_id for d in seed_documents if d.memory_id}
        source_ids={sid for d in seed_documents for sid in d.source_ids}
        for original in self.store.memories(owner):
            memory=visible_memory(original,as_of)
            if memory and source_ids.intersection(memory.current.source_ids):mids.add(memory.id)
        objects={obj.id:obj for raw in self.store.list('objects',owner) if (obj:=visible_subject(raw,owner,as_of))}
        users={obj.id for obj in objects.values() if obj.is_user}
        entities={e.target for e in edges if e.origin in mids}-users
        entities.update(obj.id for obj,matches in matching_subjects(self.store,owner,[query],as_of,limit=24) if matches and not obj.is_user)
        record_ids={edge.origin for edge in edges if edge.target in entities}
        visible_events={m.id for original in self.store.memories(owner)
                        if (m:=visible_memory(original,as_of)) and m.record_type=='event'}
        frontier=set(sorted(mids & visible_events)[:24]);event_ids=set(frontier)
        navigation=[edge for edge in active_connections(self.store,owner,as_of)
                    if edge.origin in visible_events and edge.target in visible_events]
        for _ in range(2):
            adjacent_ids=set()
            for edge in navigation:
                if edge.origin in frontier:adjacent_ids.add(edge.target)
                if edge.target in frontier:adjacent_ids.add(edge.origin)
            frontier=set(sorted(adjacent_ids-event_ids)[:max(0,24-len(event_ids))])
            event_ids.update(frontier)
            if not frontier:break
        record_ids.update(event_ids)

        sessions={};adjacent=set()
        for raw in self.store.list('sources',owner):
            source=Source.model_validate(raw)
            if source.role=='user' and source_visible(source,as_of):sessions.setdefault(source.session_id,[]).append(source)
        for group in sessions.values():
            group.sort(key=lambda s:(parse_time(s.message_at or s.created_at),s.created_at,s.ordinal))
            for index,source in enumerate(group):
                if source.id in source_ids:adjacent.update(s.id for s in group[max(0,index-1):index+2])
        pool=[d for d in documents if d.memory_id in record_ids or d.memory_id is None and d.source_ids and d.source_ids[0] in adjacent]
        return self._hybrid(owner,query,pool,vector,limit=self.settings.graph_limit)

    def _card(self, owner, document, query, budget):
        sources = self.store.sources(owner, document.source_ids)
        window_ids = {span.source_id for span in document.source_windows}
        terms = set(tokenize(query))
        sources = sorted(sources, key=lambda source: (
            source.id in window_ids,
            len(terms & set(tokenize(source.content))),
            source.message_at or source.created_at,
            source.ordinal,
        ), reverse=True)
        sources = sources[:max(1, min(8, budget // 500))]
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
                    "content": clip_bytes(memory.content, 500), "event_at": memory.event_at, "planned_at": memory.planned_at, "status": memory.status,
                    "record_type": memory.record_type, "mode": memory.mode, "context": memory.context, "structure": memory.structure,
                    "time_precision": memory.time_precision,
                    "observed_at": memory.observed_at, "temporal_scope": memory.temporal_scope}
                    for memory in document.memory_evidence]}

    def _decision_state(self, owner, query, hits, current, as_of):
        state = {"query": clip_bytes(query, min(12000, self.settings.decision_bytes // 3)),
                 "reference_time": as_of, "evidence": [], "current_conditions": []}
        for memory in current:
            entry = {"id": memory.id, "tags": memory.tags, "content": clip_bytes(memory.current.content, 800),
                     "observed_at": memory.current.observed_at, "source_ids": memory.current.source_ids}
            entry["status"] = memory.current.status
            entry["record_type"] = memory.record_type
            entry["mode"] = memory.current.mode
            entry["context"] = memory.current.context
            trial = [*state["current_conditions"], entry]
            whole = {**state, "current_conditions": trial}
            if (len(json.dumps(trial, ensure_ascii=False).encode()) <= self.settings.context_bytes
                    and len(json.dumps(whole, ensure_ascii=False).encode()) <= self.settings.decision_bytes):
                state["current_conditions"] = trial
        for document, _ in hits:
            card = self._card(owner, document, query, 700)
            memories = sorted(card["memories"], key=lambda row: row.get("observed_at") or "", reverse=True)
            for limit in (4, 2, 1, 0):
                bounded = {**card, "memories": memories[:limit]}
                trial = {**state, "evidence": [*state["evidence"], bounded]}
                if len(json.dumps(trial, ensure_ascii=False).encode()) <= self.settings.decision_bytes:
                    state = trial
                    break
        return state

    @staticmethod
    def _decode_decision(raw):
        """Validate the semantic planner contract and retain actionable field errors."""
        if not isinstance(raw, dict):
            raise OutputInvalid("decision_requires_object")
        errors = []
        modes = {'direct', 'verify_premise', 'action', 'temporal_join', 'current_state'}
        if 'query_mode' not in raw:
            errors.append({'field': 'query_mode', 'error': 'query_mode_required'})
        query_mode = raw.get('query_mode', 'direct')
        if query_mode not in modes:
            errors.append({'field': 'query_mode', 'error': 'query_mode_invalid', 'value': query_mode})
            query_mode = 'direct'
        identifiers = raw.get("relevant_memory_ids", [])
        if not isinstance(identifiers, list) or len(identifiers) > 24 or any(not isinstance(item, str) for item in identifiers):
            identifiers = []
            errors.append({"field": "relevant_memory_ids", "error": "condition_ids_invalid"})
        if 'searches' not in raw:
            errors.append({'field': 'searches', 'error': 'searches_required'})
        proposed = raw.get("searches", [])
        if not isinstance(proposed, list):
            proposed = []
            errors.append({"field": "searches", "error": "searches_invalid"})
        if len(proposed) > 2:
            errors.append({"field": "searches", "error": "extra_searches_disposed"})
        searches, positions = [], {}
        for position, item in enumerate(proposed[:2]):
            try:
                search = PlannedSearch.model_validate(item)
                if search.anchor_step is not None:
                    if search.anchor_step >= position or search.anchor_step not in positions:
                        raise ValueError("anchor_step_not_a_valid_prior_step")
                    search = search.model_copy(update={"anchor_step": positions[search.anchor_step]})
                positions[position] = len(searches)
                searches.append(search)
            except ValidationError as error:
                for issue in error.errors(include_url=False):
                    errors.append({'field': '.'.join(['searches', str(position), *map(str, issue['loc'])]),
                                   'error': issue['type'], 'message': issue['msg']})
            except ValueError as error:
                errors.append({'field': f'searches.{position}.anchor_step', 'error': str(error)})
        required = {
            'direct': set(),
            'verify_premise': {'premise_check'},
            'action': {'constraint', 'latest_state'},
            'temporal_join': {'temporal_anchor', 'answer_detail'},
            'current_state': {'latest_state'},
        }[query_mode]
        missing = sorted(required - {search.role for search in searches})
        if missing:
            errors.append({'field': 'searches', 'error': 'required_roles_missing', 'roles': missing})
        if set(raw) - {"query_mode", "searches", "relevant_memory_ids", "action_time_expression"}:
            errors.append({"field": "decision", "error": "extra_fields_disposed"})
        action_time = raw.get('action_time_expression')
        if action_time == 'null':
            action_time = None
        if action_time is not None and not isinstance(action_time, str):
            errors.append({'field': 'action_time_expression', 'error': 'action_time_invalid'})
            action_time = None
        decision = Decision(query_mode=query_mode, searches=searches, relevant_memory_ids=identifiers,
                            action_time_expression=action_time)
        return decision, errors, len(proposed)

    @staticmethod
    def _plan_error(errors):
        return any(error.get('field') == 'query_mode' or error.get('field') == 'searches'
                   or str(error.get('field', '')).startswith('searches.') for error in errors)

    @staticmethod
    def _requested_ordering(question, role):
        if role == 'latest_state':
            return 'latest'
        if role not in {'direct', 'temporal_anchor', 'answer_detail'}:
            return 'all'
        normalized = question.casefold()
        if re.search(r'\b(first|earliest|initial)\b|最早|第一次', normalized):
            return 'first'
        if re.search(r'\b(latest|most recent|currently|current)\b|最近|最新|当前|现在', normalized):
            return 'latest'
        return 'all'

    @classmethod
    def _compile_searches(cls, decision, question, as_of):
        """Fill deterministic retrieval mechanics after the model chooses semantic searches."""
        steps, errors = [], []
        for position, search in enumerate(decision.searches):
            supported_time = None
            if search.time_expression:
                supported_time = resolve_time(search.time_expression, question, as_of)
                if supported_time is None:
                    errors.append({'field': f'searches.{position}.time_expression',
                                   'error': 'time_not_supported_by_question'})
                    continue
            purpose = ('scheduled' if supported_time and
                       parse_time(supported_time).date() > parse_time(as_of).date() else 'history')
            temporal_role = search.role in {'premise_check', 'temporal_anchor', 'answer_detail'}
            steps.append(RecallStep(
                role=search.role, purpose=purpose, query=search.query,
                time_expression=search.time_expression, anchor_source_id=search.anchor_source_id,
                anchor_step=search.anchor_step, time_basis='event' if temporal_role else 'message',
                relation=search.relation, select=cls._requested_ordering(question, search.role),
                window_days=0))
        return steps, errors

    def _recall_step(self, owner, question, step, allowed_sources, documents, as_of, deadline,
                     prior_results=(), vector=None, allow_embedding=True):
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
        if scope and step.purpose == "history" and parse_time(scope).date() > parse_time(as_of).date():
            return [], "action_time_is_not_history_scope"
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
        vector = (vector if vector is not None else self._embed([step.query], deadline)[0]
                  if self.embedder and allow_embedding else None)
        hits = self._hybrid(owner, step.query, pool, vector, limit=self.settings.candidate_limit)
        coverage = "bounded_candidates" + ("_unknown_times" if unknown else "")
        return hits, coverage

    @staticmethod
    def _logical_groups(documents):
        unit_sources = {}
        source_memories = {}
        for document in documents:
            if document.memory_id is None and document.source_ids:
                unit_sources.setdefault(document.source_ids[0], set()).update(document.source_ids)
            elif document.memory_id:
                for source_id in document.source_ids:
                    source_memories.setdefault(source_id, set()).add(document.memory_id)
        return unit_sources, source_memories

    @staticmethod
    def _logical_key(document, source_memories):
        if document.memory_id:
            return 'memory', document.memory_id
        linked = {memory_id for source_id in document.source_ids
                  for memory_id in source_memories.get(source_id, set())}
        return ('memory', next(iter(linked))) if len(linked) == 1 else (
            'source', document.source_ids[0] if document.source_ids else document.id)

    @classmethod
    def _merge(cls, hits, documents=()):
        merged = {}
        unit_sources, source_memories = cls._logical_groups(documents)
        for document, score in hits:
            key = cls._logical_key(document, source_memories)
            previous = merged.get(key)
            if previous is None:
                copy = document.model_copy(deep=True)
                if key[0] == 'memory' and copy.memory_id is None:
                    memory_documents = [candidate for candidate in documents if candidate.memory_id == key[1]]
                    if memory_documents:
                        copy.memory_id = key[1]
                        copy.revision_id = memory_documents[-1].revision_id
                        copy.memory_evidence = [evidence for candidate in memory_documents for evidence in candidate.memory_evidence]
                source_key = copy.source_ids[0] if copy.source_ids else None
                copy.source_ids = list(dict.fromkeys([*copy.source_ids, *sorted(unit_sources.get(source_key, set()))]))
                merged[key] = copy, score
            else:
                best, other = (document, previous[0]) if score > previous[1] else (previous[0], document)
                copy = best.model_copy(deep=True)
                copy.memory_id = key[1] if key[0] == 'memory' else copy.memory_id
                source_key = copy.source_ids[0] if copy.source_ids else None
                copy.source_ids = list(dict.fromkeys([*best.source_ids, *other.source_ids, *sorted(unit_sources.get(source_key, set()))]))
                copy.source_windows = list({(span.source_id, span.start, span.stop): span
                                           for span in [*best.source_windows, *other.source_windows]}.values())
                copy.memory_evidence = list({memory.revision_id: memory for memory in
                                            [*best.memory_evidence, *other.memory_evidence]}.values())
                merged[key] = copy, max(score, previous[1])
        return sorted(merged.values(), key=lambda item: (-item[1], item[0].id))

    @classmethod
    def _fuse_channels(cls, channel_hits, documents):
        """Fuse independent ranked channels by logical Memory using rank, never raw score scale."""
        _, source_memories = cls._logical_groups(documents)
        scores, roles, unique = {}, {}, {}
        for channel in channel_hits:
            seen = set()
            for position, (document, _) in enumerate(channel['hits'], 1):
                key = cls._logical_key(document, source_memories)
                unique.setdefault(document.id, document)
                roles.setdefault(key, set()).add(channel['role'])
                if key in seen:
                    continue
                seen.add(key)
                scores[key] = scores.get(key, 0.0) + channel.get('weight', 1.0) / (60 + position)
        hits = [(document, scores[cls._logical_key(document, source_memories)])
                for document in unique.values()]
        return cls._merge(hits, documents), roles

    @staticmethod
    def _fallback_query_mode(query):
        normalized = query.casefold()
        if re.search(r'\b(before|after|following|preceding)\b|之前|之后|后来', normalized):
            return 'temporal_join'
        if re.search(r'\b(should|could i|can i|plan|recommend|resume|return to|start)\b|适合|能不能|可以.*吗|计划|恢复', normalized):
            return 'action'
        if re.search(r'\b(current|currently|now|latest|most recent)\b|当前|现在|最近|最新', normalized):
            return 'current_state'
        if re.search(r'\b(i recall|i remember|did i|was it)\b|我记得|是否.*发生', normalized):
            return 'verify_premise'
        return 'direct'

    @staticmethod
    def _fallback_decision(query, query_mode='direct'):
        """Build a complete bounded plan from the validated mode when planner output is unusable."""
        templates = {
            'direct': [],
            'verify_premise': [('premise_check',
                'Actual dated activities, participants and locations that confirm or correct this premise: ' + query)],
            'action': [
                ('constraint', 'Past or current medical, physical, practical, scheduling, commitment and safety conditions affecting feasibility: ' + query),
                ('latest_state', 'Later changes showing whether the conditions affecting this action still apply: ' + query)],
            'temporal_join': [
                ('temporal_anchor', 'The identifying incident, participants and occurrence time in this question: ' + query),
                ('answer_detail', 'The requested outcome or detail connected to that identifying incident: ' + query)],
            'current_state': [('latest_state', 'The most recent evidence determining the currently applicable state: ' + query)],
        }
        mode = query_mode if query_mode in templates else 'direct'
        searches = []
        for index, (role, text) in enumerate(templates[mode]):
            anchored = mode == 'temporal_join' and index == 1
            relation = ('before' if re.search(r'\bbefore\b|之前', query, re.I) else
                        'after' if re.search(r'\bafter\b|之后|后来', query, re.I) else
                        'same_day') if anchored else 'independent'
            searches.append(PlannedSearch(role=role, query=text, time_expression=None,
                anchor_source_id=None, anchor_step=0 if anchored else None, relation=relation))
        return Decision(query_mode=mode, relevant_memory_ids=[], searches=searches,
                        action_time_expression=None)

    @staticmethod
    def _broad_role_query(role, question):
        templates = {
            'direct': 'Facts, events and original statements that directly answer: ',
            'premise_check': 'Actual activities, events, participants, places and dates that confirm or correct: ',
            'constraint': 'Medical, physical, practical, scheduling, commitment, safety and feasibility conditions relevant to: ',
            'latest_state': 'Later changes, recovery, replacement, cancellation, completion and current status relevant to: ',
            'temporal_anchor': 'The identifying incident, participants, place and occurrence time for: ',
            'answer_detail': 'The outcome, consequence, response or detail connected to the identified incident in: ',
        }
        return templates[role] + question

    def _role_recall_sufficient(self, query, hits):
        if not hits:
            return False
        coverage = self._retrieval_coverage(query, hits)
        return hits[0][1] >= self.settings.role_recall_min_score or coverage['sufficient']

    @staticmethod
    def _role_graph_seeds(step, hits, limit=8):
        """Choose graph roots from relevant hits; temporal roles order only their bounded seed pool."""
        pool = list(hits[:20])
        if step.role == 'latest_state' or step.select == 'latest':
            pool.sort(key=lambda item: parse_time(
                (item[0].message_at if step.time_basis == 'message' else item[0].event_at)
                or item[0].created_at), reverse=True)
        elif step.select == 'first':
            pool.sort(key=lambda item: parse_time(
                (item[0].message_at if step.time_basis == 'message' else item[0].event_at)
                or item[0].created_at))
        return [document for document, _ in pool[:limit]]

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
            base_expanded, revision_paths = self._expand_revision_chains(request.user_id, base, documents, as_of)
            graph_seeds = [document for document, _ in base[:min(20, len(base))]]
            graph = self._graph_candidates(request.user_id, request.query, graph_seeds, documents, vector, as_of) \
                if self.settings.graph_enabled else []
            graph_expanded, graph_paths = self._expand_revision_chains(request.user_id, graph, documents, as_of)
            revision_paths.extend(graph_paths)
            retrieval_channels = [
                {'channel': 'base', 'role': 'direct', 'query': request.query,
                 'weight': 1.0, 'hits': base_expanded},
                {'channel': 'graph', 'role': 'direct', 'query': request.query,
                 'weight': .5, 'hits': graph_expanded},
            ]
            merged, retrieval_roles = self._fuse_channels(retrieval_channels, documents)
            direct_coverage = self._retrieval_coverage(request.query, base)
            current = hot_view(self.store.memories(request.user_id), as_of, self.settings.hot_limit) if self.settings.hot_enabled else []
            state = self._decision_state(request.user_id, request.query, merged, current, as_of)
            decision = Decision(searches=[], relevant_memory_ids=[])
            compiled_steps = []
            decision_errors = []
            planner_attempts = []
            if self.model:
                try:
                    raw_decision = self.model.decide(state, deadline=deadline)
                    decision, decision_errors, _ = self._decode_decision(raw_decision)
                    compiled_steps, compile_errors = self._compile_searches(decision, request.query, as_of)
                    decision_errors.extend(compile_errors)
                    planner_attempts.append({'raw': raw_decision, 'errors': list(decision_errors)})
                    if self._plan_error(decision_errors):
                        repair_state = {**state, 'previous_output': raw_decision,
                                        'repair_errors': decision_errors}
                        repaired_raw = self.model.decide(repair_state, deadline=deadline)
                        decision, decision_errors, _ = self._decode_decision(repaired_raw)
                        compiled_steps, compile_errors = self._compile_searches(decision, request.query, as_of)
                        decision_errors.extend(compile_errors)
                        planner_attempts.append({'raw': repaired_raw, 'errors': list(decision_errors)})
                    if self._plan_error(decision_errors):
                        degradation.append('decision_invalid')
                        decision = self._fallback_decision(request.query, decision.query_mode)
                        compiled_steps, _ = self._compile_searches(decision, request.query, as_of)
                except (ProviderUnavailable, OutputInvalid, ValidationError):
                    degradation.append("decision_unavailable")
                    decision = self._fallback_decision(request.query, self._fallback_query_mode(request.query))
                    compiled_steps, _ = self._compile_searches(decision, request.query, as_of)
            shown = {item["id"] for item in state["current_conditions"]}
            selected = [memory for memory in current if memory.id in shown and memory.id in decision.relevant_memory_ids]
            unavailable_condition_count = len(set(decision.relevant_memory_ids) - shown)
            if "decision_unavailable" in degradation:
                selected = []
            source_ids = {source_id for card in state["evidence"] for source_id in card["source_ids"]}
            source_ids.update(source_id for item in state["current_conditions"] for source_id in item["source_ids"])
            tool_results, step_hits = [], []
            for index, step in enumerate(compiled_steps):
                temporal_step = bool(step.time_expression or step.anchor_source_id or step.anchor_step is not None
                                     or step.relation != 'independent' or step.select != 'all')
                if (direct_coverage['sufficient'] and not temporal_step
                        and step.role in {'direct', 'answer_detail'}):
                    tool_results.append({'coverage':'direct_evidence_sufficient','new_documents':0,
                                         'fallback_executed':False})
                    step_hits.append([])
                    continue
                try:
                    step_vector, allow_step_embedding = None, True
                    if self.embedder:
                        try:
                            step_vector = self._embed([step.query], deadline)[0]
                        except ProviderUnavailable:
                            allow_step_embedding = False
                            degradation.append(f'tool_{index}_embedding_unavailable')
                    initial_hits, recall_coverage = self._recall_step(request.user_id, request.query, step, source_ids, documents,
                        as_of, deadline, step_hits, step_vector, allow_step_embedding)
                    scoped_new = len({document.id for document, _ in initial_hits} - {document.id for document, _ in merged})
                    result = {"coverage": recall_coverage, "scoped_new_documents": scoped_new, "fallback_executed": False}
                    tool_results.append(result)
                    if not initial_hits and recall_coverage in {"time_scope_unresolved", "anchor_unknown", "anchor_step_unavailable", "anchor_step_ambiguous", "action_time_is_not_history_scope"}:
                        # An invalid temporal constraint still permits a clearly diagnosed unrestricted supplemental search.
                        initial_hits = self._hybrid(request.user_id, step.query, documents, vector=step_vector,
                                                    limit=self.settings.candidate_limit)
                        result["fallback_executed"] = True
                    initial_signal = self._retrieval_coverage(step.query, initial_hits)
                    result['initial_top_score'] = initial_hits[0][1] if initial_hits else 0.0
                    result['initial_token_coverage'] = initial_signal['token_coverage']
                    broad_hits, broad_query, broad_vector = [], None, None
                    if not self._role_recall_sufficient(step.query, initial_hits):
                        broad_query = self._broad_role_query(step.role, request.query)
                        allow_broad_embedding = True
                        if self.embedder:
                            try:
                                broad_vector = self._embed([broad_query], deadline)[0]
                            except ProviderUnavailable:
                                allow_broad_embedding = False
                                degradation.append(f'tool_{index}_broad_embedding_unavailable')
                        broad_step = step.model_copy(update={'query': broad_query})
                        broad_hits, broad_coverage = self._recall_step(request.user_id, request.query, broad_step,
                            source_ids, documents, as_of, deadline, step_hits, broad_vector, allow_broad_embedding)
                        if not broad_hits and broad_coverage in {"time_scope_unresolved", "anchor_unknown",
                                "anchor_step_unavailable", "anchor_step_ambiguous", "action_time_is_not_history_scope"}:
                            broad_hits = self._hybrid(request.user_id, broad_query, documents, vector=broad_vector,
                                                      limit=self.settings.candidate_limit)
                        result.update(broadening_executed=True, broad_query=broad_query,
                                      broad_coverage=broad_coverage,
                                      broad_top_score=broad_hits[0][1] if broad_hits else 0.0)
                    else:
                        result['broadening_executed'] = False
                    hit_scores = {}
                    for document, score in [*initial_hits, *broad_hits]:
                        hit_scores[document.id] = (document, max(score, hit_scores.get(document.id, (document, 0))[1]))
                    hits = sorted(hit_scores.values(), key=lambda item: (-item[1], item[0].id))
                    step_hits.append(hits)
                    result["new_documents"] = len({document.id for document, _ in hits} - {document.id for document, _ in merged})
                    result["new_source_turns"] = len({doc.source_ids[0] for doc, _ in hits if doc.source_ids}
                                                    - {doc.source_ids[0] for doc, _ in merged if doc.source_ids})
                    role_graph_seeds = self._role_graph_seeds(step, hits)
                    graph_query = broad_query or step.query
                    graph_vector = broad_vector if broad_query else step_vector
                    extra_graph = self._graph_candidates(request.user_id, graph_query,
                        role_graph_seeds, documents,
                        graph_vector, as_of) \
                        if self.settings.graph_enabled else []
                    expanded_hits, extra_paths = self._expand_revision_chains(request.user_id, initial_hits, documents, as_of)
                    expanded_broad, broad_paths = self._expand_revision_chains(request.user_id, broad_hits, documents, as_of)
                    expanded_graph, graph_step_paths = self._expand_revision_chains(request.user_id, extra_graph, documents, as_of)
                    revision_paths.extend([*extra_paths, *broad_paths, *graph_step_paths])
                    new_channels = [
                        {'channel': f'tool_{index}', 'role': step.role, 'query': step.query,
                         'weight': 1.0, 'hits': expanded_hits},
                        {'channel': f'tool_graph_{index}', 'role': step.role, 'query': step.query,
                         'weight': .5,
                         'seed_source_ids': [key for doc in role_graph_seeds for key in doc.source_ids],
                         'hits': expanded_graph},
                    ]
                    if broad_query:
                        new_channels.append({'channel': f'tool_broad_{index}', 'role': step.role,
                                             'query': broad_query, 'weight': 1.0, 'hits': expanded_broad})
                        new_channels[-2]['query'] = graph_query
                    retrieval_channels.extend(new_channels)
                    merged, retrieval_roles = self._fuse_channels(retrieval_channels, documents)
                    source_ids.update(source_id for document, _ in hits for source_id in document.source_ids)
                except ProviderUnavailable:
                    step_hits.append([])
                    tool_results.append({"coverage": "supplement_unavailable", "new_documents": 0})
                    degradation.append("supplement_unavailable")
            # Every channel is bounded and contributes ranks on the same RRF scale.
            merged, retrieval_roles = self._fuse_channels(retrieval_channels, documents)
            baseline_order = {doc.id: index for index, (doc, _) in enumerate(merged)}
            cards = [self._card(request.user_id, document, request.query, self.settings.evidence_bytes) for document, _ in merged]
            _, source_memories = self._logical_groups(documents)
            retrieval_objectives = {}
            for channel in retrieval_channels:
                objective = (channel['role'], channel.get('query') or request.query)
                for document, _ in channel['hits']:
                    key = self._logical_key(document, source_memories)
                    retrieval_objectives.setdefault(key, set()).add(objective)
            for card, (document, _) in zip(cards, merged):
                key = self._logical_key(document, source_memories)
                card['retrieval_roles'] = sorted(retrieval_roles.get(key, {'direct'}))
                card['retrieval_objectives'] = [
                    {'role': role, 'query': query}
                    for role, query in sorted(retrieval_objectives.get(key, {('direct', request.query)}))
                ]
            cards_by_source = {document.source_ids[0]: card for (document, _), card in zip(merged, cards) if document.source_ids}
            temporal = {}
            for index, (step, hits) in enumerate(zip(compiled_steps, step_hits)):
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
            # Complete scores compare all candidates. Partial scores retain the cross-channel RRF order.
            ranked = (sorted(merged, key=lambda item: (-scores[item[0].id], item[0].id))
                      if len(scores) == len(merged) and merged else sorted(merged, key=lambda item:
                          baseline_order.get(item[0].id, len(baseline_order))))
            if self.reranker and len(scores) < len(merged):
                degradation.append("rerank_partial_or_unavailable")
            temporal_selected_ids = []
            for index in reversed(range(len(compiled_steps))):
                step = compiled_steps[index]
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
                selected_card_ids = list(dict.fromkeys(card_id for stamp, card_id in candidates if stamp == chosen))
                temporal_selected_ids.extend(selected_card_ids)
                tool_results[index]["selected_time"] = chosen.isoformat()
                tool_results[index]["selected_document_ids"] = selected_card_ids
            # first/latest are explicit query operators. They apply after Jev verifies the requested activity.
            order = list(dict.fromkeys(temporal_selected_ids))
            forced = {key: index for index, key in enumerate(order)}
            ranked.sort(key=lambda item: (0, forced[item[0].id]) if item[0].id in forced else (1, 0))
            prefix_lines = [f"[{memory.id}] {memory.current.content} (status={memory.current.status}, scope={('episodic' if memory.record_type == 'event' else 'source_reported')}, observed_at={memory.current.observed_at}, "
                            f"sources={','.join(memory.current.source_ids)})" for memory in selected]
            prefix = clip_bytes("Relevant source-reported statements:\n" + "\n".join(prefix_lines), self.settings.context_bytes) if prefix_lines else ""
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
            channels = [{"channel": channel['channel'], "role": channel['role'], "weight": channel['weight'],
                "seed_source_ids": channel.get('seed_source_ids', []),
                "source_ids": sorted({key for doc, _ in channel['hits'] for key in doc.source_ids})}
                for channel in retrieval_channels]
            self.last_search = {"base_documents": len(base), "graph_documents": len(graph),
                "graph_seed_documents": len(graph_seeds), "hot_conditions": len(current),
                "decision_cards": len(state["evidence"]), "decision_bytes": len(json.dumps(state, ensure_ascii=False).encode()),
                "selected_conditions": len(selected), "tool_results": tool_results, "rerank_documents": len(cards),
                "rerank_scored_documents": len(scores),
                "degradation": degradation, "coverage": "bounded_retrieval_not_exhaustive", "as_of": as_of,
                "decision": decision.model_dump(), "selected_condition_ids": [memory.id for memory in selected],
                "compiled_steps": [step.model_dump() for step in compiled_steps],
                "decision_errors": decision_errors,
                "planner_attempts": planner_attempts,
                "direct_coverage": direct_coverage,
                "fusion": "logical_memory_rrf",
                "revision_paths": revision_paths,
                "memory_bundles": len({document.memory_id for document, _ in merged if document.memory_id}),
                "candidate_channels": channels,
                "decision_source_ids": sorted({key for card in state["evidence"] for key in card["source_ids"]}),
                "candidate_union_limit": sum(
                    self.settings.graph_limit if 'graph' in channel['channel'] and self.settings.graph_enabled
                    else 0 if 'graph' in channel['channel'] else self.settings.candidate_limit
                    for channel in retrieval_channels),
                "temporal_selected_ids": list(dict.fromkeys(temporal_selected_ids)),
                "context_omitted_count": len(current) - len(shown),
                "unavailable_condition_selection_count": unavailable_condition_count,
                "candidates": [{"document_id": document.id, "source_ids": document.source_ids,
                    "memory_id": document.memory_id, "base_score": score, "rerank_score": scores.get(document.id),
                    "selected": document.id in {item.id for item in items}} for document, score in merged]}
            return SearchResponse(data=items)

    def close(self):
        for resource in (self.model, self.embedder, self.reranker, self.store):
            if resource is not None and hasattr(resource, "close"):
                resource.close()
