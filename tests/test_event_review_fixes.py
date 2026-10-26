"""Regressions for historical writes, protected repairs and malformed repair transport."""
from copy import deepcopy
from time import monotonic
from types import SimpleNamespace
import json
import pytest

from experiments.event_maintenance import provider_plan, examples
from remi.heat import visible_memory
from remi.graph import active_edges
from remi.providers import ProviderUnavailable
from tests.test_event_maintenance import OWNER, Model, lifecycle, req, row, update, unit
from experiments.subject_engine import SubjectGraphEngine


def test_backdated_update_uses_recalled_revision_and_preserves_later_current_view():
    engine,model=lifecycle();first=engine.store.memories(OWNER)[0].current.model_dump()
    def complete(s):
        r=update(s,content='Aster completed the workshop today.',status='occurred',time_expression='today',time_field='event_at')
        return [r]
    model.writer=complete;engine.add(req('Aster completed the workshop today.','later',5))
    latest=engine.store.memories(OWNER)[0].current.model_dump()
    model.writer=lambda s:[update(s,content='Aster plans the workshop with an extra demonstration.')]
    request=req('Aster added a demonstration to that workshop.','backfill',1)
    engine.add(request)
    stored=engine.store.memories(OWNER)[0];historical=visible_memory(stored,'2030-01-02T23:59:59Z').current
    assert len(stored.revisions)==3 and stored.current.id==latest['id']
    assert stored.current.status==latest['status'] and stored.current.event_at==latest['event_at']
    assert 'extra demonstration' in stored.current.content
    assert historical.replaces_revision_id==first['id'] and historical.status=='planned'
    assert historical.event_at is None and historical.planned_at==first['planned_at']
    assert latest['source_ids'][0] not in historical.source_ids
    assert unit(engine,'backfill').get('concurrent_recomputations',0)==0
    assert engine.store.get('requests',OWNER,'backfill')['status']=='complete'
    assert model.calls==['subjects','extract','memory']*3
    assert any(e.revision_id==historical.id for e in active_edges(engine.store,OWNER,'2030-01-02T23:59:59Z'))
    snapshot=engine.store.snapshot();engine.add(request);assert engine.store.snapshot()==snapshot


@pytest.mark.parametrize('protected_index',[0,1])
def test_repaired_conflicting_item_preserves_previously_accepted_update(protected_index):
    engine,model=lifecycle()
    def writer(s):
        good=update(s,content='Aster added refreshments to the workshop.')
        bad=row(s,subject_refs=None,ref='C999')
        values=[bad,bad];values[protected_index]=good
        if s.get('repair_errors'):
            # Even if the repair changes the protected position, its original is authoritative.
            values[protected_index]=row(s,content='Aster prefers tea.',record_type='preference',status=None)
            values[1-protected_index]=update(s,content='Aster added refreshments to the same workshop again.')
        return values
    model.writer=writer;engine.add(req('Aster added refreshments to the workshop.','conflict',1))
    memory=engine.store.memories(OWNER)[0];cache=unit(engine,'conflict')
    assert len(memory.revisions)==2
    assert memory.current.content=='Aster added refreshments to the workshop.'
    assert cache['memory_plan'][str(protected_index)]['operation']=='update'
    assert cache['memory_plan'][str(1-protected_index)]['operation']=='pending'
    assert any(i['code']=='repair_conflicts_with_accepted_event_update' for i in cache['memory_rejections'])
    assert model.calls==['subjects','extract','memory','subjects','extract','memory','memory']


def test_repair_with_duplicate_target_in_protected_slot_still_keeps_original_update():
    engine,model=lifecycle()
    def writer(s):
        good=update(s,content='Aster added refreshments to the workshop.')
        return [good,update(s,content='Aster also changed the workshop.') if s.get('repair_errors') else row(s,subject_refs=None,ref='C999')]
    model.writer=writer;engine.add(req('Aster added refreshments to the workshop.','same-target',1))
    assert len(engine.store.memories(OWNER)[0].revisions)==2
    assert unit(engine,'same-target')['memory_plan']['1']['operation']=='pending'


@pytest.mark.parametrize('field,value',[
    ('subject_refs',None),('subject_refs',3),('subject_refs',{'bad':1}),('subject_refs',[{},[],True]),
    ('target_id',{}),('target_id',[]),('related_memory_ids',None),('related_memory_ids',4),
    ('related_memory_ids',[{},[]]),('evidence',None),('evidence',4),('evidence',[{'source_id':[]}]),
    ('items',None),('items',4),('items',{'bad':1}),
])
def test_malformed_fields_are_presented_to_repair_without_transport_crash(field,value):
    packet,answer=list(examples())[0]
    previous=dict(items=[row(packet)],reason='Malformed prior output')
    if field=='items':previous[field]=deepcopy(value)
    else:previous['items'][0][field]=deepcopy(value)
    packet.update(previous_output=previous,repair_errors=[dict(path=['items',0,field],code='invalid_type')])
    original=deepcopy(packet);captured=[]
    def call(**kwargs):
        captured.append(kwargs)
        return SimpleNamespace(usage=None,choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(answer),refusal=None))])
    model=SimpleNamespace(model='test',timeout=10,calls=[],client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=call))))
    result=provider_plan(model,packet,deadline=monotonic()+10)
    assert result['items'][0]['subject_refs']==[0] and len(captured)==1
    sent=json.loads(captured[0]['messages'][-2]['content'])
    assert (sent['items'] if field=='items' else sent['items'][0]['participant_handles' if field=='subject_refs' else field])==value
    assert packet==original


def test_null_subject_refs_checkpoint_can_resume_into_one_successful_repair():
    model=Model();engine=SubjectGraphEngine(model=model);attempts=[]
    def writer(s):
        attempts.append(deepcopy(s))
        if not s.get('repair_errors'):return [row(s,subject_refs=None)]
        if len(attempts)==2:raise ProviderUnavailable('interrupted_before_repair')
        answer=row(s);answer['participant_handles']=['P0'];answer.pop('subject_refs')
        answer['evidence']=[dict(source_id='S1')]
        def call(**kwargs):
            old=json.loads(kwargs['messages'][-2]['content'])
            assert old['items'][0]['participant_handles'] is None
            return SimpleNamespace(usage=None,choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(dict(items=[answer],reason='fixed')),refusal=None))])
        transport=SimpleNamespace(model='test',timeout=10,calls=[],client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=call))))
        return provider_plan(transport,s,deadline=monotonic()+10)['items']
    model.writer=writer;request=req('Aster plans a picnic.','null-ref')
    with pytest.raises(ProviderUnavailable,match='interrupted_before_repair'):engine.add(request)
    assert unit(engine,'null-ref')['outputs']['propose']['items'][0]['subject_refs'] is None
    engine.add(request)
    assert engine.store.get('requests',OWNER,'null-ref')['status']=='complete'
    assert len(engine.store.memories(OWNER))==1
    assert model.calls==['subjects','extract','memory','memory','memory']


def test_backfill_validates_new_revision_participants_separately_from_later_participants():
    from tests.test_keyword_subject_recall import add_subject
    from remi.models import Memory
    from remi.graph import project_edges
    engine,model=lifecycle();stored=engine.store.memories(OWNER)[0]
    later_person=add_subject(engine.store,'Beryl',owner=OWNER,when='2030-01-06T00:00:00+00:00')
    source=engine.store.get('sources',OWNER,later_person['references'][0]['source_id'])
    revision=stored.current.model_copy(deep=True,update=dict(id='later-participants',maintenance_patch=None,
        observed_at='2030-01-06T00:00:00+00:00',created_at='2030-01-06T00:00:00+00:00',
        entity_ids=[*stored.current.entity_ids,later_person['id']]))
    stored.revisions.append(revision)
    engine.store.put('memories',OWNER,stored.id,stored.model_dump())
    for edge in project_edges(stored,revision):engine.store.put('edges',OWNER,edge.id,edge.model_dump())
    model.writer=lambda s:[update(s,content='Aster added a demonstration to the workshop.')]
    engine.add(req('Aster added a demonstration to the workshop.','older-participants',1))
    after=engine.store.memories(OWNER)[0]
    assert after.current.id=='later-participants' and later_person['id'] in after.current.entity_ids
    assert later_person['id'] not in after.revisions[-1].entity_ids
    assert all(check['passed'] for check in unit(engine,'older-participants')['user_binding_checks'])


def test_newer_concurrent_revision_after_recall_is_detected_even_for_a_historical_write():
    engine,model=lifecycle();mid=engine.store.memories(OWNER)[0].id;mutated=[]
    def writer(s):
        if not mutated:
            raw=engine.store.get('memories',OWNER,mid);new=deepcopy(raw['revisions'][0])
            new.update(id='concurrent-newer-view',observed_at='2030-01-06T00:00:00+00:00',
                created_at='2030-01-06T00:00:00+00:00',content='Aster changed the later workshop plan.')
            raw['revisions'].append(new);engine.store.put('memories',OWNER,mid,raw);mutated.append(new)
        return [update(s,content='Aster added a demonstration to the workshop.')]
    model.writer=writer;request=req('Aster added a demonstration to the workshop.','race-before-write',1)
    with pytest.raises(ProviderUnavailable,match='event_history_changed_retry'):engine.add(request)
    assert unit(engine,'race-before-write')['concurrent_recomputations']==1
    assert len(engine.store.memories(OWNER)[0].revisions)==2
    engine.add(request)
    after=engine.store.memories(OWNER)[0]
    assert after.current.id=='concurrent-newer-view' and len(after.revisions)==3
    assert after.revisions[-1].replaces_revision_id==after.revisions[0].id
    assert engine.store.get('requests',OWNER,'race-before-write')['status']=='complete'


def test_repair_cannot_change_protected_update_into_a_new_competing_target():
    engine,model=lifecycle()
    def writer(s):
        good=update(s,content='Aster added refreshments to the workshop.')
        if s.get('repair_errors'):
            return [dict(good,subject_refs=None,target_id=[]),row(s,content='Aster prefers tea.',record_type='preference',status=None)]
        return [good,row(s,subject_refs=None,ref='C999')]
    model.writer=writer;engine.add(req('Aster added refreshments to the workshop and prefers tea.','protected-malformed',1))
    assert len(engine.store.memories(OWNER))==2
    event=next(m for m in engine.store.memories(OWNER) if m.record_type=='event')
    assert len(event.revisions)==2 and event.current.content=='Aster added refreshments to the workshop.'
    assert 'memory_rejections' not in unit(engine,'protected-malformed')
