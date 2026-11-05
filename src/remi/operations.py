"""Scheduled backup and capacity checks with explicit retention planning."""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import os
from pathlib import Path
import shutil
from uuid import uuid4

from .maintenance import copy_database, inspect_database


@contextmanager
def operation_lock(directory):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(directory / '.maintenance.lock', os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('maintenance_already_running') from None
        yield


def capacity_report(database, backup_directory, *, min_free_bytes=1024**3, max_wal_bytes=512*1024**2):
    database, backups = Path(database).resolve(), Path(backup_directory).resolve()
    report = inspect_database(database)
    usage = shutil.disk_usage(database.parent)
    existing = [p for p in backups.glob('remi-backup-*.sqlite3') if p.is_file() and not p.is_symlink()]
    now = datetime.now(timezone.utc).timestamp()
    alerts = []
    if usage.free < min_free_bytes:
        alerts.append('database_disk_free_low')
    if report['wal_bytes'] > max_wal_bytes:
        alerts.append('wal_size_high')
    backup_age = now - max(p.stat().st_mtime for p in existing) if existing else None
    if backup_age is None or backup_age > 36*3600:
        alerts.append('backup_missing_or_stale')
    return {**report, 'free_bytes': usage.free, 'backup_count': len(existing),
            'backup_age_seconds': backup_age, 'alerts': alerts}


def backup_cycle(database, backup_directory, *, min_free_bytes=1024**3, keep_days=None):
    database, backups = Path(database).resolve(), Path(backup_directory).resolve()
    if keep_days is not None and keep_days < 1:
        raise ValueError('positive_backup_retention_required')
    with operation_lock(backups):
        initial = inspect_database(database)
        required = max(min_free_bytes, 2*(initial['database_bytes'] + initial['wal_bytes']))
        if shutil.disk_usage(backups).free < required:
            raise RuntimeError('backup_disk_reserve_insufficient')
        now = datetime.now(timezone.utc)
        destination = backups / ('remi-backup-' + now.strftime('%Y%m%dT%H%M%SZ') + '-' + uuid4().hex[:8] + '.sqlite3')
        backup = copy_database(database, destination)
        candidates = []
        if keep_days is not None:
            cutoff = now.timestamp() - keep_days*86400
            candidates = [p.name for p in backups.glob('remi-backup-*.sqlite3')
                          if p.is_file() and not p.is_symlink() and p.stat().st_mtime < cutoff]
        return {'backup': str(destination), 'verified': backup['integrity'] == 'ok',
                'retention_candidates': sorted(candidates), 'deleted_backups': 0,
                'capacity': capacity_report(database, backups, min_free_bytes=min_free_bytes)}
