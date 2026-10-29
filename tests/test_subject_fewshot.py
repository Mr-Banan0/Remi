"""Production subject examples match the current identity-only contract."""
import json

from remi.providers import scoped_schema
from remi.subject_maintenance import PROMPT, SubjectPlan, examples


def test_examples_are_english_and_validate_against_the_current_schema():
    rows=examples()
    assert len(rows)>=3
    assert json.dumps(rows,ensure_ascii=False).isascii()
    for _,output in rows:
        SubjectPlan.model_validate(output)
    assert 'Resolve the identities in CURRENT SOURCE' in PROMPT


def test_schema_exposes_only_identity_subject_rows():
    payload,_=examples()[0]
    schema=scoped_schema(SubjectPlan,payload)
    assert set(schema['properties'])=={'subjects'}
    assert schema['properties']['subjects']['maxItems']==96
    assert schema['$defs']['SubjectDraft']['properties']['operation']['enum']==['create','reuse','pending']
    assert schema['$defs']['SubjectDraft']['properties']['reason']['enum']==['same_identity','no_matching_identity','explicitly_distinct','ambiguous_identity','canonical_user']


def test_leading_example_establishes_the_actual_user_with_name_aliases():
    payload,output=examples()[0]
    assert payload['new_user_source']['role']=='user'
    subject=output['subjects'][0]
    assert subject['reason']=='canonical_user' and subject['name']=='Elena'
    assert [alias['name'] for alias in subject['aliases']]==['Ellie']
    assert set(subject['reference'])=={'evidence_id','basis_id'}


def test_early_examples_cover_existing_user_alias_and_distinct_namesake():
    rows=examples()
    alias=rows[1][1]['subjects'][0]
    distinct=rows[2][1]['subjects'][-1]
    assert alias['operation']=='reuse' and alias['name']=='@user' and [row['name'] for row in alias['aliases']]==['Rio']
    assert distinct['operation']=='create' and distinct['reason']=='explicitly_distinct'
