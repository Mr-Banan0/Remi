"""Recall subject-scoped memories, then supplement only with unbound memories when needed."""
from copy import deepcopy
from types import SimpleNamespace

from experiments.maintenance_clock import maintenance_clock
from experiments.subject_identity import visible_subject
from experiments.subject_recall import pack_history
from experiments.event_maintenance import memory_fingerprint
from remi.clock import parse_time
from remi.engine import source_visible
from remi.heat import visible_memory
from remi.retrieval import bm25


MEMORY_LIMIT = 6
MEMORY_EVIDENCE_LIMIT = 3
# A subject-scoped result is sufficient when its best lexical score is close to the
# best score in the complete allowed pool. Golden calibration can tune this value.
SUBJECT_RECALL_CONFIDENCE = 0.80


def recall_memories(engine, state, bindings, view):
    """Keep the fixed subject scope and use unbound memories as a program fallback."""
    state = deepcopy(state)
    owner = state['owner']
    primary = state['new_user_source']
    clock = maintenance_clock(state)
    confirmed = set(bindings['subject_ids'].values())
    objects = {
        obj.id: obj.model_dump()
        for raw in engine.store.list('objects', owner)
        if (obj := visible_subject(raw, owner, clock))
    }
    objects.update({row['id']: row for row in bindings['entity_rows']})

    candidate_text='\n'.join(item['content'] for item in state.get('memory_candidates',[]))
    query = '\n'.join(part for part in [candidate_text,primary['content'],
        '\n'.join(source['content'] for source in state.get('context_sources', []))] if part)
    pools = {'subjects': [], 'unbound': []}
    originals = {}
    snapshot_hashes = {}
    lanes = {}
    for raw in engine.store.memories(owner):
        memory = visible_memory(raw, clock)
        if memory is None or memory.record_type == 'relation':
            continue
        ids = set(memory.current.entity_ids)
        if ids and not ids.intersection(confirmed):
            continue
        if not ids <= objects.keys():
            continue
        sources = engine.store.sources(owner, memory.current.source_ids)
        if len(sources) != len(set(memory.current.source_ids)) or not all(source_visible(source, clock) for source in sources):
            continue
        lane = 'subjects' if ids else 'unbound'
        pools[lane].append(memory)
        lanes[memory.id] = lane
        source_rows={source.id:source.model_dump() for source in sources}
        preferred=[]
        for revision in reversed(memory.revisions):
            patch=revision.maintenance_patch or {}
            if patch.get('fields'):
                preferred.append(patch.get('source_id'));break
        if memory.current.source_ids:preferred.append(memory.current.source_ids[0])
        source_scores=bm25(query,[SimpleNamespace(id=source.id,text=source.content) for source in sources])
        preferred.extend(source.id for source in sorted(sources,key=lambda source:(-source_scores.get(source.id,0.0),source.id)))
        chosen=[]
        for source_id in preferred:
            if source_id in source_rows and source_id not in chosen:chosen.append(source_id)
            if len(chosen)==MEMORY_EVIDENCE_LIMIT:break
        originals[memory.id] = [source_rows[source_id] for source_id in chosen]
        snapshot_hashes[memory.id] = memory_fingerprint(raw)

    allowed = [*pools['subjects'], *pools['unbound']]
    scores = bm25(query, [SimpleNamespace(id=memory.id, text=memory.current.content) for memory in allowed])
    subject_top = max((scores.get(memory.id, 0.0) for memory in pools['subjects']), default=0.0)
    allowed_top = max(scores.values(), default=0.0)
    confidence = subject_top / allowed_top if allowed_top else 0.0
    supplement = not pools['subjects'] or subject_top <= 0 or confidence < SUBJECT_RECALL_CONFIDENCE
    selected_pool = allowed if supplement else pools['subjects']
    selected = [memory for memory in selected_pool if scores.get(memory.id, 0.0) > 0]
    selected.sort(key=lambda memory: (
        -scores[memory.id],
        -parse_time(memory.current.observed_at).timestamp(),
        memory.id,
    ))
    selected = selected[:MEMORY_LIMIT]

    cards = []
    for memory in selected:
        revision = memory.current
        cards.append(dict(
            id=memory.id,
            revision_id=revision.id,
            record_type=memory.record_type,
            content=revision.content,
            status=revision.status,
            planned_at=revision.planned_at,
            event_at=revision.event_at,
            mode=revision.mode,
            context=revision.context,
            entity_ids=revision.entity_ids,
            sources=originals[memory.id],
            recall_lane=lanes[memory.id],
            related_memory_ids=[
                edge.target
                for edge in engine.store.edges_for(owner, origins=[memory.id])
                if edge.relation == 'related' and edge.revision_id == revision.id
            ],
        ))

    prepared, trace = pack_history(
        engine.store,
        {**state, 'history': [], 'history_limit': MEMORY_LIMIT},
        cards,
    )
    prepared['memory_snapshot_hashes'] = {
        card['id']: snapshot_hashes[card['id']]
        for card in prepared['history']
    }
    needed = {object_id for row in prepared['history'] for object_id in row['entity_ids']}
    catalog = {row['id']: row for row in state['subject_catalog']}
    catalog.update({object_id: objects[object_id] for object_id in needed | confirmed if object_id in objects})
    prepared['subject_catalog'] = list(catalog.values())
    return prepared, dict(
        confirmed_subject_ids=sorted(confirmed),
        subject_memory_ids=sorted(memory.id for memory in pools['subjects']),
        unbound_memory_ids=sorted(memory.id for memory in pools['unbound']),
        allowed_memory_ids=sorted(memory.id for memory in allowed),
        lanes=lanes,
        subject_top_score=subject_top,
        allowed_top_score=allowed_top,
        subject_recall_confidence=confidence,
        subject_recall_threshold=SUBJECT_RECALL_CONFIDENCE,
        supplement_triggered=supplement,
        supplemented_unbound_memory_ids=sorted(
            memory.id for memory in selected if lanes[memory.id] == 'unbound'
        ),
        **trace,
    )
