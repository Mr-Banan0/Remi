from copy import deepcopy
from types import SimpleNamespace
from remi.providers import JevReranker
from scripts.role_only_rerank import configure


def test_role_only_retains_temporal_operator_and_original_card():
    ranker = SimpleNamespace(_evidence_card=JevReranker._evidence_card)
    configure(ranker)
    card = dict(id='D1',original='A personal condition.',
                retrieval_objectives=[dict(role='constraint',query='A navigation phrase')],
                temporal_targets=[dict(id='T1',query='A dated incident',original='An incident happened.',time='2025-01-01')])
    before = deepcopy(card)
    result = ranker._evidence_card(card)
    assert result['retrieval_objectives'] == [dict(role='constraint')]
    assert result['temporal_targets'] == card['temporal_targets']
    assert result['original'] == card['original']
    assert card == before
