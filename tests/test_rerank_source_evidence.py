import copy
import json
from time import monotonic

import httpx

from remi.providers import JevReranker


def test_jev_receives_original_evidence_and_retains_callers_memory_view():
    card = dict(id='d',original='[s] user: The rehearsal was cancelled.',source_ids=['s'],
                summary='The rehearsal happened.',memories=[dict(content='The rehearsal happened.')],
                retrieval_objectives=[dict(role='premise_check',query='Whether the rehearsal occurred')],
                temporal_targets=[dict(id='t',query='The rehearsal',original='The rehearsal was cancelled.',
                                       time='2025-01-01',memories=[dict(content='The rehearsal happened.')])])
    original = copy.deepcopy(card)

    def handler(request):
        body = json.loads(request.content)
        evidence = body['questions']['d']['instructions']['evidence']
        assert evidence['original'] == card['original']
        assert evidence['source_ids'] == ['s']
        assert evidence['retrieval_objectives'] == card['retrieval_objectives']
        assert 'summary' not in evidence and 'memories' not in evidence
        target = body['questions']['t']['instructions']['target']
        assert target['original'] == 'The rehearsal was cancelled.'
        assert target['time'] == '2025-01-01' and 'memories' not in target
        return httpx.Response(200,json={'answers':{
            'd':{'type':'noul','noul':.9},'t':{'type':'noul','noul':.1}}})

    ranker = JevReranker('dummy',transport=httpx.MockTransport(handler))
    try:
        result = ranker.rank('Did my rehearsal happen?', [card], deadline=monotonic()+10)
        assert result.scores == {'d':.9} and result.temporal_scores == {'t':.1}
        assert card == original
    finally:
        ranker.close()
