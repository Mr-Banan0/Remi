from fastapi.testclient import TestClient

from embergraph.api import create_app
from .helpers import TestEngine as MemoryEngine
from .helpers import ScriptedModel, request


def test_contract_health_auth_add_search():
    app = create_app(MemoryEngine(model=ScriptedModel()), api_key="test-secret")
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
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
