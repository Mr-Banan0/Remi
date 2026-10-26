"""Bounded recovery ledger in existing request checkpoints, with original source provenance."""
from types import SimpleNamespace
from remi.identity import stable_id
from remi.clock import parse_time
from remi.retrieval import bm25

RECOVERY_LIMIT=2


def pending_jobs(store,owner,clarification):
    requests=store.list('requests',owner); resolved=set(); records=[]
    for request in requests:
        if request.get('status')=='resolved':resolved.add(request['request_id'])
        for unit in request.get('units',{}).values():
            if unit.get('phase')=='indexed' and unit.get('resolved_pending'):
                resolved.add(unit['resolved_pending']['key'])
    for request in requests:
        if request.get('recovery') or request.get('request_id')==clarification.request_id:continue
        for unit_key,unit in request.get('units',{}).items():
            if unit.get('phase')!='indexed':continue
            state=unit.get('shared_recall',{});source=state.get('new_user_source')
            if not source:continue
            stored=store.get('sources',owner,source['id'])
            if not stored or stored['session_id']!=clarification.session_id:continue
            if parse_time(source.get('message_at') or source['created_at'])>parse_time(clarification.message_at or clarification.created_at):continue
            for disposition in unit.get('candidate_dispositions',[]):
                if disposition['status']!='pending':continue
                index=disposition['candidate_id']; item=unit.get('memory_plan',{}).get(index)
                if not item:continue
                key=stable_id('pending_',request['request_id'],unit_key,index)
                if key in resolved:continue
                records.append(dict(key=key,content=item['content'],reason=disposition['reason'],
                    source=stored,source_window=state.get('source_window'),claim_type=item['record_type'],
                    document_id=stable_id('doc_pending_',source['id'],index,item['content']),
                    clarification=clarification.model_dump()))
    preceding={s.id for s in store.context_before(owner,clarification.session_id,clarification,limit=1)}
    scores=bm25(clarification.content,[SimpleNamespace(id=r['key'],text=r['content']) for r in records])
    for record in records:
        if record['source']['id'] in preceding:scores[record['key']]=scores.get(record['key'],0)+1
    records=[r for r in records if scores.get(r['key'],0)>0]
    records.sort(key=lambda r:(-scores.get(r['key'],0),r['key']))
    return records[:RECOVERY_LIMIT]
