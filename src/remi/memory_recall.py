"""Recall subject-scoped memories, then supplement only with unbound memories when needed."""
from copy import deepcopy
from collections import Counter
from types import SimpleNamespace

from .maintenance_clock import maintenance_clock
from .subject_identity import visible_subject
from .subject_recall import pack_history
from .memory_maintenance import concrete_topic_continuity, memory_fingerprint
from .clock import parse_time
from .heat import visible_memory
from .retrieval import bm25
from .identity import stable_id


def source_visible(source, as_of):
    return parse_time(source.message_at or source.created_at) <= parse_time(as_of)


MEMORY_LIMIT = 12
MEMORY_EVIDENCE_LIMIT = 3
# A subject-scoped result is sufficient when its best lexical score is close to the
# best score in the complete allowed pool. Golden calibration can tune this value.
SUBJECT_RECALL_CONFIDENCE = 0.80


def recall_memories(engine, state, bindings, view, *, deadline=None):
    """Keep the fixed subject scope and use unbound memories as a program fallback."""
    state = deepcopy(state)
    owner = state['owner']
    primary = state['new_user_source']
    clock = maintenance_clock(state)
    confirmed = set(bindings['subject_ids'].values())
    pending_candidates={subject_id for values in state.get('pending_subject_candidates',{}).values()
                        for subject_id in values}
    subject_scope=confirmed | pending_candidates
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
        if ids and not ids.intersection(subject_scope):
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
    candidates = list(state.get('memory_candidates', []))
    vectors = ([None] * len(candidates) if not candidates or engine.embedder is None else
               engine._embed([row['content'] for row in candidates], deadline))
    documents = {memory.id: stable_id('doc_', memory.current.id) for memory in selected_pool}
    connections = [edge for edge in engine.store.edges_for(owner)
                   if edge.relation == 'related' and edge.revision_id == next(
                       (memory.current.id for memory in selected_pool if memory.id == edge.memory_id), None)]
    selected=[];candidate_target_ids={};candidate_related_ids={};candidate_rankings={}
    by_id={memory.id:memory for memory in selected_pool}
    labels=[]
    for obj in objects.values():
        labels.append(obj.get('name'))
        labels.extend(alias.get('name') for alias in obj.get('aliases', []))
    for candidate,vector in zip(candidates,vectors):
        lexical=bm25(candidate['content'],[SimpleNamespace(id=memory.id,text=memory.current.content)
                                          for memory in selected_pool])
        lexical_ids=sorted(lexical,key=lambda key:(-lexical[key],key))
        semantic={}
        if vector is not None:
            raw=engine.store.vector_scores(owner,vector,[documents[memory.id] for memory in selected_pool])
            semantic={memory.id:raw[documents[memory.id]] for memory in selected_pool if documents[memory.id] in raw}
        semantic_ids=sorted(semantic,key=lambda key:(-semantic[key],key))
        fused=Counter()
        for ranking in (lexical_ids,semantic_ids):
            for position,memory_id in enumerate(ranking,1):fused[memory_id]+=1/(60+position)
        ranked_ids=sorted(fused,key=lambda key:(-fused[key],-parse_time(by_id[key].current.observed_at).timestamp(),key))
        same_kind=[memory_id for memory_id in ranked_ids
                   if by_id[memory_id].record_type==candidate['record_type']]
        semantic_same_kind=sorted(same_kind,key=lambda key:(-semantic.get(key,-1.0),key))
        semantic_positions={memory_id:position for position,memory_id in enumerate(semantic_same_kind)}
        semantic_margins={}
        for position,memory_id in enumerate(semantic_same_kind):
            score=semantic.get(memory_id)
            runner_up=semantic.get(semantic_same_kind[position+1]) if position+1<len(semantic_same_kind) else None
            semantic_margins[memory_id]=(score-runner_up if score is not None and runner_up is not None
                                         else score if score is not None else None)
        target_reasons={}
        for memory_id in same_kind:
            memory=by_id[memory_id]
            anchor=dict(content=memory.current.content,
                        origin_content=memory.revisions[0].content if memory.revisions else memory.current.content)
            lexical_continuity=concrete_topic_continuity(candidate['content'],anchor,labels)
            score=semantic.get(memory_id);margin=semantic_margins.get(memory_id)
            semantic_continuity=(semantic_positions.get(memory_id)==0 and score is not None and margin is not None
                and score>=engine.settings.memory_update_min_similarity
                and margin>=engine.settings.memory_update_min_margin)
            if lexical_continuity or semantic_continuity:
                target_reasons[memory_id]=dict(
                    lexical_continuity=lexical_continuity,
                    semantic_similarity=score,
                    semantic_margin=margin,
                    semantic_continuity=semantic_continuity)
        targets=[memory_id for memory_id in same_kind if memory_id in target_reasons][:3]
        related=ranked_ids[:6]
        candidate_target_ids[candidate['ref']]=targets
        candidate_related_ids[candidate['ref']]=related
        candidate_rankings[candidate['ref']]=dict(
            keyword=lexical_ids[:6],semantic=semantic_ids[:6],fused=ranked_ids[:6],
            update_targets=targets,update_target_reasons=target_reasons,
            update_min_similarity=engine.settings.memory_update_min_similarity,
            update_min_margin=engine.settings.memory_update_min_margin,
            related_targets=related)
        for memory_id in [*targets,*related]:
            memory=by_id[memory_id]
            if memory not in selected:selected.append(memory)
        seeds=set(targets[:2]);neighbors=[]
        for edge in connections:
            if edge.origin in seeds and edge.target in by_id:neighbors.append(edge.target)
            if edge.target in seeds and edge.origin in by_id:neighbors.append(edge.origin)
        for memory_id in neighbors[:2]:
            memory=by_id[memory_id]
            if memory not in selected:selected.append(memory)
    for subject_id in sorted(pending_candidates):
        candidate=next((memory for memory in selected_pool if subject_id in memory.current.entity_ids),None)
        if candidate is not None and candidate not in selected:selected.append(candidate)
    selected=selected[:MEMORY_LIMIT]

    cards = []
    for memory in selected:
        revision = memory.current
        cards.append(dict(
            id=memory.id,
            revision_id=revision.id,
            record_type=memory.record_type,
            content=revision.content,
            origin_content=memory.revisions[0].content,
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
    prepared['candidate_target_ids']={ref:[memory_id for memory_id in ids
        if memory_id in {card['id'] for card in prepared['history']}]
        for ref,ids in candidate_target_ids.items()}
    prepared['candidate_related_ids']={ref:[memory_id for memory_id in ids
        if memory_id in {card['id'] for card in prepared['history']}]
        for ref,ids in candidate_related_ids.items()}
    needed = {object_id for row in prepared['history'] for object_id in row['entity_ids']}
    catalog = {row['id']: row for row in state['subject_catalog']}
    catalog.update({object_id: objects[object_id] for object_id in needed | confirmed if object_id in objects})
    prepared['subject_catalog'] = list(catalog.values())
    return prepared, dict(
        confirmed_subject_ids=sorted(confirmed),
        pending_candidate_subject_ids=sorted(pending_candidates),
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
        candidate_rankings=candidate_rankings,
        candidate_target_ids=prepared['candidate_target_ids'],
        candidate_related_ids=prepared['candidate_related_ids'],
        **trace,
    )
