"""Context-limit recovery preserves source and temporal questions without a fixed token guess."""
from copy import deepcopy
import json
from time import monotonic

import httpx
import pytest

from remi.providers import JevReranker


@pytest.mark.parametrize('status', [400, 413])
def test_context_limit_splits_questions_and_preserves_every_score(status):
    requests, successful = [], []
    def handler(request):
        questions=json.loads(request.content)['questions']
        requests.append(set(questions))
        if len(questions)>3:
            return httpx.Response(status,json={'error':{'message':'{"error_type":"max_tokens_exceeded"} (request id: test)'}})
        successful.extend(questions)
        return httpx.Response(200,json={'answers':{
            key:dict(type='noul',noul=.75) for key in questions}})
    ranker=JevReranker('dummy',transport=httpx.MockTransport(handler))
    cards=[dict(id=f'D{i}',original='A supported claim.',temporal_targets=[
        dict(id=f'T{i}_{j}',original='A dated claim.',query='When?',time='2025-01-01') for j in range(2)])
        for i in range(4)]
    original=deepcopy(cards)
    try:
        result=ranker.rank('What happened?',cards,deadline=monotonic()+10)
        assert set(result.scores)=={f'D{i}' for i in range(4)}
        assert set(result.temporal_scores)=={f'T{i}_{j}' for i in range(4) for j in range(2)}
        assert len(successful)==len(set(successful))==12
        assert len(requests)==7
        assert cards==original
        assert sum(bool(call.get('split')) for call in ranker.calls)==3
    finally:
        ranker.close()


@pytest.mark.parametrize('status,body', [
    (401,{'error':{'code':'invalid_api_key'}}),
    (400,{'error':{'message':'invalid question type'}}),
])
def test_other_protocol_errors_keep_existing_failure_behavior(status,body):
    requests=[]
    def handler(request):
        requests.append(request)
        return httpx.Response(status,json=body)
    ranker=JevReranker('dummy',transport=httpx.MockTransport(handler))
    try:
        result=ranker.rank('Question',[dict(id='a',original='A'),dict(id='b',original='B')],deadline=monotonic()+10)
        assert not result.scores and len(requests)==1
    finally:
        ranker.close()


def test_single_oversized_question_terminates_without_recursive_retry():
    requests=[]
    def handler(request):
        requests.append(request)
        return httpx.Response(400,json={'error':{'code':'context_length_exceeded'}})
    ranker=JevReranker('dummy',transport=httpx.MockTransport(handler))
    try:
        result=ranker.rank('Question',[dict(id='a',original='A')],deadline=monotonic()+10)
        assert not result.scores and len(requests)==1
    finally:
        ranker.close()
