"""Recovery creates verified fresh copies and lock registries release idle owners."""
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic
import pytest
from remi.engine import MemoryEngine
from remi.maintenance import copy_database, inspect_database
from remi.providers import ProviderUnavailable
from remi.storage import SQLiteStore


def test_owner_lock_registry_reentrant_timeout_and_cleanup():
    engine = MemoryEngine()
    entered, release = Event(), Event()
    def hold():
        with engine._scope('a', monotonic() + 2):
            with engine._scope('a', monotonic() + 2):
                entered.set()
                assert release.wait(3)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(hold)
        try:
            assert entered.wait(1)
            with pytest.raises(ProviderUnavailable):
                with engine._scope('a', monotonic()):
                    pass
            assert len(engine._locks) == 1
        finally:
            release.set()
        future.result(timeout=2)
    assert engine._locks == {}
    for index in range(100):
        with engine._scope(str(index), monotonic() + 1):
            pass
    assert engine._locks == {}


@pytest.mark.skipif(os.getenv('REMI_TEST_SCHEMA_APPROVED') != '1', reason='Approved temporary six-table copies required')
def test_backup_restore_include_wal_and_preserve_existing_destination(tmp_path):
    source, backup, restored = [tmp_path / name for name in ('source.sqlite3', 'backup.sqlite3', 'restored.sqlite3')]
    SQLiteStore.initialize(source, allow_create_schema=True)
    store = SQLiteStore(source)
    try:
        store.put('requests', 'owner', 'request', {'value': 'committed checkpoint'})
        report = copy_database(source, backup)
        assert report['integrity'] == 'ok' and report['records']['requests'] == 1
        store.put('requests', 'owner', 'request', {'value': 'later'})
        copy_database(backup, restored)
        recovered = SQLiteStore(restored)
        try:
            assert recovered.get('requests', 'owner', 'request')['value'] == 'committed checkpoint'
        finally:
            recovered.close()
        original = backup.read_bytes()
        with pytest.raises(FileExistsError):
            copy_database(source, backup)
        assert backup.read_bytes() == original
        with pytest.raises(TimeoutError):
            copy_database(source, tmp_path / 'timeout.sqlite3', timeout=-1)
        assert not (tmp_path / 'timeout.sqlite3').exists()
        assert list(tmp_path.glob('.remi-backup-*')) == []
        assert inspect_database(source)['integrity'] == 'ok'
    finally:
        store.close()
