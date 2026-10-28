"""Deterministic current-name routing with complete same-name candidate groups."""
from copy import deepcopy

import pytest

from remi.storage import MemoryStore
from remi.subject_maintenance import review
from remi.subject_recall import prepare_candidates
from remi.subject_repair import build_repair_state, finish_repair
from remi.providers import ProviderUnavailable
from tests.test_subject_identity import plan
from tests.test_keyword_subject_recall import OWNER, add_subject, source, state


def recalled(store, text, context=()):
    return prepare_candidates(store, state(text, context))


def test_no_stored_subject_uses_subject_first():
    packet, _ = recalled(MemoryStore(), 'A new colleague arrived.')
    assert packet['maintenance_route'] == dict(order='subject_first', name_matches=[], collision_groups=[])


def test_one_name_match_uses_subject_first_and_reports_its_id():
    store = MemoryStore()
    person = add_subject(store, 'Lior')
    packet, _ = recalled(store, 'Lior arrived.')
    assert packet['maintenance_route'] == dict(order='subject_first',
        name_matches=[dict(name='Lior', subject_ids=[person['id']])], collision_groups=[])


def test_same_name_on_two_ids_keeps_subject_first_with_both_candidates():
    store = MemoryStore()
    for key in ['person-a', 'person-b']:
        add_subject(store, 'Lior', key=key)
    packet, _ = recalled(store, 'Lior arrived.')
    group = dict(name='Lior', subject_ids=['person-a', 'person-b'])
    assert packet['maintenance_route'] == dict(order='subject_first', name_matches=[group], collision_groups=[group])


def test_name_and_alias_on_different_ids_form_one_collision():
    store = MemoryStore()
    add_subject(store, 'Lior', key='person-a')
    add_subject(store, 'Haven', key='person-b', aliases=['Lior'])
    packet, _ = recalled(store, 'Lior arrived.')
    assert packet['maintenance_route']['collision_groups'] == [dict(name='Lior', subject_ids=['person-a', 'person-b'])]


def test_repeated_names_aliases_and_sources_count_one_identity_once():
    store = MemoryStore()
    row = add_subject(store, 'Lior', key='person-a', aliases=['Lior', 'LIOR', 'Lee'])
    row['aliases'].extend(deepcopy(row['aliases']))
    store.put('objects', OWNER, row['id'], row)
    packet, _ = recalled(store, 'Lior, Lee and LIOR arrived.')
    route = packet['maintenance_route']
    assert route['order'] == 'subject_first' and not route['collision_groups']
    assert route['name_matches'] == [dict(name='Lee', subject_ids=['person-a']), dict(name='Lior', subject_ids=['person-a'])]


def test_different_names_with_one_id_each_keep_subject_first():
    store = MemoryStore()
    add_subject(store, 'Lior', key='person-a')
    add_subject(store, 'Haven', key='person-b')
    packet, _ = recalled(store, 'Lior and Haven arrived.')
    assert packet['maintenance_route']['order'] == 'subject_first'
    assert len(packet['maintenance_route']['name_matches']) == 2


def test_shared_name_alias_exposes_every_namesake_candidate():
    store = MemoryStore()
    add_subject(store, 'Lior', key='person-a', aliases=['Lee'])
    add_subject(store, 'Haven', key='person-b', aliases=['Lee'])
    packet, _ = recalled(store, 'I met Lee.')
    assert len(packet['subject_catalog']) == 2
    expected=[dict(name='Lee',subject_ids=['person-a','person-b'])]
    assert packet['maintenance_route'] == dict(order='subject_first', name_matches=expected, collision_groups=expected)


def test_context_only_name_collision_keeps_subject_first():
    store = MemoryStore()
    for key in ['person-a', 'person-b']:
        add_subject(store, 'Lior', key=key)
    packet, _ = recalled(store, 'They arrived.', [source('previous', 'Lior sent a message.')])
    assert len(packet['subject_catalog']) == 2
    assert packet['maintenance_route'] == dict(order='subject_first', name_matches=[], collision_groups=[])


def test_implicit_canonical_user_does_not_count_as_name_hit():
    store = MemoryStore()
    add_subject(store, 'Owner', user=True)
    add_subject(store, 'user', key='person-a')
    packet, _ = recalled(store, 'The user arrived.')
    assert len(packet['subject_catalog']) == 2
    assert packet['maintenance_route']['name_matches'] == [dict(name='user', subject_ids=['person-a'])]
    assert packet['maintenance_route']['order'] == 'subject_first'


def test_canonical_user_explicit_alias_counts_as_a_real_name_hit():
    store = MemoryStore()
    user = add_subject(store, 'Owner', user=True, aliases=['Lior'])
    add_subject(store, 'Lior', key='person-a')
    packet, _ = recalled(store, 'Lior arrived.')
    assert packet['maintenance_route']['order'] == 'subject_first'
    assert packet['maintenance_route']['collision_groups'][0]['subject_ids'] == sorted([user['id'], 'person-a'])


@pytest.mark.parametrize('text', ['LIOR arrived.', 'Lior arrived.'])
def test_name_collision_is_case_normalized(text):
    store = MemoryStore()
    add_subject(store, 'Lior', key='person-a')
    add_subject(store, 'LIOR', key='person-b')
    packet, _ = recalled(store, text)
    assert packet['maintenance_route']['order'] == 'subject_first'
    assert len(packet['maintenance_route']['name_matches']) == 1


def test_latin_word_boundaries_and_chinese_collision():
    store = MemoryStore()
    for key in ['min-a', 'min-b']:
        add_subject(store, 'Min', key=key)
    for key in ['qiu-a', 'qiu-b']:
        add_subject(store, '秋禾', key=key)
    packet, _ = recalled(store, 'The administrator called 秋禾.')
    assert packet['maintenance_route']['collision_groups'] == [dict(name='秋禾', subject_ids=['qiu-a', 'qiu-b'])]


def test_collision_group_survives_top_candidate_limit():
    store = MemoryStore()
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
    store = MemoryStore()
    for index in range(6):
        add_subject(store, 'Lior', key='person-'+str(index))
    packet, _ = recalled(store, 'Lior arrived.')
    assert len(packet['subject_catalog']) == 6
    assert packet['maintenance_route']['order'] == 'subject_first'
    assert len(packet['maintenance_route']['collision_groups'][0]['subject_ids']) == 6


@pytest.mark.parametrize('has_user,count', [(False, 7), (True, 6)])
def test_collision_overflow_is_explicit_and_keeps_store_unchanged(has_user, count):
    store = MemoryStore()
    if has_user:
        add_subject(store, 'Owner', user=True)
    for index in range(count):
        add_subject(store, 'Lior', key='person-'+str(index))
    before = deepcopy(store.records)
    with pytest.raises(ProviderUnavailable, match='subject_name_collision_exceeds_candidate_limit'):
        recalled(store, 'Lior arrived.')
    assert store.records == before


def test_multiple_collision_groups_are_preserved_together():
    store = MemoryStore()
    for name in ['Lior', 'Haven']:
        for index in range(2):
            add_subject(store, name, key=name+'-'+str(index))
    packet, _ = recalled(store, 'Lior and Haven arrived.')
    assert len(packet['maintenance_route']['collision_groups']) == 2
    assert len(packet['subject_catalog']) == 4


def test_same_name_same_kind_reuse_requires_unique_identity_evidence():
    store = MemoryStore()
    for key in ['person-a', 'person-b']:
        add_subject(store, 'Lior', key=key)
    packet, _ = recalled(store, 'Lior arrived.')
    basis = next(row for row in packet['identity_bases'] if row['target_id']=='person-a')
    raw = plan(name='Lior',kind='person',operation='reuse',target='person-a',sid='now',basis=basis['id'])
    result = review(raw,packet,store)
    assert any(issue['code']=='reuse_requires_unique_identity_evidence' for issue in result['issues'])
    assert not result['bindings']['subject_ids']


def test_collision_reuse_is_allowed_when_kind_leaves_one_compatible_identity():
    store = MemoryStore()
    add_subject(store,'Vale',key='person-vale',kind='person')
    add_subject(store,'Vale',key='animal-vale',kind='animal')
    packet, _ = recalled(store, 'Vale needs a walk.')
    basis = next(row for row in packet['identity_bases'] if row['target_id']=='animal-vale')
    raw = plan(name='Vale',kind='animal',operation='reuse',target='animal-vale',sid='now',basis=basis['id'])
    result = review(raw,packet,store)
    assert not result['issues']
    assert result['bindings']['subject_ids']=={'0':'animal-vale'}


def test_unique_alias_allows_reuse_when_canonical_names_collide():
    store = MemoryStore()
    add_subject(store,'Lior',key='person-a',aliases=['Lee'])
    add_subject(store,'Lior',key='person-b')
    packet, _ = recalled(store, 'Lee arrived.')
    basis = next(row for row in packet['identity_bases'] if row['target_id']=='person-a' and row['text']=='Lee')
    raw = plan(name='Lior',kind='person',operation='reuse',target='person-a',sid='now',basis=basis['id'])
    result = review(raw,packet,store)
    assert not result['issues']
    assert result['bindings']['subject_ids']=={'0':'person-a'}


def test_unique_current_name_supplies_its_reuse_basis_programmatically():
    store=MemoryStore()
    add_subject(store,'Lior',key='person-a')
    packet,_=recalled(store,'Lior is also called Lee.')
    raw=plan(name='Lior',kind='person',operation='reuse',target='person-a',aliases=['Lee'],sid='now',basis=None)
    result=review(raw,packet,store)
    assert not result['issues']
    assert result['bindings']['subject_ids']=={'0':'person-a'}


@pytest.mark.parametrize('stored_count',[0,1])
def test_named_pending_requires_at_least_two_matching_candidates(stored_count):
    store=MemoryStore()
    for index in range(stored_count):add_subject(store,'Lior',key='person-'+str(index))
    packet,_=recalled(store,'Lior arrived.')
    raw=plan(name='Lior',kind='person',operation='pending',target=None,sid='now',basis=None)
    result=review(raw,packet,store)
    assert any(issue['code']=='pending_requires_multiple_identity_candidates' for issue in result['issues'])
    assert not result['bindings']['subject_ids']


def test_named_pending_accepts_an_unresolved_same_kind_collision():
    store=MemoryStore()
    for key in ['person-a','person-b']:add_subject(store,'Lior',key=key)
    packet,_=recalled(store,'Lior arrived.')
    raw=plan(name='Lior',kind='person',operation='pending',target=None,sid='now',basis=None)
    result=review(raw,packet,store)
    assert not result['issues']
    assert not result['bindings']['subject_ids']


def test_create_overlapping_a_recalled_name_requires_one_confirmation():
    store=MemoryStore()
    add_subject(store,'Lior',key='existing-lior')
    packet,_=recalled(store,'I met a different Lior, also called Lee, today.')
    raw=plan(name='Lior',kind='person',operation='create',target=None,aliases=['Lee'],sid='now',basis=None)
    first=review(raw,packet,store)
    assert any(issue['code']=='create_overlaps_recalled_identity' for issue in first['issues'])
    assert not first['bindings']['subject_ids']

    repair=build_repair_state(raw,first,packet)
    confirmed=finish_repair(repair,raw,packet,store)
    assert not confirmed['issues']
    assert set(confirmed['bindings']['subject_ids'].values()) != {'existing-lior'}


def test_create_overlapping_a_collision_can_be_repaired_to_pending():
    store=MemoryStore()
    for key in ['person-a','person-b']:add_subject(store,'Lior',key=key,aliases=['Lee'])
    packet,_=recalled(store,'Lee arrived.')
    raw=plan(name='Lee',kind='person',operation='create',target=None,sid='now',basis=None)
    first=review(raw,packet,store)
    assert any(issue['code']=='create_overlaps_recalled_identity' for issue in first['issues'])

    repair=build_repair_state(raw,first,packet)
    pending=plan(name='Lee',kind='person',operation='pending',target=None,sid='now',basis=None)
    corrected=finish_repair(repair,pending,packet,store)
    assert not corrected['issues']
    assert not corrected['bindings']['subject_ids']
