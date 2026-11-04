"""Compare full-edge filtering with indexed adjacency in an approved temporary bank."""
import argparse
import json
import statistics
import tempfile
import time
from pathlib import Path

from remi.models import Edge
from remi.storage import SQLiteStore


def run(size, repeats):
    with tempfile.TemporaryDirectory(prefix='remi-graph-bench-') as directory:
        path = Path(directory) / 'bank.sqlite3'
        SQLiteStore.initialize(path, allow_create_schema=True)
        store = SQLiteStore(path)
        try:
            with store.transaction():
                for index in range(size):
                    edge = Edge(id=f'e{index:09d}', owner='bench', origin='seed' if index < 8 else f'm{index}',
                        target=f'n{index}', relation='related', memory_id=f'm{index}', revision_id='r1',
                        target_revision_id='r2', source_id='s1', quote='synthetic')
                    store.put('edges', 'bench', edge.id, edge.model_dump())
            def baseline():
                return [edge.id for edge in store.edges_for('bench')
                        if edge.relation == 'related' and 'seed' in (edge.origin, edge.target)]
            def indexed():
                return sorted({edge.id for edge in [
                    *store.edges_for('bench', origins=['seed'], relation='related'),
                    *store.edges_for('bench', targets=['seed'], relation='related')]})
            assert sorted(baseline()) == indexed()
            result = {'edges': size, 'neighbors': len(indexed()), 'equivalent': True}
            for label, method in [('full_scan', baseline), ('indexed', indexed)]:
                timings = []
                for _ in range(repeats):
                    started = time.perf_counter(); method()
                    timings.append((time.perf_counter() - started) * 1000)
                result[label + '_median_ms'] = statistics.median(timings)
            result['plans'] = {column: [row[3] for row in store.connection.execute(
                f'EXPLAIN QUERY PLAN SELECT record_json FROM edges INDEXED BY edge_{column} WHERE owner=? AND {column} IN (?) '
                'AND relation=? ORDER BY id', ('bench', 'seed', 'related'))] for column in ('origin', 'target')}
            return result
        finally:
            store.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--allow-temp-schema', action='store_true')
    parser.add_argument('--sizes', nargs='+', type=int, default=[1000, 10000, 100000])
    parser.add_argument('--repeats', type=int, default=5)
    args = parser.parse_args()
    if not args.allow_temp_schema:
        parser.error('Use --allow-temp-schema after approval for temporary six-table databases.')
    print(json.dumps([run(size, args.repeats) for size in args.sizes], indent=2))
