import pytest

from remi.storage import SQLiteStore, TABLES
from scripts.golden_snapshots import capture_query_snapshot, copy_query_snapshot


def test_query_snapshot_excludes_future_ingestion_and_remains_immutable(tmp_path):
    database = tmp_path/'bank.sqlite3'
    SQLiteStore.initialize(database, allow_create_schema=True)
    store = SQLiteStore(database)
    store.put('sources','u1','before',dict(id='before',owner='u1',content='Before query'))
    snapshot = capture_query_snapshot(database,tmp_path,'query-one')
    store.put('sources','u1','future',dict(id='future',owner='u1',content='After query'))
    assert capture_query_snapshot(database,tmp_path,'query-one') == snapshot
    replay = tmp_path/'replay.sqlite3'
    copy_query_snapshot(snapshot,replay)
    restored = SQLiteStore(replay)
    assert [x['id'] for x in restored.list('sources','u1')] == ['before']
    assert {x[0] for x in restored.connection.execute("select name from sqlite_master where type='table'")} == TABLES
    restored.put('sources','u1','later-query',dict(id='later-query',owner='u1',content='Later'))
    restored.close()
    copy_query_snapshot(snapshot,replay)
    restored = SQLiteStore(replay)
    assert [x['id'] for x in restored.list('sources','u1')] == ['before']
    restored.close()
    assert len(store.list('sources','u1')) == 2
    store.close()


def test_snapshot_copy_checks_integrity_before_replacing_bank(tmp_path):
    database = tmp_path/'bank.sqlite3'
    SQLiteStore.initialize(database, allow_create_schema=True)
    snapshot = capture_query_snapshot(database,tmp_path,'query')
    with pytest.raises(ValueError,match='fingerprint_mismatch'):
        copy_query_snapshot({**snapshot,'sha256':'wrong'},tmp_path/'copy.sqlite3')
    assert not (tmp_path/'copy.sqlite3').exists()
