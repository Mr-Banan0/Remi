"""Deterministic current-name routing with complete same-name candidate groups."""
from copy import deepcopy

import pytest

from experiments.subject_engine import SubjectMemoryStore
from experiments.subject_recall import prepare_candidates
from remi.providers import ProviderUnavailable
from tests.test_keyword_subject_recall import OWNER, add_subject, source, state


def recalled(store, text, context=()):
    return prepare_candidates(store, state(text, context))


def test_no_stored_subject_uses_subject_first():
    packet, _ = recalled(SubjectMemoryStore(), 'A new colleague arrived.')
    assert packet['maintenance_route'] == dict(order='subject_first', name_matches=[], collision_groups=[])


def test_one_name_match_uses_subject_first_and_reports_its_id():
    store = SubjectMemoryStore()
    person = add_subject(store, 'Lior')
    packet, _ = recalled(store, 'Lior arrived.')
    assert packet['maintenance_route'] == dict(order='subject_first',
        name_matches=[dict(name='Lior', subject_ids=[person['id']])], collision_groups=[])


def test_same_name_on_two_ids_keeps_subject_first_with_both_candidates():
    store = SubjectMemoryStore()
    for key in ['person-a', 'person-b']:
        add_subject(store, 'Lior', key=key)
    packet, _ = recalled(store, 'Lior arrived.')
    group = dict(name='Lior', subject_ids=['person-a', 'person-b'])
    assert packet['maintenance_route'] == dict(order='subject_first', name_matches=[group], collision_groups=[group])


def test_name_and_alias_on_different_ids_form_one_collision():
    store = SubjectMemoryStore()
    add_subject(store, 'Lior', key='person-a')
    add_subject(store, 'Haven', key='person-b', aliases=['Lior'])
    packet, _ = recalled(store, 'Lior arrived.')
    assert packet['maintenance_route']['collision_groups'] == [dict(name='Lior', subject_ids=['person-a', 'person-b'])]


def test_repeated_names_aliases_and_sources_count_one_identity_once():
    store = SubjectMemoryStore()
    row = add_subject(store, 'Lior', key='person-a', aliases=['Lior', 'LIOR', 'Lee'])
    row['aliases'].extend(deepcopy(row['aliases']))
    store.put('objects', OWNER, row['id'], row)
    packet, _ = recalled(store, 'Lior, Lee and LIOR arrived.')
    route = packet['maintenance_route']
    assert route['order'] == 'subject_first' and not route['collision_groups']
    assert route['name_matches'] == [dict(name='Lee', subject_ids=['person-a']), dict(name='Lior', subject_ids=['person-a'])]


def test_different_names_with_one_id_each_keep_subject_first():
    store = SubjectMemoryStore()
    add_subject(store, 'Lior', key='person-a')
    add_subject(store, 'Haven', key='person-b')
    packet, _ = recalled(store, 'Lior and Haven arrived.')
    assert packet['maintenance_route']['order'] == 'subject_first'
    assert len(packet['maintenance_route']['name_matches']) == 2


def test_description_only_matches_keep_subject_first():
    store = SubjectMemoryStore()
    add_subject(store, 'Lior', key='person-a', descriptions=['a nurse'])
    add_subject(store, 'Haven', key='person-b', descriptions=['a nurse'])
    packet, _ = recalled(store, 'I met a nurse.')
    assert len(packet['subject_catalog']) == 2
    assert packet['maintenance_route'] == dict(order='subject_first', name_matches=[], collision_groups=[])


def test_context_only_name_collision_keeps_subject_first():
    store = SubjectMemoryStore()
    for key in ['person-a', 'person-b']:
        add_subject(store, 'Lior', key=key)
    packet, _ = recalled(store, 'They arrived.', [source('previous', 'Lior sent a message.')])
    assert len(packet['subject_catalog']) == 2
    assert packet['maintenance_route'] == dict(order='subject_first', name_matches=[], collision_groups=[])


def test_implicit_canonical_user_does_not_count_as_name_hit():
    store = SubjectMemoryStore()
    add_subject(store, 'Owner', user=True)
    add_subject(store, 'user', key='person-a')
    packet, _ = recalled(store, 'The user arrived.')
    assert len(packet['subject_catalog']) == 2
    assert packet['maintenance_route']['name_matches'] == [dict(name='user', subject_ids=['person-a'])]
    assert packet['maintenance_route']['order'] == 'subject_first'


def test_canonical_user_explicit_alias_counts_as_a_real_name_hit():
    store = SubjectMemoryStore()
    user = add_subject(store, 'Owner', user=True, aliases=['Lior'])
    add_subject(store, 'Lior', key='person-a')
    packet, _ = recalled(store, 'Lior arrived.')
    assert packet['maintenance_route']['order'] == 'subject_first'
    assert packet['maintenance_route']['collision_groups'][0]['subject_ids'] == sorted([user['id'], 'person-a'])


@pytest.mark.parametrize('text', ['LIOR arrived.', 'Lior arrived.'])
def test_name_collision_is_case_normalized(text):
    store = SubjectMemoryStore()
    add_subject(store, 'Lior', key='person-a')
    add_subject(store, 'LIOR', key='person-b')
    packet, _ = recalled(store, text)
    assert packet['maintenance_route']['order'] == 'subject_first'
    assert len(packet['maintenance_route']['name_matches']) == 1


def test_latin_word_boundaries_and_chinese_collision():
    store = SubjectMemoryStore()
    for key in ['min-a', 'min-b']:
        add_subject(store, 'Min', key=key)
    for key in ['qiu-a', 'qiu-b']:
        add_subject(store, '秋禾', key=key)
    packet, _ = recalled(store, 'The administrator called 秋禾.')
    assert packet['maintenance_route']['collision_groups'] == [dict(name='秋禾', subject_ids=['qiu-a', 'qiu-b'])]


def test_collision_group_survives_top_candidate_limit():
    store = SubjectMemoryStore()
    add_subject(store, 'Owner', user=True)
    long_names = ['Alexandria', 'Constantine', 'Maximilian', 'Theophilus', 'Valentina']
    for name in long_names:
        add_subject(store, name)
    for key in ['lior-a', 'lior-b']:
        add_subject(store, 'Lior', key=key)
    packet, trace = recalled(store, ' '.join([*long_names, 'Lior']))
    supplied = {row['id'] for row in packet['subject_catalog']}
    assert {'lior-a', 'lior-b'} <= supplied and len(supplied) == 6
    assert packet['maintenance_route']['order'] == 'subject_first'
    assert set(trace['selected_subject_ids']) == supplied
    assert set(trace['limit_skipped_subject_ids']) == set(trace['keyword_matched_subject_ids']) - supplied
    assert trace['subject_recall_truncated']


def test_exactly_six_colliding_identities_fill_all_slots_without_user():
    store = SubjectMemoryStore()
    for index in range(6):
        add_subject(store, 'Lior', key='person-'+str(index))
    packet, _ = recalled(store, 'Lior arrived.')
    assert len(packet['subject_catalog']) == 6
    assert packet['maintenance_route']['order'] == 'subject_first'
    assert len(packet['maintenance_route']['collision_groups'][0]['subject_ids']) == 6


@pytest.mark.parametrize('has_user,count', [(False, 7), (True, 6)])
def test_collision_overflow_is_explicit_and_keeps_store_unchanged(has_user, count):
    store = SubjectMemoryStore()
    if has_user:
        add_subject(store, 'Owner', user=True)
    for index in range(count):
        add_subject(store, 'Lior', key='person-'+str(index))
    before = deepcopy(store.records)
    with pytest.raises(ProviderUnavailable, match='subject_name_collision_exceeds_candidate_limit'):
        recalled(store, 'Lior arrived.')
    assert store.records == before


def test_multiple_collision_groups_are_preserved_together():
    store = SubjectMemoryStore()
    for name in ['Lior', 'Haven']:
        for index in range(2):
            add_subject(store, name, key=name+'-'+str(index))
    packet, _ = recalled(store, 'Lior and Haven arrived.')
    assert len(packet['maintenance_route']['collision_groups']) == 2
    assert len(packet['subject_catalog']) == 4
