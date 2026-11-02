from copy import deepcopy
from remi.providers import JevReranker, RankResult, SUPPORT_QUESTION, TEMPORAL_MATCH_QUESTION
from remi.providers import ACTION_CONDITION_QUESTION as QUESTION


def test_condition_scope_preserves_temporal_questions_and_later_direct_calls():
    ranker = object.__new__(JevReranker)
    calls=[]
    def score(query, questions, temporal_ids, deadline):
        calls.append(deepcopy(questions))
        return RankResult(scores={k:.7 for k in questions if k not in temporal_ids},
                          temporal_scores={k:.8 for k in temporal_ids})
    ranker._score_questions=score
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


def test_condition_scope_is_shared_across_batches():
    ranker = object.__new__(JevReranker)
    questions_seen=[]
    def score(query, questions, temporal_ids, deadline):
        questions_seen.extend(q['instructions']['question'] for q in questions.values())
        return RankResult(scores={key:.8 for key in questions})
    ranker._score_questions=score
    cards=[dict(id=str(i), original='A source.',retrieval_roles=['constraint'] if i==0 else ['direct']) for i in range(9)]
    result=ranker.rank('An action request.',cards,deadline=100)
    assert len(result.scores)==9
    assert questions_seen==[QUESTION]*9
    assert not hasattr(ranker, '_support_question')
