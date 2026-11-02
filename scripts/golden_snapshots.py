"""Immutable query-boundary copies of an existing Golden database."""
import hashlib
import os
from pathlib import Path
import sqlite3
import tempfile


def capture_query_snapshot(database, output, label):
    folder = Path(output) / 'query-snapshots'
    folder.mkdir(mode=0o700, exist_ok=True)
    target = folder / (hashlib.sha256(label.encode()).hexdigest()[:24] + '.sqlite3')
    if not target.exists():
        fd, temporary = tempfile.mkstemp(dir=folder, suffix='.sqlite3')
        os.close(fd)
        try:
            with sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro', uri=True) as source:
                with sqlite3.connect(temporary) as destination:
                    source.backup(destination)
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
    return dict(path=str(target.resolve()), sha256=hashlib.sha256(target.read_bytes()).hexdigest())


def copy_query_snapshot(snapshot, target):
    source_path = Path(snapshot['path'])
    if hashlib.sha256(source_path.read_bytes()).hexdigest() != snapshot['sha256']:
        raise ValueError('query_snapshot_fingerprint_mismatch')
    with sqlite3.connect(source_path.resolve().as_uri()+'?mode=ro', uri=True) as source:
        with sqlite3.connect(target) as destination:
            source.backup(destination)
