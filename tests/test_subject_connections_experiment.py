"""Evidence-backed undirected experiment invariants, separate from production V32."""
from copy import deepcopy
import pytest
from pydantic import ValidationError

from experiments.subject_connections import ConnectionProposal,SubjectConnectionPlan,examples,experiment_state,review,commit,render,MEMORY_PROMPT
from scripts.validate_subject_identity import prepare_state
from scripts.validate_subject_connections import step_state
from tests.helpers import direct_subjects_fixture


def initial(text='Mara owns the cat Sprig.'):
    state,store,ids=prepare_state(dict(new_user_source=dict(id='current',role='user',content=text),context_sources=[],subject_catalog=[],relation_catalog=[]),'experiment')
    return experiment_state(state,store),store


def raw_subjects(state,names=('Mara','Sprig'),pending=()):
    raw=direct_subjects_fixture(state,list(names),kinds={'Sprig':'animal'},pending=pending)
    raw['connections']=[dict(subject_refs=[0,1],evidence=[dict(source_id=state['new_user_source']['id'])])]
    return raw


def test_pair_has_one_stable_id_and_appends_sources_across_order_reversal():
    state,store=initial();first=review(raw_subjects(state),state,store);commit(first,store)
    edge=store.list('edges','experiment')[0]
    next_state=step_state(store,'experiment','Sprig lives with Mara.',1,[state['new_user_source']])
    raw=raw_subjects(next_state);raw['connections'][0]['subject_refs']=[1,0]
    second=review(raw,next_state,store);commit(second,store);commit(second,store)
    edges=store.list('edges','experiment')
    assert len(edges)==1 and edges[0]['id']==edge['id']
    assert set(edges[0]['source_ids'])=={'current','step-1'}
    assert set(edges[0])=={'id','owner','endpoint_ids','source_ids'}
    assert not store.list('memories','experiment')


@pytest.mark.parametrize('fault',['same_ref','undeclared_ref','foreign_source','historical_only'])
def test_invalid_connections_are_rejected_with_valid_identities_preserved(fault):
    state,store=initial();raw=raw_subjects(state)
    if fault=='same_ref':raw['connections'][0]['subject_refs']=[0,0]
    elif fault=='undeclared_ref':raw['connections'][0]['subject_refs']=[0,20]
    elif fault=='foreign_source':raw['connections'][0]['evidence'].append(dict(source_id='foreign'))
    else:raw['connections'][0]['evidence']=[dict(source_id='historical')]
    result=review(raw,state,store);commit(result,store)
    assert result['connection_errors'] and not result['subject_errors']
    assert len(store.list('objects','experiment'))==2 and not store.list('edges','experiment')


def test_unresolved_identity_keeps_connection_pending_without_physical_edge():
    state,store=initial();raw=raw_subjects(state,pending=[1])
    result=review(raw,state,store);commit(result,store)
    assert not result['connection_errors'] and len(result['pending'])==1
    assert not store.list('edges','experiment')


def test_same_ref_is_rejected_even_when_the_identity_is_pending():
    state,store=initial();raw=raw_subjects(state,pending=[0])
    raw['connections'][0]['subject_refs']=[0,0]
    result=review(raw,state,store)
    assert result['connection_errors'] and not result['pending']


def test_semantic_relation_fields_are_outside_the_experiment_contract():
    row=dict(subject_refs=[0,1],evidence=[dict(source_id='S1')])
    for field,value in [('mode','asserted'),('operation','update'),('quote','works at'),('target_id','old'),('previous_quote','old')]:
        with pytest.raises(ValidationError):ConnectionProposal.model_validate({**row,field:value})


def test_few_shots_execute_identity_checks_and_undirected_compilation():
    for payload,raw in examples():
        SubjectConnectionPlan.model_validate(raw)
        state,store,ids=prepare_state(payload,'fewshot-connection')
        # The independent example catalog uses short IDs, then prepare_state remaps storage IDs.
        state=experiment_state(state,store)
        raw=deepcopy(raw)
        for row in raw['subjects']:
            if row['target_id']:row['target_id']=ids[row['target_id']]
        result=review(raw,state,store)
        assert not result['contract_errors'] and not result['subject_errors']
        assert not result['alias_issues'] and not result['connection_errors']


def test_memory_contract_retains_relationship_claims_and_removes_the_old_exclusion():
    assert 'Extract concrete\nrelationship statements as facts' in MEMORY_PROMPT
    assert 'only naming or continuing' not in MEMORY_PROMPT
    state,store=initial();text=render(state)
    assert 'EXISTING CONNECTIONS' in text and 'EXISTING RELATIONSHIPS' not in text
    assert 'message_at' not in text and 'observed_at' not in text
