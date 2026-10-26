"""Rebuild projections from source-backed patches after out-of-order ingestion.

Original narratives live in maintenance_patch. A branched narrative becomes an explicitly dated
chronicle; structural slots replay independently. This preserves evidence without guessing prose edits.
"""
from copy import deepcopy
from remi.clock import parse_time
from remi.models import Memory, Revision, EvidenceRef, Edge
from remi.graph import project_edges
from remi.identity import stable_id

FIELDS=('content','entity_ids','mode','context','status','planned_at','event_at','time_evidence')


def replay_revisions(store, memories, edges):
    result=[]
    for memory in memories:
        by_id={r.id:r for r in memory.revisions}
        ordered=sorted(memory.revisions,key=lambda r:(parse_time(r.observed_at),parse_time(r.created_at),r.id))
        latest=None; evidence={}; links={}; timeline=[]; branched=False; rebuilt={}
        stored_edges=store.edges_for(memory.owner,origins=[memory.id])
        for original in ordered:
            patch=original.maintenance_patch
            if patch is None:
                # Existing persisted revisions remain readable. Infer explicit field differences
                # against the revision they actually replaced, then retain the immutable snapshot.
                base=by_id.get(original.replaces_revision_id)
                changed=[key for key in FIELDS if base is None or getattr(original,key)!=getattr(base,key)]
                patch=dict(fields=['participants' if k=='entity_ids' else k for k in changed],
                    values={k:deepcopy(getattr(original,k)) for k in FIELDS},participant_mode='replace',
                    participant_ids=original.entity_ids,evidence=[e.model_dump() for e in original.evidence],
                    related_add=[e.target for e in stored_edges if e.revision_id==original.id and e.relation=='related'],
                    related_remove=[],source_id=original.source_ids[0],base_revision_id=original.replaces_revision_id)
            fields=patch['fields']; values=original.model_dump()
            if latest:
                for key in FIELDS:
                    label='participants' if key=='entity_ids' else key
                    if key not in {'content','time_evidence'} and label not in fields:
                        values[key]=deepcopy(getattr(latest,key))
                if 'participants' in fields:
                    ids=patch['participant_ids']; old=latest.entity_ids; mode=patch['participant_mode']
                    values['entity_ids']=list(dict.fromkeys([*old,*ids])) if mode=='add' else [oid for oid in old if oid not in ids] if mode=='remove' else ids
                else:values['entity_ids']=list(latest.entity_ids)
                # Date evidence and precision follow each edited slot independently.
                previous={t.field:t.model_dump() for t in latest.time_evidence}
                for t in patch['values']['time_evidence']:
                    t=t.model_dump() if hasattr(t,'model_dump') else t
                    if t['field'] in fields:previous[t['field']]=t
                values['time_evidence']=list(previous.values())
                selected=previous.get('event_at') or previous.get('planned_at')
                if selected:
                    values['time_resolution']='rule' if selected['value'] else 'unknown'
                    values['time_precision']=selected['precision']
                for field in ('planned_at','event_at'):
                    if field in fields:values[field]=patch['values'][field]
                branched=branched or (original.operation!='support' and patch['base_revision_id']!=latest.id)
            # Evidence-only support advances provenance without duplicating the narrative timeline.
            narrative=patch['values']['content']
            if original.operation!='support':timeline.append('['+original.observed_at+'] '+narrative)
            values['content']='Source-time history:\n'+'\n'.join(timeline) if branched else (latest.content if original.operation=='support' and latest else narrative)
            for ev in patch['evidence']:
                ev=ev.model_dump() if hasattr(ev,'model_dump') else ev
                evidence[ev['source_id']]=ev
            # Put this revision's own source first for source windows and exact quote attribution.
            own=patch['source_id']; ids=list(dict.fromkeys([own,*evidence]))
            values.update(source_ids=ids,evidence=[evidence[sid] for sid in ids],
                replaces_revision_id=latest.id if latest else None,maintenance_patch=patch,
                user_involvement='involved' if stable_id('obj_',memory.owner,'user') in values['entity_ids'] else 'not_involved')
            latest=Revision.model_validate(values);rebuilt[latest.id]=latest
            for mid in patch['related_add']:links[mid]=own
            for mid in patch['related_remove']:links.pop(mid,None)
            edges=[e for e in edges if e.revision_id!=latest.id]
            edges.extend(project_edges(memory,latest,latest.replaces_revision_id))
            for target,sid in links.items():
                edges.append(Edge(id=stable_id('edge_',latest.id,memory.id,target,'related'),owner=memory.owner,
                    origin=memory.id,target=target,relation='related',revision_id=latest.id,memory_id=memory.id,source_id=sid))
        # Keep append order for checkpoints which inspect the just-created revision.
        result.append(Memory.model_validate({**memory.model_dump(),'revisions':[rebuilt[r.id].model_dump() for r in memory.revisions]}))
    return result,edges
