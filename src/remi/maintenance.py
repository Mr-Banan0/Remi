"""Explicit database maintenance with fresh destinations and verified snapshots."""
from pathlib import Path
from contextlib import closing
import os
import sqlite3
from time import monotonic

from .storage import TABLES


def inspect_database(path):
    path = Path(path).resolve()
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not TABLES <= tables:
            raise RuntimeError('database_schema_missing')
        integrity = [row[0] for row in connection.execute('PRAGMA quick_check')]
        if integrity != ['ok']:
            raise RuntimeError('database_integrity_check_failed')
        result = {'integrity': 'ok', 'journal_mode': connection.execute('PRAGMA journal_mode').fetchone()[0],
                  'records': {table: connection.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                              for table in sorted(TABLES)}}
    result['database_bytes'] = path.stat().st_size
    wal = Path(str(path) + '-wal')
    result['wal_bytes'] = wal.stat().st_size if wal.exists() else 0
    return result


def copy_database(source, destination, *, timeout=60):
    """Use SQLite backup for WAL consistency; publish only a verified new file."""
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if source == destination or destination.exists():
        raise FileExistsError('database_destination_exists')
    if not source.is_file():
        raise FileNotFoundError(source)
    if not destination.parent.is_dir():
        raise FileNotFoundError(destination.parent)
    import tempfile
    descriptor, filename = tempfile.mkstemp(prefix='.remi-backup-', suffix='.sqlite3', dir=destination.parent)
    os.close(descriptor)
    temporary = Path(filename)
    started = monotonic()
    try:
        def progress(status, remaining, total):
            if monotonic() - started > timeout:
                raise TimeoutError('database_copy_timeout')
        reader = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)
        writer = sqlite3.connect(temporary)
        try:
            reader.backup(writer, pages=256, progress=progress, sleep=.01)
            # Publish a standalone file; later WAL activation is explicit.
            writer.execute('PRAGMA journal_mode=DELETE')
        finally:
            writer.close()
            reader.close()
        report = inspect_database(temporary)
        with temporary.open('rb') as stream:
            os.fsync(stream.fileno())
        # Hard-link publication is atomic and refuses an existing destination.
        os.link(temporary, destination)
        return {**report, 'seconds': monotonic() - started}
    finally:
        temporary.unlink(missing_ok=True)
        for suffix in ('-wal', '-shm', '-journal'):
            Path(str(temporary) + suffix).unlink(missing_ok=True)
