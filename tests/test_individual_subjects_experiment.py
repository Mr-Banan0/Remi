"""Identity evidence, description retrieval and validation across source times."""
from copy import deepcopy
import pytest
from pydantic import ValidationError
from experiments.subject_connections import review, commit, render, SubjectConnectionPlan
from experiments.subject_identity import SubjectRecord, matching_subjects, recall_state
from scripts.validate_subject_connections import prepare_state, step_state
from tests.test_subject_connections_experiment import initial


def plan(name='Tansy',kind='individual',operation='create',target=None,aliases=None,descriptions=None,sid='current',mention=None,basis='B1',fragment='C1'):
    names=list(aliases or [])
    if operation=='create' and name!='@user' and name not in names:names.insert(0,name)
    return dict(subjects=[dict(ref=0,operation=operation,target_id=target,name=name,kind=kind,
        aliases=[dict(name=n,source_id=sid) for n in names],
        descriptions=[dict(text=t,source_id=sid) for t in descriptions or []],
        reference=dict(evidence_id=fragment,basis_id=basis if operation=='reuse' else None))],connections=[])


def pet():
    state,store=initial('My dog Tansy is also called Tan.')
    result=review(plan(aliases=['Tan'],descriptions=['dog']),state,store)
    commit(result,store)
    return state,store,result


def test_individual_identity_stores_names_and_source_descriptions_separately():
    state,store,result=pet();obj=store.list('objects','experiment')[0]
    assert not result['subject_errors'] and not result['description_issues']
    assert obj['kind']=='individual'
    assert {a['name'] for a in obj['aliases']}=={'Tansy','Tan'}
    assert [d['text'] for d in obj['descriptions']]==['dog']
    assert obj['descriptions'][0]['quote']==state['new_user_source']['content']
    assert obj['descriptions'][0]['observed_at']==state['new_user_source']['message_at']


def test_names_and_descriptions_each_recall_the_same_identity():
    state,store,result=pet();clock=state['new_user_source']['message_at'];key=result['bindings']['subject_ids']['0']
    for query in ['Tan','my dog','Tansy']:
        assert [o.id for o,_ in matching_subjects(store,'experiment',[query],clock)]==[key]
    assert matching_subjects(store,'someone-else',['dog'],clock)==[]


def test_reuse_by_description_keeps_id_and_preserves_names():
    state,store,first=pet();key=first['bindings']['subject_ids']['0']
    later=step_state(store,'experiment','My dog is sleeping.',1,[])
    assert later['subject_catalog'][0]['id']==key
    basis=next(b for b in later['identity_bases'] if b['kind']=='description')
    raw=plan(operation='reuse',target=key,sid='step-1',mention='dog',basis=basis['id'])
    result=review(raw,later,store);commit(result,store)
    assert not result['subject_errors'] and len(store.list('objects','experiment'))==1
    assert result['bindings']['subject_ids']['0']==key
    assert {a['name'] for a in store.list('objects','experiment')[0]['aliases']}=={'Tansy','Tan'}


def test_same_description_retrieves_both_candidates_without_program_identity_merging():
    state,store=initial('My dog Tansy and my dog Fenn are different dogs.')
    raw=plan(descriptions=['dog']);other=deepcopy(raw['subjects'][0]);other.update(ref=1,name='Fenn');raw['subjects'].append(other)
    result=review(raw,state,store);commit(result,store)
    assert len(matching_subjects(store,'experiment',['dog'],state['new_user_source']['message_at']))==2
    assert len(set(result['bindings']['subject_ids'].values()))==2


@pytest.mark.parametrize('fault',['invented_text','absent_source','foreign_source','future_source'])
def test_invalid_description_is_audited_independently_of_confirmed_identity(fault):
    state,store=initial('My dog Tansy is here.');raw=plan(descriptions=['dog'])
    if fault=='invented_text':raw['subjects'][0]['descriptions'][0]['text']='cat'
    else:
        raw['subjects'][0]['descriptions'][0]['source_id']='other'
        if fault in {'foreign_source','future_source'}:
            state['source_catalog']['other']=dict(state['new_user_source'],id='other',owner='foreign' if fault=='foreign_source' else 'experiment',
                message_at='2029-01-01T00:00:00+00:00' if fault=='future_source' else state['new_user_source']['message_at'])
    result=review(raw,state,store);commit(result,store)
    assert not result['subject_errors'] and result['description_issues']
    assert len(store.list('objects','experiment'))==1 and not store.list('objects','experiment')[0]['descriptions']


def test_backdated_addition_keeps_later_observations_and_hides_them_from_earlier_recall():
    state,store=initial('Iris is a painter.')
    first=review(plan(name='Iris',descriptions=['painter']),state,store);commit(first,store)
    key=first['bindings']['subject_ids']['0']
    later=step_state(store,'experiment','Iris is an architect.',1,[])
    second=review(plan(name='Iris',operation='reuse',target=key,descriptions=['architect'],sid='step-1'),later,store);commit(second,store)
    earlier=deepcopy(later['new_user_source']);earlier.update(id='backdated',content='Iris is a designer.',message_at='2028-02-01T10:30:00+00:00')
    before=recall_state(store,'experiment',earlier,[])
    assert all(d['text']!='architect' for o in before['subject_catalog'] for d in o['descriptions'])
    third=review(plan(name='Iris',operation='reuse',target=key,descriptions=['designer'],sid='backdated'),before,store);commit(third,store)
    obj=store.get('objects','experiment',key)
    assert {d['text'] for d in obj['descriptions']}=={'painter','architect','designer'}
    assert matching_subjects(store,'experiment',['architect'],earlier['message_at'])==[]


def test_replayed_description_evidence_is_idempotent_and_new_sources_accumulate():
    state,store,result=pet();key=result['bindings']['subject_ids']['0']
    later=step_state(store,'experiment','My dog Tansy is here.',1,[])
    raw=plan(operation='reuse',target=key,descriptions=['dog'],sid='step-1')
    result=review(raw,later,store);commit(result,store);result=review(raw,later,store);commit(result,store)
    descriptions=store.get('objects','experiment',key)['descriptions']
    assert len(descriptions)==2 and {d['source_id'] for d in descriptions}=={'current','step-1'}


def test_actual_user_uses_canonical_individual_and_first_person_names():
    state,store=initial('My name is Orin, and friends call me Ori.')
    raw=plan(name='@user',aliases=['Orin','Ori'])
    first=review(raw,state,store);commit(first,store)
    obj=store.list('objects','experiment')[0]
    assert obj['is_user'] and obj['name']=='user' and obj['kind']=='individual'
    later=step_state(store,'experiment','Friends call me O.',1,[])
    second=review(plan(name='@user',operation='reuse',target=obj['id'],aliases=['O'],sid='step-1',mention='me'),later,store);commit(second,store)
    assert len(store.list('objects','experiment'))==1
    assert {'Orin','Ori','O'}<={a['name'] for a in store.get('objects','experiment',obj['id'])['aliases']}


@pytest.mark.parametrize('fault',['kind','owner','future_identity','missing_descriptions'])
def test_identity_boundary_and_current_protocol_are_enforced(fault):
    state,store,first=pet();key=first['bindings']['subject_ids']['0']
    later=step_state(store,'experiment','Tan is here.',1,[])
    raw=plan(operation='reuse',target=key,sid='step-1')
    if fault=='kind':raw['subjects'][0]['kind']='organization'
    elif fault=='owner':
        obj=store.get('objects','experiment',key);obj['owner']='foreign';store.put('objects','experiment',key,obj)
    elif fault=='future_identity':
        obj=store.get('objects','experiment',key);obj['observed_at']='2029-01-01T00:00:00+00:00';store.put('objects','experiment',key,obj)
    else:raw['subjects'][0].pop('descriptions')
    result=review(raw,later,store)
    assert result['subject_errors'] and not result['bindings']['subject_ids']


def test_text_input_explains_descriptions_with_evidence_and_leaves_machine_time_in_state():
    state,store,first=pet();later=step_state(store,'experiment','My dog is here.',1,[])
    text=render(later)
    assert 'Source-backed description: dog' in text and 'Other known names: Tan' in text
    assert 'My dog Tansy is also called Tan.' in text
    assert all(field not in text for field in ['observed_at','message_at','matched_names'])
    assert later['new_user_source']['message_at']


def test_model_wire_uses_one_source_id_namespace_in_text_schema_and_evidence():
    from types import SimpleNamespace
    from time import monotonic
    from experiments.subject_connections import maintain
    state,store,first=pet()
    later=step_state(store,'experiment','My dog is sleeping.',1,[state['new_user_source']])
    captured=[]
    def create(**request):
        captured.append(request)
        return SimpleNamespace(usage=None,choices=[SimpleNamespace(message=SimpleNamespace(content='{"subjects":[],"connections":[]}',refusal=None))])
    model=SimpleNamespace(model='gpt-4o-mini',timeout=20,calls=[],client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    raw,wire=maintain(model,later,deadline=monotonic()+20)
    assert raw=={'subjects':[],'connections':[]}
    text=wire['messages'][-1]['content']
    assert 'AVAILABLE SOURCE CITATIONS\nS1, S2' in text
    assert 'Source S1 | speaker: user' in text and 'Source S2 | speaker: user' in text
    assert 'step-1' not in text and 'current' not in text
    schema=wire['response_format']['json_schema']['schema']['$defs']
    for key in ['SourceCitation','SubjectNameDraft','DescriptionDraft']:
        assert set(schema[key]['properties']['source_id']['enum'])==({'S1','S2'} if key=='SourceCitation' else {'S1'})
    assert text.count('Source S1 | speaker: user')==1
