import json

import pytest

from fastapi.testclient import TestClient

from remi.api import create_app
from remi.models import AddResult, SubjectClarification
from remi import __version__
from remi.version import PIPELINE
from remi.settings import Settings
from .helpers import TestEngine as MemoryEngine
from .helpers import ScriptedModel, request


def test_contract_health_auth_add_search():
    app = create_app(MemoryEngine(model=ScriptedModel()), api_key="test-secret")
    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert app.title == "Remi" and app.version == __version__ == "0.2.0"
        assert health.json() == {
            "status": "ok", "service": "remi", "version": "0.2.0",
            "pipeline": PIPELINE, "initialized": True}
        assert client.post("/add", json=request("Dell event").model_dump()).status_code == 401
        headers = {"authorization": "Bearer test-secret"}
        result = client.post("/add", json=request("Dell event").model_dump(), headers=headers)
        assert result.status_code == 200
        assert result.json()["success"] is True
        response = client.post("/search", json={"user_id": "u1", "query": "Dell", "top_k": 10}, headers=headers)
        assert response.status_code == 200 and response.json()["data"]
        assert set(response.json()["data"][0]) == {"id", "content", "score", "created_at"}
        assert client.post("/search", json={"user_id": "u1", "query": "Dell", "top_k": 10},
            headers={**headers, "x-memory-reference-time": "not-a-time"}).status_code == 422


def test_bad_request_and_duplicate_conflict():
    with TestClient(create_app(MemoryEngine())) as client:
        assert client.post("/add", json={}).status_code == 422
        assert client.post("/add", json=request("A").model_dump()).status_code == 200
        assert client.post("/add", json=request("B").model_dump()).status_code == 409


def test_existing_runner_clock_header_is_supported():
    with TestClient(create_app(MemoryEngine())) as client:
        client.post("/add", json=request("Dell").model_dump())
        response = client.post("/search", json={"user_id": "u1", "query": "Dell", "top_k": 1},
                              headers={"x-traceweave-reference-time": "2024-01-01T00:00:00Z"})
        assert response.status_code == 200 and response.json()["data"] == []



def test_official_search_top_k_100_preserves_requested_result_limit():
    engine = MemoryEngine(settings=Settings(candidate_limit=120))
    with TestClient(create_app(engine, api_key="")) as client:
        for index in range(105):
            payload = request(f"SearchContractMarker event {index}", request_id=f"contract-{index}").model_dump()
            assert client.post("/add", json=payload).status_code == 200
        for top_k in (1, 50, 100):
            response = client.post("/search", json={
                "user_id": "u1", "query": "SearchContractMarker", "top_k": top_k})
            assert response.status_code == 200
            data = response.json()["data"]
            assert len(data) == top_k
            assert len({item["id"] for item in data}) == top_k
            assert all("SearchContractMarker" in item["content"] for item in data)


def test_long_search_query_reaches_retrieval_intact_and_finds_suffix_evidence():
    class RecordingEmbedder:
        def __init__(self):
            self.inputs = []

        def embed(self, texts, *, deadline):
            self.inputs.extend(texts)
            return [[1., 0.] for _ in texts]

    embedder = RecordingEmbedder()
    model = ScriptedModel()
    engine = MemoryEngine(model=model, embedder=embedder)
    long_query = "背景资料。\n" * 10000 + "请检索 SuffixEvidenceMarker 对应的历史资料。"
    assert long_query.index("SuffixEvidenceMarker") > 20000
    with TestClient(create_app(engine, api_key="")) as client:
        payload = request("SuffixEvidenceMarker identifies the retained project evidence.").model_dump()
        assert client.post("/add", json=payload).status_code == 200
        response = client.post("/search", json={
            "user_id": "u1", "query": long_query, "top_k": 100,
            "options": ["A. retained project evidence", "B. unrelated evidence"]})
        assert response.status_code == 200
        assert embedder.inputs[-1] == long_query
        assert any("SuffixEvidenceMarker" in item["content"] for item in response.json()["data"])
        assert len(json.dumps(model.decisions[-1], ensure_ascii=False).encode()) <= engine.settings.decision_bytes


@pytest.mark.parametrize("overrides", [{"top_k": 0}, {"top_k": 101}, {"query": ""}])
def test_search_rejects_out_of_contract_limits_and_empty_query(overrides):
    with TestClient(create_app(MemoryEngine(), api_key="")) as client:
        payload = {"user_id": "u1", "query": "SearchContractMarker", "top_k": 10, **overrides}
        assert client.post("/search", json=payload).status_code == 422


def test_aml_add_response_filters_internal_subject_clarifications():
    class ClarifyingEngine:
        def add(self,value):
            return AddResult(request_id=value.request_id,user_id=value.user_id,session_id=value.session_id,
                clarifications=[SubjectClarification(mention='Hamid',message='Please confirm the intended subject.',
                    candidates=[dict(subject_id='hamid-a',display='Hamid — neighbor')])])
        def close(self):pass
    with TestClient(create_app(ClarifyingEngine(),api_key='')) as client:
        response=client.post('/add',json=request('Hamid sent a note.').model_dump())
    assert response.status_code==200
    assert response.json()=={'success':True,'request_id':'r1','user_id':'u1','session_id':'s1'}
