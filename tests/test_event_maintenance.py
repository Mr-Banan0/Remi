"""V42 event lifecycle and subject-scoped recall; examples are independent of benchmarks."""
from copy import deepcopy
from time import monotonic
from types import SimpleNamespace
import json
import pytest
from experiments.subject_engine import SubjectGraphEngine, SubjectMemoryStore
from experiments.event_maintenance import validate_plan, materialize, provider_plan, examples, EventWire
from experiments.event_recall import recall_memories
from remi.models import AddRequest, AddMessage, Memory
from remi.providers import ProviderUnavailable
from tests.test_individual_subjects_experiment import plan

OWNER='event-test-owner'
STAMP=1893456000000  # 2030-01-01 UTC


def req(text,key,day=0,timestamp=True):
    return AddRequest(user_id=OWNER,session_id='session',request_id=key,messages=[AddMessage(role='user',content=text,timestamp=STAMP+day*86400000 if timestamp else None)])


def row(state,**kwargs):
    value=dict(content=state['new_user_source']['content'],record_type='event',subject_refs=[0],mode='asserted',context=None,status='planned',time_expression=None,evidence=[dict(source_id=state['new_user_source']['id'])],operation='create',target_id=None,changed_fields=[],time_field=None,change_quote=None,related_memory_ids=[])
    value.update(kwargs)
    field=value.pop('time_field');expression=value.pop('time_expression')
    if field or expression:value.setdefault('time_updates',[dict(field=field or 'planned_at',expression=expression)])
    return value


class Model:
    def __init__(self):self.calls=[];self.packets=[];self.writer=None
    def maintain_subjects(self,state,*,deadline):
        self.calls.append('subjects');self.packets.append(('subjects',deepcopy(state)))
        existing=next((s for s in state['subject_catalog'] if s['name']=='Aster'),None)
        if existing:
            basis=next(b for b in state['identity_bases'] if b['target_id']==existing['id'])
            return plan(name='Aster',operation='reuse',target=existing['id'],sid=state['new_user_source']['id'],basis=basis['id'])
        return plan(name='Aster',sid=state['new_user_source']['id'])
    def propose(self,state,*,deadline):
        self.calls.append('memory');self.packets.append(('memory',deepcopy(state)))
        return dict(items=self.writer(state) if self.writer else [row(state)],reason='test fixture')
    def close(self):pass


def unit(engine,key):return engine.store.get('requests',OWNER,key)['units']['0']


def lifecycle():
    model=Model();engine=SubjectGraphEngine(model=model)
    model.writer=lambda s:[row(s,content='Aster plans a workshop on 2030-01-03.',time_expression='January 3',time_field='planned_at')]
    engine.add(req('Aster plans a workshop on January 3.','start'))
    return engine,model


def update(state,**kwargs):
    return row(state,operation='update',target_id=state['history'][0]['id'],changed_fields=['content'],change_quote=state['new_user_source']['content'],**kwargs)


def test_same_event_reschedule_complete_and_replay_keep_versions_and_planned_time():
    engine,model=lifecycle();old=engine.store.memories(OWNER)[0];mid=old.id;pid=old.current.entity_ids
    def moved(s):
        r=update(s,content='Aster plans the workshop on 2030-01-05.',time_expression='January 5',time_field='planned_at');r['changed_fields']=['content','planned_at'];return [r]
    model.writer=moved;engine.add(req('Aster moved that workshop to January 5.','move',1))
    second=engine.store.memories(OWNER)[0]
    assert second.id==mid and len(second.revisions)==2 and second.current.entity_ids==pid
    assert second.current.planned_at.startswith('2030-01-05')
    assert old.current.planned_at.startswith('2030-01-03')
    def completed(s):
        r=update(s,content='Aster completed the workshop today.',status='occurred',time_expression='today',time_field='event_at');r['changed_fields']=['content','status','event_at'];return [r]
    model.writer=completed;final=req('Aster completed the workshop today.','finish',5);engine.add(final)
    memory=engine.store.memories(OWNER)[0]
    assert len(memory.revisions)==3 and memory.current.planned_at.startswith('2030-01-05')
    assert memory.current.event_at.startswith('2030-01-06') and '2030-01-06' in memory.current.content
    assert len(memory.current.source_ids)==3 and len(memory.current.time_evidence)==2
    assert memory.current.replaces_revision_id==second.current.id
    assert len([e for e in engine.store.edges_for(OWNER) if e.relation=='revises'])==2
    calls=list(model.calls);snapshot=engine.store.snapshot();engine.add(final)
    assert model.calls==calls and engine.store.snapshot()==snapshot
    assert model.calls==['subjects','memory']*3
    assert all(not p['history'] for k,p in model.packets if k=='subjects')
    assert len(unit(engine,'move')['memory_recall']['history'])==1


def test_repeated_similar_activity_creates_distinct_event():
    engine,model=lifecycle();model.writer=lambda s:[row(s,content='Aster plans another workshop on 2030-01-09.',time_expression='January 9')]
    engine.add(req('Aster plans another workshop on January 9.','again',1))
    assert len(engine.store.memories(OWNER))==2
    assert all(len(m.revisions)==1 for m in engine.store.memories(OWNER))


def test_unknown_source_clock_preserves_relative_expression_without_server_date():
    model=Model();engine=SubjectGraphEngine(model=model)
    model.writer=lambda s:[row(s,time_expression='tomorrow',time_field='planned_at')]
    engine.add(req('Aster plans a picnic tomorrow.','import',timestamp=False))
    r=engine.store.memories(OWNER)[0].current
    assert r.planned_at is None and r.time_precision=='unknown' and 'tomorrow' in r.content
    assert r.time_evidence[0].anchor is None and r.time_evidence[0].expression=='tomorrow'


def test_cancel_preserves_date_and_explicit_withdrawal_clears_it():
    engine,model=lifecycle()
    def cancelled(s):
        r=update(s,content='Aster cancelled the workshop.',status='cancelled');r['changed_fields']=['content','status'];return [r]
    model.writer=cancelled;engine.add(req('Aster cancelled the workshop.','cancel',1))
    assert engine.store.memories(OWNER)[0].current.planned_at.startswith('2030-01-03')
    def cleared(s):
        r=update(s,content='Aster withdrew the workshop date.',status='cancelled',time_field='planned_at');r['changed_fields']=['content','planned_at'];return [r]
    model.writer=cleared;engine.add(req('Aster withdrew the workshop date.','clear',2))
    assert engine.store.memories(OWNER)[0].current.planned_at is None


def test_partial_repair_preserves_accepted_sibling_even_when_model_rewrites_it():
    model=Model();engine=SubjectGraphEngine(model=model)
    def writer(s):
        if s.get('repair_errors'):
            return [row(s,content='WRONG replacement',record_type='fact',status=None),row(s,content='Aster planned a walk.')]
        return [row(s,content='Aster prefers tea.',record_type='preference',status=None),row(s,target_id='foreign',operation='update',changed_fields=['content'],change_quote=s['new_user_source']['content'])]
    model.writer=writer;engine.add(req('Aster prefers tea and planned a walk.','partial'))
    contents=[m.current.content for m in engine.store.memories(OWNER)]
    assert 'Aster prefers tea.' in contents and 'WRONG replacement' not in contents
    assert len(contents)==2 and model.calls==['subjects','memory','memory']


def test_pending_event_is_searchable_without_mutating_existing_event():
    engine,model=lifecycle();before=engine.store.list('memories',OWNER)
    model.writer=lambda s:[row(s,operation='pending',target_id=s['history'][0]['id'])]
    engine.add(req('Aster mentioned a workshop but its identity is unclear.','pending',1))
    assert engine.store.list('memories',OWNER)==before
    assert unit(engine,'pending')['candidate_dispositions'][0]['status']=='pending'
    assert any(d.id.startswith('doc_pending_') for d in engine.store.documents(OWNER))


def test_update_cannot_target_unrecalled_event_or_fact_and_retains_originals():
    engine,model=lifecycle()
    def bad(s):
        return [row(s,operation='update',target_id='foreign-event',changed_fields=['content'],change_quote=s['new_user_source']['content'])]
    model.writer=bad;before=engine.store.list('memories',OWNER);engine.add(req('Aster corrected a different meeting.','bad',1))
    assert engine.store.list('memories',OWNER)==before
    assert unit(engine,'bad')['memory_rejections']
    assert len(engine.store.list('sources',OWNER))==2


def test_event_links_have_current_source_evidence_and_survive_updates():
    engine,model=lifecycle();first=engine.store.memories(OWNER)[0].id
    model.writer=lambda s:[row(s,content='Aster scheduled a review following the workshop.',related_memory_ids=[s['history'][0]['id']],change_quote=s['new_user_source']['content'])]
    engine.add(req('Aster scheduled a review following that workshop.','review',1))
    review=next(m for m in engine.store.memories(OWNER) if m.id!=first)
    def edited(s):
        r=row(s,operation='update',target_id=review.id,content='Aster cancelled the review.',status='cancelled',changed_fields=['content','status'],change_quote=s['new_user_source']['content']);return [r]
    model.writer=edited;engine.add(req('Aster cancelled that review.','cancel-review',2))
    current=Memory.model_validate(engine.store.get('memories',OWNER,review.id)).current
    edges=[e for e in engine.store.edges_for(OWNER) if e.revision_id==current.id and e.relation=='related']
    assert len(edges)==1 and edges[0].target==first
    assert edges[0].source_id!=current.source_ids[0]


def test_embedding_failure_resumes_without_repeating_models():
    from tests.test_subject_reference_pipeline import FlakyEmbedder
    model=Model();engine=SubjectGraphEngine(model=model,embedder=FlakyEmbedder());request=req('Aster plans a picnic.','retry')
    with pytest.raises(ProviderUnavailable):engine.add(request)
    assert not engine.store.memories(OWNER) and not engine.store.list('objects',OWNER)
    engine.add(request);assert model.calls==['subjects','memory']
    assert len(engine.store.memories(OWNER))==1


def test_provider_uses_event_schema_with_source_and_memory_handles():
    packet,answer=list(examples())[1];captured=[]
    def call(**kwargs):
        captured.append(kwargs);return SimpleNamespace(usage=None,choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(answer),refusal=None))])
    model=SimpleNamespace(model='test',timeout=30,calls=[],client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=call))))
    result=provider_plan(model,packet,deadline=monotonic()+30)
    assert result['items'][0]['target_id']=='old' and result['items'][0]['subject_refs']==[0]
    assert result['items'][0]['evidence'][0]['source_id']=='now'
    schema=captured[0]['response_format']['json_schema']['schema']
    def check(node):
        if isinstance(node,dict):
            if '$ref' in node:assert set(node)=={'$ref'}
            for value in node.values():check(value)
        if isinstance(node,list):
            for value in node:check(value)
    check(schema)
    assert schema['$defs']['EventWire']['properties']['participant_handles']['items']['enum']==['P0']
    assert len(captured)==1


def test_repair_network_interruption_reuses_identity_and_initial_memory_reply():
    model=Model();engine=SubjectGraphEngine(model=model);attempts=[]
    def writer(s):
        attempts.append(bool(s.get('repair_errors')))
        if not s.get('repair_errors'):return [row(s,subject_refs=[91])]
        if attempts.count(True)==1:raise ProviderUnavailable('repair_network_outage')
        return [row(s)]
    model.writer=writer;request=req('Aster plans a picnic.','network-repair')
    with pytest.raises(ProviderUnavailable,match='repair_network_outage'):engine.add(request)
    assert not engine.store.memories(OWNER)
    engine.add(request)
    assert model.calls==['subjects','memory','memory','memory']
    assert len(engine.store.memories(OWNER))==1


def test_repair_checkpoint_resume_reuses_saved_model_reply():
    class Interrupt(SubjectMemoryStore):
        armed=True
        def put(self,table,owner,key,value):
            super().put(table,owner,key,value)
            if table=='requests' and self.armed and any('propose_repair' in u.get('outputs',{}) for u in value.get('units',{}).values()):
                self.armed=False;raise ProviderUnavailable('saved_reply_interrupt')
    model=Model();engine=SubjectGraphEngine(store=Interrupt(),model=model)
    model.writer=lambda s:[row(s,subject_refs=[0] if s.get('repair_errors') else [91])]
    request=req('Aster plans a picnic.','saved-repair')
    with pytest.raises(ProviderUnavailable,match='saved_reply_interrupt'):engine.add(request)
    engine.add(request)
    assert model.calls==['subjects','memory','memory'] and len(engine.store.memories(OWNER))==1


def test_pending_subject_scopes_history_to_its_name_candidates_but_cannot_update_them():
    engine,model=lifecycle();state=deepcopy(unit(engine,'start')['identity']['state'])
    subject=engine.store.list('objects',OWNER)[0];state['subject_catalog']=[subject]
    view=[dict(ref=0,name='Aster',status='pending')]
    recalled,trace=recall_memories(engine,state,dict(subject_ids={},entity_rows=[]),view)
    assert trace['uncertain_subject_ids']==[subject['id']] and len(recalled['history'])==1
    data=update(recalled);accepted,issues=validate_plan(dict(items=[data]),recalled,view)
    assert not issues
    _,memories,_,_,pending,_=materialize(engine.store,recalled,dict(subject_ids={},entity_rows=[]),accepted)
    assert not memories and len(pending)==1


def test_subject_union_retains_single_participant_event_and_excludes_other_people():
    from tests.test_keyword_subject_recall import add_subject
    engine,model=lifecycle();state=deepcopy(unit(engine,'start')['memory_recall']);aster=engine.store.list('objects',OWNER)[0]
    other=add_subject(engine.store,'Beryl',owner=OWNER,when='2029-01-01T10:00:00+00:00')
    outsider=add_subject(engine.store,'Clover',owner=OWNER,when='2029-01-01T10:00:00+00:00')
    memory=engine.store.memories(OWNER)[0];alien=memory.model_copy(deep=True);alien.id='alien-memory';alien.revisions[0].entity_ids=[outsider['id']]
    engine.store.put('memories',OWNER,alien.id,alien.model_dump())
    bindings=dict(subject_ids={'0':aster['id'],'1':other['id']},entity_rows=[aster,other])
    recalled,trace=recall_memories(engine,state,bindings,[dict(ref=0,name='Aster',status='resolved'),dict(ref=1,name='Beryl',status='resolved')])
    assert [m['id'] for m in recalled['history']]==[memory.id]
    assert trace['eligible_memory_ids']==[memory.id]


def test_same_name_candidates_keep_subject_first_order_and_pending_identity():
    from tests.test_keyword_subject_recall import add_subject
    model=Model();engine=SubjectGraphEngine(model=model)
    for key in ['aster-first','aster-second']:add_subject(engine.store,'Aster',owner=OWNER,key=key,when='2029-01-01T10:00:00+00:00')
    def subjects(state,*,deadline):
        model.calls.append('subjects');assert not state['history']
        return plan(name='Aster',operation='pending',sid=state['new_user_source']['id'])
    model.maintain_subjects=subjects
    engine.add(req('Aster plans a picnic.','namesake'))
    assert model.calls==['subjects','memory']
    assert not engine.store.memories(OWNER)
    assert unit(engine,'namesake')['shared_recall']['maintenance_route']['order']=='subject_first'


def test_foreign_source_and_duplicate_target_are_rejected_per_item():
    engine,model=lifecycle();state=unit(engine,'start')['memory_recall'];view=unit(engine,'start')['current_participants']
    accepted,issues=validate_plan(dict(items=[row(state,evidence=[dict(source_id='foreign')])]),state,view)
    assert not accepted and any(e['code']=='include_current_source_and_only_supplied_sources' for e in issues)
    bindings=unit(engine,'start')['identity']['bindings'];state,_=recall_memories(engine,state,bindings,view)
    accepted,issues=validate_plan(dict(items=[update(state),update(state)]),state,view)
    assert not accepted and sum(e['code']=='combine_changes_to_one_event' for e in issues)==2


def test_source_precision_and_quote_attribution_survive_materialization():
    model=Model();engine=SubjectGraphEngine(model=model)
    model.writer=lambda s:[row(s,content='Aster wrote that the picnic might happen tomorrow afternoon.',mode='hypothetical',time_expression='tomorrow afternoon',time_field='planned_at')]
    engine.add(req('Aster wrote that the picnic might happen tomorrow afternoon.','precision'))
    memory=engine.store.memories(OWNER)[0]
    assert memory.current.mode=='hypothetical' and 'afternoon' in memory.current.content
    assert memory.current.time_evidence[0].expression=='tomorrow afternoon'
    assert memory.current.event_at is None


def test_concurrent_event_change_invalidates_cached_target_and_recomputes_on_retry():
    engine,model=lifecycle();mid=engine.store.memories(OWNER)[0].id
    class ConcurrentEdit:
        fired=False
        def embed(self,texts,*,deadline):
            if not self.fired:
                self.fired=True
                stored=engine.store.get('memories',OWNER,mid)
                revised=deepcopy(stored['revisions'][-1]);revised.update(id='external-revision',content='Aster plans a workshop with an extra exercise.',created_at='2099-01-01T00:00:00Z')
                stored['revisions'].append(revised);engine.store.put('memories',OWNER,mid,stored)
            return [[1.,0.,1.] for _ in texts]
        def close(self):pass
    engine.embedder=ConcurrentEdit()
    model.writer=lambda s:[update(s,content='Aster added refreshments to the workshop.')]
    request=req('Aster added refreshments to the workshop.','concurrent',1)
    with pytest.raises(ProviderUnavailable,match='event_history_changed_retry'):engine.add(request)
    cache=unit(engine,'concurrent')
    assert cache['phase']=='pending' and 'memory_plan' not in cache and cache['concurrent_recomputations']==1
    assert len(engine.store.memories(OWNER)[0].revisions)==2
    engine.add(request)
    assert len(engine.store.memories(OWNER)[0].revisions)==3
    assert engine.store.memories(OWNER)[0].current.replaces_revision_id=='external-revision'
    assert model.calls==['subjects','memory']*3


def test_parts_of_day_remain_readable_after_absolute_date_conversion():
    from experiments.event_maintenance import readable_time,render_event
    text='Aster completed the lesson this afternoon.'
    result=readable_time(text,text,'2030-01-01T09:00:00+00:00')
    assert '2030-01-01 afternoon' in result
    packet,_=__import__('experiments.subject_extraction',fromlist=['prepare']).prepare(dict(new_user_source=dict(id='s',content='tomorrow',role='user',message_at=None,created_at='2030-01-01T00:00:00Z'),context_sources=[],resolved_subjects=[],history=[]))
    assert 'CURRENT REFERENCE TIME' not in render_event(packet)


def test_equivalent_normalized_date_recovers_original_phrase_and_rejects_wrong_year():
    from experiments.event_maintenance import original_time_expression
    primary=dict(content='The lesson moved to November 8.',message_at='2031-11-02T00:00:00+00:00')
    assert original_time_expression('November 8, 2031',primary)=='November 8'
    assert original_time_expression('2031-11-08',primary)=='November 8'
    assert original_time_expression('2021-11-08',primary) is None


def test_unsupported_date_in_readable_content_is_rejected_before_commit():
    engine,model=lifecycle();state=unit(engine,'start')['memory_recall'];view=unit(engine,'start')['current_participants']
    accepted,issues=validate_plan(dict(items=[row(state,content='Aster plans a workshop on 2020-01-03.')]),state,view)
    assert not accepted and any(i['code']=='readable_date_requires_source_support' for i in issues)


def test_changed_status_requires_changed_readable_content():
    engine,model=lifecycle();u=unit(engine,'start');state,_=recall_memories(engine,u['memory_recall'],u['identity']['bindings'],u['current_participants'])
    r=update(state,content=state['history'][0]['content'],status='cancelled');r['changed_fields']=['content','status']
    accepted,issues=validate_plan(dict(items=[r]),state,u['current_participants'])
    assert not accepted and any(i['code']=='reflect_changed_status_or_date_in_readable_event_content' for i in issues)
