"""Four ordinary memory types retain their identity across writing, recall and persistence."""
from copy import deepcopy
import os
from time import monotonic

import pytest
from pydantic import ValidationError

from remi.add_pipeline import AddPipeline
from remi.engine import MemoryEngine
from remi.heat import hot_view
from remi.models import Memory, ProposalDraft, SCHEMA_VERSION
from remi.providers import strict_schema, ProviderUnavailable
from remi.storage import SQLiteStore
from tests.helpers import HashEmbedder, request
from tests.test_subject_memory import SubjectModel, unit
from tests.test_subject_and_extraction import WireModel


TEXT=('I work at Vela Guild. The locker code is 4826. I prefer written agendas. '
      'After several missed handoffs, I learned to confirm the next owner in writing. '
      'I scheduled a review for June 8.')
ORDINARY=['fact', 'preference', 'experience', 'event']


class TypedModel(SubjectModel):
    def __init__(self):
        super().__init__([('user', True, []), ('Vela Guild', False, [])], [
            ('I work at Vela Guild.', [0, 1], [0, 1], 'I work at Vela Guild.'),
            ('The locker code is 4826.', [], [], None),
            ('I prefer written agendas.', [0], [], None),
            ('After several missed handoffs, I learned to confirm the next owner in writing.', [0], [], None),
            ('I scheduled a review for June 8.', [0], [], None),
        ], kinds={'Vela Guild': 'organization'})

    def propose(self, state, *, deadline):
        result=super().propose(state, deadline=deadline)
        for row, kind in zip(result['items'], ORDINARY):
            row['record_type']=kind
            if kind=='event':row.update(status='planned', time_expression='June 8')
        return result


@pytest.mark.parametrize('backend', ['memory', 'sqlite'])
def test_four_types_and_relations_survive_add_recall_indexes_and_reopening(backend, tmp_path):
    model=TypedModel();store=None
    if backend=='sqlite':
        if os.getenv('REMI_TEST_SCHEMA_APPROVED')!='1':pytest.skip('approved existing six-table test scope')
        path=tmp_path/'typed.sqlite3';SQLiteStore.initialize(path, allow_create_schema=True);store=SQLiteStore(path)
    e=MemoryEngine(store, model, HashEmbedder());e.add(request(TEXT))
    before={m.id:m.model_dump() for m in e.store.memories('u1')}
    if backend=='sqlite':
        e.close();e=MemoryEngine(SQLiteStore(path), embedder=HashEmbedder())
    try:
        records=e.store.memories('u1')
        assert {m.record_type for m in records}=={'relation', *ORDINARY}
        assert {m.id:m.model_dump() for m in records}==before
        assert all(m.tags==[m.record_type] and m.schema_version==SCHEMA_VERSION for m in records)
        relation=next(m for m in records if m.record_type=='relation')
        assert len(relation.endpoint_ids)==2 and relation.connection_evidence.quote=='I work at Vela Guild.'
        assert all(not m.endpoint_ids and m.connection_evidence is None for m in records if m.record_type!='relation')
        assert {m.record_type for m in hot_view(records, '2024-06-03T00:00:00Z')}=={'relation','fact','preference','experience'}
        documents={d.memory_id:d for d in e.store.documents('u1') if d.memory_id}
        assert all(documents[m.id].memory_evidence[0].record_type==m.record_type for m in records)
        state, _=AddPipeline(e)._shared_recall(dict(owner='u1', new_user_source=dict(
            id='new', role='user', content=TEXT, message_at='2024-06-03T00:00:00Z', created_at='2024-06-03T00:00:00Z'),
            context_sources=[]), monotonic()+10)
        assert {m['record_type'] for m in state['history']}=={'relation', *ORDINARY}
        assert [m['id'] for m in state['relation_catalog']]==[relation.id]
        assert [stage for stage, _ in model.calls]==['subjects', 'propose']
    finally:e.close()


def test_ordinary_provider_schema_exposes_exactly_four_create_types():
    schema=strict_schema(ProposalDraft)
    assert schema['$defs']['CandidateDraft']['properties']['record_type']['enum']==ORDINARY


@pytest.mark.parametrize('operation', ['create', 'keep', 'patch_relation'])
def test_ordinary_writer_cannot_create_keep_or_patch_a_subject_relationship(operation):
    e=MemoryEngine(model=SubjectModel([('user', True, []), ('Vela', False, [])],
        [('job', [0, 1], [0, 1], 'I work at Vela.')]))
    e.add(request('I work at Vela.'));before=deepcopy(e.store.memories('u1')[0])
    def attack(state, raw):
        if operation=='create':raw['items'][0]['record_type']='relation'
        else:raw['items']=[dict(candidate_id='0', operation=operation, target_id=before.id, reason='synthetic authority violation')]
        return raw
    e.model=WireModel([('user', True, []), ('Vela', False, [])], [('I have a desk at Vela.', [0, 1], [], None)], extraction=attack)
    with pytest.raises(ProviderUnavailable):e.add(request('I have a desk at Vela.', request_id='r2'))
    assert e.store.memories('u1')==[before]
    assert e.store.get('requests','u1','r2')['status']=='pending'


def test_relation_requires_two_explicit_subject_endpoints():
    e=MemoryEngine(model=TypedModel());e.add(request(TEXT))
    raw=next(m.model_dump() for m in e.store.memories('u1') if m.record_type=='fact')
    raw['record_type']='relation'
    with pytest.raises(ValidationError, match='connection_requires_two_distinct_supported_subjects'):
        Memory.model_validate(raw)


@pytest.mark.parametrize('kind', ORDINARY)
def test_ordinary_memory_keeps_participants_without_owning_relationship_endpoints(kind):
    e=MemoryEngine(model=TypedModel());e.add(request(TEXT))
    records=e.store.memories('u1');raw=next(m.model_dump() for m in records if m.record_type==kind)
    relation=next(m for m in records if m.record_type=='relation')
    raw.update(endpoint_ids=relation.endpoint_ids, connection_evidence=relation.connection_evidence.model_dump())
    with pytest.raises(ValidationError, match='ordinary_memories_use_participants_and_source_evidence'):
        Memory.model_validate(raw)


@pytest.mark.parametrize('kind', ['fact', 'preference', 'experience'])
def test_statement_types_reject_event_status_in_ordinary_write(kind):
    def invalid(state, raw):
        raw['items'][0].update(record_type=kind, status='planned')
        return raw
    e=MemoryEngine(model=WireModel([('user', True, [])], [('I have an explicit statement.', [0], [], None)], extraction=invalid))
    e.add(request('I have an explicit statement.'))
    assert not e.store.memories('u1') and unit(e)['candidate_dispositions'][0]['status']=='pending'


def test_previous_ambiguous_schema_requires_replay_and_keeps_saved_records(tmp_path):
    if os.getenv('REMI_TEST_SCHEMA_APPROVED')!='1':pytest.skip('approved existing six-table test scope')
    path=tmp_path/'old.sqlite3';SQLiteStore.initialize(path, allow_create_schema=True)
    store=SQLiteStore(path)
    old={'id':'old', 'schema_version':12, 'record_type':'relation', 'endpoint_ids':[], 'content':'An old unclassified statement.'}
    store.put('memories', 'u1', 'old', old);store.close()
    with pytest.raises(RuntimeError, match='require_subject_memory_source_replay'):SQLiteStore(path)
    import sqlite3,json
    with sqlite3.connect(path) as connection:
        assert json.loads(connection.execute('SELECT record_json FROM memories').fetchone()[0])==old
