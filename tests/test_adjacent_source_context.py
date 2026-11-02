from copy import deepcopy
from dataclasses import replace
from remi.engine import MemoryEngine
from remi.models import Source, Document, SearchRequest
from remi.source_context import source_context, with_source_context


def test_context_window_keeps_owner_session_request_and_original_primary():
    engine = MemoryEngine()
    stamp = '2025-01-01T00:00:00+00:00'
    for owner, session, request in [('u','s','r'),('other','s','r'),('u','other','r'),('u','s','other')]:
        for i in range(8):
            source = Source(id=f'{owner}-{session}-{request}-{i}', owner=owner, session_id=session,
                request_id=request, ordinal=i, role='user', content=f'Message {i}. ' * 100,
                message_at=stamp, created_at=stamp)
            engine.store.put('sources',owner,source.id,source.model_dump())
    doc = Document(id='d',owner='u',text='Message 3',source_ids=['u-s-r-3'],
        memory_id=None,revision_id=None,message_at=stamp,event_at=None,created_at=stamp)
    before = deepcopy(doc)
    expanded = with_source_context(doc, source_context(engine.store,'u',[doc],stamp))
    assert expanded.source_ids == ['u-s-r-3','u-s-r-1','u-s-r-2','u-s-r-4','u-s-r-5']
    assert doc == before
    future = engine.store.get('sources','u','u-s-r-4')
    future['message_at'] = '2025-02-01T00:00:00+00:00'
    engine.store.put('sources','u','u-s-r-4',future)
    historical = with_source_context(doc, source_context(engine.store,'u',[doc],'2025-01-15T00:00:00+00:00'))
    assert 'u-s-r-4' not in historical.source_ids
    assert 'u-s-r-5' in historical.source_ids
    planner_before = engine._card('u',doc,'Message',700)
    assert engine._card('u',doc,'Message',700) == planner_before
    card = engine._card('u',historical,'Message',engine.settings.evidence_bytes)
    assert card['source_ids'][0] == 'u-s-r-3'
    assert set(card['source_ids']) == set(historical.source_ids)
    assert len(card['original'].encode()) <= engine.settings.evidence_bytes
    assert doc == before
    engine.store.put('search_documents','u',doc.id,doc.model_dump())
    engine.settings = replace(engine.settings, graph_enabled=False)
    source_scans = []
    original_list = engine.store.list
    def tracked_list(table, owner):
        if table == 'sources':
            source_scans.append(owner)
        return original_list(table, owner)
    engine.store.list = tracked_list
    result = engine.search(SearchRequest(user_id='u',query='Message 3',top_k=1),
                           reference_time='2025-01-15T00:00:00+00:00')
    assert result.data
    assert source_scans == ['u']
    assert engine.last_search['decision_source_ids'] == ['u-s-r-3']
    assert set(engine.last_search['candidates'][0]['evidence_source_ids']) == set(historical.source_ids)
    assert engine.store.documents('u')[0].source_ids == ['u-s-r-3']
    engine.close()
