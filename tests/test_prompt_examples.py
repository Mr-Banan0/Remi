"""Executable few-shot responses match the direct source and fixed-memory contracts."""
from copy import deepcopy
import pytest
from remi.add_judgments import PROPOSE_EXAMPLES,SUBJECT_EXAMPLES
from remi.models import SubjectMaintenanceDraft,ProposalDraft
from remi.add_pipeline import AddPipeline
from remi.engine import MemoryEngine
from remi.subjects import decode_subjects,materialize_subjects
from scripts.validate_subject_identity import prepare_state

@pytest.mark.parametrize('payload,output',SUBJECT_EXAMPLES)
def test_subject_examples_execute_with_source_evidence_and_valid_targets(payload,output):
    state,store,ids=prepare_state(payload,'fewshot')
    raw=deepcopy(output)
    for row in raw['subjects']:
        if row['target_id']:row['target_id']=ids[row['target_id']]
    SubjectMaintenanceDraft.model_validate(raw)
    decisions,errors=decode_subjects(raw,state,store);assert not errors,errors
    assert not state['subject_alias_issues'],state['subject_alias_issues']
    bound=materialize_subjects(state,decisions,store)
    good,errors=AddPipeline(MemoryEngine(store))._connections(raw,state,bound)
    assert not errors,errors
    assert len(good)==len(raw['connections'])

@pytest.mark.parametrize('payload,output',PROPOSE_EXAMPLES)
def test_ordinary_examples_use_fixed_refs_and_original_sources(payload,output):
    state={**payload,'proposal':{'subjects':[dict(ref=s['ref'],name=s['name'],kind=s['kind'],is_user=s['name']=='@user',aliases=s['aliases'],evidence=[]) for s in payload['resolved_subjects']]}}
    result=AddPipeline(MemoryEngine())._proposals(output,state,[])
    assert len(result.items)==len(output['items'])

def test_extraction_examples_cover_all_four_types_and_have_no_maintenance_operations():
    assert {item['record_type'] for _,output in PROPOSE_EXAMPLES for item in output['items']}=={'fact','preference','experience','event'}
    for _,output in PROPOSE_EXAMPLES:
        ProposalDraft.model_validate(output)
        assert all('operation' not in row and 'target_id' not in row for row in output['items'])
