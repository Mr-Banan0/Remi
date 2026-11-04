"""Six explicit tables; startup opens an existing database and never creates a schema."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from threading import RLock, BoundedSemaphore
from time import monotonic

from .performance import measured, count
from .read_cache import derived
from .memory_projection import project_memory
from .models import Document, Edge, GraphObject, Memory, Source, SCHEMA_VERSION
from .search_index import RecordCache, DocumentHandle, encoded_terms, lexical_scores


SCHEMA = """
CREATE TABLE sources (owner TEXT NOT NULL, id TEXT NOT NULL, record_json TEXT NOT NULL, PRIMARY KEY(owner,id));
CREATE TABLE memories (owner TEXT NOT NULL, id TEXT NOT NULL, record_json TEXT NOT NULL,
                       search_projection TEXT, PRIMARY KEY(owner,id));
CREATE TRIGGER memory_projection_invalidated AFTER UPDATE OF record_json ON memories
BEGIN UPDATE memories SET search_projection=NULL WHERE owner=NEW.owner AND id=NEW.id; END;
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
        self._generation = 0
        self.lock = RLock()

    def change_token(self):
        with self.lock:
            return self._generation

    def get(self, table, owner, key):
        with self.lock:
            value = self.records[table].get((owner, key))
            return json.loads(json.dumps(value)) if value is not None else None

    def put(self, table, owner, key, value):
        with self.lock:
            self.records[table][owner, key] = json.loads(json.dumps(value))
            self._generation += 1

    @measured("storage.list", records=True)
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
                self._generation += 1
                self.records = {table: {tuple(key): value for key, value in values} for table, values in snapshot.items()}
                raise

    def delete(self, table, owner, key):
        with self.lock:
            self.records[table].pop((owner, key), None)
            self._generation += 1

    def close(self):
        pass

    def sources(self, owner, ids):
        return [Source.model_validate(value) for value in self.get_many("sources", owner, ids)]

    @measured("storage.get_many", records=True)
    def get_many(self, table, owner, ids):
        return [row for key in dict.fromkeys(ids)
                if (row := self.get(table, owner, key)) is not None]

    @measured("storage.source_headers", records=True)
    def source_headers(self, owner, ids=None):
        rows = self.list('sources', owner) if ids is None else self.get_many('sources', owner, ids)
        fields = ('id', 'role', 'session_id', 'message_at', 'created_at', 'ordinal')
        return [dict((field, row[field]) for field in fields) for row in rows]

    def context_before(self, owner, session_id, source, limit=1):
        from .clock import parse_time
        rows = [Source.model_validate(value) for value in self.list("sources", owner)
                if value["role"] in {"user", "assistant"} and value["session_id"] == session_id
                and (value["created_at"], value["ordinal"]) < (source.created_at, source.ordinal)
                and (not source.message_at or not value["message_at"] or parse_time(value["message_at"]) <= parse_time(source.message_at))]
        return sorted(rows, key=lambda row: (row.created_at, row.ordinal), reverse=True)[:limit]

    @measured("storage.memory_projections", records=True)
    def memory_projections(self, owner):
        return [project_memory(row) for row in self.list("memories", owner)]

    def memories(self, owner, ids=None):
        values = self.list("memories", owner) if ids is None else self.get_many("memories", owner, ids)
        return [Memory.model_validate(value) for value in values if value is not None]

    @measured("storage.search_documents", records=True)
    def search_documents(self, owner):
        return self.documents(owner)

    @measured("storage.lexical_scores", records=True)
    def lexical_scores(self, owner, query, documents):
        from .retrieval import bm25
        return bm25(query, documents)

    def documents(self, owner):
        return [Document.model_validate(value) for value in self.list("search_documents", owner)]

    @measured("storage.vector_scores", records=True)
    def vector_scores(self, owner, vector, document_ids):
        from .retrieval import cosine
        ids = set(document_ids)
        return {document.id: cosine(vector, document.vector) for document in self.documents(owner)
                if document.id in ids and document.vector is not None}

    @measured("storage.edges_for", records=True)
    def edges_for(self, owner, origins=None, targets=None, limit=None, relation=None):
        rows = [value for value in self.list("edges", owner)
                if "relation" in value
                and (origins is None or value["origin"] in origins)
                and (targets is None or value["target"] in targets)
                and (relation is None or value["relation"] == relation)]
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
        self.path = path
        self._read_slots = BoundedSemaphore(4)
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
        self._generation = 0
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
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript("BEGIN;" + SCHEMA + "COMMIT;")
        finally:
            connection.close()

    def _has_memory_projection(self):
        return "search_projection" in {row[1] for row in self.connection.execute("PRAGMA table_info(memories)")}

    def rebuild_memory_projection(self):
        """Explicit migration/backfill in the existing table; records remain unchanged."""
        with self.transaction():
            if not self._has_memory_projection():
                self.connection.execute("ALTER TABLE memories ADD COLUMN search_projection TEXT")
            self.connection.execute("CREATE TRIGGER IF NOT EXISTS memory_projection_invalidated "
                "AFTER UPDATE OF record_json ON memories BEGIN UPDATE memories SET search_projection=NULL "
                "WHERE owner=NEW.owner AND id=NEW.id; END")
            cursor = self.connection.execute("SELECT owner,id,record_json FROM memories ORDER BY owner,id")
            total = 0
            while batch := cursor.fetchmany(200):
                for owner, key, raw in batch:
                    self.connection.execute("UPDATE memories SET search_projection=? WHERE owner=? AND id=?",
                        (json.dumps(project_memory(json.loads(raw)), ensure_ascii=False), owner, key))
                    total += 1
        return total

    @measured("storage.memory_projections", records=True)
    def memory_projections(self, owner):
        with self.lock:
            self._check_external_changes()
            projected = self._has_memory_projection()
            if not projected:
                # Existing deployments use the same projection built from their original rows
                # until the explicit migration has been applied.
                return super().memory_projections(owner)
            rows = self.connection.execute("SELECT search_projection, "
                "CASE WHEN search_projection IS NULL THEN record_json END "
                "FROM memories WHERE owner=? ORDER BY id", (owner,)).fetchall()
        count("memory_projection_fallback_records", sum(raw is None for raw, _ in rows))
        count("memory_projection_bytes", sum(len((raw or original).encode()) for raw, original in rows))
        return [json.loads(raw) if raw else project_memory(json.loads(original)) for raw, original in rows]

    def enable_wal(self):
        """Explicit migration for existing deployments; the six tables stay intact."""
        with self.lock:
            mode = self.connection.execute('PRAGMA journal_mode=WAL').fetchone()[0]
            if mode != 'wal':
                raise RuntimeError('wal_migration_unavailable')

    @contextmanager
    def search_snapshot(self, deadline):
        with self.lock:
            wal = self.connection.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
        if not wal:
            # Existing rollback-journal deployments retain serialized reads until migration.
            yield None
            return
        started = monotonic()
        acquired = self._read_slots.acquire(timeout=max(0, deadline - started))
        count('read_snapshot_queue_seconds', monotonic() - started)
        if not acquired:
            raise TimeoutError('read_snapshot_capacity_timeout')
        reader = None
        try:
            reader = object.__new__(SQLiteStore)
            reader.lock = RLock()
            reader.path = self.path
            reader._depth = 0
            reader._generation = 0
            reader.record_cache = RecordCache()
            reader.connection = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True,
                check_same_thread=False, isolation_level=None, timeout=10)
            import sqlite_vec
            reader.connection.enable_load_extension(True)
            try:
                sqlite_vec.load(reader.connection)
            finally:
                reader.connection.enable_load_extension(False)
            reader.connection.execute('PRAGMA query_only=ON')
            reader.connection.execute('BEGIN')
            tables = {row[0] for row in reader.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            reader._indexed = {'search_lexical', 'search_lexical_vocab'} <= tables
            reader._data_version = reader.connection.execute('PRAGMA data_version').fetchone()[0]
            yield reader
        finally:
            if reader is not None and hasattr(reader, 'connection'):
                reader.close()
            self._read_slots.release()

    def change_token(self):
        with self.lock:
            self._check_external_changes()
            return self._data_version, self.connection.total_changes, self._generation

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

    @measured("storage.get_many", records=True)
    def get_many(self, table, owner, ids):
        self._table(table)
        keys = list(dict.fromkeys(ids))
        found = {}
        with self.lock:
            self._check_external_changes()
            missing = []
            for key in keys:
                row = self.record_cache.get((table, owner, key))
                if row is None:
                    missing.append(key)
                else:
                    found[key] = self._decode(table, row)
            for offset in range(0, len(missing), 400):
                batch = missing[offset:offset + 400]
                marks = ','.join('?' for _ in batch)
                for key, raw, vector in self.connection.execute(
                        f'SELECT id,record_json,{self._vector_column(table)} FROM {table} '
                        f'WHERE owner=? AND id IN ({marks})', [owner, *batch]):
                    row = (raw, vector)
                    self.record_cache.put((table, owner, key), row)
                    found[key] = self._decode(table, row)
        return [found[key] for key in keys if key in found]

    @measured("storage.source_headers", records=True)
    def source_headers(self, owner, ids=None):
        fields = ('id', 'role', 'session_id', 'message_at', 'created_at', 'ordinal')
        expressions = ','.join("json_extract(record_json,'$." + field + "')" for field in fields)
        keys = list(dict.fromkeys(ids)) if ids is not None else None
        rows = []
        with self.lock:
            batches = [None] if keys is None else [keys[i:i + 400] for i in range(0, len(keys), 400)]
            for batch in batches:
                clause = '' if batch is None else ' AND id IN (' + ','.join('?' for _ in batch) + ')'
                rows.extend(dict(zip(fields, row)) for row in self.connection.execute(
                    f'SELECT {expressions} FROM sources WHERE owner=?' + clause + ' ORDER BY id',
                    [owner, *(batch or [])]))
        if keys is not None:
            by_id = {row['id']: row for row in rows}
            return [by_id[key] for key in keys if key in by_id]
        return rows

    @measured("storage.search_documents", records=True)
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

    @measured("storage.lexical_scores", records=True)
    def lexical_scores(self, owner, query, documents):
        if not self._indexed:
            return super().lexical_scores(owner, query, documents)
        with self.lock:
            def build():
                lengths = {key: int(length) for key, length in self.connection.execute(
                    'SELECT document_id,token_count FROM search_lexical WHERE owner=?', (owner,))}
                count('lexical_stat_rows', len(lengths))
                # Account for dictionary slots, strings and integer payloads.
                import sys
                size = sys.getsizeof(lengths) + sum(sys.getsizeof(key) + sys.getsizeof(value)
                                                     for key, value in lengths.items())
                return lengths, size
            lengths = derived(('lexical_lengths', id(self), owner, self.change_token()), build)
            return lexical_scores(self.connection, owner, query, [document.id for document in documents], lengths=lengths)

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

    @measured("storage.list", records=True)
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
                self.connection.execute(f"INSERT INTO {table}(owner,id,record_json) VALUES(?,?,?) ON CONFLICT(owner,id) DO UPDATE SET record_json=excluded.record_json",
                                        (owner, key, json.dumps(record, ensure_ascii=False)))
                if table == "memories" and self._has_memory_projection():
                    self.connection.execute("UPDATE memories SET search_projection=? WHERE owner=? AND id=?",
                        (json.dumps(project_memory(record), ensure_ascii=False), owner, key))

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

    @measured("storage.edges_for", records=True)
    def edges_for(self, owner, origins=None, targets=None, limit=None, relation=None):
        clauses, args = ["owner=?"], [owner]
        for column, keys in (("origin", origins), ("target", targets)):
            if keys is not None:
                if not keys:
                    return []
                clauses.append(f"{column} IN ({','.join('?' for _ in keys)})")
                args.extend(keys)
        if relation is not None:
            clauses.append("relation=?")
            args.append(relation)
        with self.lock:
            suffix = " LIMIT ?" if limit is not None else ""
            if limit is not None:
                args.append(limit)
            # ORDER BY id otherwise encourages an owner-wide primary-key scan.
            # Endpoint indexes are part of the original explicit six-table schema.
            index = " INDEXED BY edge_origin" if origins is not None else " INDEXED BY edge_target" if targets is not None else ""
            rows = [json.loads(row[0]) for row in self.connection.execute(
                "SELECT record_json FROM edges" + index + " WHERE " + " AND ".join(clauses) + " ORDER BY id" + suffix, args)]
            return [Edge.model_validate(row) for row in rows if "relation" in row]

    @measured("storage.vector_scores", records=True)
    def vector_scores(self, owner, vector, document_ids):
        import sqlite_vec
        ids = list(dict.fromkeys(document_ids))
        scores = {}
        count("vector_candidate_ids", len(ids))
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
                self._generation += 1
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
