"""V49 subject-first memory maintenance with names and three broad subject kinds.

The production entry point remains unchanged. Shared memory/date/retrieval algorithms are reused.
"""
from __future__ import annotations
from copy import deepcopy
import hashlib,json
from time import monotonic
from pydantic import ValidationError
from remi.engine import MemoryEngine,DuplicateRequestConflict,source_visible
from remi.storage import MemoryStore
from remi.models import AddRequest,AddResponse,Source,Candidate,ProposalDraft,Memory,Edge
from remi.identity import stable_id
from remi.clock import utcnow,message_time,parse_time
from remi.heat import visible_memory
from remi.add_pipeline import AddPipeline,PlanIssue
from remi.providers import ProviderUnavailable,OutputInvalid
from remi.graph import active_edges, active_connections
from remi.retrieval import clip_bytes
from experiments.subject_recall import prepare_candidates, pack_history, subject_stage_state
from experiments.subject_identity import CONTEXT_LIMIT,SubjectRecord,visible_subject,matching_subjects
from experiments.subject_maintenance import review
from experiments.subject_repair import build_repair_state,finish_repair
from experiments.event_maintenance import validate_plan, candidates, materialize
from experiments.event_recall import recall_memories

PIPELINE='G11-memory-identity-gate-v52'


class SubjectMemoryStore(MemoryStore):
    """Memory revision projections use the existing logical edges collection."""
    def edges_for(self,owner,origins=None,targets=None,limit=None):
        return [Edge.model_validate(row) for row in self.list('edges',owner) if 'relation' in row and
            (origins is None or row['origin'] in origins) and (targets is None or row['target'] in targets)][:limit]

    def snapshot(self):
        return {table:[deepcopy(row) for row in records.values()] for table,records in self.records.items()}

    def restore(self,snapshot):
        if set(snapshot)!=set(self.records):raise ValueError('snapshot_requires_existing_six_collections')
        with self.transaction():
            for table,rows in snapshot.items():
                for row in rows:
                    self.put(table,row['owner'],row.get('id') or row['request_id'],row)


def shared_recall(engine,state,deadline):
    """Subject candidates and their identity evidence; memories follow identity review."""
    return prepare_candidates(engine.store,state)


class SubjectGraphEngine(MemoryEngine):
    def __init__(self,store=None,*args,**kwargs):super().__init__(store or SubjectMemoryStore(),*args,**kwargs)

    def _add_state(self,source,unit,sources,deadline):
        """Use the latest preceding message from the same conversation."""
        previous=self.store.context_before(source.owner,source.session_id,source,limit=CONTEXT_LIMIT)
        return dict(owner=source.owner,new_user_source=dict(id=source.id,role=source.role,content=source.content,
            message_at=source.message_at,created_at=source.created_at),context_sources=[dict(id=item.id,role=item.role,
            content=clip_bytes(item.content,4000),message_at=item.message_at,created_at=item.created_at)
            for item in reversed(previous)])

    def _graph_candidates(self,owner,query,seed_documents,documents,vector,as_of):
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

    def _identity_stage(self,cache,state,save,deadline):
        if 'identity' in cache:return cache['identity']
        outputs=cache.setdefault('outputs',{})
        if 'subjects' not in outputs:
            try:outputs['subjects']=self.model.maintain_subjects(subject_stage_state(state),deadline=deadline)
            except OutputInvalid:outputs['subjects']={'output_error':'model_json_invalid'}
            save()
        result=review(outputs['subjects'],state,self.store)
        if result['issues'] and not cache.get('repair_used'):
            cache.update(repair_used=True,repair_stage='subjects',
                subject_repair=build_repair_state(outputs['subjects'],result,state));save()
        if cache.get('repair_stage')=='subjects' and 'subject_repair' in cache:
            if 'subjects_repair' not in outputs:
                try:outputs['subjects_repair']=self.model.maintain_subjects(subject_stage_state(cache['subject_repair']),deadline=deadline)
                except OutputInvalid:outputs['subjects_repair']={'output_error':'model_json_invalid'}
                save()
            result=finish_repair(cache['subject_repair'],outputs['subjects_repair'],state,self.store)
        cache['identity']=result
        cache['expected_objects']={obj['id']:self.store.get('objects',state['owner'],obj['id'])
                                   for obj in result['bindings']['entity_rows']};save()
        return result

    def _memory_after_identity(self,cache,result,save,deadline):
        if 'items' in cache:return
        state=result['state'];bindings=result['bindings']
        view=[dict(ref=s['ref'],name=s['name'],kind=s['kind'],is_user=s.get('is_user',False),
            aliases=s['aliases'],
            id=bindings['subject_ids'].get(str(s['ref'])),
            current_evidence_id=s['current_evidence_id'],
            status='resolved' if str(s['ref']) in bindings['subject_ids'] else 'pending')
            for s in state['proposal']['subjects'] if s.get('current_reference_valid')]
        cache['current_participants']=deepcopy(view)
        outputs=cache.setdefault('outputs',{})
        extraction_issues=cache.setdefault('candidate_extraction_issues',{})
        extraction_payload={**state,'resolved_subjects':view,'history':[]}
        def extract(key,packet):
            try:outputs[key]=self.model.extract_memories(packet,deadline=deadline)
            except OutputInvalid as error:
                outputs[key]=getattr(error,'raw_output',None) or {'output_error':str(error)}
                extraction_issues[key]=getattr(error,'issues',[])
            save()
        if 'extract' not in outputs:extract('extract',extraction_payload)
        candidate_plan=outputs['extract'];candidate_errors=extraction_issues.get('extract',[])
        if candidate_errors and 'extract_repair' not in outputs:
            extract('extract_repair',{**extraction_payload,'repair_errors':candidate_errors,'previous_output':candidate_plan})
        if candidate_errors:candidate_plan=outputs.get('extract_repair',candidate_plan)
        try:candidate_plan=ProposalDraft.model_validate(candidate_plan).model_dump()
        except ValidationError:candidate_plan=dict(items=[],reason='candidate_extraction_unresolved')
        state={**state,'memory_candidates':[dict(ref='C'+str(index+1),**item)
            for index,item in enumerate(candidate_plan['items'])],'candidate_reason':candidate_plan['reason']}
        cache['memory_candidates']=deepcopy(state['memory_candidates'])
        if 'memory_recall' not in cache:
            recalled, trace = recall_memories(self, state, bindings, view)
            cache.update(memory_recall=recalled, memory_recall_trace=trace)
            save()
        state=cache['memory_recall'];result['state']=state
        payload={**state,'resolved_subjects':view}
        protocol_issues=cache.setdefault('extraction_protocol_issues',{})
        def call(key,packet):
            try:outputs[key]=self.model.propose(packet,deadline=deadline)
            except OutputInvalid as error:
                outputs[key]=getattr(error,'raw_output',None) or {'output_error':str(error)}
                protocol_issues[key]=getattr(error,'issues',[])
            save()
        if 'propose' not in outputs:call('propose',payload)
        accepted,errors=validate_plan(outputs['propose'],state,view)
        errors=protocol_issues.get('propose') or errors
        cache['memory_initial_issues']=deepcopy(errors)
        if errors and not cache.get('repair_used'):
            cache.update(repair_used=True,repair_stage='propose');save()
        if errors and cache.get('repair_stage')=='propose':
            if 'propose_repair' not in outputs:
                call('propose_repair',{**payload,'repair_errors':errors,'previous_output':outputs['propose']})
            fixed,remaining=validate_plan(outputs['propose_repair'],state,view,protected=accepted)
            # Accepted slots own their event targets throughout repair.
            protected_targets={row['target_id'] for row in accepted.values() if row['operation']=='update'}
            errors=list(remaining)
            for index,row in fixed.items():
                if row['operation']=='update' and row['target_id'] in protected_targets:
                    row={**row,'operation':'pending','pending_reason':'repair_conflicts_with_accepted_event_update'}
                    errors.append(dict(path=['items',int(index),'target_id'],code='repair_conflicts_with_accepted_event_update'))
                accepted[index]=row
        cache.update(memory_plan=accepted,items=candidates(accepted,state))
        final_output=outputs.get('propose_repair') if cache.get('repair_used') else outputs.get('propose')
        rows=final_output.get('items',[]) if isinstance(final_output,dict) and isinstance(final_output.get('items'),list) else []
        accepted_refs={row['ref'] for row in accepted.values()}
        validation_dispositions=[]
        for candidate in state['memory_candidates']:
            if candidate['ref'] in accepted_refs:continue
            positions=[index for index,row in enumerate(rows)
                       if isinstance(row,dict) and row.get('ref')==candidate['ref']]
            codes=list(dict.fromkeys(issue['code'] for issue in errors
                if not positions or any(issue.get('path',[])[:2]==['items',index] for index in positions)))
            if not positions:codes=['candidate_omitted_by_model',*codes]
            validation_dispositions.append(dict(candidate_id=candidate['ref'],
                item_index=positions[0] if positions else None,status='validation_failed',operation='validation_failed',
                reason_codes=list(dict.fromkeys(codes)) or ['candidate_decision_invalid']))
        cache['memory_validation_dispositions']=validation_dispositions
        if errors:cache['memory_rejections']=errors
        else:cache.pop('memory_rejections',None)
        save()

    def add(self,request):
        return self._add(request)

    def _add(self,request,*,recovery=None,deadline=None):
        request=AddRequest.model_validate(request);deadline=deadline or monotonic()+self.settings.add_timeout
        pipeline=AddPipeline(self);owner=request.user_id
        with self._scope(owner,deadline):
            if recovery and self.store.get('requests',owner,recovery['key']):
                return AddResponse(request_id=request.request_id,user_id=owner,session_id=request.session_id)
            fingerprint=hashlib.sha256(json.dumps(request.model_dump(),sort_keys=True,ensure_ascii=False).encode()).hexdigest()
            progress=self.store.get('requests',owner,request.request_id)
            if progress and progress.get('fingerprint')!=fingerprint:raise DuplicateRequestConflict('request_id_payload_conflict')
            if progress and progress['pipeline']!=PIPELINE:raise ProviderUnavailable('request_pipeline_requires_source_replay')
            if progress and progress['status']=='complete':return AddResponse(request_id=request.request_id,user_id=owner,session_id=request.session_id)
            if progress is None:
                created=utcnow();sources=[Source(id=stable_id('src_',owner,request.request_id,i),owner=owner,session_id=request.session_id,
                    request_id=request.request_id,ordinal=i,role=m.role,content=m.content,message_at=message_time(m.timestamp),created_at=created) for i,m in enumerate(request.messages)]
                if recovery:sources=[Source.model_validate(recovery['source'])]
                progress=dict(request_id=request.request_id,owner=owner,fingerprint=fingerprint,pipeline=PIPELINE,status='pending',source_ids=[s.id for s in sources],units={},created_at=created,recovery=recovery)
                with self.store.transaction():
                    for source in sources:self.store.put('sources',owner,source.id,source.model_dump())
                    self.store.put('requests',owner,request.request_id,progress)
            def save():self.store.put('requests',owner,request.request_id,progress)
            sources=self.store.sources(owner,progress['source_ids'])
            for number,unit in enumerate(self._units(sources)):
                user=next((s for s in unit if s.role=='user'),None)
                blocks=list(pipeline.chunks(user.content)) if user else [(0,0,'')]
                if recovery:
                    window=recovery.get('source_window') or dict(start=0,stop=len(user.content))
                    blocks=[(window['start'],window['stop'],user.content[window['start']:window['stop']])]
                for part,(start,stop,text) in enumerate(blocks):
                    key=str(number) if len(blocks)==1 else f'{number}.{part}'
                    cache=progress['units'].setdefault(key,dict(phase='pending',source_start=start,source_stop=stop))
                    if cache['phase']=='indexed':continue
                    if monotonic()>=deadline:raise ProviderUnavailable('add_deadline')
                    objects=[];memories=[];edges=[];pending=[]
                    if user and self.model:
                        if 'shared_recall' not in cache:
                            state=self._add_state(user,unit,sources,deadline);state['new_user_source']['content']=text
                            state.update(source_window=dict(start=start,stop=stop),pipeline=PIPELINE)
                            if recovery:
                                clarification=recovery['clarification']
                                state.update(recovery={k:recovery[k] for k in ('key','content','reason','claim_type')},
                                    recovery_clock=clarification.get('message_at') or clarification['created_at'],
                                    context_sources=[clarification])
                            shared,trace=shared_recall(self,state,deadline);cache.update(shared_recall=shared,recall_trace=trace);save()
                        state=cache['shared_recall']
                        result=self._identity_stage(cache,state,save,deadline)
                        self._memory_after_identity(cache,result,save,deadline)
                        state=deepcopy(cache['memory_recall']);bindings=result['bindings']
                        try:
                            objects,memories,edges,expected,pending,dispositions=materialize(self.store,state,bindings,cache['memory_plan'])
                        except ProviderUnavailable as error:
                            if str(error)=='event_history_changed_retry':
                                retries=cache.get('concurrent_recomputations',0)+1
                                cache.clear();cache.update(phase='pending',source_start=start,source_stop=stop,concurrent_recomputations=retries);save()
                            raise
                        cache['candidate_dispositions']=[*dispositions,*cache.get('memory_validation_dispositions',[])]
                        cache['expected_memories']=expected
                        for obj in objects:SubjectRecord.model_validate(obj)
                        visible_ids={*bindings['subject_ids'].values(),*[row['id'] for row in state.get('subject_catalog',[])]}
                        for memory in memories:
                            if not set(memory.revisions[-1].entity_ids)<=visible_ids:raise ProviderUnavailable('memory_subject_integrity_failed')
                        cache['user_binding_checks']=[dict(memory_id=m.id,passed=(stable_id('obj_',owner,'user') in m.revisions[-1].entity_ids)==(m.revisions[-1].user_involvement=='involved')) for m in memories]
                    if recovery and len(cache.get('candidate_dispositions',[]))==1:
                        decision=cache['candidate_dispositions'][0]
                        if decision['status']=='accepted' and decision.get('memory_id'):
                            cache['resolved_pending']=dict(key=recovery['key'],memory_id=decision['memory_id'],
                                evidence_source_ids=[recovery['source']['id'],recovery['clarification']['id']])
                    documents=self._documents(unit if part==0 and not recovery else [],memories,objects,
                        pending_candidates=pending if not recovery else [],owner=owner)
                    changed=[d for d in documents if (old:=self.store.get('search_documents',owner,d.id)) is None or old['text']!=d.text or self.embedder and old.get('vector') is None]
                    if changed and self.embedder:
                        vectors=self._embed([d.text for d in changed],deadline)
                        for document,vector in zip(changed,vectors):document.vector=vector
                    try:
                        with self.store.transaction():
                            if recovery and self.store.get('requests',owner,recovery['key']):
                                raise ProviderUnavailable('pending_resolution_changed_retry')
                            for oid,previous in cache.get('expected_objects',{}).items():
                                if self.store.get('objects',owner,oid)!=previous:raise ProviderUnavailable('subject_history_changed_retry')
                            for mid,previous in cache.get('expected_memories',{}).items():
                                if self.store.get('memories',owner,mid)!=previous:raise ProviderUnavailable('event_history_changed_retry')
                            for obj in objects:self.store.put('objects',owner,obj['id'],obj)
                            for memory in memories:self.store.put('memories',owner,memory.id,memory.model_dump())
                            # Replayed projections replace edges for touched memory revisions atomically.
                            touched={m.id for m in memories}
                            for old_edge in self.store.edges_for(owner):
                                if old_edge.memory_id in touched:self.store.delete('edges',owner,old_edge.id)
                            for edge in edges:self.store.put('edges',owner,edge.id,edge.model_dump())
                            for document in changed:self.store.put('search_documents',owner,document.id,document.model_dump())
                            if recovery and cache.get('resolved_pending'):
                                self.store.delete('search_documents',owner,recovery['document_id'])
                                self.store.put('requests',owner,recovery['key'],dict(request_id=recovery['key'],owner=owner,
                                    status='resolved',pipeline=PIPELINE,**cache['resolved_pending']))
                            cache.update(phase='indexed',memory_ids=[m.id for m in memories]);save()
                    except ProviderUnavailable as error:
                        if str(error) in {'subject_history_changed_retry','event_history_changed_retry'}:
                            retries=cache.get('concurrent_recomputations',0)+1
                            cache.clear();cache.update(phase='pending',source_start=start,source_stop=stop,concurrent_recomputations=retries);save()
                        raise
            if not recovery:
                from experiments.pending_recovery import pending_jobs
                from remi.models import AddMessage
                clarification=next((source for source in reversed(sources) if source.role=='user'),None)
                if clarification:
                    if 'pending_recoveries' not in progress:
                        progress['pending_recoveries']=pending_jobs(self.store,owner,clarification);save()
                    for job in progress['pending_recoveries']:
                        child=AddRequest(user_id=owner,session_id=job['source']['session_id'],
                            request_id=stable_id('recovery_',request.request_id,job['key']),
                            messages=[AddMessage(role='user',content=job['source']['content'])])
                        self._add(child,recovery=job,deadline=deadline)
            progress.update(status='complete',completed_at=utcnow());save()
        return AddResponse(request_id=request.request_id,user_id=owner,session_id=request.session_id)
