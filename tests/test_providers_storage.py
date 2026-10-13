from __future__ import annotations

import json
import os
from time import monotonic

import httpx
import pytest

from remi.clock import resolve_time
from remi.models import Command
from remi.providers import JevReranker
from remi.storage import MemoryStore, SQLiteStore, TABLES


def test_jev_native_contract_and_invalid_score_rejection():
    def handler(request):
        assert request.url.path == "/v1/systemone"
        body = json.loads(request.content)
        assert body["model"] == "jev-1.13.0"
        assert all(question["type"] == "noul" for question in body["questions"].values())
        return httpx.Response(200, json={"answers": {"a": {"type": "noul", "noul": .9}, "b": {"type": "noul", "noul": True}}})
    reranker = JevReranker("dummy", transport=httpx.MockTransport(handler))
    assert reranker.rank("Which device?", [{"id": "a", "original": "Dell"}, {"id": "b", "original": "generic"}],
                             deadline=monotonic() + 10).scores == {"a": .9}
    reranker.close()


@pytest.mark.parametrize("status", [400, 402, 429, 500])
def test_jev_failure_uses_existing_order_without_retry(status):
    reranker = JevReranker("dummy", transport=httpx.MockTransport(lambda request: httpx.Response(status)))
    assert reranker.rank("Q", [{"id": "a", "original": "text"}], deadline=monotonic() + 10).scores == {}
    assert len(reranker.calls) == 1
    reranker.close()


def test_static_schema_has_no_dynamic_source_or_memory_enums():
    schema = Command.model_json_schema()
    assert len(json.dumps(schema)) < 10000
    assert schema["additionalProperties"] is False
    assert schema["properties"]["target_id"]["anyOf"] == [{"type": "string"}, {"type": "null"}]


@pytest.mark.parametrize("expression,text,anchor,expected", [
    ("今天", "今天搬家", "2026-10-12T10:00:00Z", "2026-10-12"),
    ("yesterday", "yesterday I moved", "2026-10-12T10:00:00Z", "2026-10-11"),
    ("2026-09-24", "9月24日搬家", "2026-09-25T10:00:00Z", "2026-09-24"),
    ("2026-06-13", "Which device after the phone broke?", "2026-06-13T00:00:00Z", None),
    ("tomorrow", "tomorrow I move", None, None),
    ("2026-02-30", "2026-02-30", None, None),
])
def test_source_grounded_dates(expression, text, anchor, expected):
    actual = resolve_time(expression, text, anchor)
    assert (actual[:10] if actual else None) == expected


def test_missing_database_does_not_create_files(tmp_path):
    path = tmp_path / "new.sqlite3"
    with pytest.raises(RuntimeError):
        SQLiteStore(path)
    assert not path.exists()


def test_initialization_requires_explicit_approval(tmp_path):
    path = tmp_path / "new.sqlite3"
    with pytest.raises(PermissionError):
        SQLiteStore.initialize(path)
    assert not path.exists()


def test_memory_transaction_rolls_back_only_on_storage_failure():
    store = MemoryStore()
    store.put("requests", "u", "prior", {"value": 1})
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.put("requests", "u", "new", {"value": 2})
            raise RuntimeError("storage failure")
    assert store.get("requests", "u", "prior") == {"value": 1}
    assert store.get("requests", "u", "new") is None


def test_gpt_model_contract_and_bounded_transport_retry():
    from openai import OpenAI
    from remi.providers import GPTModel
    calls = []
    def handler(request):
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            return httpx.Response(429, json={"error": {"message": "temporary", "type": "rate_limit_error"}})
        return httpx.Response(200, json={"id": "test", "object": "chat.completion", "created": 1, "model": "gpt-4o-mini",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": '{"items":[]}'}, "finish_reason": "stop"}]})
    sdk = OpenAI(api_key="dummy-test", base_url="https://test.invalid/v1", max_retries=0,
                 http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    model = GPTModel("dummy-test", "https://test.invalid/v1", client=sdk)
    assert model.split({"new_user_source": {"content": "Original"}}, deadline=monotonic() + 5) == {"items": []}
    assert len(calls) == 2 and len(model.calls) == 2
    assert all(call["model"] == "gpt-4o-mini" for call in calls)
    assert calls[0]["response_format"]["json_schema"]["strict"] is True
    assert "dummy-test" not in repr(model.calls)
    model.close()


def test_gpt_configuration_error_has_one_attempt():
    from openai import OpenAI
    from remi.providers import GPTModel, ProviderUnavailable
    sdk = OpenAI(api_key="dummy-test", base_url="https://test.invalid/v1", max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(lambda request:
            httpx.Response(400, json={"error": {"message": "bad schema", "type": "invalid_request_error"}}))))
    model = GPTModel("dummy-test", "https://test.invalid/v1", client=sdk)
    with pytest.raises(ProviderUnavailable):
        model.split({}, deadline=monotonic() + 5)
    assert len(model.calls) == 1 and model.calls[0]["http_status"] == 400
    model.close()


@pytest.mark.skipif(os.getenv("REMI_TEST_SCHEMA_APPROVED") != "1", reason="Six-table approval required before physical database tests")
def test_sqlite_round_trip_checkpoint_vectors_and_exact_table_count(tmp_path):
    from .helpers import TestEngine as MemoryEngine
    from .helpers import HashEmbedder, ScriptedModel, query, request
    path = tmp_path / "memory.sqlite3"
    SQLiteStore.initialize(path, allow_create_schema=True)
    engine = MemoryEngine(SQLiteStore(path), ScriptedModel(), HashEmbedder())
    engine.add(request("Dell used for teaching."))
    expected = [document.vector for document in engine.store.documents("u1")]
    engine.close()
    second = MemoryEngine(SQLiteStore(path), ScriptedModel(), HashEmbedder())
    assert len(second.store.memories("u1")) == 1
    assert all(vector for vector in expected)
    assert "Dell" in second.search(query("Dell")).data[0].content
    actual = {row[0] for row in second.store.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert actual == TABLES
    second.close()
