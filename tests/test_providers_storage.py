from __future__ import annotations

import json
import os
from time import monotonic

import httpx
import pytest

from remi.clock import resolve_time
from remi.models import CardDecision, Decision
from remi.providers import DECISION_EXAMPLES, DECISION_PROMPT, JevReranker, strict_schema
from remi.storage import MemoryStore, SQLiteStore, TABLES


def test_jev_native_contract_and_invalid_score_rejection():
    def handler(request):
        assert request.url.path == "/v1/systemone"
        body = json.loads(request.content)
        assert body["model"] == "jev-1.13.0"
        assert all(question["type"] == "noul" for question in body["questions"].values())
        evidence = body['questions']['a']['instructions']['evidence']
        assert evidence['retrieval_objectives'] == [{'role': 'constraint', 'query': 'conditions limiting exercise'}]
        assert 'not lexical similarity' in body['questions']['a']['instructions']['question']
        return httpx.Response(200, json={"answers": {"a": {"type": "noul", "noul": .9}, "b": {"type": "noul", "noul": True}}})
    reranker = JevReranker("dummy", transport=httpx.MockTransport(handler))
    assert reranker.rank("Which device?", [{"id": "a", "original": "Dell", "retrieval_objectives": [
                                 {'role': 'constraint', 'query': 'conditions limiting exercise'}]},
                             {"id": "b", "original": "generic", "retrieval_objectives": []}],
                             deadline=monotonic() + 10).scores == {"a": .9}
    reranker.close()


@pytest.mark.parametrize("status,attempts", [(400,1),(402,1),(429,2),(500,2)])
def test_jev_failure_uses_one_bounded_retry_for_transient_errors(status,attempts):
    reranker = JevReranker("dummy", transport=httpx.MockTransport(lambda request: httpx.Response(status)))
    assert reranker.rank("Q", [{"id": "a", "original": "text"}], deadline=monotonic() + 10).scores == {}
    assert len(reranker.calls) == attempts
    assert [call['attempt'] for call in reranker.calls] == list(range(1,attempts+1))
    reranker.close()


def test_jev_transient_retry_can_recover_the_batch():
    attempts=[]
    def handler(request):
        attempts.append(True)
        if len(attempts)==1:return httpx.Response(500)
        return httpx.Response(200,json={"answers":{"a":{"type":"noul","noul":.8}}})
    reranker=JevReranker("dummy",transport=httpx.MockTransport(handler))
    assert reranker.rank("Q",[{"id":"a","original":"text"}],deadline=monotonic()+10).scores=={"a":.8}
    assert len(reranker.calls)==2
    reranker.close()


def test_static_schema_has_no_dynamic_source_or_memory_enums():
    schema = CardDecision.model_json_schema()
    assert len(json.dumps(schema)) < 10000
    assert schema["additionalProperties"] is False
    assert schema["properties"]["target_id"]["anyOf"] == [{"type": "string"}, {"type": "null"}]


def test_search_planner_contract_is_compact_explicit_and_covered_by_examples():
    schema = strict_schema(Decision)
    search = schema['$defs']['PlannedSearch']
    assert search['required'] == list(search['properties'])
    assert set(search['properties']) == {
        'role', 'query', 'time_expression', 'anchor_source_id', 'anchor_step', 'relation'}
    assert '"searches"' in DECISION_PROMPT and 'verify_premise: include premise_check' in DECISION_PROMPT
    assert 'action: include constraint and latest_state' in DECISION_PROMPT
    roles = [{item['role'] for item in output['searches']} for _, output in DECISION_EXAMPLES]
    assert roles == [{'premise_check'}, {'constraint', 'latest_state'}, {'temporal_anchor', 'answer_detail'}]


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
    assert model.propose({"new_user_source": {"id":"src_test", "role":"user", "content": "Original"}}, deadline=monotonic() + 5) == {"items": []}
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
        model.propose({}, deadline=monotonic() + 5)
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


def test_add_uses_short_references_without_rewriting_source_words():
    from remi.providers import semantic_references
    payload={'owner':'opaque-owner','new_user_source':{'id':'src_opaque','role':'user','content':'I said src_opaque and M1 literally.'},
        'history':[{'id':'mem_opaque','sources':[]}]}
    sent,restore=semantic_references(payload)
    assert sent['owner']=='U1' and sent['new_user_source']['id']=='S1' and sent['history'][0]['id']=='M1'
    assert sent['new_user_source']['content']==payload['new_user_source']['content']
    returned=restore({'decisions':[{'candidate_id':'S1','target_id':'M1'}],
        'items':[{'id':'M1','content':'Literal M1 remains source wording.','mentions':[],'evidence':[{'source_id':'S1'}]}]})
    assert returned['decisions'][0]=={'candidate_id':'S1','target_id':'mem_opaque'}
    assert returned['items'][0]['id']=='M1' and returned['items'][0]['evidence'][0]['source_id']=='src_opaque'
    assert returned['items'][0]['content']=='Literal M1 remains source wording.'


def test_short_refs_preserve_candidate_namespace_collisions():
    from remi.providers import semantic_references
    payload={'new_user_source':{'id':'src_actual','role':'user','content':'Original'},
        'proposal':{'items':[{'id':'M1','content':'A claim','mentions':[],'evidence':[{'source_id':'src_actual'}]}]},
        'history':[{'id':'mem_actual','sources':[]}]}
    sent,restore=semantic_references(payload)
    assert sent['proposal']['items'][0]['id']=='M1' and sent['history'][0]['id']=='M2'
    assert restore({'decisions':[{'target_id':'M2','candidate_id':'M1'}]})['decisions'][0] == {'target_id':'mem_actual','candidate_id':'M1'}


def test_extraction_provider_exposes_fixed_refs_and_original_sources():
    from openai import OpenAI
    from remi.providers import GPTModel
    calls=[]
    def handler(request):
        calls.append(json.loads(request.content))
        content=json.dumps({'items':[],'reason':'empty'})
        return httpx.Response(200,json={'id':'test','object':'chat.completion','created':1,'model':'gpt-4o-mini',
            'choices':[{'index':0,'message':{'role':'assistant','content':content},'finish_reason':'stop'}]})
    sdk=OpenAI(api_key='dummy-test',base_url='https://test.invalid/v1',max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    model=GPTModel('dummy-test','https://test.invalid/v1',client=sdk)
    state={'new_user_source':{'id':'src_actual','role':'user','content':'Original','message_at':'2024-06-02T10:00:00Z'},
        'resolved_subjects':[{'ref':3,'status':'resolved'},{'ref':8,'status':'pending'}],
        'history':[{'id':'mem_actual','sources':[]}]}
    model.propose(state,deadline=monotonic()+5)
    body=json.loads(calls[0]['messages'][-1]['content'])
    props=calls[0]['response_format']['json_schema']['schema']['$defs']['CandidateDraft']['properties']
    assert body['history'][0]['id']=='M1' and props['subject_refs']['items']['enum']==[3,8]
    assert 'operation' not in props and 'target_id' not in props
    assert not hasattr(model,'maintain')
    model.close()


def test_identity_and_extraction_contracts_have_separate_write_authority():
    from remi.models import SubjectMaintenanceDraft, ProposalDraft
    from remi.providers import strict_schema
    identity=strict_schema(SubjectMaintenanceDraft);memory=strict_schema(ProposalDraft)
    assert set(identity['properties'])=={'subjects','connections'}
    assert set(memory['properties'])=={'items','reason'}
    assert 'SubjectDraft' in identity['$defs'] and 'SubjectDraft' not in memory['$defs']
