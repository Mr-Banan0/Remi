"""Independent, frozen-input validation of stored memory changes through configured providers."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
from statistics import mean
from time import monotonic

from remi.clock import parse_time
from remi.engine import MemoryEngine
from remi.models import AddMessage, AddRequest, SearchRequest
from remi.providers import providers_from_environment
from remi.settings import Settings, load_environment
from remi.storage import SQLiteStore


def fingerprint():
    files = sorted([*Path('src').rglob('*.py'),*Path('scripts').glob('*.py')])
    manifest = {str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    return hashlib.sha256(json.dumps(manifest,sort_keys=True).encode()).hexdigest()


def validate_case(case, output):
    model, embedder, ranker = providers_from_environment()
    path=output/(case['id']+'.sqlite3')
    SQLiteStore.initialize(path,allow_create_schema=True)
    engine=MemoryEngine(SQLiteStore(path),model,embedder,ranker,Settings(add_timeout=180))
    result={'id':case['id'],'steps':[],'checks':[],'provider_calls':{},'database':str(path)}
    first=None; relation=None; prior=None
    def check(name, passed):
        result['checks'].append({'name':name,'passed':bool(passed)})
    try:
        for i,step in enumerate(case['steps']):
            before={m.id:m.model_dump() for m in engine.store.memories(case['id'])}
            request=AddRequest(user_id=case['id'],session_id='independent-conversation',request_id=f'step-{i}',messages=[
                AddMessage(role='user',content=step['text'],timestamp=int(parse_time(step['at']).timestamp()*1000))])
            started=monotonic()
            try:
                engine.add(request)
            except Exception as error:
                result['steps'].append({'index':i,'seconds':monotonic()-started,'error_type':type(error).__name__})
                check(f'step_{i}_committed',False)
                break
            all_memory=engine.store.memories(case['id']); by_id={m.id:m for m in all_memory}
            events=[m for m in all_memory if m.record_type=='event']
            relations=[m for m in all_memory if m.record_type=='relation']
            unit=engine.store.get('requests',case['id'],request.request_id)['units']
            result['steps'].append({'index':i,'seconds':monotonic()-started,
                'dispositions':[x for c in unit.values() for x in c.get('candidate_dispositions',[])],
                'memory_ids':list(by_id)})
            if i==0:
                check('initial_one_plan_one_state',len(events)==1 and len(relations)==1)
                if len(events)!=1 or len(relations)!=1: break
                first=events[0].id; relation=relations[0].model_dump()
                check('initial_date_is_plan_only',events[0].current.planned_at and events[0].current.planned_at[:10]==step['date'] and events[0].current.event_at is None)
                check('initial_subject_links_exist',bool(events[0].current.entity_ids) and bool(relations[0].current.entity_ids))
            elif i==1:
                m=by_id[first]
                check('reschedule_reuses_occurrence',len(events)==1 and len(m.revisions)==2)
                check('reschedule_changes_planned_date',m.current.planned_at and m.current.planned_at[:10]==step['date'] and m.current.event_at is None)
                old=before[first]['revisions'][-1]
                check('reschedule_preserves_local_fields',all(getattr(m.current,k)==old[k] for k in ['content','entity_ids','mode','context']))
            elif i==2:
                m=by_id[first]
                check('terminal_reuses_occurrence',len(events)==1 and len(m.revisions)==3)
                check('terminal_state_correct',m.current.status==step['status'])
                check('terminal_date_correct',m.current.planned_at and m.current.planned_at[:10]=='2027-03-12' and
                      ((m.current.event_at or '')[:10]==step['actual'] if step['actual'] else m.current.event_at is None))
                check('terminal_keeps_all_evidence',len(m.current.source_ids)==3)
            elif i in {3,4}:
                check(f'new_occurrence_{i}_has_new_identity',len(events)==i-1 and by_id[first].model_dump()==before[first])
            else:
                check('ambiguous_reference_keeps_existing_graph',before=={m.id:m.model_dump() for m in all_memory})
                check('ambiguous_reference_is_pending',any(d['status']=='pending' for d in result['steps'][-1]['dispositions']))
            if relation:
                check(f'state_unchanged_after_step_{i}',by_id[relation['id']].model_dump()==relation)
            prior=all_memory
        try:
            found=engine.search(SearchRequest(user_id=case['id'],query='Find source statements relevant to: '+case['steps'][0]['text'],top_k=3),case['steps'][-1]['at'])
            sources=engine.store.list('sources',case['id'])
            check('raw_source_search_available',bool(found.data) and any(s['content'] in item.content for s in sources for item in found.data))
            result['search']={'count':len(found.data),'degradation':engine.last_search.get('degradation',[]),'data':[x.model_dump() for x in found.data]}
        except Exception as error:
            check('raw_source_search_available',False)
            result['search']={'error_type':type(error).__name__}
        rows={name:engine.store.list(name,case['id']) for name in ['sources','objects','memories','edges','requests']}
        (output/(case['id']+'-records.json')).write_text(json.dumps(rows,ensure_ascii=False,indent=2))
        result['provider_calls']={'text':list(model.calls),'embedding':list(embedder.calls),'reranker':list(ranker.calls)}
        result['passed']=len(result['steps'])==len(case['steps']) and all(c['passed'] for c in result['checks'])
    finally:
        engine.close()
    (output/(case['id']+'-result.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps({'id':case['id'],'passed':result.get('passed',False),'checks':len(result['checks']),
                     'failed':[c['name'] for c in result['checks'] if not c['passed']]},ensure_ascii=False),flush=True)
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--cases',type=Path,required=True); p.add_argument('--output',type=Path,required=True)
    p.add_argument('--env-file',type=Path,required=True); p.add_argument('--limit',type=int); p.add_argument('--workers',type=int,default=2)
    args=p.parse_args(); load_environment(args.env_file)
    raw=args.cases.read_bytes(); cases=json.loads(raw)['cases']
    if args.limit: cases=cases[:args.limit]
    args.output.mkdir(parents=True,exist_ok=False)
    frozen=fingerprint()
    (args.output/'inputs.json').write_bytes(raw)
    (args.output/'manifest.json').write_text(json.dumps({'code_sha256':frozen,'input_sha256':hashlib.sha256(raw).hexdigest(),
        'selected_cases':[c['id'] for c in cases],'model':'gpt-4o-mini','tables':6,'expectations_enter_runtime':False},indent=2))
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results=list(pool.map(lambda c:validate_case(c,args.output),cases))
    report={'code_sha256':frozen,'code_unchanged_during_run':frozen==fingerprint(),'cases':len(results),
        'passed_cases':sum(r.get('passed',False) for r in results),
        'checks':sum(len(r['checks']) for r in results),'passed_checks':sum(c['passed'] for r in results for c in r['checks']),
        'mean_add_seconds':mean(s['seconds'] for r in results for s in r['steps']),
        'text_calls':sum(len(r['provider_calls']['text']) for r in results),
        'repair_calls':sum('repair' in c['kind'] for r in results for c in r['provider_calls']['text'])}
    (args.output/'summary.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(report),flush=True)


if __name__=='__main__': main()
