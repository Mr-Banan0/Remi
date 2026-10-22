"""One bounded candidate pool for subject/relationship maintenance and typed memory extraction."""
from __future__ import annotations

import json

from .clock import parse_time
from .heat import visible_memory
from .identity import raw_subject_candidates, visible_entity
from .models import GraphObject
from .retrieval import clip_bytes


def recall_candidates(engine, state, deadline):
    """Retrieve from originals once; downstream writers share this frozen history."""
    store = engine.store
    owner = state['owner']
    primary = state['new_user_source']
    clock = primary['message_at'] or primary['created_at']
    objects = {obj.id: obj for raw in store.list('objects', owner)
               if (obj := visible_entity(GraphObject.model_validate(raw), clock))}
    memories, originals = {}, {}
    for original in store.memories(owner):
        memory = visible_memory(original, clock)
        if memory is None or not set(memory.current.entity_ids) <= objects.keys():
            continue
        sources = store.sources(owner, memory.current.source_ids)
        if len(sources) != len(set(memory.current.source_ids)) or any(
            parse_time(s.message_at or s.created_at) > parse_time(clock) for s in sources
        ):
            continue
        memories[memory.id] = memory
        originals[memory.id] = [s.model_dump() for s in sources]

    aliases = raw_subject_candidates(store, owner, [primary, *state['context_sources']], clock)
    matched = {obj.id: names for obj, names in aliases}
    users = [obj.id for obj in objects.values() if obj.is_user]
    named = set(matched) - set(users)
    documents = [d for d in engine._visible_documents(owner, clock)
                 if d.memory_id is None or d.memory_id in memories
                 and d.revision_id == memories[d.memory_id].current.id]
    queries = [primary['content']]
    context = '\n'.join(s['content'] for s in state['context_sources'])
    if context:
        queries.append(context)
    vectors = engine._embed(queries, deadline) if documents and engine.embedder else [None] * len(queries)
    by_source = {}
    for memory in memories.values():
        for sid in memory.current.source_ids:
            by_source.setdefault(sid, []).append(memory.id)

    channels = []
    def hits(label, pool, query, vector, limit):
        ids = []
        for document, _ in engine._hybrid(owner, query, pool, vector, limit=limit):
            ids.extend([document.memory_id] if document.memory_id else
                       [mid for sid in document.source_ids for mid in by_source.get(sid, [])])
        ids = list(dict.fromkeys(ids))
        channels.append({'channel': label, 'memory_ids': ids})
        return ids

    global_hits = hits('original', documents, queries[0], vectors[0], 8)
    # A retrieved card supplies its participants even when the new referring phrase uses different words.
    seeds = named | {eid for mid in global_hits for eid in memories[mid].current.entity_ids} - set(users)
    local = [m for m in memories.values() if seeds.intersection(m.current.entity_ids)]
    local_ids = {m.id for m in local}
    local_docs = [d for d in documents if d.memory_id in local_ids]
    hits('participants', local_docs, queries[0], vectors[0], 6)
    # Exact alias hits also expose one-hop stored evidence when its wording has no lexical overlap.
    linked = sorted((m for m in local if m.endpoint_ids),
                    key=lambda m: (parse_time(m.current.observed_at), m.id), reverse=True)
    channels.append({'channel': 'relationships', 'memory_ids': [m.id for m in linked]})
    for query, vector in zip(queries[1:], vectors[1:]):
        hits('context', documents, query, vector, 8)
    channels.append({'channel': 'participant_history', 'memory_ids': [m.id for m in sorted(
        local, key=lambda m: (parse_time(m.current.observed_at), m.id), reverse=True)]})

    ordered = []
    for position in range(max((len(c['memory_ids']) for c in channels), default=0)):
        for channel in channels:
            if position < len(channel['memory_ids']) and channel['memory_ids'][position] not in ordered:
                ordered.append(channel['memory_ids'][position])

    def subject_row(eid):
        obj = objects[eid]
        names = matched.get(eid, [])
        return {'id': eid, 'name': obj.name, 'kind': obj.kind, 'is_user': obj.is_user,
                'matched_names': names, 'aliases': [dict(a.model_dump(), quote=clip_bytes(a.quote, 500))
                for a in sorted(obj.aliases, key=lambda a: a.name not in names)[:8]]}

    def bundle(ids):
        required = list(dict.fromkeys([*users, *(eid for mid in ids for eid in memories[mid].current.entity_ids)]))
        if len(required) > 24:
            return None
        entity_ids = list(dict.fromkeys([*required, *matched]))[:24]
        cards = []
        relations = []
        for mid in ids:
            memory = memories[mid]
            r = memory.current
            card = {'id': mid, 'revision_id': r.id, 'record_type': memory.record_type, 'content': r.content,
                    'status': r.status, 'planned_at': r.planned_at, 'event_at': r.event_at, 'mode': r.mode,
                    'context': r.context, 'user_involvement': r.user_involvement,
                    'subjects': [objects[eid].name for eid in r.entity_ids], 'entity_ids': r.entity_ids,
                    'endpoint_ids': memory.endpoint_ids, 'sources': originals[mid]}
            cards.append(card)
            if memory.endpoint_ids:
                relations.append({k: card[k] for k in ['id', 'revision_id', 'endpoint_ids', 'content', 'mode', 'sources']} |
                                 {'ordered_subject_ids': r.entity_ids})
        result = {'history': cards, 'subject_catalog': [subject_row(eid) for eid in entity_ids],
                  'relation_catalog': relations}
        # Trim optional alias-only hits together; every retained card keeps all its participant identities.
        while len(json.dumps(result, ensure_ascii=False).encode()) > 24000:
            if len(result['subject_catalog']) <= len(required):
                return None
            result['subject_catalog'].pop()
        return result

    selected, skipped = [], []
    result = bundle([])
    for mid in ordered:
        if len(selected) == 8:
            break
        trial = bundle([*selected, mid])
        if trial is None:
            skipped.append(mid)
        else:
            selected.append(mid)
            result = trial
    # A normal owner has a single canonical user, whose compact row always fits the budget.
    if result is None:
        from .providers import ProviderUnavailable
        raise ProviderUnavailable('shared_recall_required_subjects_exceed_budget')
    trace = {'channels': channels, 'selected_memory_ids': selected,
             'selected_subject_ids': [s['id'] for s in result['subject_catalog']], 'budget_skipped_memory_ids': skipped}
    sources = {s['id']: s for s in [primary, *state['context_sources']]}
    for card in result['history']:
        sources.update({s['id']: s for s in card['sources']})
    return {**state, **result, 'source_catalog': sources}, trace
