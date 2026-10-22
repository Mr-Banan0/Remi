"""Direct examples receive originals and catalogs, with no extracted subject candidates."""
import json
from remi.add_judgments import SUBJECT_EXAMPLES,SUBJECT_PROMPT
from remi.models import SubjectMaintenanceDraft
from remi.providers import scoped_schema

def test_all_direct_examples_use_english_and_no_preextracted_candidates():
    assert len(SUBJECT_EXAMPLES)==12
    assert json.dumps(SUBJECT_EXAMPLES,ensure_ascii=False).isascii()
    for payload,output in SUBJECT_EXAMPLES:
        assert 'proposal' not in payload and 'subject_choices' not in payload
        SubjectMaintenanceDraft.model_validate(output)
    assert 'from CURRENT SOURCE' in SUBJECT_PROMPT

def test_direct_schema_allows_new_refs_without_a_prior_subject_list():
    schema=scoped_schema(SubjectMaintenanceDraft,SUBJECT_EXAMPLES[0][0])
    assert schema['properties']['subjects']['maxItems']==96
    assert 'enum' not in schema['$defs']['SubjectDraft']['properties']['ref']
    assert set(schema['properties'])=={'subjects','connections'}
    assert schema['$defs']['SubjectDraft']['properties']['operation']['enum']==['create','reuse','pending']


def test_leading_example_establishes_actual_user_and_preserves_personal_aliases():
    payload, output = SUBJECT_EXAMPLES[0]
    assert payload['new_user_source']['role'] == 'user'
    assert output['subjects'][0]['name'] == '@user'
    assert len(output['subjects']) == 1 and len(output['subjects'][0]['aliases']) >= 2
    assert all(set(a) == {'name', 'source_id'} for _, out in SUBJECT_EXAMPLES for row in out['subjects'] for a in row['aliases'])
    assert all(set(e) == {'source_id'} for _, out in SUBJECT_EXAMPLES for row in out['subjects'] for e in row['evidence'])
    quoted_payload, quoted_output = SUBJECT_EXAMPLES[1]
    assert 'wrote' in quoted_payload['new_user_source']['content']
    assert all(row['name'] != '@user' for row in quoted_output['subjects'])
