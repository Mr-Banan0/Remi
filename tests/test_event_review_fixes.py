"""V53 regressions for historical writes, append-first fallback and concurrency."""
from copy import deepcopy
import pytest

from experiments.subject_engine import SubjectGraphEngine
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


def test_valid_target_is_kept_while_invalid_sibling_falls_back_to_add():
    engine,model=lifecycle();target=engine.store.memories(OWNER)[0].id
    model.extractor=lambda state:dict(items=[extracted(state),deepcopy(extracted(state))],reason='two candidates')
    model.writer=lambda state:[
        update(state,ref='C1',content='Aster added refreshments to the workshop.'),
        row(state,ref='C2',target_id='missing',content='Aster also mentioned another workshop.'),
    ]
    engine.add(req('Aster added refreshments and mentioned another workshop.','mixed',1))
    memories=engine.store.memories(OWNER)
    updated=next(memory for memory in memories if memory.id==target)
    assert updated.current.content=='Aster added refreshments to the workshop.' and len(memories)==2
    assert [entry['operation'] for entry in unit(engine,'mixed')['candidate_dispositions']]==['updated','created_fallback']


@pytest.mark.parametrize('bad_output',[
    {'items':None,'reason':'bad'},
    {'items':[{'ref':'C1','target_id':{},'content':'Aster plans a picnic.'}],'reason':'bad'},
    {'items':[{'ref':'C1','target_id':None,'content':None}],'reason':'bad'},
])
def test_malformed_memory_decision_falls_back_without_repair(bad_output):
    model=Model();engine=SubjectGraphEngine(model=model)
    model.propose=lambda state,deadline:bad_output
    key='malformed-'+str(len(str(bad_output)))
    engine.add(req('Aster plans a picnic.',key))
    assert len(engine.store.memories(OWNER))==1
    assert unit(engine,key)['candidate_dispositions'][0]['operation']=='created_fallback'
    assert model.calls==['subjects','extract']


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
