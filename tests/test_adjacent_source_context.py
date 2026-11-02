from copy import deepcopy
from remi.engine import MemoryEngine
from remi.models import Source, Document
from scripts.adjacent_source_context import configure, expand_document


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
    expanded = expand_document(engine,'u',doc)
    assert expanded.source_ids == ['u-s-r-3','u-s-r-1','u-s-r-2','u-s-r-4','u-s-r-5']
    assert doc == before
    future = engine.store.get('sources','u','u-s-r-4')
    future['message_at'] = '2025-02-01T00:00:00+00:00'
    engine.store.put('sources','u','u-s-r-4',future)
    historical = expand_document(engine,'u',doc,'2025-01-15T00:00:00+00:00')
    assert 'u-s-r-4' not in historical.source_ids
    assert 'u-s-r-5' in historical.source_ids
    planner_before = engine._card('u',doc,'Message',700)
    configure(engine)
    assert engine._card('u',doc,'Message',700) == planner_before
    card = engine._card('u',doc,'Message',engine.settings.evidence_bytes)
    assert card['source_ids'][0] == 'u-s-r-3'
    assert set(card['source_ids']) == set(expanded.source_ids)
    assert len(card['original'].encode()) <= engine.settings.evidence_bytes
    assert doc == before
    engine.close()
