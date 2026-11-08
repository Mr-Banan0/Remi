"""Candidate depth and unique-source budgets are independent of question domains."""
import pytest
from remi.engine import MemoryEngine
from remi.settings import Settings
from tests.test_search_roles import document


@pytest.mark.parametrize('scenario', ['travel_budget', 'schedule_conflict', 'equipment_availability'])
def test_duplicate_documents_spend_one_source_slot(scenario):
    docs = [document(str(i)).model_copy(update={'source_ids':['shared']}) for i in range(4)]
    independent = document(scenario)
    docs.append(independent)
    engine = MemoryEngine(settings=Settings(recall_depth=5,candidate_limit=2))
    engine.store.lexical_scores = lambda *args: {doc.id:100-i for i,doc in enumerate(docs)}
    engine.store.vector_scores = lambda *args: {doc.id:1-i/10 for i,doc in enumerate(docs)}
    hits = engine._recall_candidates('u1',scenario,docs,[1.0],limit=2)
    assert {engine._source_key(doc) for doc,_ in hits} == {'shared',independent.source_ids[0]}
    engine.settings = Settings(recall_depth=4,candidate_limit=2)
    shallow = engine._recall_candidates('u1',scenario,docs,[1.0],limit=2)
    assert {engine._source_key(doc) for doc,_ in shallow} == {'shared'}


def test_single_channel_hit_survives_fusion_by_package():
    first,second,third = [document(k) for k in ['one','two','three']]
    engine = MemoryEngine(settings=Settings(recall_depth=3))
    engine.store.lexical_scores = lambda *args: {'one':3,'two':2,'three':1}
    engine.store.vector_scores = lambda *args: {'three':.9,'one':.8}
    hits=engine._recall_candidates('u1','query',[first,second,third],[1],limit=3)
    assert {doc.id for doc,_ in hits} == {'one','two','three'}
    assert len(hits)==3


def test_recall_depth_env_and_bounds(monkeypatch):
    monkeypatch.delenv('REMI_RECALL_DEPTH', raising=False)
    assert Settings.from_environment().recall_depth == 40
    monkeypatch.setenv('REMI_RECALL_DEPTH','120')
    assert Settings.from_environment().recall_depth==120
    for invalid in [0,1001]:
        with pytest.raises(ValueError,match='recall_depth_invalid'):
            Settings(recall_depth=invalid)
