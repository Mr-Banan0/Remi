"""Independent, frozen-input validation of typed memory extraction and append-only snapshots through configured providers."""
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
    first=None; saved_statement=None; prior=None
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
            statements=[m for m in all_memory if m.record_type in {'fact','preference','experience'}]
            unit=engine.store.get('requests',case['id'],request.request_id)['units']
            result['steps'].append({'index':i,'seconds':monotonic()-started,
                'dispositions':[x for c in unit.values() for x in c.get('candidate_dispositions',[])],
                'memory_ids':list(by_id)})
            if i==0:
                check('initial_one_plan_one_state',len(events)==1 and len(statements)==1)
                if len(events)!=1 or len(statements)!=1: break
                first=events[0].id; saved_statement=statements[0].model_dump()
                check('initial_date_is_plan_only',events[0].current.planned_at and events[0].current.planned_at[:10]==step['date'] and events[0].current.event_at is None)
                check('initial_subject_links_exist',bool(events[0].current.entity_ids) and bool(statements[0].current.entity_ids))
            else:
                check(f'step_{i}_all_previous_records_unchanged',all(by_id.get(mid) and by_id[mid].model_dump()==raw for mid,raw in before.items()))
                added=[m for m in all_memory if m.id not in before]
                check(f'step_{i}_all_new_records_have_one_revision',all(len(m.revisions)==1 for m in added))
                if i in {1,2}:
                    expected_status='planned' if i==1 else step['status']
                    check(f'step_{i}_new_event_saved',len(added)==1 and added[0].record_type=='event' and added[0].current.status==expected_status)
                    if len(added)==1:
                        current=added[0].current
                        if i==1:check('new_plan_date',bool(current.planned_at) and current.planned_at[:10]==step['date'])
                        elif step['actual']:check('new_actual_date',bool(current.event_at) and current.event_at[:10]==step['actual'])
                check(f'step_{i}_memory_review_calls',all(c['kind'] in {'add_subjects','add_subjects_repair','add_propose','add_propose_repair'} for c in model.calls))
            if saved_statement:
                check(f'state_unchanged_after_step_{i}',by_id[saved_statement['id']].model_dump()==saved_statement)
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
