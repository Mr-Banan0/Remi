"""Generic source, date-anchor and recall-union invariants. Fully offline."""
from time import monotonic

import pytest

from .helpers import TestEngine as MemoryEngine
from remi.models import Document, RecallStep
from remi.providers import RankResult
from remi.settings import Settings
from tests.helpers import ScriptedModel, command, query, request


@pytest.mark.parametrize("text,quoted", [
    ("The access\n  remains restricted.", "The access remains restricted."),
    ("I’m assigned to the “Orion” project.", 'I\'m assigned to the "Orion" project.'),
    ("项目访问权限\n仍受限制。", "项目访问权限 仍受限制。"),
])
def test_equivalent_quote_is_copied_from_the_actual_original(text, quoted):
    model = ScriptedModel(lambda state, errors: {"commands": [command(state, kind="fact", quote=quoted)]})
    engine = MemoryEngine(model=model)
    engine.add(request(text))
    assert engine.store.memories("u1")[0].current.quote == text
    assert len(model.extractions) == 1




def test_long_input_windows_cover_every_character():
    from remi.add_pipeline import AddPipeline
    text='Access'+(' '*21000)+'remains restricted.'
    chunks=list(AddPipeline.chunks(text))
    assert ''.join(c[2] for c in chunks)==text
    assert chunks[0][0]==0 and chunks[-1][1]==len(text)
    assert all(right[0]==left[1] for left,right in zip(chunks,chunks[1:]))


def test_invalid_source_is_diagnosed_and_the_unit_stays_retryable():
    from remi.providers import ProviderUnavailable
    model=ScriptedModel(lambda state,errors:{'commands':[{**command(state,kind='fact'),'source_id':'foreign'}]})
    engine=MemoryEngine(model=model)
    with pytest.raises(ProviderUnavailable): engine.add(request('Access remains restricted.'))
    saved=engine.store.get('requests','u1','r1')['units']['0']
    assert saved['phase']=='pending'
    assert saved['validation'][0]['error']=='candidate_source_requires_supplied_original'
    assert not engine.store.memories('u1')


def test_each_memory_keeps_its_own_date_and_exact_supporting_clause():
    def extract(state, errors):
        return {"commands": [
            command(state, content="2024-06-01 review", quote="Yesterday I reviewed Orion.", time="yesterday"),
            command(state, content="2024-06-03 launch", quote="Tomorrow I launch Vega.", time="tomorrow", status="planned"),
            command(state, content="Access remains restricted", kind="fact", quote="Access remains restricted.")
        ]}
    engine = MemoryEngine(model=ScriptedModel(extract))
    engine.add(request("Yesterday I reviewed Orion. Tomorrow I launch Vega. Access remains restricted."))
    events = [memory for memory in engine.store.memories("u1") if memory.tags[0] == "event"]
    assert {(memory.current.event_at or memory.current.planned_at)[:10] for memory in events} == {"2024-06-01", "2024-06-03"}
    facts = [memory for memory in engine.store.memories("u1") if memory.tags[0] == "fact"]
    assert len(facts) == 1 and facts[0].current.status == "stated"


def test_context_source_does_not_pollute_the_event_anchor():
    engine = MemoryEngine(model=ScriptedModel(lambda state, errors: {"commands": [
        command(state, time="today", quote=state["new_user_source"]["content"])]}))
    engine.add(request("Today I reviewed Orion."))
    first = engine.store.memories("u1")[0].current.source_ids[0]
    engine.model.extractor = lambda state, errors: {"commands": [{**command(state, time="today"),
        "context_source_ids": [first]}]}
    engine.add(request("Today I launched Vega.", request_id="r2", when="2024-06-03T10:00:00Z"))
    step = RecallStep(query="reviewed Orion", time_expression=None, anchor_source_id=first,
        time_basis="event", relation="same_day", select="all", window_days=0, anchor_step=None)
    hits, coverage = engine._recall_step("u1", "Review date?", step, {first},
        engine._visible_documents("u1", "2024-06-05T00:00:00Z"), "2024-06-05T00:00:00Z", monotonic() + 10)
    assert coverage.startswith("bounded_candidates") and hits
    assert all(doc.event_at.startswith("2024-06-02") for doc, _ in hits)


def test_sufficient_direct_channel_skips_unrestricted_supplement():
    class UnionRanker:
        def __init__(self): self.cards = []
        def rank(self, text, cards, *, deadline):
            self.cards = cards
            return RankResult({card["id"]: .99 if "PRIMARY" in card["original"] else .1 for card in cards})
    model = ScriptedModel(lambda state, errors: {"commands": []}, lambda state: {"steps": [{
        "query": "SECONDARY", "time_expression": None, "anchor_source_id": None, "time_basis": "message",
        "relation": "independent", "select": "all", "window_days": 0, "anchor_step": None}], "relevant_memory_ids": []})
    ranker = UnionRanker()
    engine = MemoryEngine(model=model, reranker=ranker,
        settings=Settings(candidate_limit=2, graph_limit=1, graph_enabled=False))
    for index, text in enumerate(("PRIMARY answer", "PRIMARY background", "SECONDARY one", "SECONDARY two")):
        engine.add(request(text, request_id=str(index)))
    result = engine.search(query("PRIMARY", top_k=1), "2024-06-04T00:00:00Z")
    assert len(ranker.cards) == 2
    assert engine.last_search['tool_results'][0]['coverage']=='direct_evidence_sufficient'
    assert "PRIMARY" in result.data[0].content
    assert engine.last_search["candidate_union_limit"] == 2
    assert len(engine.last_search["candidate_channels"]) == 2


def test_partial_rerank_retains_the_original_question_baseline():
    class PartialRanker:
        def rank(self, text, cards, *, deadline):
            return RankResult({cards[0]["id"]: .9})
    model = ScriptedModel(lambda state, errors: {"commands": []}, lambda state: {"steps": [{
        "query": "SECONDARY", "time_expression": None, "anchor_source_id": None, "time_basis": "message",
        "relation": "independent", "select": "all", "window_days": 0, "anchor_step": None}], "relevant_memory_ids": []})
    engine = MemoryEngine(model=model, reranker=PartialRanker(), settings=Settings(graph_enabled=False))
    engine.add(request("PRIMARY answer"))
    engine.add(request("SECONDARY unrelated", request_id="r2"))
    retrieve = engine._hybrid
    engine._hybrid = lambda owner, text, docs, vector=None, limit=None: [
        (doc, .005 if text == "PRIMARY" else .032) for doc, _ in retrieve(owner, text, docs, vector, limit)
        if text in doc.text]
    result = engine.search(query("PRIMARY", top_k=1), "2024-06-04T00:00:00Z")
    assert "PRIMARY" in result.data[0].content
    assert "rerank_partial_or_unavailable" not in engine.last_search["degradation"]


def test_fallback_recall_logs_its_actual_new_source():
    model = ScriptedModel(lambda state, errors: {"commands": []}, lambda state: {"steps": [{
        "query": "SECONDARY", "time_expression": None, "anchor_source_id": "unknown", "time_basis": "message",
        "relation": "same_day", "select": "all", "window_days": 0, "anchor_step": None}], "relevant_memory_ids": ["unknown_condition"]})
    engine = MemoryEngine(model=model, settings=Settings(graph_enabled=False))
    engine.add(request("PRIMARY answer"))
    engine.add(request("SECONDARY detail", request_id="r2"))
    engine.search(query("PRIMARY"), "2024-06-04T00:00:00Z")
    result = engine.last_search["tool_results"][0]
    assert result["coverage"] == "anchor_unknown" and result["fallback_executed"]
    assert result["scoped_new_documents"] == 0 and result["new_documents"] == 1
    assert result["new_source_turns"] == 1
    assert engine.last_search["unavailable_condition_selection_count"] == 1


def test_sufficient_direct_and_graph_channels_define_the_natural_bound():
    channels = {name: [Document(id=f"{name}_{index}", owner="u1", text=name,
        source_ids=[f"source_{name}_{index}"], memory_id=None, revision_id=None,
        message_at="2024-06-02T10:00:00Z", event_at=None, created_at="2024-06-02T10:00:00Z")
        for index in range(52)] for name in ("base", "one", "two")}
    model = ScriptedModel(decide=lambda state: {"steps": [{"query": name, "time_expression": None,
        "anchor_source_id": None, "time_basis": "message", "relation": "independent", "select": "all",
        "window_days": 0, "anchor_step": None} for name in ("one", "two")], "relevant_memory_ids": []})
    class Ranker:
        def rank(self, text, cards, *, deadline):
            self.count = len(cards)
            return RankResult()
    ranker = Ranker()
    engine = MemoryEngine(model=model, reranker=ranker)
    engine._visible_documents = lambda *args: [doc for group in channels.values() for doc in group]
    engine._hybrid = lambda owner, text, *args, **kwargs: [(doc, .01) for doc in channels[text][:40]]
    engine._graph_candidates = lambda owner, text, seeds, *args: [(doc, .02)
        for doc in channels[seeds[0].text][40:]]
    engine._card = lambda owner, doc, *args: {"id": doc.id, "source_ids": doc.source_ids,
        "original": doc.text, "summary": doc.text, "memories": [], "message_at": doc.message_at, "event_at": None}
    result = engine.search(query("base", top_k=10), "2024-06-04T00:00:00Z")
    assert ranker.count == engine.last_search["candidate_union_limit"] == 52
    assert all(result['coverage']=='direct_evidence_sufficient' for result in engine.last_search['tool_results'])
    assert all("base" in item.content for item in result.data)
    assert len(engine.last_search["candidate_channels"]) == 2


def test_genuine_two_event_dates_in_the_same_primary_source_remain_ambiguous():
    model = ScriptedModel(lambda state, errors: {"commands": [
        command(state, content="Orion review", quote="Yesterday I reviewed Orion.", time="yesterday"),
        command(state, content="Vega launch", quote="Today I launched Vega.", time="today")]})
    engine = MemoryEngine(model=model)
    engine.add(request("Yesterday I reviewed Orion. Today I launched Vega."))
    anchor = engine.store.memories("u1")[0].current.source_ids[0]
    step = RecallStep(query="review", time_expression=None, anchor_source_id=anchor,
        time_basis="event", relation="same_day", select="all", window_days=0, anchor_step=None)
    hits, coverage = engine._recall_step("u1", "On that day?", step, {anchor},
        engine._visible_documents("u1", "2024-06-05T00:00:00Z"), "2024-06-05T00:00:00Z", monotonic() + 10)
    assert hits == [] and coverage == "time_scope_unresolved"

