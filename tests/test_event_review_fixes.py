"""V51 regressions for historical writes, protected repairs and repair transport."""
from copy import deepcopy
from time import monotonic
from types import SimpleNamespace
import json
import pytest

from experiments.event_maintenance import examples, provider_plan
from experiments.subject_engine import SubjectGraphEngine, SubjectMemoryStore
from remi.graph import active_edges
from remi.heat import visible_memory
from remi.providers import ProviderUnavailable
from tests.test_event_maintenance import OWNER, Model, extracted, lifecycle, req, row, update, unit


def test_backdated_update_uses_historical_revision_and_preserves_later_view():
    engine,model=lifecycle();first=engine.store.memories(OWNER)[0].current.model_dump()
    model.writer=lambda state:[update(state,content='Aster completed the workshop today.')]
    engine.add(req('Aster completed the workshop today.','later',5));latest=engine.store.memories(OWNER)[0].current.model_dump()
    model.writer=lambda state:[update(state,content='Aster plans the workshop with an extra demonstration.')]
    engine.add(req('Aster added a demonstration to that workshop.','backfill',1))
    stored=engine.store.memories(OWNER)[0];historical=visible_memory(stored,'2030-01-02T23:59:59Z').current
    assert stored.current.status==latest['status'] and stored.current.event_at==latest['event_at']
    assert 'extra demonstration' in stored.current.content
    assert historical.replaces_revision_id==first['id'] and historical.status=='planned'
    assert unit(engine,'backfill').get('concurrent_recomputations',0)==0
    assert any(edge.revision_id==historical.id for edge in active_edges(engine.store,OWNER,'2030-01-02T23:59:59Z'))


@pytest.mark.parametrize('protected_index',[0,1])
def test_repair_preserves_an_accepted_update_when_sibling_is_invalid(protected_index):
    engine,model=lifecycle()
    def extractor(state):
        base=extracted(state)
        return dict(items=[base,deepcopy(base)],reason='two candidates')
    model.extractor=extractor
    def writer(state):
        good=update(state,ref='C'+str(protected_index+1),content='Aster added refreshments to the workshop.')
        bad=dict(row(state,ref='C'+str(2-protected_index)),operation='update',target_id='missing')
        values=[bad,bad];values[protected_index]=good
        if state.get('repair_errors'):
            values[protected_index]=row(state,ref='C'+str(protected_index+1),content='protected rewrite attempt')
            values[1-protected_index]=update(state,ref='C'+str(2-protected_index),content='competing update')
        return values
    model.writer=writer;engine.add(req('Aster added refreshments to the workshop.','conflict',1))
    memory=engine.store.memories(OWNER)[0]
    assert len(memory.revisions)==2 and memory.current.content=='Aster added refreshments to the workshop.'
    assert unit(engine,'conflict')['memory_plan'][str(protected_index)]['operation']=='update'


@pytest.mark.parametrize('field,value',[
    ('operation',None),('operation',4),('target_id',{}),('target_id',[]),
    ('content',None),('content',4),('pending_reason',{}),('items',None),('items',4),('items',{'bad':1}),
])
def test_malformed_decision_fields_are_presented_to_repair_without_transport_crash(field,value):
    packet,answer=list(examples())[0]
    previous=dict(items=[row(packet)],reason='Malformed prior output')
    if field=='items':previous[field]=deepcopy(value)
    else:previous['items'][0][field]=deepcopy(value)
    packet.update(previous_output=previous,repair_errors=[dict(path=['items',0,field],code='invalid_type')])
    original=deepcopy(packet);captured=[]
    def call(**kwargs):
        captured.append(kwargs);return SimpleNamespace(usage=None,choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(answer),refusal=None))])
    model=SimpleNamespace(model='test',timeout=10,calls=[],client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=call))))
    result=provider_plan(model,packet,deadline=monotonic()+10)
    assert set(result['items'][0])=={'ref','operation','target_id','content','pending_reason'} and len(captured)==1
    sent=json.loads(captured[0]['messages'][-2]['content'])
    assert (sent['items'] if field=='items' else sent['items'][0][field])==value
    assert packet==original


def test_repair_checkpoint_resume_reuses_saved_model_reply():
    class InterruptStore(SubjectMemoryStore):
        armed=True
        def put(self,table,owner,key,value):
            super().put(table,owner,key,value)
            if table=='requests' and self.armed and any('propose_repair' in unit.get('outputs',{}) for unit in value.get('units',{}).values()):
                self.armed=False;raise ProviderUnavailable('saved_reply_interrupt')
    model=Model();engine=SubjectGraphEngine(store=InterruptStore(),model=model)
    model.writer=lambda state:[row(state) if state.get('repair_errors') else dict(row(state),operation='update',target_id='missing')]
    request=req('Aster plans a picnic.','saved-repair')
    with pytest.raises(ProviderUnavailable,match='saved_reply_interrupt'):engine.add(request)
    engine.add(request)
    assert model.calls==['subjects','extract','memory','memory'] and len(engine.store.memories(OWNER))==1


def test_newer_concurrent_revision_after_recall_is_detected():
    engine,model=lifecycle();mid=engine.store.memories(OWNER)[0].id;mutated=[]
    def writer(state):
        if not mutated:
            raw=engine.store.get('memories',OWNER,mid);new=deepcopy(raw['revisions'][0])
            new.update(id='concurrent-newer-view',observed_at='2030-01-06T00:00:00+00:00',created_at='2030-01-06T00:00:00+00:00',content='Aster changed the later workshop plan.')
            raw['revisions'].append(new);engine.store.put('memories',OWNER,mid,raw);mutated.append(True)
        return [update(state,content='Aster added a demonstration to the workshop.')]
    model.writer=writer
    with pytest.raises(ProviderUnavailable,match='event_history_changed_retry'):
        engine.add(req('Aster added a demonstration to the workshop.','race-before-write',1))
    assert unit(engine,'race-before-write')['concurrent_recomputations']==1
