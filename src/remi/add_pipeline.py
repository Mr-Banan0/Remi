"""Two model stages: subject maintenance, then typed memory extraction and validated append."""
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
from .identity import normalized_name, source_name_present, stable_id
from .models import AddResponse, Candidate, CardDecision, ConnectionChangeDraft, ConnectionDraft, EvidenceRef, GraphObject, Memory, ProposalPlan, Revision, Source, SubjectDecision
from .providers import OutputInvalid, ProviderUnavailable
from .add_recall import recall_candidates
from .subjects import decode_subjects, materialize_subjects

PIPELINE = 'G11-readable-subjects-v32'


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
        from .models import ProposalDraft
        plan=ProposalDraft.model_validate(raw)
        sources=self._sources(state);primary=state['new_user_source']['id']
        subjects=state.get('proposal',{}).get('subjects',[])
        refs={s['ref'] for s in subjects};items=[]
        for index,item in enumerate(plan.items):
            ids=[e.source_id for e in item.evidence]
            if any(sid not in sources for sid in ids):raise PlanIssue('candidate_source_requires_supplied_original')
            if primary not in ids:
                dispositions.append({'candidate_id':str(index),'status':'context_only','reason':'context_interprets_current_input'});continue
            if not set(item.subject_refs)<=refs:raise PlanIssue('memory_candidate_requires_subject_view_refs')
            items.append(Candidate(id=str(index),content=item.content,subject_refs=list(dict.fromkeys(item.subject_refs)),
                connection=None,record_type=item.record_type,mode=item.mode,context=item.context,
                status=item.status,time_expression=item.time_expression,evidence=[EvidenceRef(source_id=sid,quote=sources[sid]['content']) for sid in dict.fromkeys(ids)]))
        return ProposalPlan(subjects=subjects,items=items,reason=plan.reason)

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

    def _shared_recall(self, state, deadline):
        return recall_candidates(self.engine, state, deadline)

    @staticmethod
    def _write_context(state, bindings):
        """Attach program validation results to the same recalled source snapshot."""
        return {**state, 'identity_issues': bindings['identity_dispositions']}

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

    def _stage_call(self, name, payload, cache, save, deadline):
        outputs=cache.setdefault('outputs',{})
        if name not in outputs:
            method=self.engine.model.maintain_subjects
            try: outputs[name]=method(payload,deadline=deadline)
            except OutputInvalid: outputs[name]={'output_error':'model_output_invalid'}
            except ProviderUnavailable:
                cache['last_failure']={'stage':name,'reason':'provider_unavailable'};save();raise
            save()
        return outputs[name]

    def _connections(self, raw, state, bindings):
        good,errors={},{}
        rows=raw.get('connections',[]) if isinstance(raw,dict) and isinstance(raw.get('connections'),list) else []
        catalog={r['id']:r for r in state.get('relation_catalog',[])}
        subjects={s['ref'] for s in state['proposal']['subjects']}
        counts=Counter(r.get('ref') for r in rows if isinstance(r,dict) and type(r.get('ref')) is int)
        candidates=[]
        for index,row in enumerate(rows):
            try:
                change=ConnectionChangeDraft.model_validate(row)
                if counts[change.ref]!=1:raise PlanIssue('contact_ref_selected_more_than_once')
                if not set(change.subject_refs)<=subjects:raise PlanIssue('contact_requires_declared_subject_refs')
                if change.source_id!=state['new_user_source']['id'] or change.quote not in state['new_user_source']['content']:
                    raise PlanIssue('contact_requires_current_original_excerpt')
                cid='c'+str(change.ref)
                link=ConnectionDraft(subject_refs=change.subject_refs,source_id=change.source_id,quote=change.quote,mode=change.mode)
                candidates.append(Candidate(id=cid,content=change.quote,subject_refs=change.subject_refs,connection=link,
                    evidence=[EvidenceRef(source_id=change.source_id,quote=change.quote)]).model_dump())
                ids=[bindings['subject_ids'].get(str(r)) for r in change.subject_refs]
                if change.operation!='pending' and (None in ids or len(set(ids))!=2):
                    raise PlanIssue('contact_requires_two_distinct_resolved_subjects')
                if change.operation in {'create','pending'}:
                    if change.target_id is not None or change.previous_quote is not None:raise PlanIssue('new_or_pending_contact_has_no_old_target')
                else:
                    target=catalog.get(change.target_id)
                    if target is None or sorted(ids)!=target['endpoint_ids'] or change.mode!=target['mode']:
                        raise PlanIssue('contact_update_requires_same_owned_endpoints_and_mode')
                    if ids!=target['ordered_subject_ids']:raise PlanIssue('contact_update_preserves_participant_direction')
                    if change.previous_quote!=target['content']:raise PlanIssue('contact_change_anchors_previous_proposition')
                good[cid]={'candidate_id':cid,'operation':change.operation,'target_id':change.target_id}
            except ValueError as error:errors[str(index)]=self._error(error)
        state['proposal']['items']=candidates
        return good,errors

    def _identity_stage(self, state, cache, save, deadline):
        if 'identity_plan' in cache:return cache['identity_context'],cache['bindings']
        raw=self._stage_call('subjects',state,cache,save,deadline)
        def review(output):
            reviewed=deepcopy(state)
            subjects,errors=decode_subjects(output,reviewed,self.store)
            bindings=materialize_subjects(reviewed,subjects,self.store)
            links,link_errors=self._connections(output,reviewed,bindings)
            return reviewed,subjects,bindings,links,errors,link_errors
        reviewed,subjects,bindings,links,errors,link_errors=review(raw)
        validation=[{'stage':'subjects','ref':r,'error':e} for r,e in errors.items()]+[
            {'stage':'connections','ref':r,'error':e} for r,e in link_errors.items()]
        cache['validation']=validation
        if validation and (not cache.get('repair_used') or cache.get('repair_stage')=='subjects'):
            cache.update(repair_used=True,repair_stage='subjects');save()
            accepted={r for r,d in subjects.items() if r not in errors and d.target_id is not None}
            def rows(key):
                value=raw.get(key,[]) if isinstance(raw,dict) else []
                return value if isinstance(value,list) else []
            def selected(row,refs):
                return isinstance(row,dict) and type(row.get('ref')) is int and row['ref'] in refs
            accepted_links={int(cid[1:]) for cid in links}
            frozen={'subjects':[row for row in rows('subjects') if selected(row,accepted)],
                'connections':[row for row in rows('connections') if selected(row,accepted_links)]}
            repair={**state,'accepted_subjects':frozen['subjects'],'accepted_connections':frozen['connections'],
                'rejected_subjects':[row for row in rows('subjects') if not selected(row,accepted)],
                'rejected_connections':[row for row in rows('connections') if not selected(row,accepted_links)],
                'repair_errors':validation}
            fixed=self._stage_call('subjects_repair',repair,cache,save,deadline)
            if isinstance(fixed,dict) and set(fixed)==set(frozen) and all(isinstance(fixed.get(k),list) for k in frozen):
                merged={key:frozen[key]+[row for row in fixed[key] if not (isinstance(row,dict) and
                    type(row.get('ref')) is int and row['ref'] in (accepted_links if key=='connections' else accepted))] for key in frozen}
                reviewed,subjects,bindings,links,errors,link_errors=review(merged)
        if errors and not subjects and any(type(ref) is not int or ref<0 for ref in errors):
            cache['last_failure']={'stage':'subjects','reason':'subject_output_invalid'};save()
            raise ProviderUnavailable('subject_output_invalid')
        for candidate in reviewed['proposal']['items']:
            links.setdefault(candidate['id'],{'candidate_id':candidate['id'],'operation':'pending','target_id':None})
        cache.update(identity_plan=True,identity_context=reviewed,bindings=bindings,subject_alias_issues=reviewed['subject_alias_issues'],
            connection_changes=list(links.values()),
            subject_dispositions=[d.model_dump() for d in subjects.values()],
            connection_dispositions=[{'candidate_id':cid,'status':change['operation']} for cid,change in links.items()],
            identity_errors=[{'ref':r,'error':e} for r,e in errors.items()]+[{'connection':r,'error':e} for r,e in link_errors.items()])
        cache.pop('last_failure',None);save()
        return reviewed,bindings

    def _write_plan(self, state, cache, save):
        """Validate extracted records and compile writes without a model review."""
        if 'write_plan' in cache:
            return [CardDecision.model_validate(x) for x in cache['write_plan']['decisions']]
        good={}
        unresolved={d['candidate_id'] for d in state.get('identity_issues',[])}
        for raw in state['proposal']['items']:
            c=Candidate.model_validate(raw)
            if c.connection is not None:
                continue
            reason=None
            if c.id in unresolved:
                reason='candidate_identity_unresolved'
            elif c.record_type=='event' and c.status is None:
                reason='event_creation_requires_status'
            elif c.record_type!='event' and (c.status is not None or c.time_expression is not None):
                reason='statements_preserve_time_scope_in_content'
            elif c.time_expression and not self._date(c.time_expression,c,state):
                reason='event_date_requires_source_support'
            if reason:
                good[c.id]=CardDecision(candidate_id=c.id,operation='pending',reason=reason)
                cache.setdefault('validation',[]).append({'stage':'write_validation','candidate_id':c.id,'error':reason})
            else:
                good[c.id]=CardDecision(candidate_id=c.id,operation='create',record_type=c.record_type,
                    content=c.content,subject_refs=c.subject_refs,mode=c.mode,context=c.context,
                    status=c.status,time_expression=c.time_expression,reason='validated_extracted_memory')
        by_id={c['id']:c for c in state['proposal']['items']}
        for change in cache.get('connection_changes',[]):
            c=by_id[change['candidate_id']];link=c['connection']
            if change['operation'] in {'keep','pending'}:
                good[c['id']]=CardDecision(candidate_id=c['id'],operation=change['operation'],target_id=change['target_id'],reason='explicit_contact_disposition')
                continue
            good[c['id']]=CardDecision(candidate_id=c['id'],operation='create' if change['operation']=='create' else 'patch_relation',
                target_id=change['target_id'],record_type='relation',content=link['quote'],
                subject_refs=link['subject_refs'],mode=link['mode'],context='source_reported',reason='explicit_candidate_contact_patch')
        relation_targets=Counter(d.target_id for d in good.values() if d.operation=='patch_relation')
        for cid,d in list(good.items()):
            if d.operation=='patch_relation' and relation_targets[d.target_id]>1:
                good[cid]=CardDecision(candidate_id=cid,operation='pending',reason='conflicting_contact_updates_require_clarification')
        ordered=[good[c['id']] for c in state['proposal']['items']]
        cache['write_plan']={'decisions':[d.model_dump() for d in ordered]}
        cache['candidate_dispositions']=[{'candidate_id':d.candidate_id,'status':'pending' if d.operation=='pending' else 'accepted',
            'operation':d.operation,'reason':d.reason} for d in ordered]
        cache.pop('last_failure',None);save()
        return ordered

    def _materialize(self, state, bindings, decisions):
        owner = state['owner']
        primary = state['new_user_source']
        observed = primary['message_at'] or primary['created_at']
        candidates = {c['id']: Candidate.model_validate(c) for c in state['proposal']['items']}
        memories, edges, expected = {}, [], {}
        entity_rows = {obj['id']: obj for obj in bindings['entity_rows']}
        user_id = stable_id('obj_', owner, 'user')
        relation_groups = {}
        for decision in decisions:
            candidate = candidates[decision.candidate_id]
            if decision.operation in {'keep', 'pending'}:
                continue
            evidence = list(candidate.evidence)
            if decision.operation == 'patch_relation':
                relation_groups.setdefault(decision.target_id, []).append(decision)
                continue
            mid = stable_id('mem_', owner, primary['id'], state.get('source_window'), candidate.id)
            entity_ids = list(dict.fromkeys(bindings['subject_ids'][str(r)] for r in decision.subject_refs))
            stamp = self._date(decision.time_expression, candidate, state)
            status = decision.status if decision.record_type == 'event' else 'stated'
            revision = Revision(id=stable_id('rev_', mid, 'create'),
                content=absolute_content(decision.content, '\n'.join(e.quote for e in evidence), primary['message_at'] or primary['created_at']),
                source_ids=[e.source_id for e in evidence], quote=evidence[0].quote, evidence=evidence, entity_ids=entity_ids,
                user_involvement='involved' if user_id in entity_ids else 'not_involved',
                mode=decision.mode, context=decision.context or "source_reported", status=status,
                event_at=stamp if status == 'occurred' else None,
                planned_at=stamp if status in {'planned', 'desired', 'cancelled'} else None,
                observed_at=observed, created_at=utcnow(), time_resolution='rule' if stamp else 'unknown',
                time_precision='day' if stamp else 'unknown', operation='create')
            connection = candidate.connection if decision.record_type=='relation' else None
            if connection:revision.quote=connection.quote
            endpoints = sorted(bindings['subject_ids'][str(ref)] for ref in connection.subject_refs) if connection else []
            memory = Memory(id=mid, owner=owner, record_type=decision.record_type, revisions=[revision],
                endpoint_ids=endpoints,connection_evidence=EvidenceRef(source_id=connection.source_id,quote=connection.quote) if connection else None)
            memories[mid] = memory
            expected[mid] = None
            edges.extend(project_edges(memory, revision))
        for mid,group in relation_groups.items():
            original=Memory.model_validate(self.store.get('memories',owner,mid))
            previous=visible_memory(original,observed).current
            recalled=next(r for r in state['relation_catalog'] if r['id']==mid)
            if previous.id!=recalled['revision_id']:raise HistoryChanged('contact_history_changed_retry')
            if len(group)!=1:raise PlanIssue('one_contact_update_per_target')
            decision=group[0];candidate=candidates[decision.candidate_id];link=candidate.connection
            evidence=list({e.source_id:e for e in [*candidate.evidence,*previous.evidence]}.values())
            revision=previous.model_copy(update={'id':stable_id('rev_',mid,primary['id'],state.get('source_window'),'contact_update'),
                'content':link.quote,'source_ids':[e.source_id for e in evidence], 'evidence':evidence,'quote':link.quote,
                'observed_at':observed,'created_at':utcnow(),'operation':'update', 'replaces_revision_id':previous.id})
            original.revisions.append(revision)
            current=original.current
            original.connection_evidence=EvidenceRef(source_id=current.source_ids[0],quote=current.quote)
            memories[mid]=Memory.model_validate(original.model_dump());expected[mid]=self.store.get('memories',owner,mid)
            edges.extend(project_edges(original,revision,previous.id))
        objects = list(entity_rows.values())
        return objects, list(memories.values()), edges, expected

    def _user_binding_checks(self, state, objects, memories, edges):
        """Verify each projected revision before embedding or the atomic write."""
        owner = state['owner']
        user_id = stable_id('obj_', owner, 'user')
        rows = {obj['id']: obj for obj in objects}
        checks = []
        for raw in objects:
            obj=GraphObject.model_validate(raw)
            valid=obj.owner==owner
            for alias in obj.aliases:
                source=self.store.get('sources',owner,alias.source_id)
                valid=valid and source is not None and alias.quote in source['content'] and source_name_present(normalized_name(alias.name),normalized_name(alias.quote))
            checks.append({'subject_id':obj.id,'aliases_valid':bool(valid),'passed':bool(valid)})
        for memory in memories:
            try:
                Memory.model_validate(memory.model_dump())
                record_valid = memory.owner == owner
            except ValidationError:
                record_valid = False
            for revision in memory.revisions[-1:]:
                projected = [e for e in edges if e.revision_id == revision.id and e.relation == 'mentions']
                # Materialization appends exactly one revision to each changed card.
                targets = [e.target for e in projected]
                valid = (record_valid and set(targets) == set(revision.entity_ids)
                    and len(targets) == len(set(targets)) and len(revision.entity_ids) == len(set(revision.entity_ids))
                    and all(e.owner == owner and e.origin == memory.id and e.memory_id == memory.id for e in projected))
                for eid in revision.entity_ids:
                    raw = rows.get(eid) or self.store.get('objects', owner, eid)
                    if raw is None:
                        valid = False
                        continue
                    try:
                        obj = GraphObject.model_validate(raw)
                        valid = valid and obj.id == eid and obj.owner == owner and obj.is_user == (eid == user_id)
                    except ValidationError:
                        valid = False
                connections=[e for e in edges if e.revision_id==revision.id and e.relation=='related']
                valid = valid and (len(connections)==(1 if memory.endpoint_ids else 0))
                if connections:
                    e=connections[0]
                    valid = valid and sorted([e.origin,e.target])==sorted(memory.endpoint_ids) and e.owner==owner and e.memory_id==memory.id and e.source_id==revision.source_ids[0] and revision.quote in self.store.get('sources',owner,e.source_id)['content']
                checks.append({'memory_id': memory.id, 'revision_id': revision.id,
                    'user_involvement': revision.user_involvement, 'user_bound': user_id in targets, 'passed': bool(valid)})
        return checks

    @staticmethod
    def _invalidate_plan(cache):
        cache.setdefault('history_conflicts', []).append({
            'candidate_dispositions':cache.get('candidate_dispositions', []),
            'recalled_versions':{h['id']:h['revision_id'] for h in cache.get('write_context', {}).get('history', [])}})
        for name in ['propose','write_context','shared_recall','recall_trace','identity_plan','identity_context','bindings','write_plan','repair_stage',
                     'user_binding_checks','subject_dispositions','subject_alias_issues','connection_changes']:
            cache.pop(name, None)
        for name in ['propose','propose_repair','subjects','subjects_repair']:
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
                        state['pipeline']=PIPELINE
                        state['source_window'] = {'start': start, 'stop': stop}
                        if 'shared_recall' not in cache:
                            context,trace=self._shared_recall(state,deadline)
                            cache.update(shared_recall=context,recall_trace=trace);save()
                        identity_context,bindings=self._identity_stage(cache['shared_recall'],cache,save,deadline)
                        candidate_state={**identity_context,'resolved_subjects':[{'ref':sub['ref'],'name':sub['name'],
                            'aliases':sub['aliases'],'kind':sub['kind'],'id':bindings['subject_ids'].get(str(sub['ref'])),'status':'resolved' if str(sub['ref']) in bindings['subject_ids'] else 'pending'}
                            for sub in identity_context['proposal']['subjects']]}
                        proposal=self._proposal_stage(candidate_state,cache,save,deadline)
                        identity_context=deepcopy(identity_context)
                        identity_context['proposal']['items']=[c for c in identity_context['proposal']['items'] if c['connection']] + [c.model_dump() for c in proposal.items]
                        bindings=materialize_subjects(identity_context,{d['subject_ref']:SubjectDecision.model_validate(d)
                            for d in cache['subject_dispositions']},self.store)
                        cache.update(identity_context=identity_context,bindings=bindings)
                        if 'write_context' not in cache:
                            cache['write_context']=self._write_context(identity_context,bindings);save()
                        context=cache['write_context']
                        decisions=self._write_plan(context,cache,save)
                        try:
                            objects, memories, edges, expected = self._materialize(context, bindings, decisions)
                        except HistoryChanged:
                            self._invalidate_plan(cache)
                            save()
                            raise
                        checks = self._user_binding_checks(context, objects, memories, edges)
                        cache['user_binding_checks'] = checks
                        save()
                        if not all(check['passed'] for check in checks):
                            raise ProviderUnavailable('user_binding_integrity_failed')
                        pending_ids = {d.candidate_id for d in decisions if d.operation == 'pending'}
                        pending_candidates = [Candidate.model_validate(c) for c in context['proposal']['items'] if c['id'] in pending_ids]
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
                            for target, original_record in cache.get('bindings',{}).get('expected_objects',{}).items():
                                if self.store.get('objects',request.user_id,target)!=original_record:
                                    self._invalidate_plan(cache)
                                    raise HistoryChanged('subject_changed_retry')
                            for target, original_record in expected.items():
                                if self.store.get('memories', request.user_id, target) != original_record:
                                    self._invalidate_plan(cache)
                                    raise HistoryChanged('write_history_changed_retry')
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
