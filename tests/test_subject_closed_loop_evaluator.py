"""Independent evaluator checks distinguish valid protocol from identity and graph mistakes."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from scripts.validate_subject_closed_loop import (
    cases, claim_checks, evaluate_step, json_return_counts, matches, request_for, summarize, validate_cases,
)

OWNER='evaluator-owner'


def person(oid,name,aliases=(),user=False,descriptions=()):
    return dict(id=oid,name=name,is_user=user,kind='individual',
        aliases=[dict(name=alias) for alias in aliases],
        descriptions=[dict(text=value) for value in descriptions])


class Store:
    def __init__(self,objects,participants):
        self.objects=objects
        self.records=[SimpleNamespace(id='memory',current=SimpleNamespace(source_ids=['current'],entity_ids=participants))]
    def list(self,table,owner):
        assert table=='objects' and owner==OWNER
        return self.objects
    def memories(self,owner):
        assert owner==OWNER
        return self.records


def packet(rows,bindings,*,name_hits=()):
    return dict(outputs=dict(subjects=dict(subjects=rows,connections=[]),propose=dict(items=[])),
        identity=dict(bindings=dict(subject_ids=bindings),issues=[],contract_errors=[],subject_errors={}),
        shared_recall=dict(new_user_source=dict(id='current'),
            maintenance_route=dict(order='subject_first',name_matches=[dict(name='Delia',subject_ids=list(name_hits))],collision_groups=[])))


def baseline():
    case=dict(id='simple',identities={'delia':['Delia Fenn','Delia']})
    specification=dict(text='Delia helped me.',subjects=['user','delia'],aliases={'delia':['Delia']},recall=['delia'])
    objects=[person('u','user',user=True),person('d','Delia Fenn',['Delia'])]
    rows=[dict(ref=0,name='@user',operation='reuse',target_id='u'),dict(ref=1,name='Delia Fenn',operation='reuse',target_id='d')]
    return case,specification,objects,packet(rows,{'0':'u','1':'d'},name_hits=['d']),{'user':'u','delia':'d'}


def evaluate(bundle,participants=('u','d')):
    case,specification,objects,unit,oracle=deepcopy(bundle)
    return evaluate_step(case,specification,Store(objects,list(participants)),OWNER,unit,oracle,{row['id'] for row in objects})


def test_fixed_independent_scenarios_validate_and_have_three_turns():
    values=validate_cases(cases())
    assert len(values)==8 and all(len(case['steps'])==3 for case in values)
    assert {case['id'] for case in values}>={'english_full_to_short','chinese_full_to_short',
        'single_candidate_new_namesake','unnamed_person_gets_name','quoted_first_person',
        'participant_binding_and_scope','historical_candidates_stay_out'}


def test_new_session_follows_the_stored_alias_turn():
    case=cases()[0]
    assert request_for(case,'o',1).session_id!=request_for(case,'o',2).session_id
    assert case['steps'][1]['aliases']=={'delia':['Delia']}
    assert case['steps'][2]['recall']==['delia']


def test_correct_subject_alias_recall_and_materialized_memory_close_the_loop():
    assert evaluate(baseline())['passed']


def test_correct_subject_with_wrong_memory_participant_fails_memory_dimension():
    result=evaluate(baseline(),participants=['u','other'])
    assert result['dimensions']['identity']
    assert not result['dimensions']['memory_binding']
    assert not result['passed']


def test_rejected_current_ref_counts_coverage_but_fails_accepted_identity():
    bundle=baseline();bundle[3]['identity']['bindings']['subject_ids'].pop('1')
    result=evaluate(bundle)
    assert result['dimensions']['coverage']
    assert not result['dimensions']['identity']


def test_historical_extra_row_has_separate_scope_failure_without_current_identity_failure():
    bundle=baseline()
    bundle[3]['outputs']['subjects']['subjects'].append(dict(ref=2,name='Old candidate',operation='pending',target_id=None))
    result=evaluate(bundle)
    assert result['dimensions']['identity'] and result['dimensions']['coverage']
    assert not result['dimensions']['current_scope']


def test_description_does_not_count_as_saved_alias_or_identity_name():
    bundle=baseline();bundle[2][1]['aliases']=[];bundle[2][1]['descriptions']=[dict(text='Delia')]
    result=evaluate(bundle)
    assert not result['dimensions']['alias']
    assert not matches(person('x','different',descriptions=['Delia']),['Delia'])


def test_next_turn_requires_actual_current_name_recall():
    bundle=baseline();bundle[3]['shared_recall']['maintenance_route']['name_matches']=[]
    result=evaluate(bundle)
    assert result['dimensions']['identity']
    assert not result['dimensions']['recall']


def test_single_candidate_namesake_requires_a_new_identity():
    case=dict(id='newcomer',identities={'old':['Corin'],'new':['Corin','Cora']})
    specification=dict(text='I met another Corin.',subjects=['user','new'],new=['new'],distinct=[['old','new']])
    objects=[person('u','user',user=True),person('old','Corin')]
    unit=packet([dict(ref=0,name='@user',target_id='u'),dict(ref=1,name='Corin',target_id='old')],{'0':'u','1':'old'})
    result=evaluate_step(case,specification,Store(objects,['u','old']),OWNER,unit,{'user':'u','old':'old'},{'u','old'})
    assert result['dimensions']['coverage']
    assert not result['dimensions']['identity']
    assert not result['dimensions']['memory_binding']


def test_new_namesake_is_distinct_and_nickname_stored():
    case=dict(id='newcomer',identities={'old':['Corin'],'new':['Corin','Cora']})
    specification=dict(text='I met another Corin called Cora.',subjects=['user','new'],new=['new'],aliases={'new':['Cora']},distinct=[['old','new']])
    objects=[person('u','user',user=True),person('old','Corin'),person('new','Corin',['Cora'])]
    unit=packet([dict(ref=0,name='@user',target_id='u'),dict(ref=1,name='Corin',target_id=None,operation='create')],{'0':'u','1':'new'})
    result=evaluate_step(case,specification,Store(objects,['u','new']),OWNER,unit,{'user':'u','old':'old'},{'u','old'})
    assert result['passed']


def test_quoted_first_person_does_not_authorize_api_user_binding():
    case=dict(id='quoted',identities={'speaker':['Soraya'],'pet':['Pecan']})
    specification=dict(text='Soraya wrote: I adopted Pecan.',subjects=['speaker','pet'])
    objects=[person('s','Soraya'),person('p','Pecan'),person('u','user',user=True)]
    unit=packet([dict(ref=0,name='Soraya',target_id='s'),dict(ref=1,name='Pecan',target_id='p'),dict(ref=2,name='@user',target_id='u')],{'0':'s','1':'p','2':'u'})
    result=evaluate_step(case,specification,Store(objects,['s','p','u']),OWNER,unit,{'speaker':'s','pet':'p','user':'u'},{'s','p','u'})
    assert not result['dimensions']['identity'] and not result['dimensions']['memory_binding']


def test_shared_repair_budget_is_distinct_from_model_json_syntax():
    bundle=baseline();bundle[3]['outputs'].update(subjects_repair=bundle[3]['outputs']['subjects'],propose_repair={})
    assert not evaluate(bundle)['dimensions']['call_budget']
    assert json_return_counts([{'response':{'content':'{}'}},{'response':{'content':'[]'}},
        {'response':{'content':'{'}},{'error_type':'ConnectError'}])==dict(
            responses=3,syntax_valid=2,object_valid=1,object_invalid=1,syntax_invalid=1,no_response_attempts=1)


def test_dry_run_subject_results_remain_unevaluated():
    summary=summarize([dict(id='a',steps=[],passed=None,dry_run=True)],
                      [dict(id='a',steps=[{},{}])],1,True,0)
    assert summary['completed_steps']==0 and summary['planned_steps']==2
    assert summary['unevaluated_runs']==1 and summary['passed_runs']==0
    assert summary['semantic_identity_accuracy'] is None


def test_fixture_rejects_new_identity_outside_current_subjects():
    with pytest.raises(ValueError,match='new_identity_requires_current_subject'):
        validate_cases([dict(id='bad',identities={'named':['Named']},steps=[dict(text='hello',subjects=['user'],new=['named'])])])


def action_memory(mid,content,participants):
    return SimpleNamespace(id=mid,current=SimpleNamespace(source_ids=['current'],content=content,entity_ids=participants))


def action_case():
    case=next(case for case in cases() if case['id']=='participant_binding_and_scope')
    return case,case['steps'][1],{'user':'u','rosa':'r','tomas':'t'}


def test_claim_oracle_rejects_swapped_ids_despite_correct_union():
    case,specification,oracle=action_case()
    memories=[action_memory('display','Rosa prepared the display board.',['t']),
              action_memory('stand','Tomas repaired the folding stand.',['r'])]
    assert {oid for memory in memories for oid in memory.current.entity_ids}=={'r','t'}
    checks=claim_checks(case,specification,memories,oracle)
    assert not all(check['passed'] for check in checks)
    assert sum(not check['passed'] for check in checks)==2


def test_claim_oracle_accepts_complete_joint_proposition():
    case,specification,oracle=action_case()
    checks=claim_checks(case,specification,[action_memory('joint',
        'Rosa prepared the display board while Tomas repaired the folding stand.',['u','r','t'])],oracle)
    assert checks and all(check['passed'] for check in checks)


def test_claim_oracle_rejects_swapped_action_owners_in_joint_content():
    case,specification,oracle=action_case()
    checks=claim_checks(case,specification,[action_memory('joint',
        'Tomas prepared the display board while Rosa repaired the folding stand.',['r','t'])],oracle)
    assert sum(not check['passed'] for check in checks)==2
    assert all('content_owner' in check['name'] for check in checks if not check['passed'])


def test_claim_oracle_accepts_supported_synonyms_and_passive_word_order():
    case,specification,oracle=action_case()
    checks=claim_checks(case,specification,[
        action_memory('display','The exhibit panel was arranged by Rosa.',['r']),
        action_memory('stand','Tomas fixed the foldable stand.',['t'])],oracle)
    assert all(check['passed'] for check in checks)


def test_missing_claim_is_explicit_failure_instead_of_unscored_success():
    case,specification,oracle=action_case()
    checks=claim_checks(case,specification,[action_memory('display',
        'Rosa prepared the display board.',['r'])],oracle)
    assert [check['name'] for check in checks if not check['passed']]==['claim_stand_repair_covered']


def test_quote_adoption_requires_author_and_pet_in_the_same_claim_record():
    case=next(case for case in cases() if case['id']=='quoted_first_person')
    specification=case['steps'][0];oracle={'user':'u','soraya':'s','pecan':'p'}
    memories=[action_memory('adoption','Pecan was adopted as a pet rabbit.',['p']),
              action_memory('note','Soraya wrote a note.',['s'])]
    assert {oid for memory in memories for oid in memory.current.entity_ids}=={'s','p'}
    checks=claim_checks(case,specification,memories,oracle)
    assert not all(check['passed'] for check in checks)
    good=claim_checks(case,specification,[action_memory('adoption',
        'Soraya adopted a rabbit named Pecan.',['s','p'])],oracle)
    assert all(check['passed'] for check in good)
