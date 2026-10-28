from __future__ import annotations

from datetime import timedelta

from remi.clock import parse_time
from .helpers import TestEngine as MemoryEngine
from remi.heat import hot_view
from remi.models import RecallStep
from remi.providers import ProviderUnavailable, RankResult
from remi.settings import Settings
from .helpers import HashEmbedder, ScriptedModel, command, query, request


def test_cold_evidence_remains_globally_searchable():
    engine = MemoryEngine(model=ScriptedModel(), embedder=HashEmbedder())
    engine.add(request("In 2010 I used an IBM ThinkPad T60.", when="2010-02-01T10:00:00+00:00"))
    assert not hot_view(engine.store.memories("u1"), "2026-10-12T00:00:00+00:00")
    assert "ThinkPad" in engine.search(query("ThinkPad T60"), "2026-10-12T00:00:00+00:00").data[0].content


def test_old_valid_condition_is_hot_without_a_recent_event():
    engine = MemoryEngine(model=ScriptedModel(lambda state, errors: {"commands": [command(state, kind="fact")]}))
    engine.add(request("I have a severe peanut allergy.", when="2010-02-01T10:00:00+00:00"))
    assert len(hot_view(engine.store.memories("u1"), "2026-10-12T00:00:00+00:00")) == 1


def test_heat_does_not_change_ids_sources_vectors_or_confirmation():
    engine = MemoryEngine(model=ScriptedModel(), embedder=HashEmbedder())
    engine.add(request("ThinkPad helped teaching."))
    memory = engine.store.memories("u1")[0]
    documents = engine.store.documents("u1")
    engine.search(query("ThinkPad"), "2026-10-12T00:00:00+00:00")
    after = engine.store.memories("u1")[0]
    assert after.id == memory.id
    assert after.current == memory.current
    assert engine.store.documents("u1") == documents


def test_historical_query_excludes_future_sources_and_revisions():
    engine = MemoryEngine(model=ScriptedModel())
    engine.add(request("January context.", when="2024-01-01T10:00:00+00:00"))
    engine.add(request("Future SecretDevice.", when="2025-01-01T10:00:00+00:00", request_id="r2"))
    result = engine.search(query("SecretDevice"), "2024-01-02T00:00:00+00:00")
    assert all("SecretDevice" not in item.content for item in result.data)


def test_source_turn_dedupes_raw_and_memory_paths():
    engine = MemoryEngine(model=ScriptedModel(), embedder=HashEmbedder())
    engine.add(request("Dell supported remote teaching.", assistant="The Dell was useful."))
    assert len(engine.search(query("Dell remote teaching")).data) == 1


def test_current_conditions_are_bounded_and_selected_in_one_decision():
    model = ScriptedModel(lambda state, errors: {"commands": [command(state, kind="fact")]},
                          lambda state: {"query_mode": "direct", "action_time_expression": None,
                                         "searches": [], "relevant_memory_ids": [state["current_conditions"][0]["id"]]})
    engine = MemoryEngine(model=model, settings=Settings(hot_limit=3, context_bytes=1500))
    for index in range(8):
        engine.add(request("Project constraint " + str(index), request_id=str(index)))
    result = engine.search(query("Project constraint"))
    assert len(model.decisions) == 1
    assert len(model.decisions[0]["current_conditions"]) <= 3
    assert engine.last_search["decision_bytes"] <= 24000
    assert engine.last_search["selected_conditions"] == 1
    assert all(item.content.startswith("Relevant source-reported statements:") for item in result.data)


def test_graph_reads_only_matching_object_with_a_cap():
    engine = MemoryEngine(model=ScriptedModel(lambda state, errors: {"commands": [command(state,
        objects=[{"kind": "project", "name": "Northstar"}])]}), embedder=HashEmbedder(), settings=Settings(graph_limit=2))
    for index in range(6):
        engine.add(request("Northstar meeting " + str(index), request_id=str(index)))
    engine.search(query("Northstar meeting"))
    assert engine.last_search["graph_documents"] <= 2


def test_no_graph_ablation_keeps_global_search():
    engine = MemoryEngine(model=ScriptedModel(), settings=Settings(graph_enabled=False, hot_enabled=False))
    engine.add(request("Dell evidence."))
    assert engine.search(query("Dell")).data
    assert engine.last_search["graph_documents"] == 0
    assert engine.last_search["hot_conditions"] == 0


def test_invalid_reference_date_plan_falls_back_and_retains_base():
    model = ScriptedModel(decide=lambda state: {"query_mode": "direct", "action_time_expression": None,
         "searches": [{"role": "direct", "query": "Dell", "time_expression": "2024-06-13",
         "anchor_source_id": None, "anchor_step": None, "relation": "independent"}], "relevant_memory_ids": []})
    engine = MemoryEngine(model=model)
    engine.add(request("Dell evidence."))
    result = engine.search(query("Which Dell device did I use?"), "2024-06-13T12:00:00+00:00")
    assert "Dell" in result.data[0].content
    assert "decision_invalid" in engine.last_search["degradation"]


def test_question_date_and_plus_minus_three_day_range():
    engine = MemoryEngine(model=ScriptedModel())
    engine.add(request("Northstar workshop.", when="2024-01-19T10:00:00+00:00"))
    step = RecallStep(query="Northstar workshop", time_expression="2024-01-20", anchor_source_id=None,
                      time_basis="message", relation="independent", select="all", window_days=3)
    documents = engine._visible_documents("u1", "2024-02-01T00:00:00+00:00")
    hits, coverage = engine._recall_step("u1", "What happened on 2024-01-20?", step, set(), documents,
                                        "2024-02-01T00:00:00+00:00", float("inf"))
    assert hits
    assert all(document.message_at.startswith("2024-01-19") for document, _ in hits)
    assert "bounded" in coverage


def test_time_recall_preserves_candidates_for_activity_check():
    engine = MemoryEngine(model=ScriptedModel())
    for date in ("2024-06-09", "2024-06-15"):
        engine.add(request("Email Alicia", when=date + "T10:00:00+00:00", request_id=date))
    step = RecallStep(query="Email Alicia", time_expression="2024-06-06", anchor_source_id=None,
                      time_basis="message", relation="after", select="first", window_days=0)
    hits, coverage = engine._recall_step("u1", "First email after 2024-06-06?", step, set(), engine.store.documents("u1"),
                                        "2024-07-01T00:00:00+00:00", float("inf"))
    assert {document.message_at[:10] for document, _ in hits} == {"2024-06-09", "2024-06-15"}
    assert coverage.startswith("bounded")


def test_decision_failure_and_reranker_failure_return_base_evidence():
    def fail(state):
        raise ProviderUnavailable("test")
    class Ranker:
        def rank(self, query, cards, *, deadline):
            raise ProviderUnavailable("test")
    engine = MemoryEngine(model=ScriptedModel(decide=fail), reranker=Ranker())
    engine.add(request("Dell evidence."))
    assert engine.search(query("Dell")).data
    assert "decision_unavailable" in engine.last_search["degradation"]
    assert "rerank_partial_or_unavailable" in engine.last_search["degradation"]


def test_partial_rerank_preserves_original_order():
    class Ranker:
        def rank(self, query, cards, *, deadline):
            return RankResult(scores={cards[-1]["id"]: .99})
    engine = MemoryEngine(model=ScriptedModel())
    engine.add(request("Dell evidence A"))
    engine.add(request("Dell evidence B", request_id="r2"))
    before = [item.id for item in engine.search(query("Dell")).data]
    engine.reranker = Ranker()
    assert [item.id for item in engine.search(query("Dell")).data] == before


def test_evidence_cards_keep_original_words_under_budget():
    engine = MemoryEngine(model=ScriptedModel())
    for index in range(40):
        engine.add(request("Long evidence " + str(index) + " details " * 300, request_id=str(index)))
    engine.search(query("Long evidence"))
    state = engine.model.decisions[-1]
    assert engine.last_search["decision_bytes"] <= engine.settings.decision_bytes
    assert state["evidence"] and all(card["original"] for card in state["evidence"])
