"""Unified Remi Add pipeline: subject identity, memory candidates and revision maintenance."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from time import monotonic

from pydantic import ValidationError

from .clock import message_time, parse_time, utcnow
from .heat import visible_memory
from .identity import stable_id
from .models import AddRequest, AddResult, Candidate, Edge, Memory, ProposalDraft, Source, SubjectClarification
from .providers import OutputInvalid, ProviderUnavailable
from .retrieval import clip_bytes
from .subject_recall import prepare_candidates, subject_stage_state
from .subject_identity import CONTEXT_LIMIT, SubjectRecord, visible_subject, matching_subjects
from .subject_maintenance import review
from .subject_repair import build_repair_state, finish_repair
from .memory_maintenance import validate_plan, candidates, materialize
from .memory_recall import recall_memories
from .version import PIPELINE


class DuplicateRequestConflict(ValueError):
    pass


class PlanIssue(ValueError):
    def __init__(self, reason, details=None):
        super().__init__(reason)
        self.details = details or {}


def shared_recall(engine, state, deadline):
    """Recall subject identities before the fixed-scope memory stage."""
    return prepare_candidates(engine.store, state)


class AddPipeline:
    def __init__(self, engine):
        self.engine = engine
        self.store = engine.store

    def __getattr__(self, name):
        return getattr(self.engine, name)

    @staticmethod
    def _response(request, progress=None):
        rows=(progress or {}).get('clarifications',[])
        return AddResult(request_id=request.request_id,user_id=request.user_id,session_id=request.session_id,
                         clarifications=[SubjectClarification.model_validate(row) for row in rows])

    @staticmethod
    def _collect_clarifications(progress):
        rows=[];seen=set()
        for key in sorted(progress.get('units',{})):
            for row in progress['units'][key].get('subject_clarifications',[]):
                identity=(row.get('type'),row.get('mention'),tuple(candidate.get('subject_id') for candidate in row.get('candidates',[])))
                if identity not in seen:
                    rows.append(row);seen.add(identity)
        return rows

    def _subject_clarifications(self, cache, result):
        pending_map=result['state'].get('pending_subject_candidates',{})
        pending_refs={int(ref) for ref in pending_map}
        if not pending_refs:return []
        state=result['state'];owner=state['owner'];clock=state['new_user_source'].get('message_at') or state['new_user_source'].get('created_at') or utcnow()
        catalog={row['id']:row for row in state.get('subject_catalog',[])}
        contexts={subject_id:[] for subject_id in catalog}
        for memory in self.store.memories(owner):
            current=visible_memory(memory,clock)
            if current is None:continue
            for subject_id in current.current.entity_ids:
                if subject_id in contexts:
                    contexts[subject_id].append((parse_time(current.current.observed_at),current.current.content))
        clarifications=[]
        for subject in state.get('proposal',{}).get('subjects',[]):
            if subject.get('ref') not in pending_refs or str(subject.get('ref')) in result['bindings']['subject_ids']:continue
            mention=subject.get('name')
            if not isinstance(mention,str) or not mention.strip():continue
            label=mention.strip()
            candidate_ids=pending_map.get(str(subject['ref']),[])
            candidate_rows=[]
            for subject_id in dict.fromkeys(candidate_ids):
                stored=catalog[subject_id];name=stored.get('name') or next((alias.get('name') for alias in stored.get('aliases',[]) if alias.get('name')),label)
                details=sorted(contexts.get(subject_id,[]),reverse=True)
                detail=details[0][1] if details else next((row.get('quote') for row in reversed(stored.get('references',[])) if row.get('quote')),None)
                display=name if not detail else name+' — '+clip_bytes(' '.join(str(detail).split()),240)
                candidate_rows.append(dict(subject_id=subject_id,display=display))
            message=(f'Multiple identities match "{label}". Please confirm the intended subject.' if len(candidate_rows)>1
                     else f'The identity of "{label}" is uncertain. Please confirm the intended subject.')
            clarifications.append(SubjectClarification(mention=label,message=message,candidates=candidate_rows).model_dump())
        return clarifications

    def _disambiguate_pending_subjects(self,cache,result,save,deadline):
        pending=result['state'].get('pending_subject_candidates',{})
        if not pending:return
        state=deepcopy(cache['memory_recall'])
        proposal={str(row['ref']):row for row in result['state']['proposal']['subjects']}
        pending_rows=[]
        for ref,candidate_ids in pending.items():
            subject=proposal.get(str(ref))
            if subject is None:continue
            memories=[dict(id=row['id'],record_type=row['record_type'],content=row['content'],entity_ids=row['entity_ids'])
                      for row in state.get('history',[]) if set(row.get('entity_ids',[])).intersection(candidate_ids)]
            pending_rows.append(dict(ref=int(ref),name=subject.get('name'),kind=subject.get('kind'),
                                     candidate_ids=candidate_ids,memories=memories))
        if not pending_rows:return
        decisions=[dict(ref=row['ref'],target_id=row.get('target_id'),content=row['content'])
                   for row in cache.get('memory_plan',{}).values()]
        packet={**state,'identity_disambiguation':dict(pending=pending_rows,memory_decisions=decisions)}
        outputs=cache.setdefault('outputs',{})
        if 'subjects_disambiguation' not in outputs:
            try:outputs['subjects_disambiguation']=self.model.maintain_subjects(subject_stage_state(packet),deadline=deadline)
            except OutputInvalid:outputs['subjects_disambiguation']={'output_error':'model_json_invalid'}
            save()
        raw=outputs['subjects_disambiguation']
        reviewed=review(raw,packet,self.store)
        allowed_refs={row['ref'] for row in pending_rows}
        returned_refs={row.get('ref') for row in raw.get('subjects',[]) if isinstance(row,dict)} if isinstance(raw,dict) else set()
        if reviewed['issues'] or not returned_refs<=allowed_refs:
            cache['subject_disambiguation_issues']=deepcopy(reviewed['issues']) or [dict(code='disambiguation_returned_unlisted_ref')]
            return
        for ref,subject_id in reviewed['bindings']['subject_ids'].items():
            if int(ref) not in allowed_refs:continue
            result['bindings']['subject_ids'][ref]=subject_id
            replacement=next((row for row in reviewed['state']['proposal']['subjects'] if str(row['ref'])==ref),None)
            if replacement is not None:
                result['state']['proposal']['subjects']=[replacement if str(row['ref'])==ref else row
                                                         for row in result['state']['proposal']['subjects']]
            result['state']['pending_subject_candidates'].pop(ref,None)
        by_id={row['id']:row for row in result['bindings']['entity_rows']}
        for row in reviewed['bindings']['entity_rows']:by_id[row['id']]=row
        result['bindings']['entity_rows']=list(by_id.values())
        for row in reviewed['bindings']['entity_rows']:
            cache['expected_objects'].setdefault(row['id'],self.store.get('objects',state['owner'],row['id']))
        cache['subject_disambiguation_result']=dict(
            resolved_refs=sorted(int(ref) for ref in reviewed['bindings']['subject_ids']),
            pending_refs=sorted(int(ref) for ref in result['state'].get('pending_subject_candidates',{})))
        save()

    def _shared_recall(self, state, deadline):
        """Expose the unified recall step to validation and replay tooling."""
        return shared_recall(self.engine, state, deadline)

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

    def _add_state(self,source,unit,sources,deadline):

        """Use the latest preceding message from the same conversation."""

        previous=self.store.context_before(source.owner,source.session_id,source,limit=CONTEXT_LIMIT)

        return dict(owner=source.owner,new_user_source=dict(id=source.id,role=source.role,content=source.content,

            message_at=source.message_at,created_at=source.created_at),context_sources=[dict(id=item.id,role=item.role,

            content=clip_bytes(item.content,4000),message_at=item.message_at,created_at=item.created_at)

            for item in reversed(previous)])

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

        if candidate_errors:

            candidate_plan=outputs.get('extract_repair',candidate_plan)

            candidate_errors=extraction_issues.get('extract_repair',[])

        if candidate_errors:raise ProviderUnavailable('candidate_extraction_unresolved')

        try:candidate_plan=ProposalDraft.model_validate(candidate_plan).model_dump()

        except ValidationError:candidate_plan=dict(items=[],reason='candidate_extraction_unresolved')

        state={**state,'memory_candidates':[dict(ref='C'+str(index+1),**item)

            for index,item in enumerate(candidate_plan['items'])],'candidate_reason':candidate_plan['reason']}

        cache['memory_candidates']=deepcopy(state['memory_candidates'])

        if 'memory_recall' not in cache:

            recalled, trace = recall_memories(self, state, bindings, view, deadline=deadline)

            cache.update(memory_recall=recalled, memory_recall_trace=trace)

            save()

        state=cache['memory_recall'];result['state']=state

        payload={**state,'resolved_subjects':view}

        protocol_issues=cache.setdefault('memory_protocol_issues',{})

        def call(key,packet):

            try:outputs[key]=self.model.propose(packet,deadline=deadline)

            except OutputInvalid as error:

                outputs[key]=getattr(error,'raw_output',None) or {'output_error':str(error)}

                protocol_issues[key]=getattr(error,'issues',[])

            save()

        if 'propose' not in outputs:call('propose',payload)

        accepted,errors=validate_plan(outputs['propose'],state,view)

        errors=[*protocol_issues.get('propose',[]),*errors]

        cache['memory_initial_issues']=deepcopy(errors)

        if errors:
            accepted_refs={row['ref'] for row in accepted.values()}
            repair_candidates=[row for row in state['memory_candidates'] if row['ref'] not in accepted_refs]
            previous=outputs['propose'] if isinstance(outputs['propose'],dict) else {}
            previous_items=previous.get('items') if isinstance(previous.get('items'),list) else []
            previous_rows=[row for row in previous_items if isinstance(row,dict)
                           and row.get('ref') in {item['ref'] for item in repair_candidates}]
            repair_state={**payload,'memory_candidates':repair_candidates,
                'previous_memory_output':dict(items=previous_rows,reason=previous.get('reason','')),
                'memory_repair_errors':errors}
            if repair_candidates and 'propose_repair' not in outputs:call('propose_repair',repair_state)
            if repair_candidates:
                protected={row['target_id'] for row in accepted.values() if row.get('target_id')}
                repaired,repair_errors=validate_plan(outputs.get('propose_repair',{}),repair_state,view,
                                                      protected=protected)
                accepted.update({'repair:'+key:value for key,value in repaired.items()})
                errors=[*protocol_issues.get('propose_repair',[]),*repair_errors]

        accepted_refs={row['ref'] for row in accepted.values()}
        dispositions=[]
        for candidate in state['memory_candidates']:
            if candidate['ref'] in accepted_refs:continue
            issue=next((row for row in errors if row.get('candidate_ref')==candidate['ref']),None)
            dispositions.append(dict(candidate_id=candidate['ref'],status='validation_failed',operation='rejected',
                memory_id=None,reason=(issue or {}).get('code','memory_decision_invalid')))
        cache.update(memory_plan=accepted,items=candidates(accepted,state),memory_validation_dispositions=dispositions)

        if errors:cache['memory_rejections']=deepcopy(errors)

        else:cache.pop('memory_rejections',None)

        save()
        self._disambiguate_pending_subjects(cache,result,save,deadline)

    def add(self,request):

        return self._add(request)

    def _add(self,request,*,recovery=None,deadline=None):

        request=AddRequest.model_validate(request);deadline=deadline or monotonic()+self.settings.add_timeout

        pipeline=AddPipeline(self);owner=request.user_id

        with self._scope(owner,deadline):

            if recovery and self.store.get('requests',owner,recovery['key']):

                return pipeline._response(request)

            fingerprint=hashlib.sha256(json.dumps(request.model_dump(),sort_keys=True,ensure_ascii=False).encode()).hexdigest()

            progress=self.store.get('requests',owner,request.request_id)

            if progress and progress.get('fingerprint')!=fingerprint:raise DuplicateRequestConflict('request_id_payload_conflict')

            if progress and progress['pipeline']!=PIPELINE:raise ProviderUnavailable('request_pipeline_requires_source_replay')

            if progress and progress['status']=='complete':return pipeline._response(request,progress)

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

                        if 'subject_clarifications' not in cache:

                            cache['subject_clarifications']=pipeline._subject_clarifications(cache,result);save()

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

                from .pending_recovery import pending_jobs

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

            progress.update(status='complete',completed_at=utcnow(),clarifications=pipeline._collect_clarifications(progress));save()

        return pipeline._response(request,progress)
