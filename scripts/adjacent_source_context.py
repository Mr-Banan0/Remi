"""Experiment-only bounded context from adjacent messages in the same ingestion request."""

from remi.engine import source_visible
from remi.models import Source


def expand_document(engine, owner, document, as_of=None):
    primary = engine.store.sources(owner, document.source_ids[:1])
    if not primary:
        return document
    primary = primary[0]
    neighbors = sorted((row for row in engine.store.list('sources', owner)
        if row['session_id'] == primary.session_id and row['request_id'] == primary.request_id
        and abs(row['ordinal'] - primary.ordinal) <= 2
        and (as_of is None or source_visible(Source.model_validate(row), as_of))), key=lambda row: row['ordinal'])
    ids = list(dict.fromkeys([*document.source_ids, *(row['id'] for row in neighbors)]))
    return document.model_copy(update={'source_ids': ids})


def configure(engine):
    original = engine._card
    visible = engine._visible_documents
    clocks = {}
    def visible_documents(owner, as_of):
        clocks[owner] = as_of
        return visible(owner, as_of)
    engine._visible_documents = visible_documents
    def card(owner, document, query, budget):
        if budget == engine.settings.evidence_bytes:
            document = expand_document(engine, owner, document, clocks.get(owner))
        return original(owner, document, query, budget)
    engine._card = card
