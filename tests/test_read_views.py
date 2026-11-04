"""Derived views agree with full reconstruction through writes and rollback."""
import os
import pytest
from remi.heat import visible_memories, visible_memory
from remi.read_cache import read_cache, derived
from remi.storage import SQLiteStore
from .test_graph_expansion_budget import bank
from .test_search_index import document, save
from remi.retrieval import bm25


def test_visible_views_match_recomputation_and_invalidate_on_write_and_rollback():
    engine, memories, _, _ = bank(2)
    store = engine.store
    clock = '2025-01-01T00:00:00Z'
    with read_cache():
        first = visible_memories(store, 'u1', clock)
        assert visible_memories(store, 'u1', clock) is first
        assert first == {m.id: visible_memory(m, clock) for m in store.memories('u1')}
        assert visible_memories(store, 'other', clock) == {}
        assert visible_memories(store, 'u1', '2000-01-01T00:00:00Z') == {}
        row = store.get('memories', 'u1', memories[0].id)
        row['pinned'] = True
        store.put('memories', 'u1', row['id'], row)
        assert visible_memories(store, 'u1', clock)[row['id']].pinned
        with pytest.raises(ValueError):
            with store.transaction():
                store.delete('memories', 'u1', row['id'])
                assert row['id'] not in visible_memories(store, 'u1', clock)
                raise ValueError('rollback')
        assert visible_memories(store, 'u1', clock)[row['id']].pinned
    assert visible_memories(store, 'u1', clock) is not first


def test_derived_cache_enforces_budget_and_cleans_exception_context():
    calls = []
    def build():
        calls.append(1)
        return object(), 20
    with pytest.raises(ValueError):
        with read_cache(max_bytes=30, max_entries=1):
            a = derived('a', build)
            assert derived('a', build) is a
            derived('b', build)
            assert derived('a', build) is not a
            raise ValueError('expected')
    assert derived('a', build) is not a
    with read_cache(max_bytes=1):
        assert derived('a', build) is not derived('a', build)


@pytest.mark.skipif(os.getenv('REMI_TEST_INDEX_APPROVED') != '1', reason='Temporary index approval required')
def test_lexical_statistics_reused_and_invalidated_by_local_and_peer_writes(tmp_path):
    path = tmp_path / 'stats.sqlite3'
    SQLiteStore.initialize(path, allow_create_schema=True)
    store = SQLiteStore(path)
    store.rebuild_search_index(allow_create_schema=True)
    peer = SQLiteStore(path)
    docs = [document('a', 'meeting Monday'), document('b', 'meeting Tuesday')]
    try:
        for doc in docs:
            save(store, doc)
        statements = []
        store.connection.set_trace_callback(statements.append)
        with read_cache():
            for pool in (docs, docs[:1], docs[1:]):
                assert store.lexical_scores('a', 'meeting', pool) == bm25('meeting', pool)
            reads = [sql for sql in statements if sql.startswith('SELECT document_id,token_count')]
            assert len(reads) == 1
            docs[0] = document('a', 'meeting meeting Monday')
            save(peer, docs[0])
            assert store.lexical_scores('a', 'meeting', docs) == bm25('meeting', docs)
            with pytest.raises(ValueError):
                with store.transaction():
                    save(store, document('a', 'temporary'))
                    store.lexical_scores('a', 'temporary', docs)
                    raise ValueError('rollback')
            assert store.lexical_scores('a', 'meeting', docs) == bm25('meeting', docs)
            store.delete('search_documents', 'a', 'b')
            assert store.lexical_scores('a', 'meeting', docs[:1]) == bm25('meeting', docs[:1])
    finally:
        peer.close()
        store.close()


def test_long_revision_chain_and_historical_backfill_keep_query_time_boundaries():
    from datetime import datetime, timedelta, timezone
    engine, memories, _, _ = bank(1)
    original = memories[0]
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    revisions = [original.current.model_copy(update={
        'id': f'revision-{index:03d}', 'observed_at': (start + timedelta(days=index)).isoformat(),
        'created_at': (start + timedelta(days=index)).isoformat(), 'content': f'State {index}',
        'replaces_revision_id': f'revision-{index-1:03d}' if index else None}) for index in range(40)]
    original.revisions = revisions
    engine.store.put('memories', 'u1', original.id, original.model_dump())
    with read_cache():
        for day in (0, 10, 39):
            clock = (start + timedelta(days=day)).isoformat()
            view = visible_memories(engine.store, 'u1', clock)[original.id]
            assert view.current.id == f'revision-{day:03d}' and len(view.revisions) == day + 1
        backfill = revisions[0].model_copy(update={'id': 'backfill', 'observed_at': (start - timedelta(days=1)).isoformat()})
        original.revisions.append(backfill)
        engine.store.put('memories', 'u1', original.id, original.model_dump())
        view = visible_memories(engine.store, 'u1', start.isoformat())[original.id]
        assert view.current.id == 'revision-000' and len(view.revisions) == 2
