"""Exact lexical index and lazy document handles for SQLite Search.

FTS stores Remi-tokenized, hex-encoded terms. Ranking uses the existing BM25
formula over the caller's visible pool, preserving historical-query statistics.
"""
from collections import OrderedDict
import math

from .models import Document
from .retrieval import tokenize


class RecordCache:
    """Byte- and entry-bounded LRU of immutable SQLite rows."""

    def __init__(self, max_bytes=8 * 1024 * 1024, max_entries=2048):
        self.max_bytes = max_bytes
        self.max_entries = max_entries
        self.rows = OrderedDict()
        self.bytes = 0
        self.hits = 0
        self.misses = 0

    def clear(self):
        self.rows.clear()
        self.bytes = 0

    def discard(self, key):
        old = self.rows.pop(key, None)
        if old is not None:
            self.bytes -= old[1]

    def get(self, key):
        item = self.rows.get(key)
        if item is None:
            self.misses += 1
            return None
        self.rows.move_to_end(key)
        self.hits += 1
        return item[0]

    def put(self, key, row):
        self.discard(key)
        size = len(row[0].encode()) + (len(row[1]) if row[1] else 0)
        size += sum(len(str(part).encode()) for part in key)
        if size > self.max_bytes or self.max_entries <= 0:
            return
        while self.rows and (self.bytes + size > self.max_bytes or len(self.rows) >= self.max_entries):
            _, (_, removed) = self.rows.popitem(last=False)
            self.bytes -= removed
        self.rows[key] = (row, size)
        self.bytes += size


class DocumentHandle:
    """Metadata participates in retrieval; full payload loads on first use."""

    def __init__(self, store, metadata):
        self._store = store
        self._metadata = metadata
        self._document = None

    def _load(self):
        if self._document is None:
            raw = self._store.get('search_documents', self.owner, self.id)
            if raw is None:
                raise RuntimeError('search_document_changed_during_read')
            self._document = Document.model_validate(raw)
        return self._document

    def __getattr__(self, name):
        if name in self._metadata:
            return self._metadata[name]
        return getattr(self._load(), name)

    def model_copy(self, **kwargs):
        return self._load().model_copy(**kwargs)


def encoded_terms(text):
    terms = tokenize(text)
    return len(terms), ' '.join(term.encode('utf-8').hex() for term in terms)


def lexical_scores(connection, owner, query, document_ids):
    allowed = set(document_ids)
    lengths = {key: int(length) for key, length in connection.execute(
        'SELECT document_id, token_count FROM search_lexical WHERE owner=?', (owner,))
        if key in allowed}
    if lengths.keys() != allowed:
        raise RuntimeError('search_index_incomplete_run_rebuild')
    average = sum(lengths.values()) / max(len(lengths), 1) or 1
    scores = {}
    for term in set(tokenize(query)):
        counts = {key: count for key, count in connection.execute(
            'SELECT f.document_id, count(*) FROM search_lexical_vocab v '
            'JOIN search_lexical f ON f.rowid=v.doc '
            'WHERE v.term=? AND f.owner=? GROUP BY f.document_id',
            (term.encode('utf-8').hex(), owner)) if key in allowed}
        idf = math.log(1 + (len(lengths) - len(counts) + .5) / (len(counts) + .5))
        for key, tf in counts.items():
            scores[key] = scores.get(key, 0.0) + idf * tf * 2.5 / (
                tf + 1.5 * (.25 + .75 * lengths[key] / average))
    return scores
