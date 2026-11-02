"""Experiment-only bounded context from adjacent messages in the same ingestion request."""

def expand_document(engine, owner, document):
    primary = engine.store.sources(owner, document.source_ids[:1])
    if not primary:
        return document
    primary = primary[0]
    neighbors = sorted((row for row in engine.store.list('sources', owner)
        if row['session_id'] == primary.session_id and row['request_id'] == primary.request_id
        and abs(row['ordinal'] - primary.ordinal) <= 2), key=lambda row: row['ordinal'])
    ids = list(dict.fromkeys([*document.source_ids, *(row['id'] for row in neighbors)]))
    return document.model_copy(update={'source_ids': ids})


def configure(engine):
    original = engine._card
    def card(owner, document, query, budget):
        if budget == engine.settings.evidence_bytes:
            document = expand_document(engine, owner, document)
        return original(owner, document, query, budget)
    engine._card = card
