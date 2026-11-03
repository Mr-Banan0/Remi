"""Replay query snapshots to isolate deterministic storage and retrieval changes.

Planner decisions come from the supplied report. Synthetic embeddings and rerank
scores keep external model variability out of this offline equivalence check.
Run with the selected runtime's src directory on PYTHONPATH.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
from time import perf_counter

from remi.engine import MemoryEngine
from remi.models import SearchRequest
from remi.providers import RankResult
from remi.settings import Settings
from remi.storage import SQLiteStore


class RecordedPlanner:
    def __init__(self, trace):
        self.outputs = [deepcopy(attempt['raw']) for attempt in trace['planner_attempts']]
        self.inputs = []

    def decide(self, state, *, deadline):
        self.inputs.append(deepcopy(state))
        return self.outputs.pop(0)


class DeterministicEmbedder:
    def embed(self, texts, *, deadline):
        return [[(byte - 127.5) / 128 for byte in hashlib.shake_256(text.encode()).digest(1024)]
                for text in texts]


class DeterministicRanker:
    def __init__(self):
        self.inputs = []

    def rank(self, query, cards, *, deadline):
        self.inputs.append(deepcopy(cards))
        return RankResult(
            scores={card['id']: int(hashlib.sha256(card['id'].encode()).hexdigest()[:8], 16) / 2**32
                    for card in cards},
            temporal_scores={target['id']: .9 for card in cards
                             for target in card.get('temporal_targets', [])},
        )


def replay(report, mode):
    results = []
    with tempfile.TemporaryDirectory(prefix='remi-index-replay-') as temporary:
        questions = [*report['questions'], *report['lifecycle_searches']]
        for index, question in enumerate(questions):
            path = Path(temporary) / f'{index}.sqlite3'
            snapshot = Path(question['query_snapshot']['path']).resolve()
            expected_hash = question['query_snapshot'].get('sha256')
            if expected_hash and hashlib.sha256(snapshot.read_bytes()).hexdigest() != expected_hash:
                raise ValueError('query_snapshot_hash_mismatch')
            with sqlite3.connect(snapshot.as_uri() + '?mode=ro', uri=True) as source:
                with sqlite3.connect(path) as target:
                    source.backup(target)
            store = SQLiteStore(path)
            try:
                if mode == 'indexed':
                    store.rebuild_search_index(allow_create_schema=True)
                owners = [row[0] for row in store.connection.execute('SELECT DISTINCT owner FROM sources')]
                if len(owners) != 1:
                    candidate_ids = [row['document_id'] for row in question['trace']['candidates']]
                    owners = sorted({row[0] for key in candidate_ids for row in store.connection.execute(
                        'SELECT owner FROM search_documents WHERE id=?', (key,))})
                if len(owners) != 1:
                    raise ValueError('snapshot_query_owner_unresolved')
                planner = RecordedPlanner(question['trace'])
                ranker = DeterministicRanker()
                engine = MemoryEngine(store, planner, DeterministicEmbedder(), ranker, Settings())
                request = SearchRequest(
                    query=question.get('question', question.get('query')),
                    user_id=owners[0], top_k=100,
                )
                started = perf_counter()
                response = engine.search(request, question['trace']['as_of'])
                results.append({
                    'question_id': question.get('question_id', str(index)),
                    'response': response.model_dump(),
                    'planner_inputs': planner.inputs,
                    'ranker_inputs': ranker.inputs,
                    'trace': engine.last_search,
                    'seconds': perf_counter() - started,
                })
            finally:
                store.close()
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    parser.add_argument('mode', choices=('baseline', 'indexed'))
    parser.add_argument('output', type=Path)
    parser.add_argument('--allow-create-index', action='store_true')
    args = parser.parse_args()
    if args.mode == 'indexed' and not args.allow_create_index:
        parser.error('indexed replay requires --allow-create-index')
    if args.output.exists():
        parser.error('choose a fresh output path')
    results = replay(json.loads(args.report.read_text()), args.mode)
    with args.output.open('x') as output:
        json.dump(results, output, ensure_ascii=False)
    print(json.dumps({'cases': len(results), 'seconds': sum(row['seconds'] for row in results)}))


if __name__ == '__main__':
    main()
