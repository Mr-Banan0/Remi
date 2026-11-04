"""Incremental metadata agrees with authoritative histories and snapshot boundaries."""
import json
import os
from time import monotonic

import pytest

from remi.heat import visible_memory, visible_memories
from remi.read_cache import read_cache
from remi.storage import SQLiteStore
from .test_graph_expansion_budget import bank


pytestmark = pytest.mark.skipif(os.getenv("REMI_TEST_SCHEMA_APPROVED") != "1",
    reason="Temporary six-table schema approval required")


def populate(store):
    engine, memories, _, _ = bank(2)
    snapshot = engine.store.snapshot()
    with store.transaction():
        for table, rows in snapshot.items():
            for row in rows:
                store.put(table, row["owner"], row.get("id") or row["request_id"], row)
    engine.close()
    return memories


def assert_views(store, clock):
    expected = {m.id: v for m in store.memories('u1') if (v := visible_memory(m, clock))}
    with read_cache():
        view = visible_memories(store, 'u1', clock)
        assert dict(view) == expected
        assert {k: h.current.id for k, h in view.headers.items()} == {k: m.current.id for k, m in expected.items()}
    return expected


@pytest.fixture
def store(tmp_path):
    path = tmp_path / 'projection.sqlite3'
    SQLiteStore.initialize(path, allow_create_schema=True)
    value = SQLiteStore(path)
    populate(value)
    yield value
    value.close()


def test_headers_load_without_full_history_and_bodies_are_lazy(store, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Global history decoding')
    monkeypatch.setattr(store, 'memories', forbidden)
    views = visible_memories(store, 'u1', '2030-01-01T00:00:00Z')
    assert len(views) == 2
    assert len(views.headers) == 2
    with pytest.raises(AssertionError):
        views[next(iter(views))]


def test_historical_backfill_ties_and_deleted_memory_match_recomputation(store):
    row = store.memories('u1')[0]
    first = row.current
    row.revisions = [first.model_copy(update={'id': 'late', 'observed_at': '2025-02-01T00:00:00Z',
        'created_at': '2025-02-02T00:00:00Z'}),
        first.model_copy(update={'id': 'early', 'observed_at': '2024-01-01T08:00:00+08:00',
        'created_at': '2026-01-01T00:00:00Z'}),
        first.model_copy(update={'id': 'tie-z', 'observed_at': '2025-02-01T00:00:00Z',
        'created_at': '2025-02-02T00:00:00Z'})]
    store.put('memories', 'u1', row.id, row.model_dump())
    for clock in ['2023-01-01T00:00:00Z', '2024-01-01T00:00:00Z', '2025-02-01T00:00:00Z']:
        assert_views(store, clock)
    assert visible_memories(store, 'other', '2030-01-01T00:00:00Z') == {}
    store.delete('memories', 'u1', row.id)
    assert_views(store, '2030-01-01T00:00:00Z')


def test_peer_updates_raw_sql_invalidation_and_rollback(store):
    peer = SQLiteStore(store.path)
    clock = '2030-01-01T00:00:00Z'
    try:
        row = store.memories('u1')[0].model_dump()
        with read_cache():
            visible_memories(store, 'u1', clock)
            row['pinned'] = True
            peer.put('memories', 'u1', row['id'], row)
            assert visible_memories(store, 'u1', clock).headers[row['id']].pinned
            with pytest.raises(RuntimeError):
                with store.transaction():
                    changed = dict(row, pinned=False)
                    store.put('memories', 'u1', row['id'], changed)
                    assert not visible_memories(store, 'u1', clock).headers[row['id']].pinned
                    raise RuntimeError('rollback')
            assert visible_memories(store, 'u1', clock).headers[row['id']].pinned
        row['pinned'] = False
        peer.connection.execute('UPDATE memories SET record_json=? WHERE owner=? AND id=?',
                                (json.dumps(row), 'u1', row['id']))
        assert peer.connection.execute('SELECT search_projection FROM memories WHERE id=?', (row['id'],)).fetchone()[0] is None
        assert not visible_memories(store, 'u1', clock).headers[row['id']].pinned
        assert_views(store, clock)
    finally:
        peer.close()


def test_wal_snapshot_projection_and_body_share_same_commit(store):
    row = store.memories('u1')[0].model_dump()
    with store.search_snapshot(monotonic()+10) as reader:
        view = visible_memories(reader, 'u1', '2030-01-01T00:00:00Z')
        row['pinned'] = True
        store.put('memories', 'u1', row['id'], row)
        assert not view.headers[row['id']].pinned
        assert not view[row['id']].pinned
    assert visible_memories(store, 'u1', '2030-01-01T00:00:00Z')[row['id']].pinned


def test_explicit_migration_is_idempotent_and_preserves_records(store):
    before = store.snapshot() if hasattr(store, 'records') else {
        table: store.list(table, 'u1') for table in ('memories','sources','edges','search_documents')}
    store.connection.execute('DROP TRIGGER memory_projection_invalidated')
    store.connection.execute('ALTER TABLE memories DROP COLUMN search_projection')
    # Ordinary reads leave the original schema intact until migration.
    assert_views(store, '2030-01-01T00:00:00Z')
    assert not store._has_memory_projection()
    assert store.rebuild_memory_projection() == 2
    assert store.rebuild_memory_projection() == 2
    for table, records in before.items():
        assert store.list(table, 'u1') == records
    assert_views(store, '2030-01-01T00:00:00Z')


def test_failed_migration_rolls_back_schema_and_records(store, monkeypatch):
    import remi.storage as module
    store.connection.execute('DROP TRIGGER memory_projection_invalidated')
    store.connection.execute('ALTER TABLE memories DROP COLUMN search_projection')
    before = store.list('memories', 'u1')
    project = module.project_memory
    calls = 0
    def fail_second(record):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError('injected migration failure')
        return project(record)
    monkeypatch.setattr(module, 'project_memory', fail_second)
    with pytest.raises(RuntimeError):
        store.rebuild_memory_projection()
    assert not store._has_memory_projection()
    assert store.list('memories', 'u1') == before
    monkeypatch.setattr(module, 'project_memory', project)
    assert_views(store, '2030-01-01T00:00:00Z')
