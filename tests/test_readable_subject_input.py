"""Wire-level evidence, ID restoration and repair contracts for readable subject input."""
from copy import deepcopy
import json
from time import monotonic
from types import SimpleNamespace

from remi.add_judgments import SUBJECT_EXAMPLES
from remi.providers import GPTModel
from remi.subject_input import render_subject_input


def source(key, text):
    return dict(id=key, role='user', content=text, message_at='2028-02-01T10:00:00+00:00')


def capture(state, result=None):
    sent = {}
    def create(**kwargs):
        sent.update(kwargs)
        return SimpleNamespace(usage=None, choices=[SimpleNamespace(message=SimpleNamespace(
            content=json.dumps(result or dict(subjects=[], connections=[])), refusal=None))])
    sdk = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    model = GPTModel('offline', 'https://test.invalid', client=sdk)
    output = model.maintain_subjects(state, deadline=monotonic()+5)
    return sent, output


def test_text_input_keeps_originals_and_identity_evidence_with_no_retrieval_or_clock_fields():
    previous=source('old', 'Cedar is my neighbor.')
    current=source('new', 'I met another Cedar on February 2.')
    state=dict(new_user_source=current,context_sources=[previous],subject_catalog=[dict(id='stored',
        name='Cedar',kind='person',is_user=False,matched_names=['Cedar'],aliases=[dict(name='Cedar',
        source_id='old',quote=previous['content'],observed_at='2028-02-01T10:00:00+00:00')])],
        relation_catalog=[],history=[])
    before=deepcopy(state)
    sent,_=capture(state)
    text=sent['messages'][-1]['content']
    assert current['content'] in text and previous['content'] in text
    assert 'Subject E1 | type: person | name: Cedar' in text
    assert text.count(previous['content'])==1
    assert 'Identity evidence: source S2.' in text
    assert all(key not in text for key in ['matched_names','message_at','observed_at','source_reference_time'])
    assert '2028-02-01T10:00:00+00:00' not in text
    assert state==before


def test_rendered_demonstrations_use_live_text_protocol_and_json_answers():
    sent,_=capture(dict(new_user_source=source('new','A current statement.'),context_sources=[],subject_catalog=[],relation_catalog=[]))
    for (payload,expected),request,answer in zip(SUBJECT_EXAMPLES,sent['messages'][1:-1:2],sent['messages'][2:-1:2]):
        assert request['content']==render_subject_input(payload)
        assert request['content'].startswith('CURRENT SOURCE')
        assert json.loads(answer['content'])==expected
    assert len(sent['messages'])==2*len(SUBJECT_EXAMPLES)+2


def test_text_protocol_keeps_scoped_schema_and_restores_source_and_stored_ids():
    state=dict(new_user_source=source('current','Ash keeps a dog.'),context_sources=[],relation_catalog=[],
        subject_catalog=[dict(id='obj_existing',name='Aster',kind='person',aliases=[dict(name='Ash',quote='Aster is also called Ash.')])])
    result=dict(subjects=[dict(ref=0,operation='reuse',target_id='E1',name='Aster',kind='person',aliases=[],evidence=[dict(source_id='S1')])],connections=[])
    sent,output=capture(state,result)
    assert output['subjects'][0]['target_id']=='obj_existing'
    assert output['subjects'][0]['evidence']==[dict(source_id='current')]
    schema=sent['response_format']['json_schema']['schema']
    assert schema['$defs']['SubjectDraft']['properties']['target_id']['anyOf'][0]['enum']==['E1']
    assert schema['$defs']['SourceCitation']['properties']['source_id']['enum']==['S1']
    assert sent['temperature']==0


def test_history_preserves_attribution_modes_and_ordered_relationship_participants():
    old=source('prior', 'Maren wrote: I hope to join Westhaven Team.')
    state=dict(new_user_source=source('now','Maren joined.'),context_sources=[],subject_catalog=[],
        relation_catalog=[dict(id='relationship',content='I hope to join Westhaven Team.',mode='document',
            endpoint_ids=['a','b'],ordered_subject_ids=['b','a'],sources=[old])],
        history=[dict(id='fact',record_type='fact',content='Maren has an invitation.',entity_ids=['b'],
            mode='document',context='After approval',sources=[old])])
    sent,_=capture(state)
    text=sent['messages'][-1]['content']
    assert 'ordered participants: b -> a | expression: document' in text
    assert 'Maren has an invitation.' in text and 'Condition: After approval' in text
    assert old['content'] in text and text.count(old['content'])==1
    assert 'speaker: user' in text


def test_repair_text_retains_fixed_decisions_rejected_refs_and_validation_errors():
    state=dict(new_user_source=source('now','Cedar left the team.'),context_sources=[],subject_catalog=[],relation_catalog=[],
        accepted_subjects=[dict(ref=4,operation='create',target_id=None,name='Cedar',kind='person',aliases=[],evidence=[dict(source_id='now')])],
        accepted_connections=[],rejected_subjects=[],rejected_connections=[dict(ref=7,subject_refs=[4,99],operation='create')],
        repair_errors=[dict(stage='connections',ref='7',error='contact_requires_declared_subject_refs')])
    sent,_=capture(state)
    text=sent['messages'][-1]['content']
    assert 'FIXED ACCEPTED SUBJECT DECISIONS' in text and 'ref: 4' in text
    assert 'RELATIONSHIP DECISIONS TO CORRECT' in text and 'ref: 7' in text
    assert 'contact_requires_declared_subject_refs' in text
    assert 'source_id: S1' in text
