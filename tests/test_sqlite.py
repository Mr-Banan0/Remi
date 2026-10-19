from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from remi.api import create_app
from .helpers import TestEngine as MemoryEngine
from remi.heat import hot_view
from remi.providers import ProviderUnavailable
from remi.storage import SQLiteStore, TABLES
from .helpers import HashEmbedder, ScriptedModel, command, query, request


pytestmark = pytest.mark.skipif(os.getenv("REMI_TEST_SCHEMA_APPROVED") != "1",
                               reason="Enable the approved six-table database test scope")


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "remi.sqlite3"
    SQLiteStore.initialize(path, allow_create_schema=True)
    return path


def test_checkpoint_resumes_after_process_resources_are_reopened(database):
    class FailingIndexEmbedder(HashEmbedder):
        def embed(self, texts, *, deadline):
            if any("user:" in text for text in texts):
                raise ProviderUnavailable("offline simulated index failure")
            return super().embed(texts, deadline=deadline)

    model = ScriptedModel()
    first = MemoryEngine(SQLiteStore(database), model, FailingIndexEmbedder())
    payload = request("Dell supported remote teaching.")
    with pytest.raises(ProviderUnavailable):
        first.add(payload)
    checkpoint = first.store.get("requests", "u1", "r1")
    assert checkpoint["status"] == "pending"
    assert checkpoint["units"]["0"]["phase"] == "pending"
    assert first.store.memories("u1") == []
    first.close()

    resumed_model = ScriptedModel()
    second = MemoryEngine(SQLiteStore(database), resumed_model, HashEmbedder())
    try:
        assert second.add(payload).success
        assert resumed_model.extractions == []
        memories = second.store.memories("u1")
        assert len(memories) == 1
        assert len(memories[0].revisions) == 1
        assert second.store.get("requests", "u1", "r1")["status"] == "complete"
        assert all(document.vector is not None for document in second.store.documents("u1"))
        assert "Dell" in second.search(query("Dell")).data[0].content
    finally:
        second.close()


def test_transaction_failure_keeps_preexisting_records(database):
    store = SQLiteStore(database)
    try:
        store.put("requests", "u1", "previous", {"state": "retained"})
        with pytest.raises(RuntimeError):
            with store.transaction():
                store.put("requests", "u1", "partial", {"state": "partial"})
                raise RuntimeError("offline simulated transaction failure")
        assert store.get("requests", "u1", "previous") == {"state": "retained"}
        assert store.get("requests", "u1", "partial") is None
    finally:
        store.close()


def test_prior_user_and_assistant_context_survives_reopening(database):
    first = MemoryEngine(SQLiteStore(database), ScriptedModel(lambda state, errors: {"commands": []}))
    first.add(request("Help with Northstar.", assistant="Tuesday is the first slot; Thursday is the second."))
    first.close()

    def extract(state, errors):
        assistant = next(source for source in state["context_sources"] if source["role"] == "assistant")
        assert any(source["role"] == "user" and "Northstar" in source["content"] for source in state["context_sources"])
        draft = command(state, "User selected Thursday for Northstar", status="planned",
                        objects=[{"kind": "project", "name": "Northstar"}])
        draft["context_source_ids"] = [source["id"] for source in state["context_sources"]]
        return {"commands": [draft]}

    second = MemoryEngine(SQLiteStore(database), ScriptedModel(extract))
    try:
        second.add(request("Choose the second slot.", request_id="r2", when="2024-06-02T11:00:00Z"))
        memory = second.store.memories("u1")[0]
        assert len(memory.current.source_ids) == 3
        assert memory.current.status == "planned"
        assert second.store.edges_for("u1", origins={memory.id})[0].relation == "involves"
    finally:
        second.close()


def test_revisions_and_retraction_survive_reopening(database):
    model = ScriptedModel(lambda state, errors: {"commands": [command(state, kind="preference")]})
    engine = MemoryEngine(SQLiteStore(database), model, HashEmbedder())
    engine.add(request("I prefer watermelon."))
    memory_id = engine.store.memories("u1")[0].id
    model.extractor = lambda state, errors: {"commands": [command(state, kind="preference", operation="retract", target=memory_id)]}
    engine.add(request("I withdraw the watermelon preference.", request_id="r2", when="2024-06-03T10:00:00Z"))
    engine.close()

    store = SQLiteStore(database)
    try:
        memory = store.memories("u1", [memory_id])[0]
        assert len(memory.revisions) == 2
        assert memory.current.status == "retracted"
        assert hot_view([memory], "2024-06-02T12:00:00Z")
        assert hot_view([memory], "2024-06-04T12:00:00Z") == []
        assert any(edge.relation == "revises" for edge in store.edges_for("u1"))
    finally:
        store.close()


def test_vector_and_graph_queries_respect_owner(database):
    engine = MemoryEngine(SQLiteStore(database), ScriptedModel(), HashEmbedder())
    try:
        engine.add(request("Private Dell evidence."))
        documents = engine.store.documents("u1")
        vector = documents[0].vector
        own_scores = engine.store.vector_scores("u1", vector, [document.id for document in documents])
        assert own_scores and max(own_scores.values()) == pytest.approx(1)
        assert engine.store.vector_scores("u2", vector, [document.id for document in documents]) == {}
        assert engine.store.edges_for("u2") == []
        assert engine.search(query("Dell", owner="u2")).data == []
    finally:
        engine.close()


def test_existing_database_initialization_preserves_records(database):
    store = SQLiteStore(database)
    store.put("requests", "u1", "existing", {"status": "retained"})
    store.close()
    with pytest.raises(FileExistsError):
        SQLiteStore.initialize(database, allow_create_schema=True)
    reopened = SQLiteStore(database)
    try:
        assert reopened.get("requests", "u1", "existing") == {"status": "retained"}
        tables = {row[0] for row in reopened.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert tables == TABLES
    finally:
        reopened.close()


def test_sqlite_backed_http_contract(database):
    engine = MemoryEngine(SQLiteStore(database), ScriptedModel(), HashEmbedder())
    with TestClient(create_app(engine, api_key="offline-secret")) as client:
        headers = {"authorization": "Bearer offline-secret"}
        assert client.get("/ready", headers=headers).status_code == 200
        payload = request("Dell supported Northstar.").model_dump()
        assert client.post("/add", json=payload, headers=headers).status_code == 200
        assert client.post("/add", json=payload, headers=headers).status_code == 200
        result = client.post("/search", json=query("Dell").model_dump(), headers=headers)
        assert result.status_code == 200 and "Dell" in result.json()["data"][0]["content"]
        assert len(engine.store.memories("u1")) == 1


def test_cold_record_remains_vector_searchable_after_reopening(database):
    engine = MemoryEngine(SQLiteStore(database), ScriptedModel(), HashEmbedder())
    engine.add(request("In 2010 the ThinkPad T60 supported teaching.", when="2010-06-02T10:00:00Z"))
    engine.close()
    second = MemoryEngine(SQLiteStore(database), ScriptedModel(), HashEmbedder())
    try:
        assert hot_view(second.store.memories("u1"), "2026-10-12T00:00:00Z") == []
        assert "ThinkPad" in second.search(query("ThinkPad T60"), "2026-10-12T00:00:00Z").data[0].content
    finally:
        second.close()
