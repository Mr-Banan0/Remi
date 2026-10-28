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


def test_leading_example_establishes_the_actual_user_with_name_aliases():
    payload,output=examples()[0]
    assert payload['new_user_source']['role']=='user'
    subject=output['subjects'][0]
    assert subject['name']=='@user' and len(subject['aliases'])>=2
    assert set(subject['reference'])=={'evidence_id','basis_id'}
