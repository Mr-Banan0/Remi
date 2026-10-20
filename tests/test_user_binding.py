"""Attribution and graph invariants, independent of benchmark people or questions."""
from copy import deepcopy
import os

import pytest
from pydantic import ValidationError

from remi.add_pipeline import AddPipeline
from remi.engine import MemoryEngine
from remi.identity import resolve_mentions, stable_id
from remi.models import GraphObject, Memory, Mention
from remi.providers import ProviderUnavailable
from remi.storage import MemoryStore, SQLiteStore
from tests.helpers import HashEmbedder, request


def claim(content, involvement, names=()):
    return {'content': content, 'user_involvement': involvement,
            'mentions': [{'name': n} for n in names]}


class IdentityModel:
    def __init__(self, claims, reviewed=None):
        self.claims = claims
        self.reviewed = reviewed
        self.calls = []

    def propose(self, state, *, deadline):
        self.calls.append(('propose', deepcopy(state)))
        return {'items': [{**c, 'evidence': [{'source_id': state['new_user_source']['id']}]}
                          for c in self.claims], 'reason': 'independent attribution fixture'}

    def maintain(self, state, *, deadline):
        self.calls.append(('maintain', deepcopy(state)))
        if callable(self.reviewed):
            return self.reviewed(state)
        return {'decisions': [self.decision(c, (self.reviewed or {}).get(c['id'], c['user_involvement']))
                              for c in state['proposal']['items']]}

    @staticmethod
    def decision(c, involvement):
        if involvement == 'uncertain':
            return {'candidate_id': c['id'], 'operation': 'pending', 'reason': 'speaker attribution unresolved'}
        return {'candidate_id': c['id'], 'operation': 'create', 'record_type': 'relation',
                'content': c['content'], 'mode': 'asserted', 'user_involvement': involvement,
                'context': None, 'status': None, 'time_expression': None, 'reason': 'reviewed original speaker'}


def unit(engine, rid='r1', owner='u1'):
    return engine.store.get('requests', owner, rid)['units']['0']


@pytest.mark.parametrize('text', ['I prefer linen.', '我偏好亚麻。', 'Prefiero el lino.'])
def test_missing_user_reference_is_detected_and_bound_without_extra_call(text):
    model = IdentityModel([claim(text, 'involved')])
    engine = MemoryEngine(model=model)
    engine.add(request(text))
    memory = engine.store.memories('u1')[0]
    user_id = stable_id('obj_', 'u1', 'user')
    assert memory.current.entity_ids == [user_id]
    assert engine.store.get('objects', 'u1', user_id)['name'] == 'user'
    assert engine.store.get('objects', 'u1', user_id)['is_user']
    assert unit(engine)['user_reference_checks'][0]['action'] == 'user_reference_added'
    assert unit(engine)['user_binding_checks'][0]['passed']
    assert [c[0] for c in model.calls] == ['propose', 'maintain']
    assert model.calls[1][1]['resolved_subjects'] == {'0': ['user']}


@pytest.mark.parametrize('proposed,reviewed,names,action', [
    ('not_involved', 'not_involved', ['user', 'Lena'], 'user_reference_removed'),
    ('uncertain', 'involved', ['Lena'], 'consistent'),
    ('uncertain', 'not_involved', ['user', 'Lena'], 'user_reference_removed'),
])
def test_final_review_controls_user_binding_and_preserves_other_subjects(proposed, reviewed, names, action):
    model = IdentityModel([claim('A preference involving Lena', proposed, names)], {'0': reviewed})
    engine = MemoryEngine(model=model)
    engine.add(request('Lena and I discussed preferences.'))
    memory = engine.store.memories('u1')[0]
    objects = [engine.store.get('objects', 'u1', eid) for eid in memory.entity_ids]
    assert 'Lena' in [o['name'] for o in objects]
    assert any(o['is_user'] for o in objects) == (reviewed == 'involved')
    assert memory.current.user_involvement == reviewed
    assert unit(engine)['user_reference_checks'][0]['action'] == action
    assert unit(engine)['user_involvement_reviews'][0]['changed'] == (proposed != reviewed)
    assert all(c['passed'] for c in unit(engine)['user_binding_checks'])
    assert len(model.calls) == 2


@pytest.mark.parametrize('proposed,reviewed', [('involved','not_involved'), ('not_involved','involved')])
@pytest.mark.parametrize('resolved', [True, False])
def test_conflicting_reviews_use_bounded_repair_and_preserve_valid_sibling(proposed, reviewed, resolved):
    def maintain(state):
        return {'decisions': [IdentityModel.decision(c,
            (proposed if resolved and state.get('repair_errors') else reviewed) if c['id']=='0' else 'not_involved')
            for c in state['proposal']['items']]}
    model = IdentityModel([claim('A relationship with Lena', proposed, ['Lena']),
                           claim('Lena likes tea', 'not_involved', ['Lena'])], maintain)
    engine = MemoryEngine(model=model)
    engine.add(request('I know Lena. Lena likes tea.'))
    assert len(model.calls) == 3
    assert [c['id'] for c in model.calls[-1][1]['proposal']['items']] == ['0']
    assert unit(engine)['candidate_dispositions'][1]['status'] == 'accepted'
    assert unit(engine)['candidate_dispositions'][0]['status'] == ('accepted' if resolved else 'pending')
    audit = unit(engine)['user_involvement_reviews'][0]
    assert audit['changed'] and audit['proposed']==proposed and audit['initial_review']==reviewed
    assert audit['reviewed'] == (proposed if resolved else None)


def test_mixed_personal_and_quoted_claims_have_separate_attribution():
    model = IdentityModel([
        claim('The user likes tea', 'involved'),
        claim('Lena likes coffee', 'not_involved', ['Lena']),
    ])
    engine = MemoryEngine(model=model)
    engine.add(request('I like tea. Lena said, "I like coffee."'))
    cards = {m.current.content: m for m in engine.store.memories('u1')}
    user_id = stable_id('obj_', 'u1', 'user')
    assert user_id in cards['The user likes tea'].entity_ids
    assert user_id not in cards['Lena likes coffee'].entity_ids
    assert cards['Lena likes coffee'].entity_ids == [stable_id('obj_', 'u1', 'lena')]


def test_uncertain_attribution_preserves_source_and_pending_hint_without_user_node():
    engine = MemoryEngine(model=IdentityModel([claim('An unnamed speaker likes tea', 'uncertain', ['user'])]))
    engine.add(request('An unsigned note says, "I like tea."'))
    assert not engine.store.memories('u1') and not engine.store.list('objects', 'u1')
    assert unit(engine)['candidate_dispositions'][0]['status'] == 'pending'
    assert any(d.id.startswith('doc_pending_') for d in engine.store.documents('u1'))
    assert engine.store.list('sources', 'u1')[0]['content'] == 'An unsigned note says, "I like tea."'


def test_missing_proposal_attribution_uses_shared_repair_budget_and_keeps_sources():
    raw = claim('A claim', 'involved'); del raw['user_involvement']
    model = IdentityModel([raw]); engine = MemoryEngine(model=model)
    with pytest.raises(ProviderUnavailable, match='proposal_output_invalid'):
        engine.add(request('I like tea.'))
    assert [c[0] for c in model.calls] == ['propose', 'propose']
    assert not engine.store.memories('u1') and engine.store.list('sources', 'u1')


@pytest.mark.parametrize('repair_succeeds', [True, False])
def test_missing_review_attribution_repairs_only_affected_candidate(repair_succeeds):
    def maintain(state):
        decisions = []
        for c in state['proposal']['items']:
            d = IdentityModel.decision(c, 'involved')
            if c['id'] == '1' and (not state.get('repair_errors') or not repair_succeeds):
                del d['user_involvement']
            decisions.append(d)
        return {'decisions': decisions}
    model = IdentityModel([claim('I like tea', 'involved'), claim('I like linen', 'involved')], maintain)
    engine = MemoryEngine(model=model)
    engine.add(request('I like tea and linen.'))
    assert [c['id'] for c in model.calls[-1][1]['proposal']['items']] == ['1']
    assert len(model.calls) == 3
    assert len(engine.store.memories('u1')) == (2 if repair_succeeds else 1)
    assert unit(engine)['candidate_dispositions'][0]['status'] == 'accepted'


def test_user_identity_is_stable_per_owner_and_works_with_backdated_sources():
    engine = MemoryEngine(model=IdentityModel([claim('A personal preference', 'involved', ['USER'])]))
    for owner, rid, when in [('u1', 'r1', '2024-06-02T10:00:00Z'),
                              ('u1', 'r2', '2024-05-02T10:00:00Z'),
                              ('u2', 'r1', '2024-06-02T10:00:00Z')]:
        engine.add(request('I have a preference.', owner=owner, request_id=rid, when=when))
    ids = []
    for owner in ['u1', 'u2']:
        objects = engine.store.list('objects', owner)
        assert len(objects) == 1 and objects[0]['is_user']
        assert all(m.entity_ids == [objects[0]['id']] for m in engine.store.memories(owner))
        ids.append(objects[0]['id'])
    assert ids[0] != ids[1]
    assert engine.store.get('objects', 'u1', ids[0])['observed_at'].startswith('2024-05-02')


def test_user_marker_requires_direct_user_source_and_retired_marker_is_an_ordinary_name():
    source = {'id': 's', 'role': 'assistant', 'content': 'I prefer tea.',
              'message_at': '2024-06-02T00:00:00Z', 'created_at': '2024-06-02T00:00:00Z'}
    ids, _, issues = resolve_mentions(MemoryStore(), 'u1', [Mention(name='user')], [source], source['created_at'])
    assert not ids and issues[0]['reason'] == 'user_requires_user_source'
    source['role'] = 'user'
    ids, _, issues = resolve_mentions(MemoryStore(), 'u1', [Mention(name='self')], [source], source['created_at'])
    assert not ids and issues[0]['reason'] == 'mention_requires_source_name'


def test_universal_user_hub_does_not_expand_all_personal_memories_into_local_recall():
    model = IdentityModel([claim('I like linen', 'involved')]); engine = MemoryEngine(model=model)
    engine.add(request('I like linen.'))
    model.claims = [claim('I enjoy archery', 'involved')]
    engine.add(request('I enjoy archery.', request_id='r2'))
    assert unit(engine, 'r2')['bindings']['recall_trace'][0]['local'] == []


@pytest.mark.parametrize('fault', ['missing_edge', 'all_edges', 'missing_node', 'revision_gap', 'wrong_origin', 'wrong_owner'])
def test_graph_or_revision_gap_blocks_commit_and_records_failure(monkeypatch, fault):
    original = AddPipeline._materialize
    def broken(pipeline, state, bindings, decisions):
        objects, memories, edges, expected = original(pipeline, state, bindings, decisions)
        if fault == 'missing_edge': edges = [e for e in edges if e.relation != 'mentions']
        elif fault == 'all_edges': edges = []
        elif fault == 'missing_node': objects = []
        elif fault == 'revision_gap': memories[0].revisions[0].entity_ids = []
        else:
            for edge in edges:
                if edge.relation == 'mentions':
                    if fault == 'wrong_origin': edge.origin = 'different-memory'
                    else: edge.owner = 'different-owner'
        return objects, memories, edges, expected
    monkeypatch.setattr(AddPipeline, '_materialize', broken)
    engine = MemoryEngine(model=IdentityModel([claim('I like linen', 'involved')]), embedder=HashEmbedder())
    with pytest.raises((ProviderUnavailable, ValidationError)):
        engine.add(request('I like linen.'))
    assert not engine.store.memories('u1') and not engine.store.list('objects', 'u1')
    assert not engine.store.list('edges', 'u1') and engine.store.list('sources', 'u1')
    assert not engine.embedder.calls
    assert unit(engine)['user_binding_checks'][0]['passed'] is False


@pytest.mark.parametrize('fixed', [True, False])
def test_unbacked_name_uses_existing_proposal_repair_then_keeps_good_sibling(fixed):
    class NamedModel(IdentityModel):
        def propose(self, state, *, deadline):
            self.claims[1]['mentions'] = [{'name': 'Iris' if fixed and state.get('repair_errors') else 'Translated Iris'}]
            return super().propose(state, deadline=deadline)
        def maintain(self, state, *, deadline):
            self.calls.append(('maintain', deepcopy(state)))
            return {'decisions': [self.decision(c, 'uncertain' if any(
                i['candidate_id'] == c['id'] for i in state['identity_issues']) else c['user_involvement'])
                for c in state['proposal']['items']]}
    model = NamedModel([claim('I like linen', 'involved'), claim('Iris likes tea', 'not_involved')])
    engine = MemoryEngine(model=model)
    engine.add(request('I like linen. Iris likes tea.'))
    assert [c[0] for c in model.calls] == ['propose', 'propose', 'maintain']
    assert len(engine.store.memories('u1')) == (2 if fixed else 1)
    assert unit(engine)['proposal_name_checks'][0]['gaps'][0]['name'] == 'Translated Iris'
    assert unit(engine)['candidate_dispositions'][0]['status'] == 'accepted'


def test_program_user_reference_fits_full_source_name_budget():
    names = ['Person' + str(i) for i in range(12)]
    engine = MemoryEngine(model=IdentityModel([claim('A personal group connection', 'involved', names)]))
    engine.add(request('I know ' + ', '.join(names) + '.'))
    assert len(engine.store.memories('u1')[0].entity_ids) == 13
    assert len(unit(engine)['propose']['items'][0]['mentions']) == 13


def test_index_retry_reuses_review_and_binding_checks():
    class Interrupted(HashEmbedder):
        ready = False
        def embed(self, texts, *, deadline):
            if not self.ready: raise ProviderUnavailable('simulated embedding outage')
            return super().embed(texts, deadline=deadline)
    model = IdentityModel([claim('I like linen', 'involved')]); embedder = Interrupted()
    engine = MemoryEngine(model=model, embedder=embedder)
    with pytest.raises(ProviderUnavailable): engine.add(request('I like linen.'))
    assert unit(engine)['user_binding_checks'][0]['passed']
    embedder.ready = True
    engine.add(request('I like linen.'))
    assert len(model.calls) == 2 and len(engine.store.memories('u1')) == 1


@pytest.mark.parametrize('matching', [True, False])
def test_keep_preserves_confirmed_user_attribution(matching):
    model = IdentityModel([claim('A preference', 'involved')])
    engine = MemoryEngine(model=model)
    engine.add(request('I have a preference.'))
    before = engine.store.memories('u1')[0]
    model.claims = [claim('A preference', 'involved' if matching else 'not_involved')]
    model.reviewed = lambda state: {'decisions': [{'candidate_id': '0', 'operation': 'keep',
        'target_id': before.id, 'reason': 'claimed duplicate'}]}
    engine.add(request('A preference.', request_id='r2'))
    assert engine.store.memories('u1') == [before]
    assert unit(engine, 'r2')['candidate_dispositions'][0]['status'] == ('accepted' if matching else 'pending')
    assert len([c for c in model.calls if c[0]=='maintain']) == (2 if matching else 3)


def test_record_contract_rejects_wrong_owner_identity_and_false_user_flag():
    engine = MemoryEngine(model=IdentityModel([claim('I like linen', 'involved')]))
    engine.add(request('I like linen.'))
    memory = engine.store.memories('u1')[0].model_dump()
    memory['owner'] = 'different-owner'
    with pytest.raises(ValidationError, match='revision_user_binding'):
        Memory.model_validate(memory)
    obj = engine.store.list('objects', 'u1')[0]
    with pytest.raises(ValidationError, match='user_entity'):
        GraphObject.model_validate({**obj, 'is_user': False})


@pytest.mark.skipif(os.getenv('REMI_TEST_SCHEMA_APPROVED') != '1', reason='Enable the approved six-table test scope')
def test_old_record_contract_requires_explicit_source_replay(tmp_path):
    path = tmp_path / 'approved-six-tables.sqlite3'
    SQLiteStore.initialize(path, allow_create_schema=True)
    store = SQLiteStore(path)
    store.put('objects', 'u1', 'old', {'schema_version': 10, 'id': 'old', 'name': 'self', 'is_self': True})
    store.close()
    with pytest.raises(RuntimeError, match='user_identity_source_replay'):
        SQLiteStore(path)
