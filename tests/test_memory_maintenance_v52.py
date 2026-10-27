"""V52 protocol closure: candidate coverage, identity targets and compact model input."""
import json
from time import monotonic
from types import SimpleNamespace

from experiments.event_maintenance import examples, provider_plan
from experiments.subject_engine import SubjectGraphEngine, SubjectMemoryStore
from experiments.subject_recall import pack_history, prepare_candidates, RECALL_BYTES
from tests.test_event_maintenance import Model, OWNER, extracted, req, row, unit
from tests.test_keyword_subject_recall import add_subject, state, source


def test_long_evidence_history_keeps_current_view_in_recall_budget():
    store=SubjectMemoryStore();add_subject(store,'Edda')
    packet,_=prepare_candidates(store,state('Edda confirmed the schedule.'))
    sources=[source('history-'+str(index),('context '+str(index)+' ')*900) for index in range(50)]
    card=dict(id='long-supported-memory',content='Edda confirmed the schedule.',record_type='fact',entity_ids=[],sources=sources)
    filled,trace=pack_history(store,packet,[card])
    assert trace['selected_memory_ids']==['long-supported-memory']
    assert trace['compacted_memory_ids']==['long-supported-memory']
    assert len(filled['history'][0]['sources'])<=3 and trace['recall_bytes']<=RECALL_BYTES


def test_every_extracted_candidate_receives_a_disposition_including_omissions():
    model=Model();engine=SubjectGraphEngine(model=model)
    def extractor(state):
        return dict(items=[extracted(state,content='Aster plans a picnic.'),
            extracted(state,content='Aster plans a walk.')],reason='two candidates')
    model.extractor=extractor
    model.writer=lambda state:[row(state,ref='C1',content='Aster plans a picnic.')]
    engine.add(req('Aster plans a picnic and a walk.','coverage'))
    dispositions=unit(engine,'coverage')['candidate_dispositions']
    assert {(entry['candidate_id'],entry['operation']) for entry in dispositions}=={
        ('C1','created'),('C2','validation_failed')}
    omitted=next(entry for entry in dispositions if entry['candidate_id']=='C2')
    assert 'candidate_omitted_by_model' in omitted['reason_codes']


def test_invalid_returned_candidate_has_specific_validation_disposition():
    model=Model();engine=SubjectGraphEngine(model=model)
    model.writer=lambda state:[dict(row(state),operation='update',target_id='missing')]
    engine.add(req('Aster plans a picnic.','invalid'))
    disposition=unit(engine,'invalid')['candidate_dispositions'][0]
    assert disposition['candidate_id']=='C1' and disposition['operation']=='validation_failed'
    assert 'update_requires_eligible_recalled_same_kind' in disposition['reason_codes']


def test_prompt_lists_fixed_candidate_fields_and_same_kind_targets():
    state,answer=list(examples())[1];captured=[]
    def create(**request):
        captured.append(request)
        return SimpleNamespace(usage=None,choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(answer),refusal=None))])
    model=SimpleNamespace(model='test',timeout=10,calls=[],client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    provider_plan(model,state,deadline=monotonic()+10)
    body=captured[0]['messages'][-1]['content']
    props=captured[0]['response_format']['json_schema']['schema']['$defs']['MemoryDecision']['properties']
    assert '# CANDIDATE MEMORIES' in body and 'participants: P0' in body
    assert 'eligible same-kind targets: M1' in body
    assert set(props)=={'ref','operation','target_id','content','pending_reason'}


def test_program_owned_fields_are_absent_from_all_few_shot_answers():
    forbidden={'record_type','participant_handles','subject_refs','mode','context','status','time_expression',
               'evidence','planned_at','event_at','participant_action','links'}
    for _,answer in examples():
        for item in answer['items']:
            assert not forbidden.intersection(item)
