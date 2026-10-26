"""Reference evidence and the full experimental Add/Search checkpoint boundary."""
from copy import deepcopy
from types import SimpleNamespace
from time import monotonic
import pytest
from fastapi.testclient import TestClient
from experiments.subject_engine import SubjectGraphEngine,SubjectMemoryStore
from experiments.subject_maintenance import review,commit
from experiments.subject_identity import recall_state
from scripts.validate_subject_identities import step_state
from tests.test_subject_maintenance_experiment import initial
from tests.test_individual_subjects_experiment import plan
from remi.models import AddRequest,AddMessage,SearchRequest
from remi.providers import ProviderUnavailable
from remi.engine import DuplicateRequestConflict
from remi.api import create_app


def nurse_state():
    state,store=initial('One person named Esra is also called Ez; another named Esra is also called Essie.')
    raw=plan(name='Esra',aliases=['Ez'])
    other=deepcopy(raw['subjects'][0]);other.update(ref=1,aliases=[dict(name='Essie',source_id='current')]);raw['subjects'].append(other)
    first=review(raw,state,store);commit(first,store)
    later=step_state(store,'experiment','Ez sent a message.',1,[state['new_user_source']])
    selected=next(o for o in later['subject_catalog'] if any(a['name']=='Ez' for a in o['aliases']))
    basis=next(b for b in later['identity_bases'] if b['target_id']==selected['id'] and b['text']=='Ez')
    raw=plan(name='Esra',operation='reuse',target=selected['id'],sid='step-1',mention='Ez',basis=basis['id'])
    return later,store,raw,selected['id']


def test_context_reference_accepts_correct_subject_without_old_name_or_full_description_in_current_source():
    state,store,raw,key=nurse_state();result=review(raw,state,store)
    assert not result['subject_errors'] and result['bindings']['subject_ids']=={'0':key}
    assert result['state']['new_user_source']['content']=='Ez sent a message.'


@pytest.mark.parametrize('fault',['other_subject_basis','fabricated_mention','historical_mention','missing_basis','foreign_basis_source','future_basis_source'])
def test_reference_requires_real_current_mention_owned_visible_evidence_and_matching_target(fault):
    state,store,raw,key=nurse_state();ref=raw['subjects'][0]['reference']
    if fault=='other_subject_basis':ref['basis_id']=next(b['id'] for b in state['identity_bases'] if b['target_id']!=key)
    elif fault=='fabricated_mention':ref['evidence_id']='C999'
    elif fault=='historical_mention':ref.update(source_id='current')
    elif fault=='missing_basis':ref['basis_id']='B999'
    else:
        basis=next(b for b in state['identity_bases'] if b['id']==ref['basis_id']);sid=basis['source_id']
        source=store.get('sources','experiment',sid)
        if fault=='foreign_basis_source':source['owner']='foreign'
        else:source['message_at']='2030-01-01T00:00:00+00:00'
        store.put('sources','experiment',sid,source);state['source_catalog'].pop(sid,None)
    result=review(raw,state,store)
    assert result['subject_errors'] and not result['bindings']['subject_ids']


def test_shared_name_alias_retains_distinct_candidates_and_explicit_pending():
    state,store=initial('Ione is also called Lee, and Rami is also called Lee.')
    raw=plan(name='Ione',aliases=['Lee']);second=deepcopy(raw['subjects'][0]);second.update(ref=1,name='Rami');raw['subjects'].append(second)
    result=review(raw,state,store);commit(result,store)
    later=step_state(store,'experiment','Lee arrived.',1,[state['new_user_source']])
    pending=plan(name='Lee',operation='pending',sid='step-1')
    result=review(pending,later,store)
    assert not result['subject_errors'] and not result['bindings']['subject_ids']
    assert len(later['subject_catalog'])==2


class Model:
    def __init__(self):self.calls=[]
    def maintain_subjects(self,state,*,deadline):
        self.calls.append('subject');source=state['new_user_source'];existing=state['subject_catalog']
        if existing:
            subject=next(o for o in existing if o['name']=='Nolan');basis=next(b for b in state['identity_bases'] if b['target_id']==subject['id'] and b['text']=='Nole')
            return plan(name='Nolan',operation='reuse',target=subject['id'],sid=source['id'],mention='Nole',basis=basis['id'])
        return plan(name='Nolan',aliases=['Nole'],sid=source['id'])
    def propose(self,state,*,deadline):
        self.calls.append('memory')
        return dict(items=[dict(content=state['new_user_source']['content'],record_type='fact',subject_refs=[0],mode='asserted',
            context=None,status=None,time_expression=None,evidence=[dict(source_id=state['new_user_source']['id'])])],reason='source-grounded fixture')
    def decide(self,state,*,deadline):return dict(steps=[],relevant_memory_ids=[])
    def close(self):pass


def request(text='Nolan is also called Nole.',key='r1'):
    return AddRequest(request_id=key,user_id='reference-owner',session_id='session',messages=[AddMessage(role='user',content=text,timestamp=1833000000000)])


def test_complete_add_reuses_context_reference_and_search_returns_actual_original():
    model=Model();engine=SubjectGraphEngine(model=model)
    engine.add(request());engine.add(request('Nole likes jasmine tea.','r2'))
    assert model.calls==['subject','memory','subject','memory']
    objects=engine.store.list('objects','reference-owner');memories=engine.store.memories('reference-owner')
    assert len(objects)==1 and len(memories)==2
    assert all(m.current.entity_ids==[objects[0]['id']] for m in memories)
    found=engine.search(SearchRequest(user_id='reference-owner',query='jasmine tea',top_k=5),'2029-01-01T00:00:00Z')
    assert any('Nole likes jasmine tea.' in item.content for item in found.data)


class FlakyEmbedder:
    def __init__(self):self.calls=0
    def embed(self,texts,*,deadline):
        self.calls+=1
        if self.calls==1:raise ProviderUnavailable('temporary_embedding_outage')
        return [[1.,0.,1.] for _ in texts]
    def close(self):pass


def test_embedding_failure_preserves_model_checkpoint_and_atomic_records_then_resumes():
    model=Model();embedder=FlakyEmbedder();engine=SubjectGraphEngine(model=model,embedder=embedder)
    with pytest.raises(ProviderUnavailable):engine.add(request())
    assert not engine.store.list('objects','reference-owner') and not engine.store.memories('reference-owner')
    assert not engine.store.documents('reference-owner') and model.calls==['subject','memory']
    engine.add(request())
    assert model.calls==['subject','memory'] and len(engine.store.memories('reference-owner'))==1


def test_snapshot_restore_keeps_idempotence_and_conflict_checks():
    engine=SubjectGraphEngine(model=Model());engine.add(request());snapshot=engine.store.snapshot()
    restored=SubjectMemoryStore();restored.restore(snapshot);model=Model();resumed=SubjectGraphEngine(restored,model=model)
    resumed.add(request());assert model.calls==[] and restored.snapshot()==snapshot
    with pytest.raises(DuplicateRequestConflict):resumed.add(request('Changed text.'))


def test_new_pipeline_is_callable_through_existing_http_contract():
    engine=SubjectGraphEngine(model=Model())
    with TestClient(create_app(engine,api_key='local-test')) as client:
        response=client.post('/add',json=request().model_dump(),headers={'Authorization':'Bearer local-test'})
        assert response.status_code==200 and response.json()['success']
        response=client.post('/search',json=dict(user_id='reference-owner',query='Nole',top_k=3),
            headers={'Authorization':'Bearer local-test','x-memory-reference-time':'2029-01-01T00:00:00Z'})
        assert response.status_code==200 and response.json()['data']


def test_invalid_memory_output_has_one_repair_then_preserves_original_without_bad_memory():
    class InvalidMemory(Model):
        def propose(self,state,*,deadline):
            self.calls.append('memory')
            return {'items':[{'unrecognized':'field'}],'reason':'invalid model fixture'}
    model=InvalidMemory();engine=SubjectGraphEngine(model=model)
    engine.add(request())
    assert model.calls==['subject','memory','memory']
    assert not engine.store.memories('reference-owner')
    assert engine.store.documents('reference-owner')
    unit=engine.store.get('requests','reference-owner','r1')['units']['0']
    assert unit['memory_rejections'] and unit['repair_stage']=='propose' and unit['phase']=='indexed'


def test_model_validation_cli_forwards_mode_without_abbreviating_model(monkeypatch):
    import sys
    from scripts import validate_reference_models as entry
    captured=[]
    monkeypatch.setattr(sys,'argv',['validate_reference_models','--model','deepseek-flash','--mode','extra','--output','unused','--env-file','unused'])
    monkeypatch.setattr(entry.benchmark,'main',lambda:captured.append(list(sys.argv)))
    entry.main()
    assert '--mode' in captured[0] and captured[0][captured[0].index('--mode')+1]=='extra'


def test_graph_search_uses_query_matches_as_seeds_and_keeps_canonical_user_out_of_implicit_expansion(monkeypatch):
    from remi.identity import stable_id
    engine=SubjectGraphEngine(model=Model());engine.add(request())
    owner='reference-owner';person=engine.store.list('objects',owner)[0]
    user=stable_id('obj_',owner,'user')
    engine.store.put('objects',owner,user,dict(id=user,owner=owner,name='user',kind='person',is_user=True,
        aliases=[],references=[],observed_at=person['observed_at']))
    source=engine.store.list('sources',owner)[0]['id'];pair=sorted([user,person['id']]);key=stable_id('con_',owner,*pair)
    engine.store.put('edges',owner,key,dict(id=key,owner=owner,endpoint_ids=pair,source_ids=[source]))
    captured=[]
    def hybrid(owner,query,pool,vector,limit):captured.append(list(pool));return []
    monkeypatch.setattr(engine,'_hybrid',hybrid)
    engine._graph_candidates(owner,'unrelated topic',[],engine.store.documents(owner),None,'2029-01-01T00:00:00Z')
    assert captured==[[]]
