"""Six explicit tables; startup opens an existing database and never creates a schema."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from threading import RLock

from .models import Document, Edge, GraphObject, Memory, Source, SCHEMA_VERSION
from .search_index import RecordCache, DocumentHandle, encoded_terms, lexical_scores


SCHEMA = """
CREATE TABLE sources (owner TEXT NOT NULL, id TEXT NOT NULL, record_json TEXT NOT NULL, PRIMARY KEY(owner,id));
CREATE TABLE memories (owner TEXT NOT NULL, id TEXT NOT NULL, record_json TEXT NOT NULL, PRIMARY KEY(owner,id));
CREATE TABLE objects (owner TEXT NOT NULL, id TEXT NOT NULL, record_json TEXT NOT NULL, PRIMARY KEY(owner,id));
CREATE TABLE edges (owner TEXT NOT NULL, id TEXT NOT NULL, origin TEXT NOT NULL, target TEXT NOT NULL,
                    relation TEXT NOT NULL, record_json TEXT NOT NULL, PRIMARY KEY(owner,id));
CREATE INDEX edge_target ON edges(owner,target,relation);
CREATE INDEX edge_origin ON edges(owner,origin,relation);
CREATE TABLE requests (owner TEXT NOT NULL, id TEXT NOT NULL, record_json TEXT NOT NULL, PRIMARY KEY(owner,id));
CREATE TABLE search_documents (owner TEXT NOT NULL, id TEXT NOT NULL, record_json TEXT NOT NULL,
                               vector BLOB, PRIMARY KEY(owner,id));
"""
TABLES = {"sources", "memories", "objects", "edges", "requests", "search_documents"}


class MemoryStore:
    """Deterministic offline store with the same record-oriented interface."""

    def __init__(self):
        self.records = {table: {} for table in TABLES}
        self.lock = RLock()

    def get(self, table, owner, key):
        with self.lock:
            value = self.records[table].get((owner, key))
            return json.loads(json.dumps(value)) if value is not None else None

    def put(self, table, owner, key, value):
        with self.lock:
            self.records[table][owner, key] = json.loads(json.dumps(value))

    def list(self, table, owner):
        with self.lock:
            return [json.loads(json.dumps(value)) for (scope, _), value in self.records[table].items() if scope == owner]

    @contextmanager
    def transaction(self):
        with self.lock:
            snapshot = json.loads(json.dumps({table: list(values.items()) for table, values in self.records.items()}))
            try:
                yield
            except Exception:
                self.records = {table: {tuple(key): value for key, value in values} for table, values in snapshot.items()}
                raise

    def delete(self, table, owner, key):
        with self.lock:
            self.records[table].pop((owner, key), None)

    def close(self):
        pass

    def sources(self, owner, ids):
        return [Source.model_validate(value) for key in dict.fromkeys(ids)
                if (value := self.get("sources", owner, key)) is not None]

    def context_before(self, owner, session_id, source, limit=1):
        from .clock import parse_time
        rows = [Source.model_validate(value) for value in self.list("sources", owner)
                if value["role"] in {"user", "assistant"} and value["session_id"] == session_id
                and (value["created_at"], value["ordinal"]) < (source.created_at, source.ordinal)
                and (not source.message_at or not value["message_at"] or parse_time(value["message_at"]) <= parse_time(source.message_at))]
        return sorted(rows, key=lambda row: (row.created_at, row.ordinal), reverse=True)[:limit]

    def memories(self, owner, ids=None):
        values = self.list("memories", owner) if ids is None else [self.get("memories", owner, key) for key in dict.fromkeys(ids)]
        return [Memory.model_validate(value) for value in values if value is not None]

    def search_documents(self, owner):
        return self.documents(owner)

    def lexical_scores(self, owner, query, documents):
        from .retrieval import bm25
        return bm25(query, documents)

    def documents(self, owner):
        return [Document.model_validate(value) for value in self.list("search_documents", owner)]

    def vector_scores(self, owner, vector, document_ids):
        from .retrieval import cosine
        ids = set(document_ids)
        return {document.id: cosine(vector, document.vector) for document in self.documents(owner)
                if document.id in ids and document.vector is not None}

    def edges_for(self, owner, origins=None, targets=None, limit=None):
        rows = [value for value in self.list("edges", owner)
                if "relation" in value
                and (origins is None or value["origin"] in origins)
                and (targets is None or value["target"] in targets)]
        return [Edge.model_validate(value) for value in rows[:limit]]

    def snapshot(self):
        return {table: [json.loads(json.dumps(row)) for row in records.values()]
                for table, records in self.records.items()}

    def restore(self, snapshot):
        if set(snapshot) != set(self.records):
            raise ValueError("snapshot_requires_existing_six_collections")
        with self.transaction():
            for table, rows in snapshot.items():
                for row in rows:
                    self.put(table, row["owner"], row.get("id") or row["request_id"], row)



class SQLiteStore(MemoryStore):
    def __init__(self, path: str | Path):
        path = Path(path).resolve()
        if not path.is_file():
            raise RuntimeError("database_missing_use_explicit_approved_init")
        self.lock = RLock()
        self.connection = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, check_same_thread=False,
                                          isolation_level=None, timeout=10)
        self.connection.execute("PRAGMA busy_timeout=10000")
        self.connection.execute("PRAGMA foreign_keys=ON")
        actual = {row[0] for row in self.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not TABLES <= actual:
            self.connection.close()
            raise RuntimeError("database_schema_missing_use_explicit_approved_init")
        old_memory = self.connection.execute("SELECT 1 FROM memories WHERE COALESCE(json_extract(record_json,'$.schema_version'),0) != ? LIMIT 1", (SCHEMA_VERSION,)).fetchone()
        old_entity = self.connection.execute("SELECT 1 FROM objects WHERE (COALESCE(json_extract(record_json,'$.schema_version'),0) != ? OR COALESCE(json_extract(record_json,'$.kind'),'') NOT IN ('person','animal','organization')) LIMIT 1", (SCHEMA_VERSION,)).fetchone()
        if old_memory or old_entity:
            self.connection.close()
            raise RuntimeError("database_records_require_subject_memory_source_replay")
        import sqlite_vec
        self.connection.enable_load_extension(True)
        try:
            sqlite_vec.load(self.connection)
        finally:
            self.connection.enable_load_extension(False)
        self._depth = 0
        self.record_cache = RecordCache()
        self._data_version = self.connection.execute("PRAGMA data_version").fetchone()[0]
        self._indexed = {'search_lexical', 'search_lexical_vocab'} <= actual

    @staticmethod
    def initialize(path: str | Path, *, allow_create_schema=False):
        if not allow_create_schema:
            raise PermissionError("explicit_schema_approval_required")
        path = Path(path).resolve()
        if path.exists():
            raise FileExistsError("database_already_exists")
        path.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive file creation protects concurrent initialization and existing data.
        with path.open("xb"):
            pass
        connection = sqlite3.connect(path)
        try:
            connection.executescript("BEGIN;" + SCHEMA + "COMMIT;")
        finally:
            connection.close()

    def _check_external_changes(self):
        version = self.connection.execute("PRAGMA data_version").fetchone()[0]
        if version != self._data_version:
            self.record_cache.clear()
            self._data_version = version
            tables = {row[0] for row in self.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self._indexed = {'search_lexical', 'search_lexical_vocab'} <= tables

    def get(self, table, owner, key):
        self._table(table)
        with self.lock:
            self._check_external_changes()
            cache_key = (table, owner, key)
            row = self.record_cache.get(cache_key)
            if row is None:
                row = self.connection.execute(
                    f"SELECT record_json, {self._vector_column(table)} FROM {table} WHERE owner=? AND id=?",
                    (owner, key)).fetchone()
                if row is not None:
                    self.record_cache.put(cache_key, row)
            return self._decode(table, row) if row else None

    def search_documents(self, owner):
        with self.lock:
            self._check_external_changes()
        if not self._indexed:
            return super().search_documents(owner)
        fields = ('id', 'owner', 'source_ids', 'memory_id', 'revision_id',
                  'message_at', 'event_at', 'created_at')
        expressions = ','.join("json_extract(record_json,'$." + field + "')" for field in fields)
        with self.lock:
            rows = self.connection.execute(
                f"SELECT {expressions} FROM search_documents WHERE owner=? ORDER BY id", (owner,)).fetchall()
        handles = []
        for row in rows:
            metadata = dict(zip(fields, row))
            metadata['source_ids'] = json.loads(metadata['source_ids'])
            handles.append(DocumentHandle(self, metadata))
        return handles

    def lexical_scores(self, owner, query, documents):
        if not self._indexed:
            return super().lexical_scores(owner, query, documents)
        with self.lock:
            return lexical_scores(self.connection, owner, query, [document.id for document in documents])

    def rebuild_search_index(self, *, allow_create_schema=False):
        """Explicit, transactional migration; existing banks remain untouched until requested."""
        if not allow_create_schema:
            raise PermissionError('explicit_schema_approval_required')
        with self.transaction():
            self.connection.execute("CREATE VIRTUAL TABLE IF NOT EXISTS search_lexical USING fts5("
                "owner UNINDEXED, document_id UNINDEXED, token_count UNINDEXED, terms, tokenize='ascii')")
            self.connection.execute("CREATE VIRTUAL TABLE IF NOT EXISTS search_lexical_vocab USING "
                "fts5vocab(search_lexical, 'instance')")
            self.connection.execute('DELETE FROM search_lexical')
            for rowid, owner, key, raw in self.connection.execute('SELECT rowid,owner,id,record_json FROM search_documents'):
                length, terms = encoded_terms(json.loads(raw)['text'])
                self.connection.execute('INSERT INTO search_lexical(rowid,owner,document_id,token_count,terms) VALUES(?,?,?,?,?)', (rowid,owner,key,length,terms))
        self._indexed = True

    def list(self, table, owner):
        self._table(table)
        with self.lock:
            return [self._decode(table, row) for row in self.connection.execute(
                f"SELECT record_json, {self._vector_column(table)} FROM {table} WHERE owner=? ORDER BY id", (owner,))]

    def put(self, table, owner, key, value):
        self._table(table)
        record = dict(value)
        with self.transaction():
            self._check_external_changes()
            self.record_cache.discard((table, owner, key))
            if table == "edges":
                self.connection.execute("INSERT INTO edges VALUES(?,?,?,?,?,?) ON CONFLICT(owner,id) DO UPDATE SET "
                    "origin=excluded.origin,target=excluded.target,relation=excluded.relation,record_json=excluded.record_json",
                    (owner, key, record["origin"], record["target"], record["relation"], json.dumps(record, ensure_ascii=False)))
            elif table == "search_documents":
                import sqlite_vec
                vector = record.pop("vector", None)
                blob = sqlite_vec.serialize_float32(vector) if vector is not None else None
                self.connection.execute("INSERT INTO search_documents VALUES(?,?,?,?) ON CONFLICT(owner,id) DO UPDATE SET "
                    "record_json=excluded.record_json,vector=excluded.vector", (owner, key, json.dumps(record, ensure_ascii=False), blob))
                if self._indexed:
                    rowid = self.connection.execute('SELECT rowid FROM search_documents WHERE owner=? AND id=?', (owner,key)).fetchone()[0]
                    self.connection.execute('DELETE FROM search_lexical WHERE rowid=?', (rowid,))
                    length, terms = encoded_terms(record['text'])
                    self.connection.execute('INSERT INTO search_lexical(rowid,owner,document_id,token_count,terms) VALUES(?,?,?,?,?)', (rowid,owner,key,length,terms))
            else:
                self.connection.execute(f"INSERT INTO {table} VALUES(?,?,?) ON CONFLICT(owner,id) DO UPDATE SET record_json=excluded.record_json",
                                        (owner, key, json.dumps(record, ensure_ascii=False)))

    def context_before(self, owner, session_id, source, limit=1):
        with self.lock:
            rows = self.connection.execute("SELECT record_json FROM sources WHERE owner=? "
                "AND json_extract(record_json,'$.session_id')=? AND json_extract(record_json,'$.role') IN ('user','assistant') "
                "AND (json_extract(record_json,'$.created_at') < ? OR (json_extract(record_json,'$.created_at')=? "
                "AND json_extract(record_json,'$.ordinal')<?)) "
                "AND (? IS NULL OR json_extract(record_json,'$.message_at') IS NULL "
                "OR julianday(json_extract(record_json,'$.message_at'))<=julianday(?)) "
                "ORDER BY json_extract(record_json,'$.created_at') DESC,json_extract(record_json,'$.ordinal') DESC LIMIT ?",
                (owner, session_id, source.created_at, source.created_at, source.ordinal, source.message_at, source.message_at, limit))
            return [Source.model_validate_json(row[0]) for row in rows]

    def delete(self, table, owner, key):
        self._table(table)
        with self.transaction():
            self._check_external_changes()
            self.record_cache.discard((table, owner, key))
            if table == 'search_documents' and self._indexed:
                self.connection.execute('DELETE FROM search_lexical WHERE rowid IN '
                    '(SELECT rowid FROM search_documents WHERE owner=? AND id=?)', (owner,key))
            self.connection.execute(f"DELETE FROM {table} WHERE owner=? AND id=?", (owner, key))

    def edges_for(self, owner, origins=None, targets=None, limit=None):
        clauses, args = ["owner=?"], [owner]
        for column, keys in (("origin", origins), ("target", targets)):
            if keys is not None:
                if not keys:
                    return []
                clauses.append(f"{column} IN ({','.join('?' for _ in keys)})")
                args.extend(keys)
        with self.lock:
            suffix = " LIMIT ?" if limit is not None else ""
            if limit is not None:
                args.append(limit)
            rows = [json.loads(row[0]) for row in self.connection.execute(
                "SELECT record_json FROM edges WHERE " + " AND ".join(clauses) + " ORDER BY id" + suffix, args)]
            return [Edge.model_validate(row) for row in rows if "relation" in row]

    def vector_scores(self, owner, vector, document_ids):
        import sqlite_vec
        ids = list(dict.fromkeys(document_ids))
        scores = {}
        with self.lock:
            for offset in range(0, len(ids), 500):
                batch = ids[offset:offset + 500]
                placeholders = ",".join("?" for _ in batch)
                rows = self.connection.execute(
                    f"SELECT id, 1.0-vec_distance_cosine(vector, ?) FROM search_documents "
                    f"WHERE owner=? AND vector IS NOT NULL AND id IN ({placeholders})",
                    [sqlite_vec.serialize_float32(vector), owner, *batch])
                scores.update({row[0]: float(row[1]) for row in rows})
        return scores

    @contextmanager
    def transaction(self):
        with self.lock:
            top = self._depth == 0
            if top:
                self.connection.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield
                if top:
                    self.connection.execute("COMMIT")
            except Exception:
                if top:
                    self.connection.execute("ROLLBACK")
                self.record_cache.clear()
                raise
            finally:
                self._depth -= 1

    def close(self):
        self.record_cache.clear()
        self.connection.close()

    @staticmethod
    def _table(table):
        if table not in TABLES:
            raise ValueError("unknown_table")

    @staticmethod
    def _vector_column(table):
        return "vector" if table == "search_documents" else "NULL"

    @staticmethod
    def _decode(table, row):
        import struct
        value = json.loads(row[0])
        if table == "search_documents":
            value["vector"] = list(struct.unpack("<" + "f" * (len(row[1]) // 4), row[1])) if row[1] else None
        return value
