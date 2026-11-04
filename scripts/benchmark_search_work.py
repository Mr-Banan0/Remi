"""Offline scale probe with synthetic memories, exact vectors and no model calls."""
import argparse
import json
import tempfile
from pathlib import Path
from time import perf_counter

from remi.engine import MemoryEngine
from remi.models import Source, Revision, Memory, SearchRequest
from remi.settings import Settings
from remi.storage import SQLiteStore

STAMP = '2024-01-01T00:00:00+00:00'


class FixedEmbedding:
    def embed(self, texts, *, deadline):
        return [[1.0, *([0.0] * 1023)] for _ in texts]


def run(size):
    with tempfile.TemporaryDirectory(prefix='remi-search-scale-') as directory:
        path = Path(directory) / 'memory.sqlite3'
        SQLiteStore.initialize(path, allow_create_schema=True)
        store = SQLiteStore(path)
        engine = MemoryEngine(store=store, embedder=FixedEmbedding(), settings=Settings(search_timeout=180))
        try:
            with store.transaction():
                for index in range(size):
                    text = f'Archive item {index} concerns topic{index % 100}.'
                    source = Source(id=f's{index}', owner='bench', session_id=f'session{index // 20}',
                        request_id=f'req{index}', ordinal=index, role='user', content=text,
                        message_at=STAMP, created_at=STAMP)
                    revision = Revision(id=f'r{index}', content=text, source_ids=[source.id], quote=text,
                        evidence=[{'source_id': source.id, 'quote': text}], entity_ids=[], user_involvement='not_involved',
                        mode='asserted', context='personal', status='stated', planned_at=None, event_at=None,
                        observed_at=STAMP, created_at=STAMP, time_resolution='unknown', time_precision='unknown', operation='create')
                    memory = Memory(id=f'm{index}', owner='bench', record_type='fact', revisions=[revision])
                    store.put('sources', 'bench', source.id, source.model_dump())
                    store.put('memories', 'bench', memory.id, memory.model_dump())
                    for document in engine._documents([], [memory], owner='bench'):
                        document.vector = [1.0, *([0.0] * 1023)]
                        store.put('search_documents', 'bench', document.id, document.model_dump())
            store.rebuild_search_index(allow_create_schema=True)
            started = perf_counter()
            response = engine.search(SearchRequest(user_id='bench', query='topic7', top_k=10), '2025-01-01T00:00:00Z')
            return {'memories': size, 'vector_dimensions': 1024, 'returned': len(response.data),
                    'seconds': perf_counter() - started, 'performance': engine.last_performance}
        finally:
            engine.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--allow-temp-schema', action='store_true')
    parser.add_argument('--sizes', type=int, nargs='+', default=[1000, 10000])
    args = parser.parse_args()
    if not args.allow_temp_schema:
        parser.error('Temporary original tables and lexical index require approval.')
    for size in args.sizes:
        print(json.dumps(run(size)), flush=True)
