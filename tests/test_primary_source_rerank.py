from copy import deepcopy
from types import SimpleNamespace
import remi.providers as providers
from scripts.primary_source_rerank import configure


def test_experiment_changes_only_reranker_view(monkeypatch):
    monkeypatch.setattr(providers, 'SUPPORT_QUESTION', providers.SUPPORT_QUESTION)
    raw = {'id':'d','original':'[a] user: Yes.\n\n[b] assistant: The blue option?',
           'source_ids':['a','b'],'summary':'User chose blue','retrieval_objectives':[]}
    before = deepcopy(raw)
    blocks = [{'rendered':'[a] user: Yes.'}, {'rendered':'[b] assistant: The blue option?'}]
    engine = SimpleNamespace(_card=lambda *args:deepcopy(raw), _source_blocks=lambda *args:deepcopy(blocks),
                             reranker=SimpleNamespace(_evidence_card=providers.JevReranker._evidence_card))
    configure(engine)
    card = engine._card('u',object(),'question',6000)
    assert card == before
    evidence = engine.reranker._evidence_card(card)
    assert evidence['original'] == blocks[0]['rendered']
    assert evidence['context'] == blocks[1]['rendered']
    assert evidence['source_ids'] == ['a','b']
    assert 'summary' not in evidence
    assert card == before and raw == before
