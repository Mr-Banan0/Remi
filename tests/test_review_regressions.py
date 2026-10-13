"""Cross-stage invariants found by the G01 structure review. Entirely offline."""
import json
import sqlite3
from time import monotonic

import httpx
import pytest
from fastapi.testclient import TestClient

from embergraph.api import create_app
from embergraph.clock import absolute_content, message_time, resolve_time
from .helpers import TestEngine as MemoryEngine
from embergraph.heat import hot_view
from embergraph.models import Decision, RecallStep, SplitPlan, WrittenPlan
from embergraph.providers import JevReranker, OutputInvalid, RankResult, strict_schema
from embergraph.settings import Settings
from embergraph.storage import MemoryStore, SQLiteStore
from tests.helpers import ScriptedModel, command, query, request


def test_hot_budget_is_allocated_to_eligible_conditions():
    model = ScriptedModel(lambda state, errors: {"commands": [command(state,
        kind="fact" if "budget" in state["new_user_source"]["content"] else "event",
        status="stated" if "budget" in state["new_user_source"]["content"] else "planned")]})
    engine = MemoryEngine(model=model, settings=Settings(hot_limit=1))
    engine.add(request("The budget limit is 10000."))
    engine.add(request("A meeting is planned.", request_id="r2"))
    engine.search(query("Prepare an expenditure proposal"))
    assert [item["kind"] for item in model.decisions[-1]["current_conditions"]] == ["fact"]


@pytest.mark.parametrize("answer", ["Dell XPS 13", "备用戴尔电脑"])
def test_long_single_line_returns_the_actual_hit_window(answer):
    engine = MemoryEngine()
    engine.add(request("filler " * 1800 + "The device was " + answer))
    result = engine.search(query(answer, top_k=1))
    assert answer in result.data[0].content


def test_turn_merge_preserves_assistant_and_memory_metadata():
    engine = MemoryEngine(model=ScriptedModel())
    engine.add(request("I asked for jogging advice.", assistant="The recommendation was BICYCLE. " + "general detail " * 250))
    result = engine.search(query("jogging advice", top_k=1))
    assert "BICYCLE" in result.data[0].content
    documents = engine._visible_documents("u1", "2025-01-01T00:00:00Z")
    merged = engine._merge(engine._hybrid("u1", "jogging advice", documents), documents)
    assert len(merged) == 1 and len(merged[0][0].source_ids) == 2
    assert merged[0][0].memory_evidence


class TemporalRanker:
    def __init__(self, partial=False):
        self.partial = partial
    def rank(self, question, cards, *, deadline):
        scores = {card["id"]: .99 if card["message_at"].startswith("2024-06-15") else .1 for card in cards}
        matches = {target["id"]: .98 if "email" in target["original"].casefold() else .01
                   for card in cards for target in card.get("temporal_targets", [])}
        if self.partial and matches:
            matches.pop(next(iter(matches)))
        return RankResult(scores, matches)


def temporal_model(select="first"):
    return ScriptedModel(decide=lambda state: {"steps": [{"query": "email Alicia", "time_expression": "2024-06-06",
        "anchor_source_id": None, "time_basis": "message", "relation": "after", "select": select,
        "window_days": 0, "anchor_step": None}], "relevant_memory_ids": []})


def test_first_survives_final_rerank_and_excludes_different_activity():
    engine = MemoryEngine(model=temporal_model(), reranker=TemporalRanker())
    for day, text in (("07", "Breakfast with Alicia"), ("09", "Email Alicia"), ("15", "Email Alicia")):
        engine.add(request(text, request_id=day, when=f"2024-06-{day}T10:00:00Z"))
    result = engine.search(query("First email Alicia after 2024-06-06?", top_k=1), "2024-07-01T00:00:00Z")
    assert "2024-06-09" in result.data[0].content and "Breakfast" not in result.data[0].content
    assert engine.last_search["tool_results"][0]["selected_time"].startswith("2024-06-09")


def test_partial_activity_check_reports_uncertainty_and_preserves_base():
    engine = MemoryEngine(model=temporal_model(), reranker=TemporalRanker(partial=True))
    for day in ("09", "15"):
        engine.add(request("Email Alicia", request_id=day, when=f"2024-06-{day}T10:00:00Z"))
    assert engine.search(query("First email after 2024-06-06?"), "2024-07-01T00:00:00Z").data
    assert "activity_unverified" in engine.last_search["tool_results"][0]["coverage"]
    assert not engine.last_search["temporal_selected_ids"]


def test_second_step_binds_the_new_first_step_time():
    model = ScriptedModel(lambda state, errors: {"commands": []})
    engine = MemoryEngine(model=model)
    engine.add(request("Anchor communication alpha", when="2024-06-06T10:00:00Z"))
    engine.add(request("Followup message beta", request_id="r2", when="2024-06-09T10:00:00Z"))
    model.decider = lambda state: {"steps": [
        {"query": "communication alpha", "time_expression": None, "anchor_source_id": None,
         "time_basis": "message", "relation": "independent", "select": "all", "window_days": 0, "anchor_step": None},
        {"query": "message beta", "time_expression": None, "anchor_source_id": None,
         "time_basis": "message", "relation": "after", "select": "first", "window_days": 0, "anchor_step": 0}],
        "relevant_memory_ids": []}
    result = engine.search(query("What happened afterwards?"), "2024-07-01T00:00:00Z")
    assert not model.decisions[-1]["evidence"]
    assert "Followup" in "\n".join(item.content for item in result.data)
    assert all(item["coverage"].startswith("bounded_candidates") for item in engine.last_search["tool_results"])


def test_ambiguous_dependency_keeps_a_diagnosed_fallback():
    engine = MemoryEngine()
    for day in ("06", "07"):
        engine.add(request("Anchor activity", request_id=day, when=f"2024-06-{day}T10:00:00Z"))
    documents = engine.store.documents("u1")
    step = RecallStep(query="Followup", time_expression=None, anchor_source_id=None,
        time_basis="message", relation="after", select="first", window_days=0, anchor_step=0)
    hits, coverage = engine._recall_step("u1", "Followup", step, set(), documents, "2024-07-01T00:00:00Z",
                                       float("inf"), [engine._hybrid("u1", "Anchor", documents)])
    assert hits == [] and coverage == "anchor_step_ambiguous"


def test_future_graph_update_preserves_past_expansion():
    model = ScriptedModel(lambda state, errors: {"commands": [command(state, objects=[{"kind": "project", "name": "Northstar"}])]})
    engine = MemoryEngine(model=model)
    engine.add(request("Northstar kickoff"))
    target = engine.store.memories("u1")[0]
    engine.add(request("Northstar budget", request_id="r2", when="2024-06-03T10:00:00Z"))
    clock = "2024-06-04T00:00:00Z"
    documents = engine._visible_documents("u1", clock)
    seed = next(document for document in documents if document.memory_id is None and document.source_ids[0] == target.current.source_ids[0])
    before = engine._graph_candidates("u1", "Northstar", [seed], documents, None)
    model.extractor = lambda state, errors: {"commands": [command(state, operation="update", target=target.id,
        objects=[{"kind": "project", "name": "Northstar"}])]}
    engine.add(request("Northstar kickoff completed", request_id="r3", when="2025-01-01T10:00:00Z"))
    after = engine._graph_candidates("u1", "Northstar", [seed], engine._visible_documents("u1", clock), None)
    assert {item.id for item, _ in before} == {item.id for item, _ in after}


def test_same_source_distinct_time_updates_have_distinct_revisions():
    model = ScriptedModel(lambda state, errors: {"commands": [command(state, "Northstar meeting", status="planned")]})
    engine = MemoryEngine(model=model)
    engine.add(request("Northstar meeting is planned."))
    target = engine.store.memories("u1")[0].id
    model.extractor = lambda state, errors: {"commands": [command(state, "Northstar meeting", operation="update", target=target,
        quote=phrase, time=phrase, status="planned") for phrase in ("6月9日", "6月15日")]}
    payload = request("Northstar meeting 6月9日，再改到6月15日。", request_id="r2", when="2024-06-03T10:00:00Z")
    engine.add(payload)
    engine.add(payload)
    memory = engine.store.memories("u1", [target])[0]
    assert len(memory.revisions) == 3 and memory.current.event_at.startswith("2024-06-15")


def test_update_retains_unspecified_event_date():
    model = ScriptedModel(lambda state, errors: {"commands": [command(state, time="6月9日", status="planned")]})
    engine = MemoryEngine(model=model)
    engine.add(request("Northstar meeting 6月9日。"))
    before = engine.store.memories("u1")[0]
    model.extractor = lambda state, errors: {"commands": [command(state, operation="update", target=before.id, status="planned")]}
    engine.add(request("Northstar meeting requires slides.", request_id="r2"))
    assert engine.store.memories("u1")[0].current.event_at == before.current.event_at


def test_confirmation_retains_validity_end_and_does_not_reopen_expiry():
    def initial(state, errors):
        draft = command(state, kind="fact", time="2024-06-02")
        draft["end_expression"] = "2024-06-09"
        return {"commands": [draft]}
    model = ScriptedModel(initial)
    engine = MemoryEngine(model=model)
    engine.add(request("Restriction applies 2024-06-02 until 2024-06-09."))
    before = engine.store.memories("u1")[0]
    model.extractor = lambda state, errors: {"commands": [command(state, kind="fact", operation="update", target=before.id)]}
    engine.add(request("The restriction still applies.", request_id="r2", when="2024-06-03T10:00:00Z"))
    after = engine.store.memories("u1")[0]
    assert (after.current.valid_from, after.current.valid_to) == (before.current.valid_from, before.current.valid_to)
    assert not hot_view([after], "2024-07-01T00:00:00Z")


def test_lifecycle_change_does_not_inherit_a_plan_as_actual_date():
    model = ScriptedModel(lambda state, errors: {"commands": [command(state, time="6月9日", status="planned")]})
    engine = MemoryEngine(model=model)
    engine.add(request("Northstar meeting 6月9日。"))
    target = engine.store.memories("u1")[0].id
    model.extractor = lambda state, errors: {"commands": [command(state, operation="update", target=target, status="occurred")]}
    engine.add(request("That meeting completed.", request_id="r2", when="2024-06-15T10:00:00Z"))
    assert engine.store.memories("u1")[0].current.event_at is None


def test_repair_scope_is_enforced_and_valid_initial_command_survives():
    def extract(state, errors):
        a = command(state, "Project A completed", quote="Project A completed")
        b = command(state, "Project B planned", quote="Project B planned", status="planned")
        if errors:
            return {"replacements": [{"position": 0, "command": {**a, "content": "Project A failed"}},
                                     {"position": 1, "command": b}]}
        return {"commands": [a, {**b, "source_id": "missing"}]}
    engine = MemoryEngine(model=ScriptedModel(extract))
    engine.add(request("Project A completed; Project B planned."))
    assert {item.current.content for item in engine.store.memories("u1")} == {"Project A completed", "Project B planned"}
    assert len(engine.model.extractions) == 2


def test_malformed_repair_keeps_original_time_failure_for_local_degradation():
    def extract(state, errors):
        if errors:
            raise OutputInvalid("simulated malformed repair")
        return {"commands": [command(state, time="yesterday")]}
    engine = MemoryEngine(model=ScriptedModel(extract))
    assert engine.add(request("The move completed yesterday.", when=None)).success
    assert len(engine.store.memories("u1")) == 1
    assert engine.store.memories("u1")[0].current.event_at is None


def test_equivalent_day_does_not_accept_an_invented_clock():
    assert resolve_time("2024-06-02T23:59:59+00:00", "6月2日完成任务", "2024-06-02T10:00:00Z") == "2024-06-02T00:00:00+00:00"


def test_invalid_timestamp_returns_422_and_creates_no_source():
    engine = MemoryEngine()
    payload = request("An ordinary message.").model_dump()
    payload["messages"][0]["timestamp"] = 10 ** 30
    with TestClient(create_app(engine), raise_server_exceptions=False) as client:
        assert client.post("/add", json=payload).status_code == 422
        assert not engine.store.list("sources", "u1")
    assert message_time(253402300799999).startswith("9999-12-31")


def test_storage_busy_returns_retryable_response_without_claiming_saved_checkpoint():
    class FailingStore(MemoryStore):
        def put(self, table, owner, key, value):
            if table == "requests":
                raise sqlite3.OperationalError("database is locked")
            return super().put(table, owner, key, value)
    engine = MemoryEngine(FailingStore())
    with TestClient(create_app(engine), raise_server_exceptions=False) as client:
        response = client.post("/add", json=request("An ordinary message.").model_dump())
        assert response.status_code == 503 and "retained checkpoint" not in response.text
        assert not engine.store.list("sources", "u1")


def test_initial_state_also_obeys_the_decision_budget():
    model = ScriptedModel(lambda state, errors: {"commands": []})
    engine = MemoryEngine(model=model, settings=Settings(decision_bytes=4000, context_bytes=512))
    engine.search(query("x" * 12000))
    assert engine.last_search["decision_bytes"] <= 4000


def test_planned_fact_is_disposed_and_cannot_become_a_current_condition():
    model = ScriptedModel(lambda state, errors: {"commands": [command(state, "I reside in Shanghai.", kind="fact",
        time="tomorrow", status="planned")]})
    engine = MemoryEngine(model=model)
    engine.add(request("I plan to live in Shanghai tomorrow."))
    engine.search(query("Where do I currently live?"), "2024-06-04T00:00:00Z")
    assert not engine.store.memories("u1")
    assert not model.decisions[-1]["current_conditions"]
    assert engine.store.list("sources", "u1")


def test_all_model_schema_properties_are_required_without_dynamic_enums():
    for model in (Decision, SplitPlan, WrittenPlan):
        schema = strict_schema(model)
        assert len(json.dumps(schema)) < 10000
        for item in [schema, *schema.get("$defs", {}).values()]:
            if item.get("type") == "object":
                assert set(item["required"]) == set(item["properties"])


def test_jev_activity_match_shares_the_existing_batch_call():
    calls = []
    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        return httpx.Response(200, json={"answers": {key: {"type": "noul", "noul": .97}
                                                    for key in body["questions"]}})
    ranker = JevReranker("dummy", transport=httpx.MockTransport(handler))
    result = ranker.rank("First email?", [{"id": "card", "original": "Email Alicia", "temporal_targets": [
        {"id": "target", "query": "email Alicia", "original": "Email Alicia"}]}], deadline=monotonic() + 5)
    assert len(calls) == 1 and result.scores == {"card": .97} and result.temporal_scores == {"target": .97}
    ranker.close()


def test_one_invalid_repair_replacement_preserves_another_valid_replacement():
    def extract(state, errors):
        a = command(state, "Project A completed", quote="Project A completed")
        b = command(state, "Project B planned", quote="Project B planned", status="planned")
        if errors:
            return {"replacements": [{"position": 0, "command": {**a, "extra": "invalid"}},
                                     {"position": 1, "command": b}]}
        return {"commands": [{**a, "source_id": "missing"}, {**b, "source_id": "missing"}]}
    engine = MemoryEngine(model=ScriptedModel(extract))
    engine.add(request("Project A completed; Project B planned."))
    assert [memory.current.content for memory in engine.store.memories("u1")] == ["Project B planned"]


@pytest.mark.skipif(__import__("os").getenv("EMBERGRAPH_TEST_SCHEMA_APPROVED") != "1", reason="Six-table approval required")
def test_sqlite_restart_retains_date_and_original_hit_windows(tmp_path):
    path = tmp_path / "regression.sqlite3"
    SQLiteStore.initialize(path, allow_create_schema=True)
    model = ScriptedModel(lambda state, errors: {"commands": [command(state, time="6月9日", status="planned")]})
    engine = MemoryEngine(SQLiteStore(path), model=model)
    engine.add(request("Northstar meeting 6月9日。"))
    before = engine.store.memories("u1")[0]
    model.extractor = lambda state, errors: {"commands": [command(state, operation="update", target=before.id, status="planned")]}
    engine.add(request("Northstar meeting requires slides.", request_id="r2"))
    model.extractor = lambda state, errors: {"commands": []}
    engine.add(request("filler " * 1800 + "The device was Dell XPS 13.", request_id="r3"))
    engine.close()
    reopened = MemoryEngine(SQLiteStore(path))
    assert reopened.store.memories("u1")[0].current.event_at == before.current.event_at
    assert "Dell" in reopened.search(query("Dell XPS 13", top_k=1)).data[0].content
    assert len(reopened.store.connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()) == 6
    reopened.close()


def test_one_bad_tool_step_preserves_independent_step_and_condition_selection():
    raw = {"steps": [
        {"query": "invalid", "time_expression": "2024-06-02", "anchor_source_id": None,
         "anchor_step": 0, "time_basis": "message", "relation": "after", "select": "first", "window_days": 0},
        {"query": "valid", "time_expression": None, "anchor_source_id": None,
         "anchor_step": None, "time_basis": "message", "relation": "independent", "select": "all", "window_days": 0}],
        "relevant_memory_ids": ["condition"]}
    decision, errors = MemoryEngine._decode_decision(raw)
    assert [step.query for step in decision.steps] == ["valid"]
    assert decision.relevant_memory_ids == ["condition"]
    assert errors == [{"position": 0, "error": "tool_step_invalid"}]


def test_message_anchor_clock_is_grounded_in_selected_source_metadata():
    engine = MemoryEngine()
    engine.add(request("Meeting with Alicia.", when="2024-06-02T10:00:00Z"))
    source = engine.store.list("sources", "u1")[0]
    step = RecallStep(query="Meeting Alicia", anchor_source_id=source["id"], anchor_step=None,
        time_expression="2024-06-02T10:00:00+00:00", time_basis="message", relation="same_day", select="all", window_days=0)
    hits, coverage = engine._recall_step("u1", "That meeting", step, {source["id"]}, engine.store.documents("u1"),
                                        "2024-07-01T00:00:00Z", float("inf"))
    assert hits and coverage.startswith("bounded_candidates")
    invalid = step.model_copy(update={"time_expression": "2024-06-02T23:59:59+00:00"})
    assert engine._recall_step("u1", "That meeting", invalid, {source["id"]}, engine.store.documents("u1"),
                               "2024-07-01T00:00:00Z", float("inf"))[1] == "time_scope_unresolved"


def test_encoded_null_arguments_commit_without_a_repair_call():
    def extract(state, errors):
        draft = command(state, kind="fact")
        draft.update(target_id="null", time_expression="null", end_expression="null")
        return {"commands": [draft]}
    model = ScriptedModel(extract)
    engine = MemoryEngine(model=model)
    engine.add(request("I have a peanut allergy."))
    assert len(model.extractions) == 1
    assert engine.store.memories("u1")[0].kind == "fact"
    assert engine.store.memories("u1")[0].current.valid_to is None


def test_encoded_null_keeps_a_dependent_tool_step_valid():
    raw = {"steps": [
        {"query": "anchor", "time_expression": "null", "anchor_source_id": "null", "anchor_step": "null",
         "time_basis": "message", "relation": "independent", "select": "all", "window_days": 0},
        {"query": "answer", "time_expression": "null", "anchor_source_id": "null", "anchor_step": 0,
         "time_basis": "message", "relation": "after", "select": "first", "window_days": 0}], "relevant_memory_ids": []}
    decision, errors = MemoryEngine._decode_decision(raw)
    assert len(decision.steps) == 2 and not errors
    assert decision.steps[1].anchor_step == 0 and decision.steps[0].time_expression is None


@pytest.mark.parametrize("expression,text,expected", [
    ("January 20th", "On January 20th I visited the market", "2024-01-20"),
    ("2024-01-20T23:59:59Z", "On January 20th I visited the market", "2024-01-20"),
    ("20 January 2023", "On 20 January 2023 I visited the market", "2023-01-20"),
    ("this morning", "I worked this morning", "2024-06-02"),
])
def test_source_grounded_english_calendar_dates(expression, text, expected):
    result = resolve_time(expression, text, "2024-06-02T10:00:00Z")
    assert result.startswith(expected) and "23:59:59" not in result


def test_two_unknown_time_fields_degrade_locally_without_losing_valid_content():
    def extract(state, errors):
        draft = command(state, kind="fact", time="next solstice")
        draft["end_expression"] = "until the eclipse"
        return {"commands": [draft]}
    engine = MemoryEngine(model=ScriptedModel(extract))
    assert engine.add(request("The restriction applies until further notice.")).success
    assert len(engine.store.memories("u1")) == 1
    assert engine.store.memories("u1")[0].current.valid_to is None


def test_supported_relative_dates_share_the_same_content_normalization():
    assert absolute_content("I worked this morning and rested last night", "I worked this morning and rested last night",
                            "2024-06-02T10:00:00Z") == "I worked 2024-06-02 and rested 2024-06-01"
