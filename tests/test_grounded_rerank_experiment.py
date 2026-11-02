import json
from time import monotonic

import httpx

from remi.providers import JevReranker
from scripts.grounded_reranker import GroundedReranker


def test_unsupported_derived_claim_keeps_source_score_in_controlled_experiment():
    def handler(request):
        body=json.loads(request.content)
        ground=body['questions']['pair_0_ground']['instructions']
        assert ground['derived_claims']==['The passport is in a drawer.']
        assert ground['original_evidence']['original']=='I like blue furniture.'
        assert 'summary' not in ground['original_evidence']
        values={'pair_0_source':.1,'pair_0_full':.9,'pair_0_ground':0,
                'pair_1_source':.9,'pair_1_full':.9,'pair_1_ground':1}
        return httpx.Response(200,json={'answers':{key:dict(type='noul',noul=value) for key,value in values.items()}})
    ranker=GroundedReranker.from_existing(JevReranker('dummy',transport=httpx.MockTransport(handler)))
    try:
        result=ranker.rank('Where is my passport?',[
            dict(id='bad',original='I like blue furniture.',summary='The passport is in a drawer.'),
            dict(id='good',original='My passport is in the drawer.',summary='The passport is in the drawer.')],
            deadline=monotonic()+10)
        assert result.scores=={'bad':.1,'good':.9}
        assert ranker.calls[0]['question_count']==6
    finally:
        ranker.close()
