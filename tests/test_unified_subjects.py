"""Unified identity operations preserve useful memories across independent alias failures."""
from copy import deepcopy

import pytest

from remi.engine import MemoryEngine
from remi.models import SubjectMaintenanceDraft
from remi.providers import ProviderUnavailable
from tests.helpers import request
from tests.test_subject_memory import SubjectModel, unit
from tests.test_subject_and_extraction import WireModel


@pytest.mark.parametrize('fault', ['extra_quote', 'source', 'missing_name', 'shape', 'container'])
@pytest.mark.parametrize('existing', [False, True])
def test_bad_alias_preserves_confirmed_identity_valid_alias_and_memory(fault, existing):
    engine = MemoryEngine(model=SubjectModel([('Mira', False, [])]))
    if existing:
        engine.add(request('Mira is my colleague.'))
    before = deepcopy(engine.store.list('objects', 'u1'))
    def aliases(state, raw):
        row = raw['subjects'][0]
        bad = dict(name='Invented', source_id=state['new_user_source']['id'])
        if fault == 'extra_quote':
            bad['quote'] = 'Obsolete generated quote'
        if fault == 'source':
            bad.update(name='Mimi', source_id='foreign')
        elif fault == 'missing_name':
            bad['name'] = 'AbsentName'
        elif fault == 'shape':
            bad = {'unexpected': 'value'}
        row['aliases'] = 'malformed' if fault == 'container' else [bad, dict(
            name='Mimi', source_id=state['new_user_source']['id'])]
        return raw
    model = WireModel([('Mira', False, [])], [('Mira likes tea.', [0], [], None)], identity=aliases)
    engine.model = model
    engine.add(request('Mira is also called Mimi. Mira likes tea.', request_id='r2'))
    objects = engine.store.list('objects', 'u1')
    assert len(objects) == 1 and (not before or objects[0]['id'] == before[0]['id'])
    names = {a['name'] for a in objects[0]['aliases']}
    assert 'Invented' not in names
    assert ('Mimi' in names) == (fault != 'container')
    assert engine.store.memories('u1')[0].entity_ids == [objects[0]['id']]
    assert not unit(engine, 'r2')['identity_errors']
    assert unit(engine, 'r2')['subject_alias_issues']
    assert ('Mimi' in model.calls[-1][1]['resolved_subjects'][0]['aliases']) == (fault != 'container')
    assert [name for name, _ in model.calls] == ['subjects', 'propose']


def test_bad_identity_evidence_keeps_dependent_memory_pending_despite_valid_alias():
    def invalid(state, raw):
        raw['subjects'][0]['evidence'][0]['source_id'] = 'foreign'
        return raw
    engine = MemoryEngine(model=WireModel([('Mira', False, ['Mimi'])],
        [('Mira likes tea.', [0], [], None)], identity=invalid))
    engine.add(request('Mira is also called Mimi. Mira likes tea.'))
    assert not engine.store.list('objects', 'u1') and not engine.store.memories('u1')
    assert unit(engine)['candidate_dispositions'][0]['reason'] == 'candidate_identity_unresolved'


def test_reuse_without_new_names_preserves_subject_and_supports_changed_event():
    engine = MemoryEngine(model=SubjectModel([('Mira', False, [])]))
    engine.add(request('Mira is my colleague.'))
    before = deepcopy(engine.store.list('objects', 'u1'))
    model = SubjectModel([('Mira', False, [])], [('Mira rescheduled the visit.', [0], [], None)])
    engine.model = model
    engine.add(request('Mira rescheduled the visit.', request_id='r2'))
    raw = unit(engine, 'r2')['outputs']['subjects']
    assert set(raw) == {'subjects', 'connections'}
    assert raw['subjects'][0]['operation'] == 'reuse'
    assert engine.store.list('objects', 'u1') == before
    assert engine.store.memories('u1')[0].entity_ids == [before[0]['id']]
    assert len(model.calls) == 2


def test_repair_receives_rejected_rows_and_preserves_accepted_sibling():
    def repair(state, raw):
        if state.get('repair_errors'):
            assert [r['ref'] for r in state['accepted_subjects']] == [0]
            assert [r['ref'] for r in state['rejected_subjects']] == [1, 1]
            assert state['rejected_subjects'][0]['name'] == 'Orin'
            raw['subjects'] = [r for r in raw['subjects'] if r['ref'] == 1]
        else:
            raw['subjects'].append(deepcopy(raw['subjects'][1]))
        return raw
    model = WireModel([('Ren', False, []), ('Orin', False, [])],
        [('Ren and Orin met.', [0, 1], [], None)], identity=repair)
    engine = MemoryEngine(model=model)
    engine.add(request('Ren and Orin met.'))
    assert not unit(engine)['identity_errors']
    assert len(engine.store.list('objects', 'u1')) == 2
    assert len(engine.store.memories('u1')[0].entity_ids) == 2
    assert len(model.calls) == 3


def test_two_refs_reusing_same_identity_remain_unresolved_with_valid_sibling():
    engine = MemoryEngine(model=SubjectModel([('Ren', False, [])]))
    engine.add(request('Ren is a colleague.'))
    target = engine.store.list('objects', 'u1')[0]['id']
    def repeated(state, raw):
        sid = state['new_user_source']['id']
        raw['subjects'] = [dict(ref=ref, operation='reuse', target_id=target, name='Ren', kind='person',
            aliases=[], evidence=[dict(source_id=sid)]) for ref in [0, 1]] + [raw['subjects'][1] | {'ref': 2}]
        return raw
    engine.model = WireModel([('Ren', False, []), ('Orin', False, [])],
        [('Ren met Orin.', [0, 2], [], None), ('Orin likes tea.', [2], [], None)], identity=repeated)
    engine.add(request('Ren met Orin. Orin likes tea.', request_id='r2'))
    assert [m.current.content for m in engine.store.memories('u1')] == ['Orin likes tea.']
    assert {e['error'] for e in unit(engine, 'r2')['identity_errors']} == {'existing_identity_uses_one_local_ref'}


def test_retired_bindings_container_is_rejected_with_bounded_repair():
    model = WireModel([], identity=lambda state, raw: dict(subjects=[], connections=[], bindings=[]))
    engine = MemoryEngine(model=model)
    with pytest.raises(ProviderUnavailable):
        engine.add(request('Original source stays available.'))
    assert len(model.calls) == 2 and engine.store.list('sources', 'u1')
    assert not engine.store.list('objects', 'u1')
    with pytest.raises(ValueError):
        SubjectMaintenanceDraft.model_validate(dict(subjects=[], connections=[], bindings=[]))


def test_retired_subject_update_operation_is_rejected():
    def old_operation(state, raw):
        raw['subjects'][0]['operation'] = 'update'
        return raw
    engine = MemoryEngine(model=WireModel([('Ren', False, [])], identity=old_operation))
    engine.add(request('Ren is my colleague.'))
    assert not engine.store.list('objects', 'u1') and unit(engine)['identity_errors']


def test_earlier_alias_evidence_preserves_historical_search_visibility():
    from remi.identity import subject_hits
    engine = MemoryEngine(model=SubjectModel([('user', True, ['Rin'])]))
    engine.add(request('I am Rin.'))
    original_id = engine.store.list('objects', 'u1')[0]['id']
    engine.add(request('I am Rin.', request_id='earlier', when='2024-05-01T00:00:00Z'))
    hits = subject_hits(engine.store, 'u1', 'Rin', '2024-05-15T00:00:00Z')
    assert [obj.id for obj in hits] == [original_id]
    assert len(engine.store.list('objects', 'u1')) == 1


def test_alias_copies_the_selected_original_even_when_current_source_also_mentions_it():
    engine = MemoryEngine(model=SubjectModel([]))
    engine.add(request('Mira is also called Mimi.', request_id='earlier'))
    source_id = engine.store.list('sources', 'u1')[0]['id']
    def naming(state, raw):
        raw['subjects'][0]['evidence'] = [dict(source_id=state['new_user_source']['id'])]
        raw['subjects'][0]['aliases'] = [dict(name='Mimi', source_id=source_id)]
        return raw
    engine.model = WireModel([('Mira', False, [])], identity=naming)
    engine.add(request('Mira discussed Mimi.', request_id='r2'))
    alias = next(a for a in engine.store.list('objects', 'u1')[0]['aliases'] if a['name'] == 'Mimi')
    assert alias['source_id'] == source_id and alias['quote'] == 'Mira is also called Mimi.'


def test_invalid_alias_keeps_confirmed_relationship_endpoints_usable():
    def bad_alias(state, raw):
        raw['subjects'][0]['aliases'] = [dict(name='Invented', source_id=state['new_user_source']['id'])]
        return raw
    model = WireModel([('Ren', False, []), ('Orin', False, [])],
        [('Ren mentors Orin.', [0, 1], [0, 1], 'Ren mentors Orin.')], identity=bad_alias)
    engine = MemoryEngine(model=model)
    engine.add(request('Ren mentors Orin.'))
    assert len(engine.store.memories('u1')[0].endpoint_ids) == 2
    assert not unit(engine)['identity_errors'] and unit(engine)['subject_alias_issues']
    assert [name for name, _ in model.calls] == ['subjects', 'propose']
