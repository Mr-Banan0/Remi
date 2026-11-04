"""Synthetic long-history probe: run each runtime in a separate process on one bank.

Build a fresh temporary bank with --allow-temp-schema. Query old code first, then
query new code with --migrate. Migration changes derived metadata only. No paid calls.
"""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import resource
from time import perf_counter

from remi.engine import MemoryEngine
from remi.models import Edge, Memory, Revision, SearchRequest, Source
from remi.settings import Settings
from remi.storage import SQLiteStore


class FixedEmbedding:
    def embed(self, texts, *, deadline):
        return [[1.0] + [0.0] * 1023 for _ in texts]


def build(path, size):
    started = perf_counter()
    SQLiteStore.initialize(path, allow_create_schema=True)
    store = SQLiteStore(path)
    engine = MemoryEngine(store)
    vector = [1.0] + [0.0] * 1023
    epoch = datetime(2020, 1, 1, tzinfo=timezone.utc)
    revisions_count = 0
    with store.transaction():
        for index in range(size):
            revisions = []
            # Ten percent of records include an update and a later-ingested backfill.
            days = [index % 1460, index % 1460 + 30, index % 1460 + 10] if index % 10 == 0 else [index % 1460]
            for version, day in enumerate(days):
                stamp = (epoch + timedelta(days=day)).isoformat()
                created = (epoch + timedelta(days=day + (60 if version == 2 else 0))).isoformat()
                text = f'Archive item {index} topic{index % 100}, state {version}. ' + 'Personal historical evidence. ' * 12
                source = Source(id=f's{index}-{version}', owner='bench', session_id=f'session{index // 20}',
                    request_id=f'req{index}-{version}', ordinal=version, role='user', content=text,
                    message_at=stamp, created_at=created)
                store.put('sources', 'bench', source.id, source.model_dump())
                revision = Revision(id=f'r{index}-{version}', content=text, source_ids=[source.id], quote=text,
                    evidence=[dict(source_id=source.id, quote=text)], entity_ids=[], user_involvement='not_involved',
                    mode='asserted', context='personal', status='stated', planned_at=None, event_at=None,
                    observed_at=stamp, created_at=created, time_resolution='unknown', time_precision='unknown',
                    operation='create' if version == 0 else 'update',
                    replaces_revision_id=f'r{index}-0' if version else None)
                revisions.append(revision)
            memory = Memory(id=f'm{index}', owner='bench', record_type='fact', revisions=revisions)
            store.put('memories', 'bench', memory.id, memory.model_dump())
            for document in engine._documents([], [memory], owner='bench'):
                document.vector = vector
                store.put('search_documents', 'bench', document.id, document.model_dump())
            if index % 10 == 1:
                edge = Edge(id=f'e{index}', owner='bench', origin=f'm{index-1}', target=memory.id,
                    relation='related', revision_id=f'r{index-1}-1', target_revision_id=memory.current.id,
                    memory_id=f'm{index-1}', source_id=f's{index-1}-1',
                    quote=f'Archive item {index-1} topic{(index-1) % 100}, state 1. ' + 'Personal historical evidence. ' * 12)
                store.put('edges', 'bench', edge.id, edge.model_dump())
            revisions_count += len(revisions)
    store.rebuild_search_index(allow_create_schema=True)
    engine.close()
    return dict(memories=size, revisions=revisions_count, build_seconds=perf_counter()-started,
                database_bytes=path.stat().st_size)


def query(path, migrate, repeats):
    store = SQLiteStore(path)
    migration_started = perf_counter()
    if migrate:
        store.rebuild_memory_projection()
    migration_seconds = perf_counter()-migration_started
    engine = MemoryEngine(store, embedder=FixedEmbedding(), settings=Settings(search_timeout=180))
    results = []
    try:
        for clock in ('2025-01-01T00:00:00Z', '2022-01-01T00:00:00Z'):
            for repeat in range(repeats):
                started = perf_counter()
                response = engine.search(SearchRequest(user_id='bench', query='topic7', top_k=10), clock)
                encoded = json.dumps(response.model_dump(), sort_keys=True, ensure_ascii=False).encode()
                results.append(dict(clock=clock, repeat=repeat, seconds=perf_counter()-started,
                    response_sha256=hashlib.sha256(encoded).hexdigest(), performance=engine.last_performance))
        row = store.memories('bench', ['m0'])[0].model_dump()
        writes = []
        for i in range(20):
            row['pinned'] = bool(i % 2)
            started = perf_counter()
            store.put('memories', 'bench', 'm0', row)
            writes.append(perf_counter()-started)
    finally:
        engine.close()
    return dict(migration_seconds=migration_seconds, queries=results, memory_write_seconds=writes,
        peak_rss_native=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss, database_bytes=path.stat().st_size)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['build', 'query'])
    parser.add_argument('--path', type=Path, required=True)
    parser.add_argument('--size', type=int, default=10000)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--migrate', action='store_true')
    parser.add_argument('--allow-temp-schema', action='store_true')
    args = parser.parse_args()
    if args.mode == 'build' and not args.allow_temp_schema:
        parser.error('Temporary six-table bank and FTS index require approval.')
    result = build(args.path, args.size) if args.mode == 'build' else query(args.path, args.migrate, args.repeats)
    print(json.dumps(result), flush=True)
