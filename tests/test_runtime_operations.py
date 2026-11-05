import json
import os
from concurrent.futures import ThreadPoolExecutor
from time import monotonic

import pytest
from fastapi.testclient import TestClient
from remi.api import create_app
from remi.engine import MemoryEngine
from remi.models import SearchRequest
from remi.runtime_metrics import RuntimeMetrics
from remi.operations import backup_cycle, capacity_report, operation_lock
from remi.storage import SQLiteStore


def test_metrics_are_bounded_concurrent_and_content_free():
    metrics = RuntimeMetrics()
    def record(_):
        metrics.begin('search')
        metrics.finish({'operation':'search','total_seconds':.1,'success':True,'work':{'rows':3},'stages':{}})
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(record, range(1200)))
    row = metrics.snapshot()['operations']['search']
    assert row['completed'] == 1200 and row['inflight'] == 0
    assert row['window_count'] == 1024 and row['p95_seconds'] == .1
    assert row['work']['rows'] == 3600
    row['work']['rows'] = 0
    assert metrics.snapshot()['operations']['search']['work']['rows'] == 3600


def test_metrics_endpoint_requires_auth_and_tracks_real_search_without_text():
    engine = MemoryEngine()
    client = TestClient(create_app(engine, api_key='test-secret'))
    assert client.get('/metrics').status_code == 401
    engine.search(SearchRequest(user_id='private-owner', query='private-query', top_k=1))
    response = client.get('/metrics', headers={'x-api-key':'test-secret'})
    assert response.status_code == 200
    assert response.json()['operations']['search']['completed'] == 1
    assert 'private-owner' not in response.text and 'private-query' not in response.text
    assert TestClient(create_app(MemoryEngine(), api_key='')).get('/metrics').status_code == 503


@pytest.mark.skipif(os.getenv('REMI_TEST_SCHEMA_APPROVED') != '1', reason='Temporary schema requires approval')
def test_backup_cycle_preserves_data_and_reports_retention_without_deleting(tmp_path):
    db = tmp_path/'db.sqlite3';backups = tmp_path/'backups'
    SQLiteStore.initialize(db, allow_create_schema=True)
    store = SQLiteStore(db)
    try:
        store.put('requests','u','r',{'checkpoint':'retained'})
        assert 'backup_missing_or_stale' in capacity_report(db,backups)['alerts']
        first = backup_cycle(db, backups)
        old = first['backup'];os.utime(old,(0,0))
        second = backup_cycle(db, backups, keep_days=30)
        assert second['verified'] and second['deleted_backups'] == 0
        assert second['retention_candidates'] == [os.path.basename(old)]
        recovered = SQLiteStore(second['backup'])
        try:assert recovered.get('requests','u','r')['checkpoint']=='retained'
        finally:recovered.close()
        assert 'backup_missing_or_stale' not in second['capacity']['alerts']
        with operation_lock(backups):
            with pytest.raises(RuntimeError,match='already_running'):
                backup_cycle(db,backups)
        with pytest.raises(RuntimeError,match='reserve'):
            backup_cycle(db,backups,min_free_bytes=10**18)
    finally:store.close()


@pytest.mark.skipif(os.getenv('REMI_TEST_SCHEMA_APPROVED') != '1', reason='Temporary schema requires approval')
def test_configured_snapshot_capacity_releases_after_timeout(tmp_path,monkeypatch):
    db=tmp_path/'db.sqlite3';SQLiteStore.initialize(db,allow_create_schema=True)
    monkeypatch.setenv('REMI_SEARCH_READ_CONCURRENCY','1');store=SQLiteStore(db)
    try:
        with store.search_snapshot(monotonic()+2):
            with pytest.raises(TimeoutError):
                with store.search_snapshot(monotonic()):pass
        with store.search_snapshot(monotonic()+2) as reader:
            assert reader is not None
    finally:store.close()
