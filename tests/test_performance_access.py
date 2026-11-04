"""Access-path equivalence, owner isolation and request metric isolation."""
import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from remi.graph import active_connections
from remi.models import Edge
from remi.performance import count, request_performance
from remi.storage import SQLiteStore
from .test_graph_expansion_budget import bank, connect
from .helpers import query


def test_indexed_graph_boundary_matches_full_graph_and_uses_both_endpoints():
    engine, memories, docs, by_memory = bank(4)
    connect(engine, memories[0], memories[1])
    connect(engine, memories[1], memories[2])
    connect(engine, memories[2], memories[3])
    clock = '2025-01-01T00:00:00Z'
    all_edges = active_connections(engine.store, 'u1', clock)
    for seed in memories:
        expected = {e.id for e in all_edges if seed.id in (e.origin, e.target)}
        assert {e.id for e in active_connections(engine.store, 'u1', clock, seeds=[seed.id])} == expected
    calls = []
    original = engine.store.edges_for
    def spy(owner, *args, **kwargs):
        calls.append(kwargs)
        return original(owner, *args, **kwargs)
    engine.store.edges_for = spy
    engine._graph_candidates('u1', 'archive', [by_memory[memories[0].id]], docs, None, clock)
    assert calls and all(c.get('origins') is not None or c.get('targets') is not None for c in calls)


def test_request_measurements_reset_and_record_work():
    engine, _, _, _ = bank(3)
    engine.search(query('archive'))
    first = engine.last_performance
    assert first['operation'] == 'search' and first['success']
    assert first['stages']['engine._graph_candidates']['calls'] >= 1
    assert first['work']['storage.edges_for.returned'] >= 0
    engine.search(query('different'))
    assert engine.last_performance is not first
    assert engine.last_performance['stages']['engine._visible_documents']['calls'] == 1


def test_performance_context_is_thread_local_and_survives_failure():
    class Runner:
        @request_performance
        def run(self, amount, fail=False):
            count('items', amount)
            if fail:
                raise ValueError('expected')
    def task(n):
        runner = Runner()
        runner.run(n)
        return runner.last_performance['work']['items']
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(task, range(8))) == list(range(8))
    runner = Runner()
    with pytest.raises(ValueError):
        runner.run(3, True)
    assert not runner.last_performance['success']


@pytest.mark.skipif(os.getenv('REMI_TEST_SCHEMA_APPROVED') != '1', reason='Temporary six-table approval required')
def test_batch_reads_keep_order_owner_boundary_and_rollback(tmp_path):
    path = tmp_path / 'batch.sqlite3'
    SQLiteStore.initialize(path, allow_create_schema=True)
    store = SQLiteStore(path)
    try:
        for owner in ('a', 'b'):
            for key in ('1', '2'):
                store.put('requests', owner, key, {'id': key, 'owner': owner, 'value': 'old'})
        assert [row['id'] for row in store.get_many('requests', 'a', ['2', 'missing', '1', '2'])] == ['2', '1']
        assert all(row['owner'] == 'a' for row in store.get_many('requests', 'a', ['1', '2']))
        with pytest.raises(ValueError):
            with store.transaction():
                store.put('requests', 'a', '1', {'id': '1', 'owner': 'a', 'value': 'new'})
                assert store.get_many('requests', 'a', ['1'])[0]['value'] == 'new'
                raise ValueError('rollback')
        assert store.get_many('requests', 'a', ['1'])[0]['value'] == 'old'
        peer = SQLiteStore(path)
        try:
            peer.put('requests', 'a', '1', {'id': '1', 'owner': 'a', 'value': 'peer'})
            assert store.get_many('requests', 'a', ['1'])[0]['value'] == 'peer'
        finally:
            peer.close()
    finally:
        store.close()


@pytest.mark.skipif(os.getenv('REMI_TEST_SCHEMA_APPROVED') != '1', reason='Temporary six-table approval required')
def test_source_projection_and_edge_queries_use_existing_indexes(tmp_path):
    path = tmp_path / 'projection.sqlite3'
    SQLiteStore.initialize(path, allow_create_schema=True)
    store = SQLiteStore(path)
    try:
        source = dict(id='s', owner='a', role='user', session_id='session', message_at=None,
                      created_at='2024-01-01T00:00:00Z', ordinal=0, content='private body')
        store.put('sources', 'a', 's', source)
        assert store.source_headers('a', []) == []
        assert store.source_headers('b', ['s']) == []
        assert 'content' not in store.source_headers('a', ['s'])[0]
        assert store.source_headers('a', ['s'])[0]['message_at'] is None
        edge = Edge(id='e', owner='a', origin='left', target='right', relation='related',
                    memory_id='left', revision_id='r1', target_revision_id='r2', source_id='s', quote='private body')
        store.put('edges', 'a', edge.id, edge.model_dump())
        statements = []
        store.connection.set_trace_callback(statements.append)
        assert [e.id for e in store.edges_for('a', origins=['left'], relation='related')] == ['e']
        assert [e.id for e in store.edges_for('a', targets=['right'], relation='related')] == ['e']
        assert store.edges_for('b', origins=['left'], relation='related') == []
        store.connection.set_trace_callback(None)
        queries = [sql for sql in statements if sql.startswith('SELECT record_json FROM edges')]
        for sql in queries:
            plans = [row[3] for row in store.connection.execute('EXPLAIN QUERY PLAN ' + sql)]
            expected = 'edge_origin' if 'INDEXED BY edge_origin' in sql else 'edge_target'
            assert any(expected in plan and ('origin=?' in plan or 'target=?' in plan) for plan in plans)
    finally:
        store.close()
