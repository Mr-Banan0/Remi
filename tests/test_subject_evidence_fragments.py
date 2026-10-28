"""V41 source fragment selection, independent identity evidence and explicit name deltas."""
from copy import deepcopy
from time import monotonic
from types import SimpleNamespace
import json
import pytest

from remi.subject_maintenance import SubjectPlan, maintain, review, commit, examples
from remi.storage import MemoryStore
from remi.subject_identity import SubjectRecord, matching_subjects, visible_subject
from remi.subject_references import current_evidence, prepare_reference_state
from remi.subject_repair import build_repair_state, finish_repair
from remi.identity import stable_id
from remi.models import Source

OWNER='evidence-owner'
PAST='2028-01-01T10:00:00+00:00'
NOW='2028-01-02T10:00:00+00:00'


def packet(text, store=None, *, sid='current', role='user', when=NOW):
    store=store or MemoryStore()
    source=Source(id=sid,owner=OWNER,request_id=sid,session_id='test',ordinal=0,role=role,
                  content=text,message_at=when,created_at=when).model_dump()
    state=dict(owner=OWNER,new_user_source=source,context_sources=[],history=[],relation_catalog=[],existing_connections=[],
               subject_catalog=store.list('objects',OWNER),source_catalog={sid:source})
    return prepare_reference_state(state,store),store


def choice(name, *, operation='create', target=None, basis=None, fragment='C1', names=(), sid='current', ref=0, kind='person'):
    return dict(ref=ref,operation=operation,target_id=target,name=name,kind=kind,
                aliases=[dict(name=value,source_id=sid) for value in names],
                reference=dict(evidence_id=fragment,basis_id=basis))


def output(*rows):return dict(subjects=list(rows))


@pytest.mark.parametrize('text',[
    'I met Mira. She called me; we spoke again!',
    '我遇见了小雨。她给我打电话；我们又聊了一次！',
    '  第一行\n第二行。\nThird line?  ',
    'Mira wrote: "I arrived." Then she called me.',
    '只有一句没有标点',
])
def test_fragments_copy_exact_source_offsets_and_have_stable_ids(text):
    source=dict(id='original',content=text)
    fragments=current_evidence(source)
    assert fragments and fragments==current_evidence(deepcopy(source))
    assert [row['id'] for row in fragments]==['C'+str(i+1) for i in range(len(fragments))]
    assert all(row['source_id']=='original' and row['text']==text[row['start']:row['stop']] for row in fragments)
    assert all(left['stop']<=right['start'] for left,right in zip(fragments,fragments[1:]))
    assert source['content']==text


@pytest.mark.parametrize('operation',['create','pending'])
def test_create_and_pending_select_current_fragment_with_null_basis(operation):
    state,store=packet('Mira arrived.');raw=output(choice('Mira',operation=operation,names=['Mira']))
    result=review(raw,state,store)
    if operation=='create':assert not result['issues']
    else:assert any(issue['code']=='pending_requires_multiple_identity_candidates' for issue in result['issues'])
    local=result['state']['proposal']['subjects'][0]
    assert local['current_reference_valid'] and local['current_evidence_id']=='C1'
    expected=[dict(source_id='current',quote='Mira arrived.')] if operation=='create' else []
    assert local['evidence']==expected
    assert bool(result['bindings']['subject_ids'])==(operation=='create')


def test_numbered_fragment_keeps_me_in_source_without_model_copying_I():
    state,store=packet('Mira helped me.');user=stable_id('obj_',OWNER,'user')
    raw=output(choice('@user'))
    result=review(raw,state,store);commit(result,store)
    assert result['bindings']['subject_ids']=={'0':user}
    assert store.get('objects',OWNER,user)['references'][0]['quote']=='Mira helped me.'
    assert not store.get('objects',OWNER,user)['aliases']


def test_old_string_reference_and_subject_evidence_wire_are_rejected():
    state,store=packet('Mira arrived.')
    row=choice('Mira');row['reference']=dict(source_id='current',mention='Mira',basis_id=None)
    row['evidence']=[dict(source_id='current')]
    result=review(output(row),state,store)
    assert result['subject_errors'] and not result['bindings']['subject_ids']
    assert result['state']['proposal']['subjects'][0]['current_reference_valid'] is False


def test_invalid_current_fragment_is_separate_from_other_identity_errors():
    state,store=packet('Mira arrived.')
    bad=choice('Mira',fragment='C999')
    result=review(output(bad),state,store)
    assert any(row['code']=='reference_requires_current_evidence' for row in result['issues'])
    assert result['state']['proposal']['subjects'][0]['current_reference_valid'] is False
    bad=choice('Mira');bad['kind']='device'
    result=review(output(bad),state,store)
    assert result['subject_errors']
    assert result['state']['proposal']['subjects'][0]['current_reference_valid'] is True


def test_new_unnamed_referent_stays_outside_subject_storage():
    state,store=packet('Her daughter arrived.',sid='first',when=PAST)
    result=review(output(choice(None,sid='first')),state,store)
    assert any(issue['code']=='create_requires_stated_name' for issue in result['issues'])
    assert result['bindings']['subject_ids']=={} and result['bindings']['entity_rows']==[]
    assert store.list('objects',OWNER)==[]


def test_short_name_is_added_explicitly_and_recalled_in_a_later_turn():
    state,store=packet('Mireya Solano joined.',sid='first',when=PAST)
    first=review(output(choice('Mireya Solano',sid='first')),state,store);commit(first,store)
    key=first['bindings']['subject_ids']['0']
    later,store=packet('Mireya called.',store)
    basis=next(row for row in later['identity_bases'] if row['target_id']==key)
    row=choice('Mireya Solano',operation='reuse',target=key,basis=basis['id'],names=['Mireya'])
    second=review(output(row),later,store);commit(second,store)
    assert not second['issues'] and {a['name'] for a in store.get('objects',OWNER,key)['aliases']}=={'Mireya'}
    assert [obj.id for obj,_ in matching_subjects(store,OWNER,['Mireya sent a message.'],NOW)]==[key]
    repeated=review(output(row),later,store);commit(repeated,store)
    assert len(store.get('objects',OWNER,key)['aliases'])==1


def test_reuse_reference_never_becomes_a_name_alias():
    state,store=packet('Mireya Solano is my colleague.',sid='first',when=PAST)
    first=review(output(choice('Mireya Solano',sid='first')),state,store);commit(first,store)
    key=first['bindings']['subject_ids']['0'];later,store=packet('My colleague helped me.',store)
    basis=next(row for row in later['identity_bases'] if row['target_id']==key)
    result=review(output(choice('Mireya Solano',operation='reuse',target=key,basis=basis['id'])),later,store)
    assert not result['issues']
    assert not result['bindings']['entity_rows'][0]['aliases']


def test_historical_alias_additions_are_rejected_independently():
    state,store=packet('Mira is a designer.',sid='first',when=PAST)
    first=review(output(choice('Mira',names=['Mira'],sid='first')),state,store);commit(first,store)
    key=first['bindings']['subject_ids']['0'];later,store=packet('Mira called.',store)
    basis=next(row for row in later['identity_bases'] if row['target_id']==key)
    result=review(output(choice('Mira',operation='reuse',target=key,basis=basis['id'],
                                names=['Mira','designer'],sid='first')),later,store)
    assert not result['subject_errors'] and result['alias_issues']


def test_reference_basis_must_belong_to_selected_subject():
    state,store=packet('Mira and Tala arrived.',sid='first',when=PAST)
    first=review(output(choice('Mira',ref=0,sid='first'),choice('Tala',ref=1,sid='first')),state,store);commit(first,store)
    later,store=packet('Mira called.',store)
    mira=first['bindings']['subject_ids']['0'];tala=first['bindings']['subject_ids']['1']
    basis=next(row for row in later['identity_bases'] if row['target_id']==tala)
    result=review(output(choice('Mira',operation='reuse',target=mira,basis=basis['id'])),later,store)
    assert any(row['code']=='reference_basis_belongs_to_other_subject' for row in result['issues'])
    assert not result['bindings']['subject_ids']


def test_reference_visibility_and_speaker_boundaries_are_preserved():
    state,store=packet('My daughter arrived.',sid='first',when=PAST)
    first=review(output(choice('My daughter',sid='first')),state,store);commit(first,store)
    key=first['bindings']['subject_ids']['0'];saved=store.get('objects',OWNER,key)
    assert not visible_subject(saved,OWNER,'2027-01-01T00:00:00+00:00')
    assert visible_subject(saved,OWNER,NOW).references
    assistant,store=packet('I arrived.',store,role='assistant')
    result=review(output(choice('@user')),assistant,store)
    assert any(row['code']=='speaker_identity_requires_user_source' for row in result['issues'])


def test_schema_fewshot_and_repair_use_one_numbered_reference_contract():
    state,store=packet('Mira arrived. Then she called me.')
    requests=[]
    def create(**request):
        requests.append(request)
        return SimpleNamespace(usage=None,choices=[SimpleNamespace(message=SimpleNamespace(content='{"subjects":[]}',refusal=None))])
    model=SimpleNamespace(model='gpt-4o-mini',timeout=5,calls=[],client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    maintain(model,state,deadline=monotonic()+5)
    defs=requests[0]['response_format']['json_schema']['schema']['$defs']
    assert set(defs['IdentityReference']['properties'])=={'evidence_id','basis_id'}
    assert defs['IdentityReference']['properties']['evidence_id']['enum']==['C1','C2']
    assert 'evidence' not in defs['SubjectDraft']['properties']
    for packet_,answer in examples():
        SubjectPlan.model_validate(answer)
        assert all(row['reference']['evidence_id'] in {x['id'] for x in current_evidence(packet_['new_user_source'])} for row in answer['subjects'])
    broken=output(choice('Mira',fragment='C999'));initial=review(broken,state,store)
    repair=build_repair_state(broken,initial,state)
    final=finish_repair(repair,output(choice('Mira')),state,store)
    assert not final['subject_errors'] and final['state']['proposal']['subjects'][0]['current_reference_valid']


@pytest.mark.parametrize('fault',['primary_owner','catalog_owner','catalog_content','catalog_speaker','stored_content','stored_owner','future_catalog'])
def test_current_fragment_provenance_requires_owned_exact_original_and_speaker(fault):
    state,store=packet('Mira helped me.')
    state['source_catalog']['current']=deepcopy(state['source_catalog']['current'])
    if fault=='primary_owner':state['new_user_source']['owner']='other'
    elif fault=='catalog_owner':state['source_catalog']['current']['owner']='other'
    elif fault=='catalog_content':state['source_catalog']['current']['content']='Different original.'
    elif fault=='catalog_speaker':state['source_catalog']['current']['role']='assistant'
    elif fault=='future_catalog':state['source_catalog']['current']['message_at']='2030-01-01T00:00:00+00:00'
    else:
        stored=deepcopy(state['new_user_source'])
        if fault=='stored_content':stored['content']='Different stored original.'
        else:stored['owner']='other'
        store.put('sources',OWNER,'current',stored)
    result=review(output(choice('Mira')),state,store)
    assert not result['bindings']['subject_ids']
    assert any(row['code']=='reference_current_provenance_mismatch' for row in result['issues'])
    assert result['state']['proposal']['subjects'][0]['current_reference_valid'] is False


def test_explicit_current_window_is_verified_against_full_stored_original():
    state,store=packet('Mira arrived.')
    full='Earlier words. Mira arrived. Later words.'
    stored={**state['new_user_source'],'content':full}
    store.put('sources',OWNER,'current',stored)
    start=full.index('Mira arrived.');state['source_window']=dict(start=start,stop=start+len('Mira arrived.'))
    result=review(output(choice('Mira')),state,store)
    assert not result['issues'] and result['state']['proposal']['subjects'][0]['current_reference_valid']
    state['source_window']['start']=0
    bad=review(output(choice('Mira')),state,store)
    assert bad['subject_errors'] and not bad['bindings']['subject_ids']


@pytest.mark.parametrize('malformed',[['C1'],{'id':'C1'}])
def test_malformed_fragment_ids_become_structured_issues(malformed):
    state,store=packet('Mira arrived.')
    row=choice('Mira');row['reference']['evidence_id']=malformed;row['kind']=['individual']
    result=review(output(row),state,store)
    assert result['subject_errors'] and any(issue['code'].startswith('output_') for issue in result['issues'])
    assert not result['bindings']['subject_ids']
    repair=build_repair_state(output(row),result,state)
    final=finish_repair(repair,output(choice('Mira')),state,store)
    assert not final['subject_errors']


@pytest.mark.parametrize('malformed',[['old'],{'id':'old'}])
def test_malformed_subject_target_in_memory_handoff_is_reported(malformed):
    state,store=packet('Mira arrived.')
    state['maintenance_route']=dict(order='memory_first',collision_groups=[dict(name='Mira',subject_ids=['old','other'])])
    state['memory_maintenance']=dict(participants=[dict(ref=0,mention='Mira',target_id=None,memory_ids=[])],items=[],reason='unresolved')
    row=choice('Mira',operation='reuse',target=malformed)
    result=review(output(row),state,store)
    assert result['subject_errors'] and any(issue['code'].startswith('output_') for issue in result['issues'])
    assert not result['bindings']['subject_ids']


@pytest.mark.parametrize('with_basis',[False,True])
def test_actual_sdk_schema_ref_nodes_have_no_sibling_keywords(with_basis):
    state,store=packet('Mireya Solano arrived.',sid='first',when=PAST)
    if with_basis:
        seeded=review(output(choice('Mireya Solano',names=['Mireya Solano'],sid='first')),state,store)
        commit(seeded,store)
        state,store=packet('Mireya Solano called.',store)
        assert state['identity_bases']
    else:
        assert not state['identity_bases']
    requests=[]
    def create(**request):
        requests.append(request)
        return SimpleNamespace(usage=None,choices=[SimpleNamespace(message=SimpleNamespace(content='{"subjects":[]}',refusal=None))])
    model=SimpleNamespace(model='gpt-4o-mini',timeout=5,calls=[],client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    maintain(model,state,deadline=monotonic()+5)
    schema=requests[0]['response_format']['json_schema']['schema']
    seen=[]
    def visit(value):
        if isinstance(value,dict):
            if '$ref' in value:
                assert set(value)=={'$ref'}, value
                seen.append(value['$ref'])
            for child in value.values():visit(child)
        elif isinstance(value,list):
            for child in value:visit(child)
    visit(schema)
    assert '#/$defs/IdentityReference' in seen
