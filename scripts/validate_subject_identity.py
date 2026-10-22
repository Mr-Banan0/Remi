"""Direct-original subject-stage diagnostic with real recall and in-memory records."""
from __future__ import annotations
import argparse,hashlib,json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from time import monotonic
from remi.add_pipeline import AddPipeline
from remi.engine import MemoryEngine
from remi.identity import stable_id,normalized_name
from remi.models import AliasEvidence,GraphObject,SubjectMaintenanceDraft
from remi.providers import providers_from_environment
from remi.settings import load_environment
from remi.storage import MemoryStore
from remi.subjects import decode_subjects,materialize_subjects
from scripts.validate_subject_memory import fingerprint


def prepare_state(payload,owner):
    data=deepcopy(payload);clock='2028-02-01T10:00:00+00:00';store=MemoryStore()
    ids={o['id']:stable_id('obj_',owner,'user') if o.get('is_user') else stable_id('obj_',owner,o['id']) for o in data.get('subject_catalog',[])}
    def remap(value,key=None):
        if isinstance(value,list):return [remap(v,key) for v in value]
        if isinstance(value,dict):return {k:remap(v,k) for k,v in value.items()}
        return ids.get(value,value) if isinstance(value,str) and key in {'id','target_id','endpoint_ids','ordered_subject_ids'} else value
    data=remap(data)
    for raw in data.get('subject_catalog',[]):
        obj=GraphObject(id=raw['id'],owner=owner,name='user' if raw.get('is_user') else raw['name'],kind=raw['kind'],is_user=raw.get('is_user',False),
            aliases=[AliasEvidence(name=a['name'],source_id=a.get('source_id','historical'),quote=a['quote'],observed_at=clock) for a in raw.get('aliases',[])],observed_at=clock)
        store.put('objects',owner,obj.id,obj.model_dump())
    source={**data['new_user_source'],'owner':owner,'created_at':clock,'message_at':clock}
    context=[dict(s,owner=owner,created_at=clock,message_at=clock) for s in data.get('context_sources',[])]
    state,_=AddPipeline(MemoryEngine(store))._shared_recall(dict(owner=owner,new_user_source=source,context_sources=context),10**20)
    state['relation_catalog']=[dict(r,endpoint_ids=sorted(r['endpoint_ids']),ordered_subject_ids=r.get('ordered_subject_ids',r['endpoint_ids'])) for r in data.get('relation_catalog',[])]
    return state,store,ids


def evaluate(raw,state,store,groups,ids,relations=(),no_user=False):
    checks=[]
    def check(name,value):checks.append(dict(name=name,passed=bool(value)))
    try:SubjectMaintenanceDraft.model_validate(raw)
    except ValueError:check('output_contract',False);return checks
    state=deepcopy(state);decisions,errors=decode_subjects(raw,state,store)
    check('subject_program_validation',not errors)
    check('alias_program_validation',not state['subject_alias_issues'])
    bound=materialize_subjects(state,decisions,store)
    links,link_errors=AddPipeline(MemoryEngine(store))._connections(raw,state,bound)
    check('connection_program_validation',not link_errors)
    objects=bound['entity_rows'];possible=[]
    for index,group in enumerate(groups):
        wanted={normalized_name(n) for n in group['aliases']};target=ids.get(group.get('target'))
        candidates=[o['id'] for o in objects if wanted<={normalized_name(o['name']),*[normalized_name(a['name']) for a in o['aliases']]}
            and (o['id']==target if target else o['id'] not in ids.values())
            and (not group.get('kind') or o['kind']==group['kind'])
            and ('is_user' not in group or o['is_user']==group['is_user'])]
        possible.append(candidates);check(f'{index}_identity_and_names',bool(candidates))
    def distinct(index,used,selected):
        if index==len(possible):return selected
        for key in possible[index]:
            if key not in used:
                result=distinct(index+1,used|{key},selected+[key])
                if result is not None:return result
        return None
    selected=distinct(0,set(),[])
    check('groups_remain_distinct',selected is not None)
    if not groups:
        check('ambiguous_subjects_stay_unresolved',all(d.target_id is None for d in decisions.values()))
    if no_user:check('quoted_voice_has_no_current_user',not any(o['is_user'] for o in objects))
    for index,expected in enumerate(relations):
        matching=[r for r in raw['connections'] if expected['quote'] in r['quote'] and r['operation']==expected['operation']
            and r['mode']==expected['mode'] and r['target_id']==expected['target_id']]
        check(f'relationship_{index}_disposition',bool(matching))
        if expected['operation']!='pending':
            endpoints=[selected[i] for i in expected['endpoints']] if selected is not None else []
            check(f'relationship_{index}_endpoints',bool(endpoints) and any(
                [bound['subject_ids'].get(str(ref)) for ref in r['subject_refs']]==endpoints for r in matching))
    return checks


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--env-file',type=Path,required=True);parser.add_argument('--workers',type=int,default=2)
    args=parser.parse_args();load_environment(args.env_file);frozen=fingerprint()
    inputs=args.cases.read_bytes();cases=json.loads(inputs)['cases'];args.output.mkdir(parents=True,exist_ok=False)
    (args.output/'inputs.json').write_bytes(inputs)
    manifest=dict(code_sha256=frozen,input_sha256=hashlib.sha256(inputs).hexdigest(),stage='direct_original_subjects',databases_created=0,subject_candidate_extraction_calls=0)
    def run(case):
        model,embedder,ranker=providers_from_environment();state,store,ids=prepare_state(case['payload'],case['id']);start=monotonic()
        result=dict(id=case['id'],checks=[],model_requests=[])
        original_create=model.client.chat.completions.create
        def recorded_create(**kwargs):
            result['model_requests'].append({k:kwargs[k] for k in ['model','temperature','max_completion_tokens','messages','response_format']})
            return original_create(**kwargs)
        model.client.chat.completions.create=recorded_create
        try:
            raw=model.maintain_subjects(state,deadline=monotonic()+120)
            result.update(output=raw,checks=evaluate(raw,state,store,case['groups'],ids,case.get('relations',[]),case.get('no_user',False)))
            for key,field in [('expected_subject_rows','subjects'),('expected_connection_count','connections')]:
                if key in case:result['checks'].append(dict(name=key,passed=len(raw[field])==case[key]))
        except Exception as error:result['error_type']=type(error).__name__
        finally:
            result.update(seconds=monotonic()-start,calls=list(model.calls))
            for resource in [model,embedder,ranker]:
                if resource is not None:resource.close()
        result['passed']=bool(result['checks']) and all(c['passed'] for c in result['checks']) and 'error_type' not in result
        (args.output/(case['id']+'.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2))
        print(json.dumps(dict(id=case['id'],passed=result['passed'],failed=[c['name'] for c in result['checks'] if not c['passed']],error_type=result.get('error_type'))),flush=True)
        return result
    with ThreadPoolExecutor(max_workers=args.workers) as pool:results=list(pool.map(run,cases))
    calls=[c for r in results for c in r['calls']]
    summary=dict(manifest,code_unchanged_during_run=frozen==fingerprint(),cases=len(results),passed_cases=sum(r['passed'] for r in results),
        checks=sum(len(r['checks']) for r in results),passed_checks=sum(c['passed'] for r in results for c in r['checks']),text_calls=len(calls),
        input_tokens=sum(c.get('input_tokens',0) for c in calls),output_tokens=sum(c.get('output_tokens',0) for c in calls))
    (args.output/'summary.json').write_text(json.dumps(summary,indent=2));print(json.dumps(summary),flush=True)

if __name__=='__main__':main()
