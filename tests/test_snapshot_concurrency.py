"""SQLite readers see committed snapshots while writers wait on external models."""
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic

import pytest
from remi.engine import MemoryEngine
from remi.storage import SQLiteStore
from remi.memory_maintenance import memory_fingerprint
from .helpers import ScriptedModel, command, request, query

pytestmark = pytest.mark.skipif(os.getenv('REMI_TEST_SCHEMA_APPROVED') != '1',
                               reason='Approved temporary six-table databases required')


@pytest.fixture
def store(tmp_path):
    path = tmp_path / 'snapshot.sqlite3'
    SQLiteStore.initialize(path, allow_create_schema=True)
    store = SQLiteStore(path)
    yield store
    store.close()


def test_snapshot_keeps_committed_view_and_releases_capacity_on_failure(store):
    store.put('requests', 'u', 'r', {'value': 'old'})
    with pytest.raises(ValueError):
        with store.search_snapshot(monotonic() + 2) as reader:
            assert reader.get('requests', 'u', 'r')['value'] == 'old'
            store.put('requests', 'u', 'r', {'value': 'new'})
            assert reader.get('requests', 'u', 'r')['value'] == 'old'
            raise ValueError('abort read')
    with store.search_snapshot(monotonic() + 2) as reader:
        assert reader.get('requests', 'u', 'r')['value'] == 'new'
    for _ in range(4):
        assert store._read_slots.acquire(blocking=False)
    try:
        with pytest.raises(TimeoutError):
            with store.search_snapshot(monotonic()):
                pass
    finally:
        for _ in range(4):
            store._read_slots.release()


def test_search_finishes_while_add_model_is_waiting(store):
    entered, release = Event(), Event()
    def extract(state, errors):
        if 'new note' in state['new_user_source']['content']:
            entered.set()
            assert release.wait(5)
        return {'commands': [command(state, kind='fact')]}
    engine = MemoryEngine(store=store, model=ScriptedModel(extract=extract))
    engine.add(request('The archive contains a blue folder.'))
    with ThreadPoolExecutor(max_workers=2) as pool:
        adding = pool.submit(engine.add, request('A new note arrived.', request_id='r2'))
        try:
            assert entered.wait(2)
            searching = pool.submit(engine.search, query('blue folder'), '2025-01-01T00:00:00Z')
            assert searching.result(timeout=2).data
            assert not adding.done()
        finally:
            release.set()
        adding.result(timeout=3)


def test_search_snapshot_and_heat_write_preserve_concurrent_revision(store):
    entered, release = Event(), Event()
    model = ScriptedModel(extract=lambda state, errors: {'commands': [command(state, kind='fact')]})
    engine = MemoryEngine(store=store, model=model)
    engine.add(request('The locker code is red.'))
    original = store.memories('u1')[0]
    def decide(state):
        entered.set()
        assert release.wait(5)
        return {'query_mode': 'direct', 'action_time_expression': None, 'searches': [], 'relevant_memory_ids': []}
    model.decider = decide
    with ThreadPoolExecutor(max_workers=2) as pool:
        searching = pool.submit(engine.search, query('locker code'), '2025-01-01T00:00:00Z')
        try:
            assert entered.wait(2)
            adding = pool.submit(engine.add, request('The locker code changed to blue.', request_id='r2'))
            adding.result(timeout=2)
        finally:
            release.set()
        response = searching.result(timeout=3)
    assert all('blue' not in item.content for item in response.data)
    current = store.memories('u1', [original.id])[0]
    assert 'blue' in current.current.content
    assert current.last_accessed_at == '2025-01-01T00:00:00+00:00'
    without_heat = current.model_copy(update={'last_accessed_at': None})
    assert memory_fingerprint(without_heat) == memory_fingerprint(current)
    assert memory_fingerprint(original) != memory_fingerprint(current)


def test_rollback_journal_compatibility_requires_explicit_migration(store):
    store.connection.execute('PRAGMA journal_mode=DELETE')
    with store.search_snapshot(monotonic() + 2) as reader:
        assert reader is None
    store.enable_wal()
    with store.search_snapshot(monotonic() + 2) as reader:
        assert reader is not None
