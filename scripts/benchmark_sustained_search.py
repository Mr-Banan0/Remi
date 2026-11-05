"""Bounded concurrent local Search soak; deterministic providers, no paid calls."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import resource
from time import perf_counter

from remi.engine import MemoryEngine
from remi.models import SearchRequest
from remi.settings import Settings
from remi.storage import SQLiteStore
from scripts.benchmark_memory_projection import FixedEmbedding


def run(path, requests, concurrency):
    store = SQLiteStore(path)
    engine = MemoryEngine(store, embedder=FixedEmbedding(), settings=Settings(search_timeout=180))
    request = SearchRequest(user_id='bench', query='topic7', top_k=10)
    def search(_):
        start = perf_counter()
        response = engine.search(request, '2025-01-01T00:00:00Z')
        digest = hashlib.sha256(json.dumps(response.model_dump(), sort_keys=True).encode()).hexdigest()
        return {'seconds': perf_counter()-start, 'sha256': digest}
    try:
        expected = search(0)['sha256']
        started = perf_counter()
        with ThreadPoolExecutor(max_workers=concurrency) as workers:
            results = list(workers.map(search, range(requests)))
        elapsed = perf_counter()-started
        return {'requests': requests, 'concurrency': concurrency, 'wall_seconds': elapsed,
                'requests_per_second': requests/elapsed,
                'equivalent_results': sum(row['sha256']==expected for row in results),
                'peak_rss_native': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                'runtime': engine.runtime_metrics.snapshot(), 'results': results}
    finally:
        engine.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--path',type=Path,required=True)
    parser.add_argument('--requests',type=int,default=100)
    parser.add_argument('--concurrency',type=int,choices=range(1,9),default=4)
    args=parser.parse_args()
    if args.requests < 1: parser.error('requests must be positive')
    print(json.dumps(run(args.path,args.requests,args.concurrency)),flush=True)
