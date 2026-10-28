"""Canonical subject names, later aliases and broad subject kinds."""
from copy import deepcopy
from time import monotonic
from types import SimpleNamespace

import pytest

from remi.subject_identity import matching_subjects
from remi.subject_maintenance import commit, maintain, render, review
from scripts.validate_subject_identities import step_state
from tests.test_subject_maintenance import initial


def plan(name='Tansy',kind='person',operation='create',target=None,aliases=None,
         sid='current',mention=None,basis='B1',fragment='C1'):
    return dict(subjects=[dict(ref=0,operation=operation,target_id=target,name=name,kind=kind,
        aliases=[dict(name=value,source_id=sid) for value in list(aliases or [])],
        reference=dict(evidence_id=fragment,basis_id=basis if operation=='reuse' else None))])


def pet():
    state,store=initial('My dog Tansy is also called Tan.')
    result=review(plan(kind='animal',aliases=['Tan']),state,store)
    commit(result,store)
    return state,store,result


def test_subject_stores_name_alias_and_animal_kind():
    state,store,result=pet();obj=store.list('objects','experiment')[0]
    assert not result['subject_errors'] and not result['alias_issues']
    assert obj['name']=='Tansy' and obj['kind']=='animal' and 'species' not in obj
    assert [row['name'] for row in obj['aliases']]==['Tan']
    assert obj['aliases'][0]['quote']==state['new_user_source']['content']


def test_only_name_and_alias_recall_the_same_animal():
    state,store,result=pet();clock=state['new_user_source']['message_at'];key=result['bindings']['subject_ids']['0']
    for query in ['Tansy','Tan']:
        assert [obj.id for obj,_ in matching_subjects(store,'experiment',[query],clock)]==[key]
    assert matching_subjects(store,'experiment',['my dog'],clock)==[]
    assert matching_subjects(store,'someone-else',['dog'],clock)==[]


def test_reuse_by_name_keeps_id_name_alias_and_kind():
    state,store,first=pet();key=first['bindings']['subject_ids']['0']
    later=step_state(store,'experiment','Tansy is sleeping.',1,[])
    basis=next(row for row in later['identity_bases'] if row['target_id']==key)
    result=review(plan(kind='animal',operation='reuse',target=key,sid='step-1',basis=basis['id']),later,store)
    commit(result,store);saved=store.get('objects','experiment',key)
    assert not result['issues'] and result['bindings']['subject_ids']=={'0':key}
    assert saved['name']=='Tansy' and saved['kind']=='animal' and [a['name'] for a in saved['aliases']]==['Tan']


def test_two_explicitly_different_animals_keep_distinct_ids():
    state,store=initial('My dog Tansy and my dog Fenn are different dogs.')
    first=plan(kind='animal');second=deepcopy(first['subjects'][0]);second.update(ref=1,name='Fenn')
    result=review(dict(subjects=[first['subjects'][0],second]),state,store);commit(result,store)
    assert not result['issues'] and len(set(result['bindings']['subject_ids'].values()))==2
    assert matching_subjects(store,'experiment',['dog'],state['new_user_source']['message_at'])==[]


def test_first_confirmed_name_is_stable_and_later_name_is_an_alias():
    state,store,first=pet();key=first['bindings']['subject_ids']['0']
    later=step_state(store,'experiment','Tansy is also called T.',1,[])
    basis=next(row for row in later['identity_bases'] if row['target_id']==key)
    result=review(plan(kind='animal',operation='reuse',target=key,aliases=['T.'],sid='step-1',basis=basis['id']),later,store)
    commit(result,store);saved=store.get('objects','experiment',key)
    assert not result['issues'] and saved['name']=='Tansy'
    assert {row['name'] for row in saved['aliases']}=={'Tan','T.'}
    wrong=review(plan(name='T.',kind='animal',operation='reuse',target=key,sid='step-1',basis=basis['id']),later,store)
    assert any(issue['code']=='reuse_name_requires_selected_identity' for issue in wrong['issues'])


def test_unnamed_subject_cannot_store_an_alias_before_a_first_name():
    state,store=initial('I adopted a dog called Tan.')
    result=review(plan(name=None,kind='animal',aliases=['Tan']),state,store)
    codes={issue['code'] for issue in result['issues']}
    assert {'create_requires_stated_name','unnamed_subject_cannot_have_aliases'}<=codes


def test_alias_evidence_is_idempotent_by_name():
    state,store,first=pet();key=first['bindings']['subject_ids']['0']
    later=step_state(store,'experiment','Tansy is also called Tan.',1,[])
    basis=next(row for row in later['identity_bases'] if row['target_id']==key)
    raw=plan(kind='animal',operation='reuse',target=key,aliases=['Tan'],sid='step-1',basis=basis['id'])
    result=review(raw,later,store);commit(result,store);result=review(raw,later,store);commit(result,store)
    assert [a['name'] for a in store.get('objects','experiment',key)['aliases']]==['Tan']


def test_actual_user_is_a_canonical_person_with_name_aliases():
    state,store=initial('My name is Orin, and friends call me Ori.')
    first=review(plan(name='@user',aliases=['Orin','Ori']),state,store);commit(first,store)
    obj=store.list('objects','experiment')[0]
    assert obj['is_user'] and obj['name']=='user' and obj['kind']=='person' and 'species' not in obj
    assert {row['name'] for row in obj['aliases']}=={'Orin','Ori'}


@pytest.mark.parametrize('fault',['kind','owner','future_identity','missing_aliases'])
def test_identity_boundary_and_current_protocol_are_enforced(fault):
    state,store,first=pet();key=first['bindings']['subject_ids']['0']
    later=step_state(store,'experiment','Tan is here.',1,[])
    basis=next(row for row in later['identity_bases'] if row['target_id']==key)
    raw=plan(kind='animal',operation='reuse',target=key,sid='step-1',basis=basis['id'])
    if fault=='kind':raw['subjects'][0]['kind']='organization'
    elif fault=='owner':
        obj=store.get('objects','experiment',key);obj['owner']='foreign';store.put('objects','experiment',key,obj)
    elif fault=='future_identity':
        obj=store.get('objects','experiment',key);obj['observed_at']='2029-01-01T00:00:00+00:00';store.put('objects','experiment',key,obj)
    else:raw['subjects'][0].pop('aliases')
    result=review(raw,later,store)
    assert result['subject_errors'] and not result['bindings']['subject_ids']


def test_render_shows_name_alias_and_kind_without_machine_timestamps():
    state,store,first=pet();later=step_state(store,'experiment','Tansy is here.',1,[])
    text=render(later)
    assert 'type: animal | name: Tansy' in text and 'Known aliases: Tan' in text
    assert 'species' not in text
    assert 'My dog Tansy is also called Tan.' in text
    assert all(field not in text for field in ['observed_at','message_at','matched_names'])


def test_model_wire_uses_one_source_id_namespace_in_text_schema_and_evidence():
    state,store,first=pet();later=step_state(store,'experiment','Tansy is sleeping.',1,[state['new_user_source']]);captured=[]
    def create(**request):
        captured.append(request)
        return SimpleNamespace(usage=None,choices=[SimpleNamespace(message=SimpleNamespace(content='{"subjects":[]}',refusal=None))])
    model=SimpleNamespace(model='gpt-4o-mini',timeout=20,calls=[],client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    raw,wire=maintain(model,later,deadline=monotonic()+20)
    assert raw=={'subjects':[]};text=wire['messages'][-1]['content']
    assert 'AVAILABLE SOURCE CITATIONS\nS1, S2' in text
    assert 'Source S1 | speaker: user' in text and 'Source S2 | speaker: user' in text
    assert 'Source step-1' not in text and 'Source current' not in text
    schema=wire['response_format']['json_schema']['schema']['$defs']
    assert schema['SubjectNameDraft']['properties']['source_id']['enum']==['S1']
