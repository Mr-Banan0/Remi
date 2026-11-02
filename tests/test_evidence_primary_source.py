"""Bounded evidence cards retain the source identifying their evidence package."""
from remi.engine import MemoryEngine
from remi.models import Document, Source


def build_card(primary_role='user', budget=700):
    engine = MemoryEngine()
    stamp = '2025-05-01T09:00:00+00:00'
    sources = [
        Source(id='primary', owner='u', session_id='s', request_id='r', ordinal=0,
               role=primary_role, content='I am unavailable until noon on weekends.',
               message_at=stamp, created_at=stamp),
        Source(id='context', owner='u', session_id='s', request_id='r', ordinal=1,
               role='assistant', content='A weekend morning study schedule can build motivation. ' * 40,
               message_at=stamp, created_at=stamp),
    ]
    for source in sources:
        engine.store.put('sources', 'u', source.id, source.model_dump())
    document = Document(id='d', owner='u', text='Weekend availability', source_ids=['primary','context'],
                        memory_id=None, revision_id=None, message_at=stamp, event_at=None, created_at=stamp)
    return engine._card('u', document, 'Create a weekend morning study schedule with motivation.', budget)


def test_small_card_retains_primary_evidence_instead_of_keyword_rich_context():
    card = build_card()
    assert 'I am unavailable until noon on weekends.' in card['original']
    assert len(card['original'].encode()) <= 700
    assert card['source_ids'] == ['primary']


def test_primary_priority_follows_source_identity_for_every_speaker():
    card = build_card(primary_role='assistant')
    assert card['original'].startswith('[primary] assistant')


def test_large_card_keeps_context_after_primary_evidence():
    card = build_card(budget=6000)
    assert card['original'].startswith('[primary] user')
    assert '[context] assistant' in card['original']
    assert card['source_ids'] == ['primary', 'context']
    assert len(card['original'].encode()) <= 6000
