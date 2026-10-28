"""Evidence-backed identity maintenance invariants for the production pipeline."""
from copy import deepcopy

from remi.subject_maintenance import SubjectPlan,examples,review,commit,render,PROMPT
from remi.memory_maintenance import PROMPT as EVENT_PROMPT
from remi.subject_extraction import PROMPT as EXTRACTION_PROMPT
from remi.subject_references import prepare_reference_state
from remi.subject_repair import build_repair_state, finish_repair
from scripts.validate_subject_identities import prepare_state
from scripts.validate_subject_identities import step_state
from tests.helpers import direct_subjects_fixture


def initial(text='Mara owns the cat Sprig.'):
    state,store,ids=prepare_state(dict(new_user_source=dict(id='current',role='user',content=text),context_sources=[],subject_catalog=[],relation_catalog=[]),'experiment')
    return prepare_reference_state(state,store),store


def raw_subjects(state,names=('Mara','Sprig'),pending=()):
    raw=direct_subjects_fixture(state,list(names),kinds={name:'person' for name in names},pending=pending)
    for row in raw['subjects']:
        row.pop('evidence',None);row['reference']=dict(evidence_id='C1',basis_id=None)
        if row['operation']=='reuse':
            basis=next(b for b in state['identity_bases'] if b['target_id']==row['target_id'] and b['kind']=='name')
            row['reference']=dict(evidence_id='C1',basis_id=basis['id'])
    raw.pop('connections',None)
    return raw


def test_subject_commit_writes_identities_without_navigation_edges():
    state,store=initial();result=review(raw_subjects(state),state,store);commit(result,store)
    assert not result['issues'] and len(store.list('objects','experiment'))==2
    assert not store.list('edges','experiment') and not store.list('memories','experiment')


def test_subject_contract_rejects_connection_output_as_an_extra_field():
    state,store=initial();raw=raw_subjects(state)
    raw['connections']=[]
    result=review(raw,state,store)
    assert any(issue['code']=='response_extra_field' and issue['path']==['connections'] for issue in result['issues'])


def test_few_shots_execute_identity_checks():
    for payload,raw in examples():
        SubjectPlan.model_validate(raw)
        state,store,ids=prepare_state(payload,'fewshot-connection')
        # The independent example catalog uses short IDs, then prepare_state remaps storage IDs.
        state=prepare_reference_state(state,store)
        raw=deepcopy(raw)
        for row in raw['subjects']:
            if row['target_id']:row['target_id']=ids[row['target_id']]
            if row['reference']['basis_id']:
                original=next(b for b in payload['identity_bases'] if b['id']==row['reference']['basis_id'])
                basis=next(b for b in state['identity_bases'] if b['target_id']==ids[original['target_id']]
                           and b['kind']==original['kind'] and b['text']==original['text'])
                row['reference']['basis_id']=basis['id']
        result=review(raw,state,store)
        if any(issue['code']=='create_overlaps_recalled_identity' for issue in result['issues']):
            repair=build_repair_state(raw,result,state)
            result=finish_repair(repair,raw,state,store)
        assert not result['contract_errors'] and not result['subject_errors']
        assert not result['alias_issues']


def test_subject_contract_routes_relation_semantics_through_extraction_then_fixed_maintenance():
    assert 'substantive relationship claims as facts' in EXTRACTION_PROMPT
    assert 'their start, change or end as events' in EXTRACTION_PROMPT
    assert 'memory kind, participants, attribution, condition, status, time and evidence are fixed' in EVENT_PROMPT
    assert 'memory stage' not in PROMPT.casefold()
    assert ' fact' not in PROMPT.casefold() and 'preference' not in PROMPT.casefold() and 'event' not in PROMPT.casefold()
    assert 'connection' not in PROMPT.casefold()
    state,store=initial();text=render(state)
    assert 'EXISTING CONNECTIONS' not in text and 'EXISTING RELATIONSHIPS' not in text
    assert 'message_at' not in text and 'observed_at' not in text
