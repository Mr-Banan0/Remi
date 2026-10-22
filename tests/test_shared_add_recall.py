"""Shared retrieval reaches each writer with coherent participants and evidence."""
from copy import deepcopy
import json
from time import monotonic

import httpx
from openai import OpenAI
import pytest

from remi.add_pipeline import AddPipeline
from remi.engine import MemoryEngine
from remi.providers import GPTModel, ProviderUnavailable
from tests.helpers import HashEmbedder, request
from tests.test_direct_memory import DirectModel, card
from tests.test_subject_memory import SubjectModel, unit


def recall(engine, text, *, clock='2024-06-05T00:00:00Z', owner='u1'):
    return AddPipeline(engine)._shared_recall(dict(owner=owner,
        new_user_source=dict(id='new', role='user', content=text, message_at=clock, created_at=clock),
        context_sources=[]), monotonic()+10)


def seed_fact(engine, text='Ren organizes midnight telescope tours.', *, rid='r1', owner='u1', when='2024-06-02T10:00:00Z'):
    engine.model=SubjectModel([('Ren', False, [])], [(text, [0], [], None)])
    engine.add(request(text, request_id=rid, owner=owner, when=when))


def test_retrieved_fact_supplies_subject_without_a_name_match():
    e=MemoryEngine(embedder=HashEmbedder());seed_fact(e)
    state, trace=recall(e, 'The organizer of the midnight telescope tours is also called R.')
    obj=e.store.list('objects', 'u1')[0]
    assert trace['selected_memory_ids']==[e.store.memories('u1')[0].id]
    assert state['subject_catalog'][0]['id']==obj['id']
    assert state['subject_catalog'][0]['matched_names']==[]
    assert state['history'][0]['sources'][0]['content']=='Ren organizes midnight telescope tours.'


def test_retrieved_relationship_keeps_both_endpoints_from_one_mentioned_subject():
    e=MemoryEngine(model=SubjectModel([('Ren', False, []), ('Vela', False, [])],
        [('job', [0, 1], [0, 1], 'Ren works at Vela.')]))
    e.add(request('Ren works at Vela.'))
    state, _=recall(e, 'Ren has left that company.')
    relation=state['relation_catalog'][0]
    assert relation['id']==state['history'][0]['id']
    assert set(relation['endpoint_ids'])=={s['id'] for s in state['subject_catalog']}
    assert {s['name'] for s in state['subject_catalog']}=={'Ren', 'Vela'}


def test_two_writers_reuse_one_recall_and_the_same_history(monkeypatch):
    e=MemoryEngine(embedder=HashEmbedder());seed_fact(e)
    began=False;recalls=0
    old_recall=AddPipeline._shared_recall;old_hybrid=e._hybrid
    def shared(self, state, deadline):
        nonlocal recalls
        recalls+=1
        return old_recall(self, state, deadline)
    def hybrid(*args, **kwargs):
        assert not began, 'retrieval ran again after subject maintenance'
        return old_hybrid(*args, **kwargs)
    class Model(SubjectModel):
        def maintain_subjects(self, state, *, deadline):
            nonlocal began
            began=True
            return super().maintain_subjects(state, deadline=deadline)
    monkeypatch.setattr(AddPipeline, '_shared_recall', shared)
    monkeypatch.setattr(e, '_hybrid', hybrid)
    m=Model([('Ren', False, [])], [('Ren enjoys telescope tours.', [0], [], None)])
    e.model=m;e.add(request('Ren enjoys telescope tours.', request_id='r2'))
    assert recalls==1 and [stage for stage, _ in m.calls]==['subjects', 'propose']
    histories=[state['history'] for _, state in m.calls]
    assert histories[0] and histories[0]==histories[1]
    assert unit(e, 'r2')['shared_recall']['history']==histories[0]


def test_events_and_subjectless_facts_use_the_shared_pool():
    e=MemoryEngine(model=DirectModel([
        ('Ren scheduled the rehearsal for June 8.', ['Ren']), ('The rehearsal room code is 42.', [])],
        [card('0', content='Rehearsal on June 8', time='June 8'),
         card('1', kind='fact', content='The rehearsal room code is 42.')]))
    e.add(request('Ren scheduled the rehearsal for June 8. The rehearsal room code is 42.'))
    state, _=recall(e, 'The rehearsal time changed; the room code remains 42.')
    assert {m['record_type'] for m in state['history']}=={'event', 'fact'}
    event=next(m for m in state['history'] if m['record_type']=='event')
    assert 'patchable' not in event and event['planned_at'].startswith('2024-06-08')
    assert any(not m['entity_ids'] for m in state['history'])


def test_shared_pool_filters_owner_future_records_and_future_aliases():
    e=MemoryEngine();seed_fact(e)
    seed_fact(e, owner='u2', rid='foreign')
    seed_fact(e, rid='future', when='2025-01-01T00:00:00Z')
    obj=e.store.list('objects', 'u1')[0]
    obj['aliases'].append(dict(name='SecretFutureAlias', source_id='future', quote='SecretFutureAlias',
                               observed_at='2025-01-01T00:00:00Z'))
    e.store.put('objects', 'u1', obj['id'], obj)
    state, _=recall(e, 'Ren organizes midnight telescope tours.')
    assert len(state['history'])==1
    assert all(s['owner']=='u1' and s['request_id']=='r1' for s in state['history'][0]['sources'])
    assert 'SecretFutureAlias' not in json.dumps(state['subject_catalog'])
    assert not recall(e, 'Ren', clock='2024-01-01T00:00:00Z')[0]['history']


def test_shared_pool_bounds_history_and_keeps_all_retained_participants():
    e=MemoryEngine()
    for index in range(11):
        name=f'Person{index}'
        e.model=SubjectModel([(name, False, [])], [(f'{name} joins the telescope tour.', [0], [], None)])
        e.add(request(f'{name} joins the telescope tour.', request_id=str(index)))
    state, trace=recall(e, 'Who joined the telescope tour?')
    assert len(state['history'])==8 and len(trace['selected_memory_ids'])==8
    ids={s['id'] for s in state['subject_catalog']}
    assert all(set(m['entity_ids'])<=ids for m in state['history'])
    assert len(json.dumps({k:state[k] for k in ['history','subject_catalog','relation_catalog']}, ensure_ascii=False).encode())<=24000


def test_outage_resumes_shared_recall_snapshot_without_another_query(monkeypatch):
    e=MemoryEngine();seed_fact(e)
    class Model(SubjectModel):
        ready=False
        def propose(self, state, *, deadline):
            if not self.ready:raise ProviderUnavailable('synthetic')
            return super().propose(state, deadline=deadline)
    m=Model([('Ren', False, [])], [('Ren likes tea.', [0], [], None)])
    e.model=m
    with pytest.raises(ProviderUnavailable):e.add(request('Ren likes tea.', request_id='r2'))
    snapshot=deepcopy(unit(e, 'r2')['shared_recall'])
    def repeated(*args, **kwargs):raise AssertionError('completed recall repeated')
    monkeypatch.setattr(AddPipeline, '_shared_recall', repeated)
    m.ready=True;e.add(request('Ren likes tea.', request_id='r2'))
    assert unit(e, 'r2')['shared_recall']==snapshot


@pytest.mark.parametrize('stage', ['maintain_subjects', 'propose'])
def test_provider_transmits_shared_history_and_original_evidence(stage):
    sent=[]
    def handler(request):
        sent.append(json.loads(request.content))
        raw=dict(subjects=[], connections=[]) if stage=='maintain_subjects' else dict(items=[], reason='empty')
        return httpx.Response(200, json={'id':'test','object':'chat.completion','created':1,'model':'gpt-4o-mini',
            'choices':[{'index':0,'message':{'role':'assistant','content':json.dumps(raw)},'finish_reason':'stop'}]})
    client=OpenAI(api_key='test', base_url='https://test.invalid/v1', http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    model=GPTModel('test', 'https://test.invalid/v1', client=client)
    source=dict(id='current', role='user', content='The organizer is now called R.', message_at=None)
    prior=dict(id='previous', role='user', content='Ren organizes midnight telescope tours.')
    state=dict(new_user_source=source, context_sources=[], subject_catalog=[], relation_catalog=[], resolved_subjects=[],
               history=[dict(id='prior_fact', content=prior['content'], sources=[prior])])
    getattr(model, stage)(state, deadline=monotonic()+5);model.close()
    payload=sent[0]['messages'][-1]['content']
    if stage=='maintain_subjects':
        assert 'Memory M1' in payload and 'Source S2 | speaker: user' in payload
        assert prior['content'] in payload
    else:
        payload=json.loads(payload)
        assert payload['history'][0]['sources'][0]['content']==prior['content']
        assert payload['history'][0]['id'].startswith('M')
        assert payload['history'][0]['sources'][0]['id'].startswith('S')
