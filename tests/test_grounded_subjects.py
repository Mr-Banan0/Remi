"""Source-only evidence and known-name reuse preserve provenance and identity boundaries."""
from copy import deepcopy
import pytest
from remi.engine import MemoryEngine
from remi.models import SubjectMaintenanceDraft
from remi.providers import scoped_schema
from remi.identity import subject_hits
from tests.helpers import request
from tests.test_subject_and_extraction import WireModel
from tests.test_subject_memory import SubjectModel, unit


def known_person():
    engine = MemoryEngine(model=SubjectModel([('Aster', False, ['Ash'])]))
    engine.add(request('Aster is also called Ash.'))
    return engine


@pytest.mark.parametrize('name', ['Aster', 'Ash'])
def test_stored_identity_can_be_referenced_by_any_known_name(name):
    engine = known_person()
    before = deepcopy(engine.store.list('objects', 'u1'))
    def identity(state, raw):
        raw['subjects'][0].update(name=name)
        return raw
    model = WireModel([('Aster', False, [])], [('Ash likes tea.', [0], [], None)], identity=identity)
    engine.model = model
    engine.add(request('Ash likes tea.', request_id='r2'))
    assert engine.store.list('objects', 'u1') == before
    assert engine.store.memories('u1')[0].entity_ids == [before[0]['id']]
    assert not unit(engine, 'r2')['identity_errors']
    assert 'Ash' in model.calls[-1][1]['resolved_subjects'][0]['aliases']
    assert len(model.calls) == 2


@pytest.mark.parametrize('fault', ['unknown_name', 'unrelated_source'])
def test_reuse_requires_a_name_belonging_to_selected_id_and_grounded_source(fault):
    engine = known_person()
    def identity(state, raw):
        if fault == 'unknown_name':
            raw['subjects'][0]['name'] = 'Birch'
        return raw
    model = WireModel([('Aster', False, [])], [('A claim.', [0], [], None)], identity=identity)
    engine.model = model
    engine.add(request('Birch likes tea.' if fault == 'unrelated_source' else 'Ash met Birch.', request_id='r2'))
    assert not engine.store.memories('u1')
    assert unit(engine, 'r2')['candidate_dispositions'][0]['reason'] == 'candidate_identity_unresolved'


def test_future_alias_is_excluded_from_reuse_name_set():
    engine = known_person()
    engine.model = SubjectModel([('Aster', False, ['Later'])])
    engine.add(request('Aster is now called Later.', request_id='future', when='2024-06-09T00:00:00Z'))
    def identity(state, raw):
        raw['subjects'][0]['name'] = 'Later'
        return raw
    engine.model = WireModel([('Aster', False, [])], [('Ash likes tea.', [0], [], None)], identity=identity)
    engine.add(request('Ash likes tea.', request_id='past', when='2024-06-04T00:00:00Z'))
    assert unit(engine, 'past')['identity_errors'][0]['error'] == 'reuse_name_requires_selected_identity'
    assert not engine.store.memories('u1')


def test_program_copies_original_for_every_valid_alias_with_no_generated_quote():
    engine = MemoryEngine(model=SubjectModel([('Aster', False, ['Ash'])]))
    text = 'Aster is also called Ash. Aster likes tea.'
    engine.add(request(text))
    obj = engine.store.list('objects', 'u1')[0]
    assert all(a['quote'] == text for a in obj['aliases'])
    raw = unit(engine)['outputs']['subjects']['subjects'][0]
    assert raw['evidence'] == [{'source_id': engine.store.list('sources', 'u1')[0]['id']}]
    assert all(set(a) == {'name', 'source_id'} for a in raw['aliases'])


def test_old_generated_identity_quote_is_rejected_as_retired_contract():
    def old(state, raw):
        raw['subjects'][0]['evidence'][0]['quote'] = state['new_user_source']['content']
        return raw
    engine = MemoryEngine(model=WireModel([('Aster', False, [])], identity=old))
    engine.add(request('Aster is a person.'))
    assert not engine.store.list('objects', 'u1') and unit(engine)['identity_errors']


def test_subject_schema_selects_sources_while_relationship_schema_keeps_excerpt():
    schema = scoped_schema(SubjectMaintenanceDraft, dict(new_user_source=dict(id='S1', role='user', content='Aster owns Fern.'), subject_catalog=[], relation_catalog=[]))
    assert set(schema['$defs']['SourceCitation']['properties']) == {'source_id'}
    assert set(schema['$defs']['SubjectNameDraft']['properties']) == {'name', 'source_id'}
    assert 'quote' in schema['$defs']['ConnectionChangeDraft']['properties']


def test_known_alias_binding_keeps_ownership_relation_endpoints_usable():
    engine = known_person()
    before = engine.store.list('objects', 'u1')[0]['id']
    def canonical(state, raw):
        raw['subjects'][0]['name'] = 'Aster'
        return raw
    engine.model = WireModel([('Ash', False, []), ('Fern', False, [])],
        [('Ash owns the cat Fern.', [0, 1], [0, 1], 'Ash owns the cat Fern.')],
        kinds={'Fern': 'animal'}, identity=canonical)
    engine.add(request('Ash owns the cat Fern.', request_id='r2'))
    memory = engine.store.memories('u1')[0]
    assert before in memory.endpoint_ids and len(memory.endpoint_ids) == 2
    assert not unit(engine, 'r2')['identity_errors']
