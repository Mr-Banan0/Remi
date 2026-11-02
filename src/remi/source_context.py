"""Bounded adjacent source context for final Search evidence packages."""
from .clock import parse_time


def source_context(store, owner, documents, as_of):
    primary_ids = list(dict.fromkeys(doc.source_ids[0] for doc in documents if doc.source_ids))
    primaries = store.sources(owner, primary_ids)
    if not primaries:
        return {}
    groups = {(source.session_id, source.request_id) for source in primaries}
    rows_by_group = {key: [] for key in groups}
    cutoff = parse_time(as_of)
    for row in store.list('sources', owner):
        key = row['session_id'], row['request_id']
        if key in groups and parse_time(row['message_at'] or row['created_at']) <= cutoff:
            rows_by_group[key].append(row)
    for rows in rows_by_group.values():
        rows.sort(key=lambda row: (row['ordinal'], row['id']))
    return {source.id: [row['id'] for row in rows_by_group[(source.session_id, source.request_id)]
                       if abs(row['ordinal'] - source.ordinal) <= 2]
            for source in primaries}


def with_source_context(document, context):
    neighbors = context.get(document.source_ids[0], []) if document.source_ids else []
    return document.model_copy(update={'source_ids': list(dict.fromkeys([*document.source_ids, *neighbors]))})
