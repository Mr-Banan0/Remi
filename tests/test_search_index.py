"""Index migration is explicitly approved separately from the original six tables."""
import os
from types import SimpleNamespace

import pytest

from remi.models import Document
from remi.retrieval import bm25
from remi.search_index import DocumentHandle, RecordCache
from remi.storage import SQLiteStore

pytestmark = pytest.mark.skipif(os.getenv('REMI_TEST_INDEX_APPROVED') != '1',
                               reason='Explicit temporary index-schema approval required')


@pytest.fixture
def indexed(tmp_path):
    path = tmp_path / 'index.sqlite3'
    SQLiteStore.initialize(path, allow_create_schema=True)
    store = SQLiteStore(path)
    store.rebuild_search_index(allow_create_schema=True)
    yield store, path
    store.close()


def document(key, text, owner='a'):
    return Document(id=key, owner=owner, text=text, source_ids=[], memory_id=None,
                    revision_id=None, message_at=None, event_at=None,
                    created_at='2024-01-01T00:00:00Z', vector=[1.0, 0.0])


def save(store, doc):
    store.put('search_documents', doc.owner, doc.id, doc.model_dump())


def test_exact_multilingual_scores_subset_statistics_and_lazy_load(indexed):
    store, _ = indexed
    texts = ['周五会议改到周一。', 'meeting meeting Monday', '', 'Réunion lundi',
             '会議は月曜日です', 'İstanbul Straße', 'مرحبا اجتماع', '"OR" AND * :']
    docs = [document(str(i), text) for i, text in enumerate(texts)]
    for doc in docs:
        save(store, doc)
        save(store, document(doc.id, 'other owner', 'b'))
    handles = store.search_documents('a')
    assert all(isinstance(h, DocumentHandle) and h._document is None for h in handles)
    for pool in (docs, docs[:3], docs[::2], []):
        for query in texts:
            expected = bm25(query, pool)
            assert store.lexical_scores('a', query, pool) == expected
    assert all(h._document is None for h in handles)
    assert handles[0].text == docs[0].text
    assert handles[0].model_copy(deep=True) == docs[0]
    assert handles[1]._document is None


def test_updates_deletes_and_rollback_preserve_index_and_cache(indexed):
    store, _ = indexed
    original = document('one', 'old meeting')
    save(store, original)
    assert store.get('search_documents', 'a', 'one')['text'] == 'old meeting'
    with pytest.raises(RuntimeError):
        with store.transaction():
            save(store, document('one', 'new meeting'))
            assert store.get('search_documents', 'a', 'one')['text'] == 'new meeting'
            assert store.lexical_scores('a', 'new', [original])
            raise RuntimeError('rollback')
    assert store.get('search_documents', 'a', 'one')['text'] == 'old meeting'
    assert store.lexical_scores('a', 'new', [original]) == {}
    save(store, document('one', 'new meeting'))
    assert store.lexical_scores('a', 'old', [original]) == {}
    store.delete('search_documents', 'a', 'one')
    assert store.get('search_documents', 'a', 'one') is None
    assert store.lexical_scores('a', 'new', []) == {}


def test_external_connection_invalidation_and_owner_isolation(indexed):
    first, path = indexed
    second = SQLiteStore(path)
    try:
        save(first, document('same', 'owner a'))
        save(first, document('same', 'owner b', 'b'))
        assert first.get('search_documents','a','same')['text'] == 'owner a'
        save(second, document('same','updated'))
        assert first.get('search_documents','a','same')['text'] == 'updated'
        assert first.get('search_documents','b','same')['text'] == 'owner b'
        mutated = first.get('search_documents','a','same')
        mutated['text'] = 'caller mutation'
        assert first.get('search_documents','a','same')['text'] == 'updated'
    finally:
        second.close()


def test_migration_requires_permission_and_rebuild_is_idempotent(tmp_path):
    path = tmp_path / 'legacy.sqlite3'
    SQLiteStore.initialize(path, allow_create_schema=True)
    store = SQLiteStore(path)
    try:
        doc = document('one', 'existing history')
        save(store, doc)
        with pytest.raises(PermissionError):
            store.rebuild_search_index()
        before = store.documents('a')
        for _ in range(2):
            store.rebuild_search_index(allow_create_schema=True)
            assert store.documents('a') == before
            assert store.lexical_scores('a','history',before) == bm25('history',before)
    finally:
        store.close()


def test_cache_limits_and_eviction():
    cache = RecordCache(max_bytes=100, max_entries=2)
    for i in range(10):
        cache.put(('sources','owner',str(i)), ('a' * 20, None))
    assert cache.bytes <= 100 and len(cache.rows) <= 2
    assert cache.get(('sources','owner','0')) is None
    cache.put(('sources','owner','large'), ('x' * 101, None))
    assert cache.get(('sources','owner','large')) is None
    cache.clear()
    assert cache.bytes == 0 and not cache.rows


def test_connection_opened_before_migration_maintains_new_index(tmp_path):
    path = tmp_path / 'migration.sqlite3'
    SQLiteStore.initialize(path, allow_create_schema=True)
    first, second = SQLiteStore(path), SQLiteStore(path)
    try:
        first.rebuild_search_index(allow_create_schema=True)
        doc = document('new', 'after migration')
        save(second, doc)
        assert first.lexical_scores('a', 'migration', [doc]) == bm25('migration', [doc])
    finally:
        first.close()
        second.close()


def test_indexed_add_search_lifecycle_matches_legacy(tmp_path):
    from .helpers import TestEngine, HashEmbedder, ScriptedModel, command, query, request
    def build(name, migrate):
        path = tmp_path / name
        SQLiteStore.initialize(path, allow_create_schema=True)
        store = SQLiteStore(path)
        if migrate:
            store.rebuild_search_index(allow_create_schema=True)
        model = ScriptedModel(lambda state, errors: {'commands': [command(state, status='planned', time='tomorrow')]})
        return TestEngine(store, model, HashEmbedder())
    old, new = build('old.sqlite3', False), build('new.sqlite3', True)
    try:
        payload = request('I plan a workshop tomorrow.')
        old.add(payload)
        new.add(payload)
        for engine in (old,new):
            response = engine.search(query('workshop'))
            assert response.data and 'workshop' in response.data[0].content
        assert [(d.id,d.text,d.vector) for d in old.store.documents('u1')] == [
            (d.id,d.text,d.vector) for d in new.store.documents('u1')]
        assert [d.id for d in old.search(query('workshop')).data] == [
            d.id for d in new.search(query('workshop')).data]
    finally:
        old.close()
        new.close()
