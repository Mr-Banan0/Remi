"""Field feedback, bounded repair, protected decisions and checkpoint recovery."""
from copy import deepcopy
from types import SimpleNamespace
from time import monotonic
import json
import pytest
from remi.subject_maintenance import review, commit, maintain
from remi.subject_repair import build_repair_state, finish_repair
from remi.engine import MemoryEngine
from tests.test_subject_reference_pipeline import nurse_state, Model, request
from tests.test_subject_maintenance import initial, raw_subjects
from tests.test_subject_identity import plan
from remi.providers import ProviderUnavailable, OutputInvalid
from scripts.validate_subject_identities import step_state


def bad_reference():
    state,store,raw,key=nurse_state()
    raw['subjects'][0]['reference']['evidence_id']='C999'
    return state,store,raw,key


def test_invalid_current_fragment_has_field_level_feedback():
    state,store,raw,key=bad_reference();result=review(raw,state,store)
    entry=next(e for e in result['issues'] if e['code']=='reference_requires_current_evidence')
    assert entry['ref']==0 and entry['path']==['subjects',0,'reference','evidence_id']
    assert entry['expected']['evidence_ids']==['C1'] and entry['source_ids']==['step-1']
    assert not result['bindings']['subject_ids']


def test_missing_basis_is_checked_with_invalid_current_fragment():
    state,store,raw,key=bad_reference();raw['subjects'][0]['reference']['basis_id']='B999'
    codes={e['code'] for e in review(raw,state,store)['issues']}
    assert {'reference_requires_supplied_basis','reference_requires_current_evidence'}<=codes


def test_wire_repair_preserves_current_fragment_ids_and_short_identity_ids():
    state,store,raw,key=bad_reference();reviewed=review(raw,state,store);repair=build_repair_state(raw,reviewed,state)
    fixed=deepcopy(raw);fixed['subjects'][0]['reference']['evidence_id']='C1'
    captured=[]
    def create(**body):
        captured.append(body);translated=deepcopy(fixed)
        previous=json.loads(body['messages'][-2]['content'])
        translated['subjects'][0]['target_id']=previous['subjects'][0]['target_id']
        return SimpleNamespace(usage=None,choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(translated),refusal=None))])
    model=SimpleNamespace(model='gpt-4o-mini',timeout=10,calls=[],client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    output,_=maintain(model,repair,deadline=monotonic()+20)
    body=captured[0];messages=body['messages'];text=messages[-1]['content']
    assert messages[-2]['role']=='assistant' and messages[-1]['role']=='user'
    assert json.loads(messages[-2]['content'])['subjects'][0]['reference']['evidence_id']=='C999'
    assert 'subjects[0].reference.evidence_id' in text and 'C1' in text
    assert output['subjects'][0]['target_id']==key and output['subjects'][0]['reference']['evidence_id']=='C1'


def test_unavailable_foreign_basis_is_reported_without_its_content():
    state,store,raw,key=nurse_state()
    basis=next(b for b in state['identity_bases'] if b['id']==raw['subjects'][0]['reference']['basis_id'])
    original={**state['source_catalog'][basis['source_id']],'owner':'other','content':'foreign private words'}
    store.put('sources',state['owner'],basis['source_id'],original)
    state['source_catalog'][basis['source_id']]=original
    result=review(raw,state,store)
    assert 'foreign private words' not in json.dumps(result['issues'])
    assert any(e['code'] in {'subject_source_requires_owned_supplied_original','reference_requires_supplied_basis'} for e in result['issues'])


def test_valid_subject_and_valid_additions_survive_omission_in_repair():
    state,store=initial('Mara owns the cat Sprig, also called Twig.')
    raw=raw_subjects(state);raw['subjects'][0]['name']='invented'
    raw['subjects'][1]['aliases']=[dict(name='Twig',source_id='current')]
    first=review(raw,state,store);repair=build_repair_state(raw,first,state)
    fixed=deepcopy(raw);fixed['subjects'][0]['name']='Mara';fixed['subjects']=fixed['subjects'][:1]
    final=finish_repair(repair,fixed,state,store);commit(final,store)
    assert not final['issues'] and len(store.list('objects','experiment'))==2 and not store.list('edges','experiment')
    pet=next(o for o in store.list('objects','experiment') if o['name']=='Sprig')
    assert 'Twig' in {a['name'] for a in pet['aliases']}
    assert 'repair_restored_valid_subject' in {e['code'] for e in final['repair_audit']}


def test_valid_subject_ignores_repair_renumbering_and_rewrites():
    state,store=initial('Mara owns the cat Sprig.');raw=raw_subjects(state);raw['subjects'][0]['name']='invented'
    first=review(raw,state,store);repair=build_repair_state(raw,first,state)
    changed=deepcopy(raw);changed['subjects'][0]['name']='Mara';changed['subjects'][1]['name']='altered'
    changed['subjects'][1]['ref']=20
    final=finish_repair(repair,changed,state,store)
    assert final['effective_output']['subjects'][1]==raw['subjects'][1]
    assert any(e['code']=='repair_ignored_new_subject_ref' for e in final['repair_audit'])
    assert final['bindings']['subject_ids']['1']==first['bindings']['subject_ids']['1']


def test_alias_failures_preserve_identity_and_valid_additions():
    state,store=initial('Tansy is my dog, also called Tan.');raw=plan(kind='animal',aliases=['Tan','fiction','cat'])
    first=review(raw,state,store);assert not first['subject_errors']
    assert {e['scope'] for e in first['issues']}=={'alias'}
    repair=build_repair_state(raw,first,state);changed=deepcopy(raw)
    changed['subjects'][0].update(name='other',operation='pending',aliases=[])
    final=finish_repair(repair,changed,state,store);commit(final,store)
    obj=store.list('objects','experiment')[0]
    assert obj['name']=='Tansy' and {a['name'] for a in obj['aliases']}=={'Tan'}
    assert not final['issues']


def test_duplicate_local_refs_can_be_split_while_valid_siblings_are_protected():
    state,store=initial();raw=raw_subjects(state);raw['subjects'][1]['ref']=0
    first=review(raw,state,store);repair=build_repair_state(raw,first,state)
    changed=raw_subjects(state);final=finish_repair(repair,changed,state,store)
    assert not final['issues'] and len(final['bindings']['subject_ids'])==2


def test_duplicate_existing_identity_refs_can_be_consolidated():
    state,store=initial('Mara owns the cat Sprig.');first=review(raw_subjects(state),state,store);commit(first,store)
    later=step_state(store,'experiment','Mara greeted Sprig.',1,[])
    raw=raw_subjects(later);extra=deepcopy(raw['subjects'][0]);extra['ref']=2;raw['subjects'].append(extra)
    initial_review=review(raw,later,store);repair=build_repair_state(raw,initial_review,later)
    fixed=deepcopy(raw);fixed['subjects'].pop()
    final=finish_repair(repair,fixed,later,store)
    assert not final['issues'] and len(final['bindings']['subject_ids'])==2


@pytest.mark.parametrize('bad',[None,[],{},dict(subjects='bad',connections=[])])
def test_malformed_repair_envelope_keeps_valid_sibling_and_records_failure(bad):
    state,store=initial();raw=raw_subjects(state);raw['subjects'][0]['name']='absent'
    reviewed=review(raw,state,store);repair=build_repair_state(raw,reviewed,state)
    final=finish_repair(repair,bad,state,store)
    assert '1' in final['bindings']['subject_ids'] and final['issues']
    assert '0' not in final['bindings']['subject_ids']


class RepairModel(Model):
    def __init__(self,fail_repair=False):super().__init__();self.payloads=[];self.fail_repair=fail_repair
    def maintain_subjects(self,state,*,deadline):
        self.payloads.append(deepcopy(state));self.calls.append('subject_repair' if state.get('repair_errors') else 'subject')
        if state.get('repair_errors'):
            if self.fail_repair:self.fail_repair=False;raise ProviderUnavailable('temporary_repair_outage')
            return plan(name='Nolan',aliases=['Nole'],sid=state['new_user_source']['id'])
        return plan(name='absent',aliases=['Nole'],sid=state['new_user_source']['id'])


def test_full_engine_uses_one_detailed_subject_repair_and_shared_budget():
    model=RepairModel();engine=MemoryEngine(model=model);engine.add(request())
    assert model.calls==['subject','subject_repair','extract','memory']
    repair=model.payloads[-1]
    assert repair['previous_output']['subjects'][0]['name']=='absent'
    assert any(row['path']==['subjects',0,'name'] for row in repair['repair_errors'])
    assert len(engine.store.memories('reference-owner'))==1


def test_failed_repair_resumes_same_saved_request_without_repeating_initial_model():
    model=RepairModel(fail_repair=True);engine=MemoryEngine(model=model)
    with pytest.raises(ProviderUnavailable):engine.add(request())
    engine.add(request())
    assert model.calls==['subject','subject_repair','subject_repair','extract','memory']
    assert model.payloads[1]==model.payloads[2]


def test_alias_only_failure_uses_same_repair_budget_with_identity_retained():
    class AliasModel(Model):
        def maintain_subjects(self,state,*,deadline):
            self.calls.append('repair' if state.get('repair_errors') else 'subject')
            row=plan(name='Nolan',sid=state['new_user_source']['id'])
            if not state.get('repair_errors'):row['subjects'][0]['aliases']=[dict(name='invented',source_id=state['new_user_source']['id'])]
            return row
    model=AliasModel();engine=MemoryEngine(model=model);engine.add(request())
    assert model.calls==['subject','repair','extract','memory'] and len(engine.store.memories('reference-owner'))==1


def test_subject_repair_budget_is_separate_from_append_first_memory_fallback():
    class BothInvalid(RepairModel):
        def propose(self,state,*,deadline):self.calls.append('memory');return dict(items=[dict(invalid=True)],reason='fixture')
    model=BothInvalid();engine=MemoryEngine(model=model);engine.add(request())
    assert model.calls==['subject','subject_repair','extract','memory']
    unit=engine.store.get('requests','reference-owner','r1')['units']['0']
    assert unit['memory_rejections'] and unit['phase']=='indexed'
    assert len(engine.store.memories('reference-owner'))==1
    assert unit['candidate_dispositions'][0]['operation']=='created_fallback'


def test_invalid_subject_json_is_repaired_once_and_then_remains_rejected():
    class Invalid(Model):
        def maintain_subjects(self,state,*,deadline):self.calls.append('subject');raise OutputInvalid('invalid_json')
    model=Invalid();engine=MemoryEngine(model=model);engine.add(request())
    assert model.calls==['subject','subject','extract','memory'] and not engine.store.list('objects','reference-owner')


@pytest.mark.parametrize('initial_output',[None,[],{},dict(subjects=[],connections=None),dict(subjects='invalid',connections=[]),dict(subjects=[None],connections=[])])
def test_malformed_initial_containers_are_recoverable_with_one_valid_response(initial_output):
    state,store=initial('Tansy is here.');first=review(initial_output,state,store)
    repair=build_repair_state(initial_output,first,state)
    final=finish_repair(repair,plan(),state,store)
    assert not final['issues'] and len(final['bindings']['subject_ids'])==1


def test_schema_feedback_reports_wrong_field_actual_value_and_path():
    state,store=initial('Tansy is here.');raw=plan();raw['subjects'][0]['kind']='device'
    reviewed=review(raw,state,store)
    entry=next(i for i in reviewed['issues'] if i['path']==['subjects',0,'kind'])
    assert entry['actual']=={'kind':'device'} and entry['expected']


def test_still_invalid_repair_remains_unbound_and_has_no_extra_call():
    state,store,raw,key=bad_reference();first=review(raw,state,store)
    final=finish_repair(build_repair_state(raw,first,state),raw,state,store)
    assert final['subject_errors'] and not final['bindings']['subject_ids']



def test_complete_repair_can_withdraw_unconfirmed_subject():
    state,store=initial('Mara owns the cat Sprig.');raw=raw_subjects(state)
    raw['subjects'][0]['name']='absent'
    first=review(raw,state,store);repair=build_repair_state(raw,first,state)
    returned=dict(subjects=[deepcopy(raw['subjects'][1])])
    final=finish_repair(repair,returned,state,store);commit(final,store)
    assert not final['issues'] and set(final['bindings']['subject_ids'])=={'1'}
    assert not store.list('edges','experiment')
    assert any(e['code']=='repair_withdrew_unconfirmed_subject' for e in final['repair_audit'])


def test_withdrawing_bad_proposal_keeps_fully_valid_siblings():
    state,store=initial('Mara owns the cat Sprig.');raw=raw_subjects(state)
    bad=deepcopy(raw['subjects'][0]);bad.update(ref=2,name='absent');raw['subjects'].append(bad)
    first=review(raw,state,store);repair=build_repair_state(raw,first,state)
    returned=dict(subjects=[])
    final=finish_repair(repair,returned,state,store);commit(final,store)
    assert not final['issues'] and len(store.list('objects','experiment'))==2 and not store.list('edges','experiment')
