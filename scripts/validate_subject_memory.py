"""Independent multi-turn subject/alias/connection acceptance through real providers."""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib,json
from pathlib import Path
from time import monotonic
from remi.clock import parse_time
from remi.engine import MemoryEngine
from remi.identity import normalized_name, stable_id, subject_hits
from remi.models import AddMessage,AddRequest
from remi.providers import providers_from_environment,ProviderUnavailable
from remi.settings import load_environment,Settings
from remi.storage import SQLiteStore


def fingerprint():
    files=sorted([*Path('src').rglob('*.py'),*Path('scripts').glob('*.py')])
    manifest={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    return hashlib.sha256(json.dumps(manifest,sort_keys=True).encode()).hexdigest()


def run_case(case,output):
    model,embedder,ranker=providers_from_environment(); owner=case['id']
    path=output/(owner+'.sqlite3')
    if not path.exists(): SQLiteStore.initialize(path,allow_create_schema=True)
    engine=MemoryEngine(SQLiteStore(path),model,embedder,ranker,Settings(add_timeout=180))
    result={'id':owner,'checks':[],'steps':[]}; known={}
    def check(name,value): result['checks'].append({'name':name,'passed':bool(value)})
    try:
        for i,step in enumerate(case['steps']):
            request=AddRequest(user_id=owner,session_id='independent-subjects',request_id=f'step-{i}',messages=[
                AddMessage(role='user',content=step['text'],timestamp=int(parse_time('2027-05-03T10:00:00Z').timestamp()*1000)+i*3600000)])
            start=monotonic(); failures=[]
            for attempt in range(3):
                try: engine.add(request);break
                except Exception as error:
                    failures.append(type(error).__name__)
                    if not isinstance(error,ProviderUnavailable):break
            progress=engine.store.get('requests',owner,request.request_id)
            check(f'{i}_committed',progress and progress.get('status')=='complete')
            objects=engine.store.list('objects',owner)
            check(f'{i}_subject_kind_scope',all(o['kind'] in {'person','animal','organization'} for o in objects))
            for name in step.get('excluded_subjects',[]):
                check(f'{i}_excluded_subject_{name}',not any(normalized_name(name) in {normalized_name(o['name']),*[normalized_name(a['name']) for a in o['aliases']]} for o in objects))
            for expected in step.get('subjects',[]):
                name=normalized_name(expected['lookup'])
                candidates=[o for o in objects if o['is_user']] if name=='user' else [o.model_dump() for o in subject_hits(engine.store,owner,expected['lookup'],'2027-05-05T00:00:00Z')]
                matches=[o for o in candidates if not expected.get('evidence') or any(expected['evidence'].casefold() in a['quote'].casefold() for a in o['aliases'])]
                check(f'{i}_{expected["key"]}_unique_identity',len(matches)==1)
                if len(matches)!=1:continue
                obj=matches[0];key=expected['key']
                if 'kind' in expected:check(f'{i}_{key}_kind',obj['kind']==expected['kind'])
                if key in known:check(f'{i}_{key}_stable_id',known[key]==obj['id'])
                known[key]=obj['id']
                if 'is_user' in expected:check(f'{i}_{key}_user_attribution',obj['is_user']==expected['is_user'])
                for alias in expected.get('aliases',[]):check(f'{i}_{key}_alias_{alias}',normalized_name(alias) in {normalized_name(a['name']) for a in obj['aliases']})
                for label in expected.get('searches',[]):
                    hits=subject_hits(engine.store,owner,label,'2027-05-05T00:00:00Z')
                    check(f'{i}_{key}_search_{label}',obj['id'] in {h.id for h in hits})
            for a,b in step.get('distinct',[]):check(f'{i}_{a}_{b}_distinct',a in known and b in known and known[a]!=known[b])
            if step.get('no_user'):check(f'{i}_no_false_current_user',not any(o['is_user'] for o in objects))
            if 'object_count' in step:check(f'{i}_object_count',len(objects)==step['object_count'])
            memories=engine.store.memories(owner)
            for term in step.get('memory_contains',[]):
                check(f'{i}_memory_preserves_{term}',any(term in m.current.content for m in memories))
            for expected in step.get('links',[]):
                ids=sorted([known.get(expected['a']),known.get(expected['b'])]) if known.get(expected['a']) and known.get(expected['b']) else []
                found=[m for m in memories if m.endpoint_ids==ids and ids and m.connection_evidence and expected.get('contains','').casefold() in m.connection_evidence.quote.casefold()]
                check(f'{i}_connection_{expected["a"]}_{expected["b"]}_{expected.get("contains","")}',bool(found))
            for m in memories:
                check(f'{i}_card_{m.id}_mentions_match',set(m.entity_ids)=={e.target for e in engine.store.edges_for(owner) if e.revision_id==m.current.id and e.relation=='mentions'})
                if m.endpoint_ids:
                    es=[e for e in engine.store.edges_for(owner) if e.revision_id==m.current.id and e.relation=='related']
                    check(f'{i}_card_{m.id}_connection_valid',len(es)==1 and sorted([es[0].origin,es[0].target])==m.endpoint_ids and m.connection_evidence.quote in engine.store.get('sources',owner,m.connection_evidence.source_id)['content'])
            units=(progress or {}).get('units',{})
            result['steps'].append({'index':i,'seconds':monotonic()-start,'failures':failures,'objects':len(objects),'memories':len(memories),
                'pending_subjects':sum(d['operation']=='pending' for u in units.values() for d in u.get('subject_dispositions',[])),
                'pending_memories':sum(d['status']=='pending' for u in units.values() for d in u.get('candidate_dispositions',[]))})
            if not progress or progress.get('status')!='complete':break
        rows={t:engine.store.list(t,owner) for t in ['sources','objects','memories','edges','requests']}
        (output/(owner+'-records.json')).write_text(json.dumps(rows,ensure_ascii=False,indent=2))
        result['provider_calls']={'text':list(model.calls),'embedding':list(embedder.calls)}
        result['passed']=len(result['steps'])==len(case['steps']) and all(c['passed'] for c in result['checks'])
    finally:engine.close()
    (output/(owner+'-result.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps({'id':owner,'passed':result['passed'],'failed':[c['name'] for c in result['checks'] if not c['passed']]},ensure_ascii=False),flush=True)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--env-file',type=Path,required=True);parser.add_argument('--workers',type=int,default=2)
    args=parser.parse_args();load_environment(args.env_file);raw=args.cases.read_bytes();cases=json.loads(raw)['cases']
    args.output.mkdir(parents=True,exist_ok=False);frozen=fingerprint()
    (args.output/'inputs.json').write_bytes(raw)
    (args.output/'manifest.json').write_text(json.dumps({'code_sha256':frozen,'input_sha256':hashlib.sha256(raw).hexdigest(),'expectations_enter_runtime':False,'tables':6},indent=2))
    with ThreadPoolExecutor(max_workers=args.workers) as pool:results=list(pool.map(lambda c:run_case(c,args.output),cases))
    report={'code_sha256':frozen,'code_unchanged_during_run':frozen==fingerprint(),'cases':len(results),'passed_cases':sum(r['passed'] for r in results),
        'checks':sum(len(r['checks']) for r in results),'passed_checks':sum(c['passed'] for r in results for c in r['checks']),
        'text_calls':sum(len(r['provider_calls']['text']) for r in results),'repair_calls':sum('repair' in c['kind'] for r in results for c in r['provider_calls']['text']),
        'mean_add_seconds':sum(s['seconds'] for r in results for s in r['steps'])/sum(len(r['steps']) for r in results)}
    (args.output/'summary.json').write_text(json.dumps(report,indent=2));print(json.dumps(report),flush=True)

if __name__=='__main__':main()
