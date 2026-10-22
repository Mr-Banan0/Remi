"""Independent V33 subject/connection experiment; all records use MemoryStore."""
from __future__ import annotations
import argparse,hashlib,json,statistics
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from time import monotonic
from copy import deepcopy

from experiments.subject_connections import maintain,review,commit,experiment_state,MEMORY_PROMPT
from scripts.validate_subject_identity import prepare_state
from scripts.validate_direct_subject_memory import CASES
from remi.identity import normalized_name
from remi.providers import providers_from_environment
from remi.settings import load_environment
from remi.storage import MemoryStore
from remi.add_pipeline import AddPipeline
from remi.engine import MemoryEngine
from remi.models import ProposalDraft


def fingerprint():
    paths=sorted([*Path('src').rglob('*.py'),*Path('scripts').glob('*.py'),*Path('experiments').glob('*.py')])
    data={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    return hashlib.sha256(json.dumps(data,sort_keys=True).encode()).hexdigest()


def evaluate(case,raw,result,ids):
    checks=[]
    def check(name,value):checks.append(dict(name=name,passed=bool(value)))
    check('subject_program_validation',not result['contract_errors'] and not result['subject_errors'])
    check('alias_program_validation',not result['alias_issues'])
    check('connection_program_validation',not result['connection_errors'])
    groups=case['groups'];objects=result['bindings']['entity_rows'];possible=[]
    for index,group in enumerate(groups):
        wanted={normalized_name(n) for n in group['aliases']};target=ids.get(group.get('target'))
        matches=[o['id'] for o in objects if wanted<={normalized_name(o['name']),*[normalized_name(a['name']) for a in o['aliases']]}
            and (o['id']==target if target else o['id'] not in ids.values())
            and (not group.get('kind') or o['kind']==group['kind'])
            and ('is_user' not in group or o['is_user']==group['is_user'])]
        possible.append(matches);check(f'{index}_identity_and_names',bool(matches))
    def distinct(index,used,selected):
        if index==len(possible):return selected
        for key in possible[index]:
            if key not in used:
                found=distinct(index+1,used|{key},selected+[key])
                if found is not None:return found
        return None
    selected=distinct(0,set(),[]);check('groups_remain_distinct',selected is not None)
    if not groups:check('ambiguous_subjects_stay_unresolved',not result['bindings']['subject_ids'])
    if case.get('no_user'):check('quoted_voice_has_no_current_user',not any(o['is_user'] for o in objects))
    pairs={tuple(row['endpoint_ids']) for row in result['connections']}
    expected=set()
    for relation in case.get('relations',[]):
        if relation['operation']=='pending':
            check('required_connection_waits_for_identity',bool(result['pending']))
        else:
            pair=tuple(sorted(selected[i] for i in relation['endpoints'])) if selected is not None else ()
            expected.add(pair)
    for index,pair in enumerate(sorted(expected)):
        check(f'undirected_connection_{index}_covered',bool(pair) and pair in pairs)
    for key,field in [('expected_subject_rows','subjects'),('expected_connection_count','connections')]:
        if key in case:
            # Current rule permits the actual user and another person to share a concrete meeting.
            # The old holdout marked all links absent; its identity requirement stays unchanged.
            if key=='expected_connection_count' and case['id']=='holdout-different-namesake':continue
            check(key,len(raw.get(field,[]))==case[key])
    identity=all(c['passed'] for c in checks if c['name'].endswith('_identity_and_names') or c['name'] in {'groups_remain_distinct','ambiguous_subjects_stay_unresolved'})
    return checks,identity


def run_direct(case,output):
    model,embedder,ranker=providers_from_environment();state,store,ids=prepare_state(case['payload'],case['id'])
    state=experiment_state(state,store);start=monotonic();result=dict(id=case['id'],checks=[])
    try:
        raw,request=maintain(model,state,deadline=monotonic()+120)
        reviewed=review(raw,state,store);checks,identity=evaluate(case,raw,reviewed,ids)
        commit(reviewed,store)
        result.update(output=raw,model_requests=[request],checks=checks,identity_and_names_complete=identity,
            source=state['new_user_source']['content'],subject_errors=reviewed['subject_errors'],contract_errors=reviewed['contract_errors'],
            alias_issues=reviewed['alias_issues'],connection_errors=reviewed['connection_errors'],pending=reviewed['pending'],
            actual_subjects=reviewed['bindings']['entity_rows'],connections=store.list('edges',case['id']))
    except Exception as error:result['error_type']=type(error).__name__
    finally:
        result.update(seconds=monotonic()-start,calls=list(model.calls))
        for resource in [model,embedder,ranker]:
            if resource is not None:resource.close()
    result['passed']=bool(result['checks']) and all(c['passed'] for c in result['checks']) and 'error_type' not in result
    (output/(case['id']+'.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps(dict(id=case['id'],passed=result['passed'],failed=[c['name'] for c in result['checks'] if not c['passed']],error_type=result.get('error_type'))),flush=True)
    return result


def step_state(store,owner,text,index,earlier):
    clock=f'2028-02-01T{10+index:02d}:00:00+00:00'
    primary=dict(id=f'step-{index}',owner=owner,role='user',content=text,message_at=clock,created_at=clock)
    context=earlier[-2:]
    state,_=AddPipeline(MemoryEngine(store))._shared_recall(dict(owner=owner,new_user_source=primary,context_sources=context),monotonic()+20)
    return experiment_state(state,store)


def run_sequential(output):
    results=[]
    for name,texts in CASES:
        model,embedder,ranker=providers_from_environment();store=MemoryStore();earlier=[];steps=[];checks=[];before=None
        def check(label,value):checks.append(dict(name=label,passed=bool(value)))
        try:
            for index,text in enumerate(texts):
                state=step_state(store,name,text,index,earlier);raw,request=maintain(model,state,deadline=monotonic()+120)
                reviewed=review(raw,state,store);requests=[request]
                errors=[*reviewed['contract_errors'],*reviewed['subject_errors'].values(),*reviewed['connection_errors'].values()]
                if errors:
                    raw,request=maintain(model,{**state,'repair_errors':[dict(error=e) for e in errors]},deadline=monotonic()+120)
                    reviewed=review(raw,state,store);requests.append(request)
                commit(reviewed,store);objects=store.list('objects',name);edges=store.list('edges',name)
                check(f'{index}_program_checks',not reviewed['contract_errors'] and not reviewed['subject_errors'] and not reviewed['alias_issues'] and not reviewed['connection_errors'])
                if name=='alias_pet':
                    check(f'{index}_person_aliases',any({'Rhea','Ray'}<={a['name'] for a in o['aliases']} for o in objects))
                    if index==1:
                        check('animal_aliases',any(o['kind']=='animal' and {'Moss','Momo'}<={a['name'] for a in o['aliases']} for o in objects))
                        person=next((o['id'] for o in objects if o['kind']=='person' and not o['is_user']),None)
                        pet=next((o['id'] for o in objects if o['kind']=='animal'),None)
                        check('ownership_navigation',person is not None and pet is not None and any(set(e['endpoint_ids'])=={person,pet} for e in edges))
                elif name=='two_namesakes':check('two_distinct_people',len([o for o in objects if not o['is_user'] and o['kind']=='person'])==2)
                elif name=='employment':
                    check(f'{index}_employment_navigation',len(edges)==1)
                    if index==0:before=deepcopy(edges[0]) if edges else None
                    else:check('same_edge_appends_evidence',before is not None and len(edges)==1 and edges[0]['id']==before['id'] and set(edges[0]['source_ids'])=={'step-0','step-1'})
                else:
                    check('no_false_api_user',not any(o['is_user'] for o in objects))
                    check('quoted_owner_navigation',len(edges)==1 and len(edges[0]['endpoint_ids'])==2)
                steps.append(dict(index=index,source=text,output=raw,model_requests=requests,connections=deepcopy(edges),pending=reviewed['pending'],errors=errors))
                earlier.append(state['new_user_source'])
        except Exception as error:check('exception_'+type(error).__name__,False)
        finally:
            result=dict(id=name,passed=bool(checks) and all(c['passed'] for c in checks),checks=checks,steps=steps,calls=list(model.calls),
                records={table:store.list(table,name) for table in ['sources','objects','edges','memories']})
            for resource in [model,embedder,ranker]:
                if resource is not None:resource.close()
        (output/(name+'.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2));results.append(result)
    return results


MEMORY_CASES=[
 ('work','I work at Cedar Guild. I no longer work at Pine Guild.', ['work at Cedar Guild','no longer work at Pine Guild']),
 ('ownership','Livia owns the dog Fern.', ['owns']),
 ('wish',"Tessa wrote: 'I hope to join Brook Guild.'", ['hope']),
]


def run_memory(output):
    results=[]
    for name,text,terms in MEMORY_CASES:
        model,embedder,ranker=providers_from_environment();store=MemoryStore();state=step_state(store,'content-'+name,text,0,[])
        result=dict(id=name,source=text,checks=[])
        wire_requests=[];original_create=model.client.chat.completions.create
        def captured_create(**kwargs):
            wire_requests.append({key:kwargs[key] for key in ['model','temperature','max_completion_tokens','messages','response_format']})
            return original_create(**kwargs)
        model.client.chat.completions.create=captured_create
        try:
            raw,request=maintain(model,state,deadline=monotonic()+120);reviewed=review(raw,state,store);commit(reviewed,store)
            resolved=[]
            for subject in reviewed['state']['proposal']['subjects']:
                resolved.append(dict(ref=subject['ref'],id=reviewed['bindings']['subject_ids'].get(str(subject['ref'])),name=subject['name'],kind=subject['kind'],aliases=subject['aliases'],status='resolved' if str(subject['ref']) in reviewed['bindings']['subject_ids'] else 'pending'))
            extracted=model._json(MEMORY_PROMPT,{**reviewed['state'],'resolved_subjects':resolved},ProposalDraft,'add_propose',monotonic()+120)
            plan=ProposalDraft.model_validate(extracted)
            body=' '.join(item.content for item in plan.items)
            result.update(subject_output=raw,memory_output=extracted,model_requests=wire_requests)
            result['checks']=[dict(name='all_relationship_content_retained',passed=all(term.casefold() in body.casefold() for term in terms)),
                dict(name='only_four_memory_types',passed=all(item.record_type in {'fact','preference','experience','event'} for item in plan.items)),
                dict(name='original_mode_retained',passed=any(item.mode=='document' for item in plan.items) if name=='wish' else all(item.mode=='asserted' for item in plan.items)),
                dict(name='current_source_cited',passed=all(any(e.source_id==state['new_user_source']['id'] for e in item.evidence) for item in plan.items))]
        except Exception as error:result['error_type']=type(error).__name__
        finally:
            result['calls']=list(model.calls)
            for resource in [model,embedder,ranker]:
                if resource is not None:resource.close()
        result['passed']=bool(result['checks']) and all(c['passed'] for c in result['checks']) and 'error_type' not in result
        (output/(name+'.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2));results.append(result)
    return results


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--cases',type=Path);parser.add_argument('--mode',choices=['direct','sequential','memory'],default='direct')
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('--env-file',type=Path,required=True);parser.add_argument('--workers',type=int,default=2)
    args=parser.parse_args();load_environment(args.env_file);args.output.mkdir(parents=True,exist_ok=False);frozen=fingerprint()
    if args.mode=='direct':
        inputs=args.cases.read_bytes();(args.output/'inputs.json').write_bytes(inputs);cases=json.loads(inputs)['cases']
        with ThreadPoolExecutor(max_workers=args.workers) as pool:results=list(pool.map(lambda c:run_direct(c,args.output),cases))
    elif args.mode=='sequential':
        (args.output/'inputs.json').write_text(json.dumps(CASES,ensure_ascii=False,indent=2));results=run_sequential(args.output)
    else:
        (args.output/'inputs.json').write_text(json.dumps(MEMORY_CASES,ensure_ascii=False,indent=2));results=run_memory(args.output)
    calls=[c for row in results for c in row['calls']]
    summary=dict(experiment='V33-undirected-subject-connections',code_sha256=frozen,code_unchanged_during_run=frozen==fingerprint(),
        cases=len(results),passed_cases=sum(row['passed'] for row in results),checks=sum(len(row['checks']) for row in results),
        passed_checks=sum(c['passed'] for row in results for c in row['checks']),text_calls=len(calls),
        input_tokens=sum(c.get('input_tokens',0) for c in calls),output_tokens=sum(c.get('output_tokens',0) for c in calls),
        mean_call_seconds=statistics.mean(c['seconds'] for c in calls) if calls else None,databases_created=0,
        ordinary_model_calls=sum(c['kind']=='add_propose' for c in calls),production_pipeline='V32 unchanged')
    (args.output/'summary.json').write_text(json.dumps(summary,indent=2));print(json.dumps(summary),flush=True)

if __name__=='__main__':main()
