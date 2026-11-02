import json
from copy import deepcopy
from time import monotonic
import httpx
from remi.engine import MemoryEngine
from remi.models import Source, Document
from remi.providers import JevReranker


def test_rerank_uses_primary_and_context_while_display_keeps_full_original():
    engine = MemoryEngine()
    stamp = '2025-01-01T00:00:00+00:00'
    for i, (role,text) in enumerate([('assistant','The first option is red; the second is blue.'),
                                     ('user','I chose the second option.')]):
        source = Source(id=f's{i}',owner='u',session_id='s',request_id='r',ordinal=i,
                        role=role,content=text,message_at=stamp,created_at=stamp)
        engine.store.put('sources','u',source.id,source.model_dump())
    doc = Document(id='d',owner='u',text='The user chose blue.',source_ids=['s1','s0'],
                   memory_id=None,revision_id=None,message_at=stamp,event_at=None,created_at=stamp)
    display = engine._card('u',doc,'Which color?',6000)
    card = engine._card('u',doc,'Which color?',6000,True)
    assert {key:value for key,value in card.items() if key != 'ranking_evidence'} == display
    parts = card['ranking_evidence']
    assert 'I chose the second option.' in parts['original']
    assert 'second is blue.' in parts['context']
    assert '\n\n'.join(parts.values()) == display['original']
    before = deepcopy(card)
    def handler(request):
        body = json.loads(request.content)
        evidence = body['questions']['d']['instructions']['evidence']
        assert evidence['original'] == parts['original']
        assert evidence['context'] == parts['context']
        assert 'summary' not in evidence
        assert 'ranking_evidence' not in evidence
        assert evidence['source_ids'] == ['s1','s0']
        return httpx.Response(200,json={'answers':{'d':{'type':'noul','noul':.9}}})
    ranker = JevReranker('dummy',transport=httpx.MockTransport(handler))
    try:
        assert ranker.rank('Which color?',[card],deadline=monotonic()+10).scores == {'d':.9}
        assert card == before
    finally:
        ranker.close()
        engine.close()
