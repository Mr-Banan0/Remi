"""Two model stages, independent candidate outcomes, and narrow event patches."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import hashlib
import json
from time import monotonic

from pydantic import ValidationError

from .clock import absolute_content, message_time, parse_time, resolve_time, utcnow
from .graph import project_edges
from .heat import visible_memory
from .identity import normalized_name, resolve_mentions, stable_id
from .models import AddResponse, Candidate, CandidateDraft, CardDecision, EvidenceRef, GraphObject, Memory, ProposalPlan, Revision, Source
from .providers import OutputInvalid, ProviderUnavailable
from .retrieval import clip_bytes

PIPELINE = 'G10-local-event-maintenance-v5'


class HistoryChanged(ProviderUnavailable):
    pass


class PlanIssue(ValueError):
    def __init__(self, reason, details=None):
        super().__init__(reason)
        self.details = details or {}


class AddPipeline:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store

    @staticmethod
    def chunks(text, maximum=5000):
        start = 0
        while start < len(text):
            stop = min(len(text), start + maximum)
            if stop < len(text):
                boundary = max(text.rfind(mark, start, stop) for mark in ['\n', '.', '。', '!', '?', '！', '？'])
                if boundary > start:
                    stop = boundary + 1
            yield start, stop, text[start:stop]
            start = stop

    @staticmethod
    def _sources(state):
        sources = {s['id']: s for s in [state['new_user_source'], *state['context_sources']]}
        for record in state.get('history', []):
            sources.update({s['id']: s for s in record['sources']})
        return sources

    def _proposals(self, raw, state, dispositions):
        if not isinstance(raw, dict) or set(raw) != {'items', 'reason'} or not isinstance(raw['items'], list):
            raise PlanIssue('proposal_requires_items_and_reason')
        sources = self._sources(state)
        items = []
        for position, raw_item in enumerate(raw['items']):
            if not isinstance(raw_item, dict) or not isinstance(raw_item.get('evidence'), list):
                raise PlanIssue('candidate_requires_object_and_evidence')
            item = CandidateDraft.model_validate(raw_item).model_dump()
            item['id'] = str(position)
            evidence = []
            for citation in item.get('evidence', []):
                if not isinstance(citation, dict) or set(citation) != {'source_id'}:
                    raise PlanIssue('citation_selects_source_id_only')
                sid = citation.get('source_id')
                if sid not in sources:
                    raise PlanIssue('candidate_source_requires_supplied_original')
                evidence.append({'source_id': sid, 'quote': sources[sid]['content']})
            item['evidence'] = evidence
            candidate = Candidate.model_validate(item)
            if state['new_user_source']['id'] not in {e.source_id for e in candidate.evidence}:
                dispositions.append({'candidate_id':candidate.id,'status':'context_only',
                    'reason':'context_interprets_current_input','source_ids':[e.source_id for e in candidate.evidence]})
                continue
            candidate.evidence.sort(key=lambda e: e.source_id != state['new_user_source']['id'])
            items.append(candidate)
        if len({i.id for i in items}) != len(items):
            raise PlanIssue('candidate_ids_are_unique')
        return ProposalPlan(items=items, reason=raw['reason'])

    def _proposal_stage(self, state, cache, save, deadline):
        if 'propose' in cache:
            return ProposalPlan.model_validate(cache['propose'])
        key = 'propose_repair' if cache.get('repair_stage') == 'propose' else 'propose'
        if key not in cache.setdefault('outputs', {}):
            payload = {**state, 'repair_errors': cache.get('proposal_errors', [])} if key.endswith('repair') else state
            try:
                cache['outputs'][key] = self.engine.model.propose(payload, deadline=deadline)
                save()
            except OutputInvalid:
                cache['outputs'][key] = {'output_error': 'model_output_invalid'}
                save()
            except ProviderUnavailable:
                cache['last_failure'] = {'stage': key, 'reason': 'provider_unavailable'}
                save()
                raise
        try:
            dispositions = []
            result = self._proposals(cache['outputs'][key], state, dispositions)
            cache['source_dispositions'] = dispositions
        except (ValidationError, ValueError, OutputInvalid) as error:
            reason = self._error(error)
            cache.setdefault('validation', []).append({'stage': key, 'error': reason})
            if not cache.get('repair_used'):
                cache.update(repair_used=True, repair_stage='propose', proposal_errors=[{'error': reason}])
                save()
                return self._proposal_stage(state, cache, save, deadline)
            cache['last_failure'] = {'stage': key, 'reason': 'proposal_output_invalid'}
            save()
            raise ProviderUnavailable('proposal_output_invalid') from error
        cache['propose'] = result.model_dump()
        cache.pop('last_failure', None)
        save()
        return result

    def _recall(self, state, proposal, deadline):
        owner = state['owner']
        clock = state['new_user_source']['message_at'] or state['new_user_source']['created_at']
        sources = self._sources(state)
        identities, entity_rows, identity_dispositions = {}, {}, []
        for item in proposal.items:
            supporting = [sources[e.source_id] for e in item.evidence]
            ids, objects, dispositions = resolve_mentions(self.store, owner, item.mentions, supporting, clock)
            identities[item.id] = ids
            entity_rows.update({obj.id: obj.model_dump() for obj in objects})
            identity_dispositions.extend({'candidate_id': item.id, **entry} for entry in dispositions)
        memories = {m.id: m for original in self.store.memories(owner) if (m := visible_memory(original, clock))}
        docs = self.engine._visible_documents(owner, clock)
        vectors = self.engine._embed([c.content for c in proposal.items], deadline) if docs and self.engine.embedder else [None] * len(proposal.items)
        selected, trace = [], []
        self_ids = {obj['id'] for obj in entity_rows.values() if obj['is_self']}
        # A bounded round-robin union gives every candidate access to the shared budget.
        lists = []
        for item, vector in zip(proposal.items, vectors):
            entity_ids = set(identities[item.id]) - self_ids
            local = {m.id for m in memories.values() if entity_ids.intersection(m.entity_ids)}
            local_hits = self.engine._hybrid(owner, item.content, [d for d in docs if d.memory_id in local], vector, limit=6)
            global_hits = self.engine._hybrid(owner, item.content, [d for d in docs if d.memory_id], vector, limit=2)
            ids = list(dict.fromkeys(d.memory_id for d, _ in [*local_hits, *global_hits]))
            lists.append(ids)
            trace.append({'candidate_id': item.id, 'local': [d.memory_id for d, _ in local_hits],
                          'global': [d.memory_id for d, _ in global_hits]})
        for position in range(max((len(ids) for ids in lists), default=0)):
            for ids in lists:
                if position < len(ids) and ids[position] not in selected:
                    selected.append(ids[position])
        cards = []
        for mid in selected[:8]:
            memory = memories[mid]
            revision = memory.current
            names = [self.store.get('objects', owner, eid)['name'] for eid in revision.entity_ids
                     if self.store.get('objects', owner, eid)]
            cards.append({'id': mid, 'revision_id':revision.id, 'record_type': memory.record_type, 'content': revision.content,
                          'status': revision.status, 'planned_at': revision.planned_at, 'event_at': revision.event_at,
                          'mode': revision.mode, 'context': revision.context, 'subjects': names,
                          'patchable': memory.record_type == 'event' and revision.status == 'planned' and revision.mode == 'asserted',
                          'sources': [s.model_dump() for s in self.store.sources(owner, revision.source_ids)]})
        # Keep complete user evidence; drop least-ranked cards to fit the existing context budget.
        context = {**state, 'proposal': proposal.model_dump(), 'history': cards,
                   'identity_issues': identity_dispositions}
        def prompt_bytes():
            compact = deepcopy(context)
            for item in compact['proposal']['items']:
                item['evidence'] = [{'source_id':e['source_id']} for e in item['evidence']]
            return len(json.dumps(compact, ensure_ascii=False).encode())
        while prompt_bytes() > 32000 and context['history']:
            context['history'].pop()
        return context, {'candidate_entity_ids': identities, 'entity_rows': list(entity_rows.values()),
                         'identity_dispositions': identity_dispositions, 'recall_trace': trace}

    @staticmethod
    def _error(error):
        if isinstance(error, ValidationError):
            return 'schema:' + ','.join('.'.join(map(str, e['loc'])) + ':' + e['type'] for e in error.errors()[:3])
        return str(error).split('\n')[0][:200]

    @staticmethod
    def _date(expression, candidate, state):
        if not expression:
            return None
        sources = AddPipeline._sources(state)
        # The primary claim controls interpretation. Supporting context can resolve an explicit back-reference.
        for evidence in candidate.evidence:
            source = sources[evidence.source_id]
            stamp = resolve_time(expression, evidence.quote, source.get('message_at') or source.get('created_at'))
            if stamp:
                return stamp
        return None

    def _validate_decision(self, raw, candidate, state):
        decision = CardDecision.model_validate(raw)
        if any(d['candidate_id'] == candidate.id for d in state.get('identity_issues', [])) and decision.operation != 'pending':
            raise PlanIssue('candidate_identity_requires_clarification')
        if decision.candidate_id != candidate.id:
            raise PlanIssue('decision_selects_supplied_candidate')
        history = {h['id']: h for h in state['history']}
        target = history.get(decision.target_id)
        new_fields = [decision.record_type, decision.content, decision.mode, decision.context, decision.status]
        if decision.operation == 'create':
            if decision.target_id is not None or decision.event_change is not None:
                raise PlanIssue('creation_has_new_identity')
            if not all([decision.record_type, decision.content, decision.mode]):
                raise PlanIssue('creation_requires_complete_card')
            if decision.record_type == 'relation':
                if decision.status is not None or decision.time_expression is not None:
                    raise PlanIssue('relation_creation_uses_statement_and_source_time')
            elif decision.status is None:
                raise PlanIssue('event_creation_requires_status')
            if decision.time_expression and not self._date(decision.time_expression, candidate, state):
                raise PlanIssue('event_date_requires_source_support')
        elif decision.operation == 'patch_event':
            if target is None or not target['patchable']:
                raise PlanIssue('patch_selects_recalled_asserted_plan')
            if any(v is not None for v in new_fields) or decision.event_change is None:
                raise PlanIssue('event_patch_changes_only_date_or_status')
            if decision.event_change == 'cancel' and decision.time_expression is not None:
                raise PlanIssue('cancel_changes_status_with_no_time_argument')
            if decision.event_change == 'reschedule' and not decision.time_expression:
                raise PlanIssue('reschedule_requires_date')
            if decision.time_expression and not self._date(decision.time_expression, candidate, state):
                raise PlanIssue('event_date_requires_source_support')
        else:
            if any(v is not None for v in new_fields) or decision.event_change or decision.time_expression:
                raise PlanIssue('keep_and_pending_preserve_card_fields')
            if decision.operation == 'keep' and target is None:
                raise PlanIssue('keep_selects_recalled_card')
            if decision.operation == 'pending' and decision.target_id is not None:
                raise PlanIssue('pending_preserves_previous_records')
        return decision

    def _decode_decisions(self, raw, state):
        candidates = {c['id']: Candidate.model_validate(c) for c in state['proposal']['items']}
        items = raw.get('decisions') if isinstance(raw, dict) and set(raw) == {'decisions'} else None
        if not isinstance(items, list):
            return {}, {cid: 'maintenance_requires_decisions' for cid in candidates}
        counts = Counter(i.get('candidate_id') for i in items if isinstance(i, dict) and isinstance(i.get('candidate_id'), str))
        good, errors = {}, {}
        for cid, candidate in candidates.items():
            if counts[cid] != 1:
                errors[cid] = 'exactly_one_decision_per_candidate'
                continue
            raw_item = next(i for i in items if isinstance(i, dict) and i.get('candidate_id') == cid)
            try:
                good[cid] = self._validate_decision(raw_item, candidate, state)
            except (ValidationError, ValueError) as error:
                errors[cid] = self._error(error)
        return good, errors

    def _maintenance_stage(self, state, cache, save, deadline):
        if 'maintain' in cache:
            return [CardDecision.model_validate(x) for x in cache['maintain']['decisions']]
        outputs = cache.setdefault('outputs', {})
        if 'maintain' not in outputs:
            try:
                outputs['maintain'] = self.engine.model.maintain(state, deadline=deadline)
                save()
            except OutputInvalid:
                outputs['maintain'] = {'output_error': 'model_output_invalid'}
                save()
            except ProviderUnavailable:
                cache['last_failure'] = {'stage': 'maintain', 'reason': 'provider_unavailable'}
                save()
                raise
        good, errors = self._decode_decisions(outputs['maintain'], state)
        cache['validation'] = [{'candidate_id': cid, 'error': reason} for cid, reason in errors.items()]
        if errors and (not cache.get('repair_used') or cache.get('repair_stage') == 'maintain'):
            if not cache.get('repair_used'):
                cache.update(repair_used=True, repair_stage='maintain', repair_candidate_ids=list(errors))
                save()
            ids = set(cache['repair_candidate_ids'])
            partial = {**state, 'proposal': {**state['proposal'], 'items': [c for c in state['proposal']['items'] if c['id'] in ids]},
                       'repair_errors': [{'candidate_id': cid, 'error': reason} for cid, reason in errors.items() if cid in ids]}
            if 'maintain_repair' not in outputs:
                try:
                    outputs['maintain_repair'] = self.engine.model.maintain(partial, deadline=deadline)
                    save()
                except OutputInvalid:
                    outputs['maintain_repair'] = {'output_error': 'model_output_invalid'}
                    save()
                except ProviderUnavailable:
                    cache['last_failure'] = {'stage': 'maintain_repair', 'reason': 'provider_unavailable'}
                    save()
                    raise
            fixed, remaining = self._decode_decisions(outputs['maintain_repair'], partial)
            good.update(fixed)
            errors = {**{cid: reason for cid, reason in errors.items() if cid not in ids}, **remaining}
        for cid, reason in errors.items():
            good[cid] = CardDecision(candidate_id=cid, operation='pending', target_id=None, record_type=None,
                content=None, mode=None, context=None, status=None, event_change=None, time_expression=None, reason=reason)
        # Conflicting edits share a dependency group; independent candidates retain their decisions.
        by_target = {}
        for decision in good.values():
            if decision.operation == 'patch_event':
                by_target.setdefault(decision.target_id, []).append(decision)
        for target, decisions in by_target.items():
            signatures = {(d.event_change, d.time_expression) for d in decisions}
            if len(signatures) > 1:
                for d in decisions:
                    good[d.candidate_id] = d.model_copy(update={'operation': 'pending', 'target_id': None,
                        'event_change': None, 'time_expression': None, 'reason': 'conflicting_event_patches_require_clarification'})
        ordered = [good[item['id']] for item in state['proposal']['items']]
        cache['maintain'] = {'decisions': [d.model_dump() for d in ordered]}
        cache['candidate_dispositions'] = [{'candidate_id': d.candidate_id,
            'status': 'pending' if d.operation == 'pending' else 'accepted', 'operation': d.operation, 'reason': d.reason} for d in ordered]
        cache.pop('last_failure', None)
        save()
        return ordered

    def _materialize(self, state, bindings, decisions):
        owner = state['owner']
        primary = state['new_user_source']
        observed = primary['message_at'] or primary['created_at']
        candidates = {c['id']: Candidate.model_validate(c) for c in state['proposal']['items']}
        memories, edges, expected, used_entities = {}, [], {}, set()
        patch_groups = {}
        for decision in decisions:
            candidate = candidates[decision.candidate_id]
            if decision.operation in {'keep', 'pending'}:
                continue
            evidence = list(candidate.evidence)
            if decision.operation == 'patch_event':
                patch_groups.setdefault(decision.target_id, []).append(decision)
                continue
            mid = stable_id('mem_', owner, primary['id'], state.get('source_window'), candidate.id)
            entity_ids = bindings['candidate_entity_ids'][candidate.id]
            stamp = self._date(decision.time_expression, candidate, state)
            status = decision.status if decision.record_type == 'event' else 'stated'
            revision = Revision(id=stable_id('rev_', mid, 'create'),
                content=absolute_content(decision.content, '\n'.join(e.quote for e in evidence), primary['message_at'] or primary['created_at']),
                source_ids=[e.source_id for e in evidence], quote=evidence[0].quote, evidence=evidence, entity_ids=entity_ids,
                mode=decision.mode, context=decision.context or "source_reported", status=status,
                event_at=stamp if status == 'occurred' else None,
                planned_at=stamp if status in {'planned', 'desired', 'cancelled'} else None,
                observed_at=observed, created_at=utcnow(), time_resolution='rule' if stamp else 'unknown',
                time_precision='day' if stamp else 'unknown', operation='create')
            memory = Memory(id=mid, owner=owner, record_type=decision.record_type, revisions=[revision])
            memories[mid] = memory
            used_entities.update(entity_ids)
            expected[mid] = None
            edges.extend(project_edges(memory, revision))
        for mid, group in patch_groups.items():
            original = Memory.model_validate(self.store.get('memories', owner, mid))
            view = visible_memory(original, observed)
            previous = view.current
            recalled = next(h for h in state['history'] if h['id'] == mid)
            if previous.id != recalled['revision_id']:
                raise HistoryChanged('maintenance_history_changed_retry')
            expected[mid] = original.model_dump()
            decision = group[0]
            evidence = [e for patch in group for e in candidates[patch.candidate_id].evidence]
            combined = list({e.source_id: e for e in [*evidence, *previous.evidence]}.values())
            stamp = self._date(decision.time_expression, candidates[decision.candidate_id], state)
            changes = {'reschedule': {'planned_at': stamp}, 'complete': {'status': 'occurred', 'event_at': stamp},
                       'cancel': {'status': 'cancelled'}}[decision.event_change]
            revision = previous.model_copy(update={**changes,
                'id': stable_id('rev_', mid, primary['id'], state.get('source_window'), decision.event_change),
                'source_ids': [e.source_id for e in combined], 'quote': combined[0].quote, 'evidence': combined,
                'observed_at': observed, 'created_at': utcnow(), 'operation': decision.event_change,
                'replaces_revision_id': previous.id,
                'time_resolution': previous.time_resolution if decision.event_change == 'cancel' else ('rule' if stamp else 'unknown'),
                'time_precision': previous.time_precision if decision.event_change == 'cancel' else ('day' if stamp else 'unknown')})
            original.revisions.append(revision)
            memories[mid] = Memory.model_validate(original.model_dump())
            edges.extend(project_edges(original, revision, previous.id))
        objects = [raw for raw in bindings['entity_rows'] if raw['id'] in used_entities]
        return objects, list(memories.values()), edges, expected

    @staticmethod
    def _invalidate_maintenance(cache):
        cache.setdefault('history_conflicts', []).append({
            'candidate_dispositions':cache.get('candidate_dispositions', []),
            'recalled_versions':{h['id']:h['revision_id'] for h in cache.get('maintenance_context', {}).get('history', [])}})
        for name in ['maintenance_context','bindings','maintain','repair_candidate_ids','repair_stage']:
            cache.pop(name, None)
        for name in ['maintain','maintain_repair']:
            cache.get('outputs', {}).pop(name, None)

    def add(self, request):
        from .engine import DuplicateRequestConflict
        deadline = monotonic() + self.engine.settings.add_timeout
        with self.engine._scope(request.user_id, deadline):
            fingerprint = hashlib.sha256(json.dumps(request.model_dump(), sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            progress = self.store.get('requests', request.user_id, request.request_id)
            if progress and progress['fingerprint'] != fingerprint:
                raise DuplicateRequestConflict('request_id_payload_conflict')
            if progress and progress.get('pipeline') != PIPELINE:
                raise ProviderUnavailable('request_pipeline_requires_source_replay')
            if progress and progress['status'] == 'complete':
                return AddResponse(request_id=request.request_id, user_id=request.user_id, session_id=request.session_id)
            if not progress:
                created = utcnow()
                sources = [Source(id=stable_id('src_', request.user_id, request.request_id, i), owner=request.user_id,
                    session_id=request.session_id, request_id=request.request_id, ordinal=i, role=m.role, content=m.content,
                    message_at=message_time(m.timestamp), created_at=created) for i, m in enumerate(request.messages)]
                progress = {'fingerprint': fingerprint, 'pipeline': PIPELINE, 'status': 'pending',
                            'source_ids': [s.id for s in sources], 'units': {}, 'created_at': created}
                with self.store.transaction():
                    for source in sources:
                        self.store.put('sources', source.owner, source.id, source.model_dump())
                    self.store.put('requests', request.user_id, request.request_id, progress)
            def save():
                self.store.put('requests', request.user_id, request.request_id, progress)
            sources = self.store.sources(request.user_id, progress['source_ids'])
            for number, unit in enumerate(self.engine._units(sources)):
                user = next((s for s in unit if s.role == 'user'), None)
                blocks = list(self.chunks(user.content)) if user else [(0, 0, '')]
                for part, (start, stop, text) in enumerate(blocks):
                    key = str(number) if len(blocks) == 1 else f'{number}.{part}'
                    cache = progress['units'].setdefault(key, {'phase': 'pending', 'source_start': start, 'source_stop': stop})
                    if cache['phase'] == 'indexed':
                        continue
                    state, memories, objects, edges, expected, pending_candidates = None, [], [], [], {}, []
                    if monotonic() >= deadline:
                        raise ProviderUnavailable('add_deadline')
                    if user and self.engine.model:
                        state = self.engine._add_state(user, unit, sources, deadline)
                        state['new_user_source']['content'] = text
                        state['source_window'] = {'start': start, 'stop': stop}
                        proposal = self._proposal_stage(state, cache, save, deadline)
                        if proposal.items:
                            if 'maintenance_context' not in cache:
                                context, bindings = self._recall(state, proposal, deadline)
                                cache.update(maintenance_context=context, bindings=bindings)
                                save()
                            context, bindings = cache['maintenance_context'], cache['bindings']
                            decisions = self._maintenance_stage(context, cache, save, deadline)
                            try:
                                objects, memories, edges, expected = self._materialize(context, bindings, decisions)
                            except HistoryChanged:
                                self._invalidate_maintenance(cache)
                                save()
                                raise
                            pending_ids = {d.candidate_id for d in decisions if d.operation == 'pending'}
                            pending_candidates = [c for c in proposal.items if c.id in pending_ids]
                    documents = self.engine._documents(unit if part == 0 else [], memories, objects,
                                                       pending_candidates=pending_candidates, owner=request.user_id)
                    pending_docs = []
                    for document in documents:
                        previous = self.store.get('search_documents', request.user_id, document.id)
                        if previous and previous['text'] == document.text and (previous.get('vector') is not None or not self.engine.embedder):
                            continue
                        pending_docs.append(document)
                    if pending_docs and self.engine.embedder:
                        vectors = self.engine._embed([d.text for d in pending_docs], deadline)
                        for doc, vector in zip(pending_docs, vectors):
                            doc.vector = vector
                    try:
                        with self.store.transaction():
                            for target, original_record in expected.items():
                                if self.store.get('memories', request.user_id, target) != original_record:
                                    self._invalidate_maintenance(cache)
                                    raise HistoryChanged('maintenance_history_changed_retry')
                            for raw in objects:
                                current = self.store.get('objects', request.user_id, raw['id'])
                                if current:
                                    obj = GraphObject.model_validate(current)
                                    known = {(a.name,a.source_id) for a in obj.aliases}
                                    obj.aliases.extend(a for a in GraphObject.model_validate(raw).aliases if (a.name,a.source_id) not in known)
                                    obj.observed_at = min([obj.observed_at,raw['observed_at']],key=parse_time)
                                    raw = obj.model_dump()
                                self.store.put('objects', request.user_id, raw['id'], raw)
                            for memory in memories:
                                self.store.put('memories', memory.owner, memory.id, memory.model_dump())
                            for edge in edges:
                                self.store.put('edges', edge.owner, edge.id, edge.model_dump())
                            for document in pending_docs:
                                self.store.put('search_documents', document.owner, document.id, document.model_dump())
                            cache.update(phase='indexed', memory_ids=[m.id for m in memories])
                            save()
                    except ProviderUnavailable:
                        save()
                        raise
            progress.update(status='complete', completed_at=utcnow())
            save()
        return AddResponse(request_id=request.request_id, user_id=request.user_id, session_id=request.session_id)
