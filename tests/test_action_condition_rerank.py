from copy import deepcopy
from remi.providers import JevReranker, RankResult, SUPPORT_QUESTION, TEMPORAL_MATCH_QUESTION
from scripts.action_condition_rerank import configure, QUESTION


def test_condition_scope_preserves_temporal_questions_and_later_direct_calls():
    ranker = object.__new__(JevReranker)
    calls=[]
    def score(query, questions, temporal_ids, deadline):
        calls.append(deepcopy(questions))
        return RankResult(scores={k:.7 for k in questions if k not in temporal_ids},
                          temporal_scores={k:.8 for k in temporal_ids})
    ranker._score_questions=score
    configure(ranker)
    card=dict(id='D1',original='A personal condition.',retrieval_roles=['constraint'],
              temporal_targets=[dict(id='T1',query='An incident',original='It happened.')])
    before=deepcopy(card)
    result=ranker.rank('Help me plan.',[card],deadline=100)
    assert calls[0]['D1']['instructions']['question']==QUESTION
    assert calls[0]['T1']['instructions']['question']==TEMPORAL_MATCH_QUESTION
    assert result.temporal_scores=={'T1':.8}
    ranker.rank('When was it?',[dict(id='D2',original='Yesterday.',retrieval_roles=['direct'])],deadline=100)
    assert calls[1]['D2']['instructions']['question']==SUPPORT_QUESTION
    assert len(calls)==2 and card==before
