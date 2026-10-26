"""Recall memory evidence after subjects are confirmed, with bounded uncertain alternatives."""
from copy import deepcopy
from itertools import zip_longest
from experiments.maintenance_clock import maintenance_clock
from types import SimpleNamespace
from remi.engine import source_visible
from remi.heat import visible_memory
from remi.clock import parse_time
from remi.retrieval import bm25
from remi.identity import normalized_name
from experiments.subject_identity import visible_subject
from experiments.subject_recall import pack_history
from experiments.event_maintenance import memory_fingerprint


def recall_memories(engine,state,bindings,view,*,query_override=None):
    state=deepcopy(state); owner=state['owner']; primary=state['new_user_source']
    clock=maintenance_clock(state)
    confirmed=set(bindings['subject_ids'].values()); uncertain=set()
    for participant in view:
        if participant['status']!='pending':continue
        name=normalized_name(participant['name'])
        for subject in state['subject_catalog']:
            names={normalized_name(subject['name']),*[normalized_name(a['name']) for a in subject.get('aliases',[])]}
            if name in names:uncertain.add(subject['id'])
    seeds=confirmed|uncertain
    objects={obj.id:obj.model_dump() for raw in engine.store.list('objects',owner) if (obj:=visible_subject(raw,owner,clock))}
    objects.update({row['id']:row for row in bindings['entity_rows']})
    selected=[]; originals={}; snapshot_hashes={}; lanes={}
    for raw in engine.store.memories(owner):
        memory=visible_memory(raw,clock)
        if memory is None or memory.record_type=='relation':continue
        ids=set(memory.current.entity_ids)
        lane='unbound' if not ids else 'subjects' if ids&seeds else 'supplement'
        if lane=='supplement' and query_override is None:continue
        lanes[memory.id]=lane
        if not ids<=objects.keys():continue
        sources=engine.store.sources(owner,memory.current.source_ids)
        if len(sources)!=len(set(memory.current.source_ids)) or not all(source_visible(s,clock) for s in sources):continue
        selected.append(memory);originals[memory.id]=[s.model_dump() for s in sources]
        snapshot_hashes[memory.id]=memory_fingerprint(raw)
    query=query_override or primary['content']+'\n'+'\n'.join(s['content'] for s in state.get('context_sources',[]))
    scores=bm25(query,[SimpleNamespace(id=m.id,text=m.current.content) for m in selected])
    # A soft shared-participant bonus keeps topical evidence competitive with unrelated joint events.
    selected.sort(key=lambda m:(-(scores.get(m.id,0)+.5*len(confirmed&set(m.current.entity_ids))),
        -parse_time(m.current.observed_at).timestamp(),m.id))
    # Interleave top-ranked lanes so either lane can use the next available complete-card slot.
    groups=[[m for m in selected if lanes[m.id]==lane] for lane in ('subjects','unbound','supplement')]
    selected=[m for batch in zip_longest(*groups) for m in batch if m is not None]
    if query_override is not None:
        selected.sort(key=lambda m:-scores.get(m.id,0))
        selected=[m for m in selected if scores.get(m.id,0)>0]
    cards=[]
    for memory in selected:
        r=memory.current
        cards.append(dict(id=memory.id,revision_id=r.id,record_type=memory.record_type,content=r.content,
            status=r.status,planned_at=r.planned_at,event_at=r.event_at,mode=r.mode,context=r.context,
            entity_ids=r.entity_ids,sources=originals[memory.id],recall_lane=lanes[memory.id],
            related_memory_ids=[edge.target for edge in engine.store.edges_for(owner,origins=[memory.id])
                                if edge.relation=='related' and edge.revision_id==r.id]))
    if query_override is not None:
        existing=state.get('history',[])
        known={card['id'] for card in existing}
        cards=[*existing,*[card for card in cards if card['id'] not in known]]
    # pack_history receives a clean selection so a failed first card cannot retain stale history.
    prepared,trace=pack_history(engine.store,{**state,'history':[], 'history_limit':8 if query_override is not None else 6},cards)
    prepared['memory_snapshot_hashes']={card['id']:state.get('memory_snapshot_hashes',{}).get(card['id']) or snapshot_hashes[card['id']] for card in prepared['history']}
    # Participant display includes existing event actors, without adding them to current selectable refs.
    needed={oid for row in prepared['history'] for oid in row['entity_ids']}
    catalog={row['id']:row for row in state['subject_catalog']}
    catalog.update({oid:objects[oid] for oid in needed|confirmed if oid in objects})
    prepared['subject_catalog']=list(catalog.values())
    return prepared,dict(confirmed_subject_ids=sorted(confirmed),uncertain_subject_ids=sorted(uncertain),
        eligible_memory_ids=[m.id for m in selected],lanes=lanes,supplement= query_override is not None,**trace)
