"""Keyword coverage, compact identity evidence and one-message Add integration."""
from copy import deepcopy
from time import monotonic
from types import SimpleNamespace
import json
import pytest

from experiments.subject_identity import matching_subjects
from experiments.subject_recall import prepare_candidates, pack_history, subject_stage_state, recall_bytes, RECALL_BYTES
from experiments.subject_engine import SubjectGraphEngine, SubjectMemoryStore, shared_recall
from experiments.subject_maintenance import render, review, maintain
from remi.identity import stable_id
from remi.models import Source
from tests.test_individual_subjects_experiment import plan
from tests.test_subject_reference_pipeline import Model, request

OWNER = 'keyword-owner'
CLOCK = '2028-02-02T10:00:00+00:00'
PAST = '2028-02-01T10:00:00+00:00'


def source(sid, text, *, owner=OWNER, when=PAST, role='user', session='session', ordinal=0):
    return Source(id=sid, owner=owner, request_id=sid, session_id=session, ordinal=ordinal,
                  role=role, content=text, message_at=when, created_at=when).model_dump()


def add_subject(store, name, *, key=None, aliases=(), user=False, kind='person', owner=OWNER, when=PAST, text=None):
    key = key or stable_id('obj_', owner, 'user' if user else name)
    sid = 'evidence-'+key
    all_aliases=list(aliases)
    original = source(sid, text or f'{name}; '+ '; '.join(all_aliases), owner=owner, when=when)
    store.put('sources', owner, sid, original)
    row = dict(id=key, owner=owner, name='user' if user else name, kind=kind,is_user=user, observed_at=when,
               aliases=[dict(name=n, source_id=sid, quote=original['content'], observed_at=when) for n in all_aliases],
               references=[dict(source_id=sid,quote=original['content'],observed_at=when)])
    store.put('objects', owner, key, row)
    return row


def state(text, context=(), owner=OWNER):
    return dict(owner=owner, new_user_source=source('now', text, owner=owner, when=CLOCK), context_sources=list(context))


def ids(hits):
    return [obj.id for obj, _ in hits]


def test_canonical_names_and_aliases_are_both_recalled():
    store=SubjectMemoryStore()
    named=add_subject(store,'Nila')
    alias=add_subject(store,'Riven',aliases=['Sol'])
    described=add_subject(store,'Tova',aliases=['Tov'])
    hits=matching_subjects(store,OWNER,['Nila and Sol met Tov.'],CLOCK)
    assert set(ids(hits))=={named['id'],alias['id'],described['id']}


def test_current_alias_and_context_names_are_all_recalled():
    store=SubjectMemoryStore()
    first=add_subject(store,'Aster');second=add_subject(store,'Briar');third=add_subject(store,'Cairn',aliases=['Cai'])
    assert set(ids(matching_subjects(store,OWNER,['Aster met Cai.','Briar replied.'],CLOCK)))=={first['id'],second['id'],third['id']}


def test_repeated_evidence_does_not_change_ranking_or_expand_the_identity_card():
    store=SubjectMemoryStore();row=add_subject(store,'Tavi',aliases=['Tav'])
    before=ids(matching_subjects(store,OWNER,['Tavi is here.'],CLOCK))
    for index in range(60):
        original=source('repeat-'+str(index),'Tavi is also called Tav.')
        store.put('sources',OWNER,original['id'],original)
        row['aliases'].append(dict(name='Tav',source_id=original['id'],quote=original['content'],observed_at=PAST))
    store.put('objects',OWNER,row['id'],row);snapshot=deepcopy(store.records)
    packet,_=prepare_candidates(store,state('Tavi is here.'))
    assert ids(matching_subjects(store,OWNER,['Tavi is here.'],CLOCK))==before
    assert [a['name'] for a in packet['subject_catalog'][0]['aliases']]==['Tav']
    assert len(packet['identity_bases'])==2 and store.records==snapshot


def test_late_alias_is_searchable_and_its_basis_survives_compaction_and_reuse():
    store=SubjectMemoryStore();row=add_subject(store,'Eryn',aliases=[*[f'Handle{i}' for i in range(40)],'Zeya'])
    packet,_=prepare_candidates(store,state('Zeya also goes by Zee.'))
    assert [o['id'] for o in packet['subject_catalog']]==[row['id']]
    basis=next(b for b in packet['identity_bases'] if b['text']=='Zeya')
    raw=plan(name='Eryn',operation='reuse',target=row['id'],aliases=['Zee'],sid='now',mention='Zeya',basis=basis['id'])
    result=review(raw,packet,store)
    assert not result['issues']
    final=result['bindings']['entity_rows'][0]
    assert {a['name'] for a in row['aliases']}|{'Zee'}=={a['name'] for a in final['aliases']}
    assert store.get('objects',OWNER,row['id'])==row


def test_keyword_matching_respects_latin_word_boundaries_and_chinese_names():
    store=SubjectMemoryStore();a=add_subject(store,'Min');b=add_subject(store,'秋禾')
    assert ids(matching_subjects(store,OWNER,['The administrator called 秋禾.'],CLOCK))==[b['id']]
    assert ids(matching_subjects(store,OWNER,['MIN arrived.'],CLOCK))==[a['id']]


@pytest.mark.parametrize('has_user',[False,True])
def test_candidate_count_is_five_other_subjects_plus_the_existing_user(has_user):
    store=SubjectMemoryStore();names=[f'Person{i}' for i in range(9)]
    for name in names:add_subject(store,name)
    if has_user:user=add_subject(store,'Owner',user=True)
    packet,trace=prepare_candidates(store,state(' '.join(names)))
    assert len(packet['subject_catalog'])==5+has_user
    assert len(trace['limit_skipped_subject_ids'])==4
    assert trace['subject_recall_truncated']
    if has_user:assert packet['subject_catalog'][0]['id']==user['id']
    assert 'partial candidate list' in render(packet)


def test_same_name_candidates_keep_separate_ids_in_the_available_slots():
    store=SubjectMemoryStore()
    for index,role in enumerate(['my neighbor','my colleague']):
        add_subject(store,'Lior',key='person-'+str(index),aliases=[role])
    packet,_=prepare_candidates(store,state('Lior sent a message.'))
    assert {o['id'] for o in packet['subject_catalog']}=={'person-0','person-1'}
    assert len({b['target_id'] for b in packet['identity_bases']})==2


def test_only_the_latest_supporting_message_contributes_keyword_candidates():
    store=SubjectMemoryStore();old=add_subject(store,'Orla');recent=add_subject(store,'Soren')
    context=[source('old','Orla called.'),source('recent','Soren called.'),source('reply','What did he say?',role='assistant')]
    packet,trace=prepare_candidates(store,state('He will arrive soon.',context))
    assert trace['context_source_ids']==['reply']
    assert not packet['subject_catalog']
    assert {old['id'],recent['id']}.isdisjoint(trace['keyword_matched_subject_ids'])


def test_foreign_future_subjects_and_context_are_outside_keyword_recall():
    store=SubjectMemoryStore();a=add_subject(store,'Lumi');add_subject(store,'Keir',owner='elsewhere')
    add_subject(store,'Nori',when='2030-01-01T00:00:00+00:00')
    context=[source('foreign','Lumi called.',owner='elsewhere'),source('future','Lumi called.',when='2030-01-01T00:00:00+00:00')]
    packet,_=prepare_candidates(store,state('Keir and Nori called.',context))
    assert not packet['subject_catalog'] and not packet['context_sources']


def test_navigation_neighbors_do_not_expand_keyword_subject_candidates():
    store=SubjectMemoryStore();a=add_subject(store,'Fara');b=add_subject(store,'Iven')
    store.put('edges',OWNER,'link',dict(id='link',owner=OWNER,endpoint_ids=[a['id'],b['id']],source_ids=[a['references'][0]['source_id']]))
    packet,_=prepare_candidates(store,state('Fara called.'))
    assert [o['id'] for o in packet['subject_catalog']]==[a['id']]
    assert 'existing_connections' not in packet


def test_long_identity_evidence_is_excerpted_without_changing_stored_original():
    store=SubjectMemoryStore();text='unrelated start '*2000+'Neris is also called Neri.'+' unrelated end'*2000
    row=add_subject(store,'Neris',aliases=['Neri'],text=text);snapshot=deepcopy(store.records)
    packet,_=prepare_candidates(store,state('Neris called.'));body=render(packet)
    assert 'Neris is also called Neri.' in body and text not in body
    assert len(body.encode())<6000 and recall_bytes(packet)<=RECALL_BYTES
    assert store.records==snapshot and packet['source_catalog'][row['aliases'][0]['source_id']]['content']==text


def test_large_history_is_skipped_and_later_small_history_fills_remaining_space():
    store=SubjectMemoryStore();row=add_subject(store,'Edda');packet,_=prepare_candidates(store,state('Edda called.'))
    large=dict(id='large',content='padding '*10000,record_type='fact',entity_ids=[],sources=[source('large-source','old content')])
    small=dict(id='small',content='A small fact.',record_type='fact',entity_ids=[],sources=[source('small-source','A small fact.')])
    filled,trace=pack_history(store,packet,[large,small])
    assert [o['id'] for o in filled['subject_catalog']]==[row['id']]
    assert trace['selected_memory_ids']==['small'] and trace['budget_skipped_memory_ids']==['large']
    assert trace['recall_bytes']<=RECALL_BYTES


def test_subject_model_view_keeps_identity_evidence_and_excludes_memory_history():
    store=SubjectMemoryStore();row=add_subject(store,'Edda');packet,_=prepare_candidates(store,state('Edda called.'))
    memory=dict(id='old-memory',content='UNRELATED_MEMORY_MARKER',record_type='fact',entity_ids=[],sources=[source('old-memory-source','UNRELATED_ORIGINAL_MARKER')])
    filled,_=pack_history(store,packet,[memory]);view=subject_stage_state(filled);body=render(filled)
    assert not view['history'] and 'old-memory-source' not in view['source_catalog']
    assert row['references'][0]['source_id'] in view['source_catalog']
    assert 'UNRELATED_MEMORY_MARKER' not in body and 'UNRELATED_ORIGINAL_MARKER' not in body
    assert filled['history']==[memory]
    raw=plan(name='Edda',operation='reuse',target=row['id'],sid='now',basis=filled['identity_bases'][0]['id'])
    result=review(raw,filled,store)
    assert not result['issues'] and result['state']['history']==[memory]


def test_subject_wire_schema_and_body_use_the_same_compact_source_scope():
    store=SubjectMemoryStore();add_subject(store,'Edda');packet,_=prepare_candidates(store,state('Edda called.'))
    memory=dict(id='old-memory',content='UNRELATED_MEMORY_MARKER',record_type='fact',entity_ids=[],sources=[source('old-memory-source','UNRELATED_ORIGINAL_MARKER')])
    filled,_=pack_history(store,packet,[memory]);captured=[]
    def create(**body):
        captured.append(body)
        return SimpleNamespace(usage=None,choices=[SimpleNamespace(message=SimpleNamespace(content='{"subjects":[]}',refusal=None))])
    model=SimpleNamespace(model='gpt-4o-mini',timeout=10,calls=[],client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    maintain(model,filled,deadline=monotonic()+10)
    body=captured[0];text=body['messages'][-1]['content']
    schema=body['response_format']['json_schema']['schema']['$defs']
    assert schema['SubjectNameDraft']['properties']['source_id']['enum']==['S1']
    assert 'UNRELATED_MEMORY_MARKER' not in text and 'UNRELATED_ORIGINAL_MARKER' not in text


def test_subject_validation_rejects_a_memory_only_source_as_identity_evidence():
    store=SubjectMemoryStore();row=add_subject(store,'Edda');packet,_=prepare_candidates(store,state('Edda called.'))
    memory=dict(id='old-memory',content='Edda is called Invented.',record_type='fact',entity_ids=[],sources=[source('old-memory-source','Edda is called Invented.')])
    filled,_=pack_history(store,packet,[memory]);raw=plan(name='Edda',operation='reuse',target=row['id'],sid='now',basis=filled['identity_bases'][0]['id'])
    raw['subjects'][0]['aliases']=[dict(name='Invented',source_id='old-memory-source')]
    result=review(raw,filled,store)
    assert result['alias_issues'] and not result['subject_errors']
    assert all(a['name']!='Invented' for a in result['bindings']['entity_rows'][0]['aliases'])


def test_experimental_add_context_has_the_latest_preceding_message_and_excludes_following_reply():
    engine=SubjectGraphEngine();owner=OWNER
    values=[source('old','Old message.',when='2028-02-01T10:00:00+00:00'),
            source('antecedent','Neris called.',when='2028-02-01T10:01:00+00:00'),
            source('question','What did she say?',when='2028-02-01T10:02:00+00:00',role='assistant'),
            source('current','She will visit.',when='2028-02-01T10:03:00+00:00'),
            source('following','FOLLOWING_REPLY_MARKER',when='2028-02-01T10:04:00+00:00',role='assistant'),
            source('other-session','UNRELATED_SESSION',when='2028-02-01T10:02:30+00:00',session='another')]
    for value in values:engine.store.put('sources',owner,value['id'],value)
    current=Source.model_validate(values[3]);unit=[current,Source.model_validate(values[4])]
    packet=engine._add_state(current,unit,unit,monotonic()+5)
    assert [row['id'] for row in packet['context_sources']]==['question']


def test_subject_recall_keeps_keyword_identity_and_defers_memory_selection(monkeypatch):
    engine=SubjectGraphEngine(model=Model());engine.add(request());owner='reference-owner'
    person=add_subject(engine.store,'Vela',owner=owner,when='2027-01-01T10:00:00+00:00')
    # Force a semantic-memory hit independently of literal names in the current message.
    document=next(d for d in engine.store.documents(owner) if d.memory_id)
    monkeypatch.setattr(engine,'_hybrid',lambda *args,**kwargs:[(document,1.0)])
    packet,trace=shared_recall(engine,state('Vela called.',owner=owner),monotonic()+10)
    assert [o['id'] for o in packet['subject_catalog']]==[person['id']]
    assert packet['history']==[]
    assert trace['selected_subject_ids']==[person['id']]


def test_full_add_uses_one_shared_recall_then_two_model_calls_with_different_views(monkeypatch):
    from experiments import subject_engine as module
    class Capture(Model):
        def __init__(self):super().__init__();self.subject_states=[];self.memory_states=[]
        def maintain_subjects(self,state,*,deadline):
            self.subject_states.append(deepcopy(state));return super().maintain_subjects(state,deadline=deadline)
        def propose(self,state,*,deadline):
            self.memory_states.append(deepcopy(state));return super().propose(state,deadline=deadline)
    calls=[];original=module.shared_recall
    def recall(*args):calls.append(True);return original(*args)
    monkeypatch.setattr(module,'shared_recall',recall)
    model=Capture();engine=SubjectGraphEngine(model=model)
    engine.add(request());engine.add(request('The teammate likes jasmine tea.','r2'))
    assert len(calls)==2 and model.calls==['subject','memory','subject','memory']
    assert not model.subject_states[-1]['history'] and model.memory_states[-1]['history']
    assert {s['id'] for s in model.subject_states[-1]['subject_catalog']} <= {s['id'] for s in model.memory_states[-1]['subject_catalog']}


def test_identity_basis_uses_full_original_when_its_recent_message_view_is_clipped():
    store=SubjectMemoryStore();text='Opening material. '*1000+'Veyra is also called Vey.'
    row=add_subject(store,'Veyra',aliases=['Vey'],text=text)
    original=store.get('sources',OWNER,row['aliases'][0]['source_id'])
    context={**original,'content':original['content'][:100]}
    packet,_=prepare_candidates(store,state('Veyra called.',[context]))
    assert len(packet['identity_bases'])==2
    assert 'Veyra is also called Vey.' in render(packet)
    raw=plan(name='Veyra',operation='reuse',target=row['id'],sid='now',basis=packet['identity_bases'][0]['id'])
    result=review(raw,packet,store)
    assert not result['issues'] and result['bindings']['subject_ids']=={'0':row['id']}
    assert packet['context_sources'][0]['content']==context['content']
