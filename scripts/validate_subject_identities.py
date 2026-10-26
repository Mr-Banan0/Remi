"""Independent V49 subject name and kind experiment; all records use MemoryStore."""
from __future__ import annotations
import argparse,hashlib,json,statistics
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from time import monotonic
from copy import deepcopy

from experiments.subject_maintenance import maintain,review,commit
from experiments.subject_references import prepare_reference_state
from remi.add_judgments import PROPOSE_PROMPT
from scripts.validate_subject_identity import prepare_state as prepare_legacy_fixture
from experiments.subject_identity import CONTEXT_LIMIT, SubjectRecord, recall_state, matching_subjects
from experiments.subject_repair import build_repair_state,finish_repair
from scripts.validate_direct_subject_memory import CASES
from remi.identity import normalized_name
from remi.providers import providers_from_environment
from remi.settings import load_environment
from remi.storage import MemoryStore
from remi.add_pipeline import AddPipeline
from remi.engine import MemoryEngine
from remi.models import ProposalDraft


def experiment_state(state,store):
    """Discard relationship fixture fields before identity-only evaluation."""
    state=deepcopy(state);state.pop('relation_catalog',None);state.pop('existing_connections',None)
    from experiments.subject_recall import maintenance_route
    primary=state['new_user_source'];clock=primary.get('message_at') or primary.get('created_at')
    hits=matching_subjects(store,state['owner'],[primary['content']],clock,limit=None)
    state['maintenance_route']=maintenance_route(hits,primary['content'])
    return prepare_reference_state(state,store)


MEMORY_PROMPT=PROPOSE_PROMPT.replace(
    'The preceding stage handles identity aliases and continuing relationships between subjects. Extract distinct\n'
    'facts,preferences,lessons and events present alongside them. A source containing only naming or continuing\n'
    'relationships, or an acknowledgement without new useful content, can return items=[].',
    'The preceding stage handles subject identities. Extract continuing\n'
    'relationship statements as facts and relationship changes as events, preserving participants, source mode\n'
    'and attribution. Preserve preferences and lessons as their appropriate types.\n'
    'A naming-only statement or acknowledgement without new useful content can return items=[].')


# Fixture adaptation stays in the evaluator; model decisions use the V49 contract only.
def prepare_state(payload,owner):
    legacy=deepcopy(payload)
    for row in legacy.get('subject_catalog',[]):
        if row['kind']=='individual':row['kind']='person'
        if row.get('name') is None:row['name']='__unnamed_subject__'
    state,store,ids=prepare_legacy_fixture(legacy,owner)
    for raw in store.list('objects',owner):
        row={key:value for key,value in raw.items() if key!='schema_version'}
        row['kind']='person' if raw['kind']=='individual' else raw['kind']
        original=next((s for s in payload.get('subject_catalog',[]) if ids.get(s['id'])==raw['id']),{})
        row['name']=original.get('name',row.get('name'))
        row['aliases']=[alias for alias in row.get('aliases',[])
                        if normalized_name(alias['name'])!=normalized_name(row.get('name') or '')]
        row['references']=deepcopy(original.get('references',[]))
        if not row['references']:
            evidence=next((alias for alias in raw.get('aliases',[]) if alias.get('quote')),None)
            if evidence:
                row['references']=[dict(source_id=evidence.get('source_id','historical'),quote=evidence['quote'],
                    observed_at=evidence.get('observed_at',row['observed_at']))]
        obj=SubjectRecord.model_validate(row)
        store.put('objects',owner,obj.id,obj.model_dump())
    for source in state['source_catalog'].values():store.put('sources',owner,source['id'],source)
    for obj in store.list('objects',owner):
        for evidence in [*obj['aliases'],*obj.get('references',[])]:
            original=next((source for source in state['source_catalog'].values() if evidence['quote'] in source['content']),None)
            if original is None:
                sid='fixture-'+str(len(state['source_catalog']))
                original=dict(state['new_user_source'],id=sid,content=evidence['quote'])
                state['source_catalog'][sid]=original;store.put('sources',owner,sid,original)
            evidence['source_id']=original['id']
        store.put('objects',owner,obj['id'],obj)
    requested=[ids[row['id']] for row in payload.get('subject_catalog',[]) if row.get('id') in ids]
    state['subject_catalog']=[store.get('objects',owner,key) for key in requested]
    from experiments.subject_references import prepare_reference_state
    return experiment_state(prepare_reference_state(state,store),store),store,ids

EXTRA_CASES=[
 ('animal_name',['My dog Tansy is also called Tan.','Tansy is sleeping.','Tan is resting.']),
 ('role_memory_boundary',['My colleague Selene is a designer at Alder Cooperative.',
                          'Selene is now a painter, and is also called Seli.','Seli sent a message.']),
 ('shared_role_memory',['Two colleagues work with me: a nurse named Esra and a baker named Esra.',
                        'The person named Esra sent a message.']),
 ('cross_kind_namesake',['My cousin Vale and my dog Vale are different individuals.',
                         'My dog Vale needs a walk.']),
]


def fingerprint():
    paths=sorted([*Path('src').rglob('*.py'),*Path('scripts').glob('*.py'),*Path('experiments').glob('*.py'),*Path('experiments').glob('*.json')])
    data={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    return hashlib.sha256(json.dumps(data,sort_keys=True).encode()).hexdigest()


def evaluate(case,raw,result,ids):
    checks=[]
    def check(name,value):checks.append(dict(name=name,passed=bool(value)))
    check('subject_program_validation',not result['contract_errors'] and not result['subject_errors'])
    check('alias_program_validation',not result['alias_issues'])
    groups=case['groups'];objects=result['bindings']['entity_rows'];possible=[]
    for index,group in enumerate(groups):
        wanted={normalized_name(n) for n in group['aliases']};target=ids.get(group.get('target'))
        matches=[o['id'] for o in objects if wanted<={*[normalized_name(n) for n in [o.get('name')] if n],*[normalized_name(a['name']) for a in o['aliases']]}
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
    if 'expected_subject_rows' in case:check('expected_subject_rows',len(raw.get('subjects',[]))==case['expected_subject_rows'])
    identity=all(c['passed'] for c in checks if c['name'].endswith('_identity_and_names') or c['name'] in {'groups_remain_distinct','ambiguous_subjects_stay_unresolved'})
    return checks,identity


def run_direct(case,output):
    model,embedder,ranker=providers_from_environment();state,store,ids=prepare_state(case['payload'],case['id'])
    start=monotonic();result=dict(id=case['id'],checks=[])
    try:
        raw,request=maintain(model,state,deadline=monotonic()+120)
        reviewed=review(raw,state,store);requests=[request];attempt_outputs=[raw]
        initial_issues=deepcopy(reviewed['issues'])
        if initial_issues:
            repair=build_repair_state(raw,reviewed,state)
            raw,request=maintain(model,repair,deadline=monotonic()+120)
            reviewed=finish_repair(repair,raw,state,store);requests.append(request);attempt_outputs.append(raw)
        checks,identity=evaluate(case,raw,reviewed,ids)
        commit(reviewed,store)
        result.update(output=raw,attempt_outputs=attempt_outputs,model_requests=requests,initial_issues=initial_issues,
            final_issues=reviewed['issues'],checks=checks,identity_and_names_complete=identity,
            source=state['new_user_source']['content'],subject_errors=reviewed['subject_errors'],contract_errors=reviewed['contract_errors'],
            alias_issues=reviewed['alias_issues'],
            actual_subjects=reviewed['bindings']['entity_rows'])
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
    return recall_state(store,owner,primary,earlier[-CONTEXT_LIMIT:])


def run_sequential(output,extra=False):
    results=[]
    for name,texts in (EXTRA_CASES if extra else CASES):
        model,embedder,ranker=providers_from_environment();store=MemoryStore();earlier=[];steps=[];checks=[];before=None
        def check(label,value):checks.append(dict(name=label,passed=bool(value)))
        try:
            for index,text in enumerate(texts):
                state=step_state(store,name,text,index,earlier);raw,request=maintain(model,state,deadline=monotonic()+120)
                reviewed=review(raw,state,store);requests=[request];attempt_outputs=[raw]
                initial_issues=deepcopy(reviewed['issues'])
                if initial_issues:
                    repair=build_repair_state(raw,reviewed,state)
                    raw,request=maintain(model,repair,deadline=monotonic()+120)
                    reviewed=finish_repair(repair,raw,state,store);requests.append(request);attempt_outputs.append(raw)
                commit(reviewed,store);objects=store.list('objects',name)
                check(f'{index}_program_checks',not reviewed['contract_errors'] and not reviewed['subject_errors'] and not reviewed['alias_issues'])
                if name=='alias_pet':
                    check(f'{index}_person_names',any(o['name']=='Rhea' and 'Ray' in {a['name'] for a in o['aliases']} for o in objects))
                    if index==1:
                        rhea=next(o for o in objects if o['name']=='Rhea')
                        selected=set(reviewed['bindings']['subject_ids'].values())
                        check('alias_reuses_existing_person',rhea['id'] in selected)
                        check('context_only_user_excluded',not any(o['is_user'] and o['id'] in selected for o in objects))
                        check('animal_names',any(o['name']=='Moss' and o['kind']=='animal'
                            and 'Momo' in {a['name'] for a in o['aliases']} for o in objects))
                elif name=='two_namesakes':check('two_distinct_people',len([o for o in objects if not o['is_user'] and o['kind']=='person'])==2)
                elif name=='employment':
                    check(f'{index}_stable_employment_subjects',len(objects)==2 and any(o['is_user'] for o in objects)
                          and any(o['kind']=='organization' for o in objects))
                elif name=='animal_name':
                    pets=[o for o in objects if o['name']=='Tansy']
                    check(f'{index}_stable_pet',len(pets)==1)
                    if pets:
                        pet=pets[0]
                        check(f'{index}_animal_kind',pet['kind']=='animal')
                        if index>0:check(f'{index}_reuses_pet',pet['id'] in reviewed['bindings']['subject_ids'].values())
                elif name=='role_memory_boundary':
                    people=[o for o in objects if not o['is_user'] and o['kind']=='person']
                    check(f'{index}_one_person',len(people)==1)
                    check(f'{index}_separate_organization',any(o['kind']=='organization' for o in objects))
                    if index>=1 and people:
                        person=people[0]
                        selected=set(reviewed['bindings']['subject_ids'].values())
                        check(f'{index}_only_current_person_selected',selected=={person['id']})
                        check(f'{index}_new_alias',any(a['name']=='Seli' for a in person['aliases']))
                        check(f'{index}_roles_excluded_from_aliases',all(term not in {a['name'] for a in person['aliases']} for term in ['designer','painter','my colleague']))
                elif name in {'shared_role_memory','cross_kind_namesake'}:
                    people=[o for o in objects if not o['is_user'] and o['kind'] in {'person','animal'}]
                    check(f'{index}_two_subjects',len(people)==2)
                    if index==1:
                        if name=='cross_kind_namesake':
                            selected=[o for o in people if o['kind']=='animal']
                            check('kind_selects_animal_identity',len(selected)==1 and selected[0]['id'] in reviewed['bindings']['subject_ids'].values())
                        else:
                            check('ambiguous_namesake_stays_pending',not reviewed['bindings']['subject_ids'])
                            check('roles_excluded_from_aliases',all(not {a['name'] for a in o['aliases']}.intersection({'nurse','baker','a nurse','a baker'}) for o in people))
                else:
                    check('no_false_api_user',not any(o['is_user'] for o in objects))
                    check('quoted_owner_subjects',len(objects)==2)
                steps.append(dict(index=index,source=text,output=raw,attempt_outputs=attempt_outputs,model_requests=requests,
                    recall_subject_ids=[o['id'] for o in state['subject_catalog']],effective_output=reviewed.get('effective_output',raw),
                    initial_issues=initial_issues,final_issues=reviewed['issues'],repair_audit=reviewed.get('repair_audit',[]),
                    errors=[e['code'] for e in reviewed['issues']]))
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
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--cases',type=Path);parser.add_argument('--mode',choices=['direct','sequential','extra','memory'],default='direct')
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('--env-file',type=Path,required=True);parser.add_argument('--workers',type=int,default=2)
    args=parser.parse_args();load_environment(args.env_file);args.output.mkdir(parents=True,exist_ok=False);frozen=fingerprint()
    if args.mode=='direct':
        inputs=args.cases.read_bytes();(args.output/'inputs.json').write_bytes(inputs);cases=json.loads(inputs)['cases']
        with ThreadPoolExecutor(max_workers=args.workers) as pool:results=list(pool.map(lambda c:run_direct(c,args.output),cases))
    elif args.mode in {'sequential','extra'}:
        cases=EXTRA_CASES if args.mode=='extra' else CASES
        (args.output/'inputs.json').write_text(json.dumps(cases,ensure_ascii=False,indent=2));results=run_sequential(args.output,extra=args.mode=='extra')
    else:
        (args.output/'inputs.json').write_text(json.dumps(MEMORY_CASES,ensure_ascii=False,indent=2));results=run_memory(args.output)
    calls=[c for row in results for c in row['calls']]
    summary=dict(experiment='V49-subject-kinds',code_sha256=frozen,code_unchanged_during_run=frozen==fingerprint(),
        cases=len(results),passed_cases=sum(row['passed'] for row in results),checks=sum(len(row['checks']) for row in results),
        passed_checks=sum(c['passed'] for row in results for c in row['checks']),text_calls=len(calls),
        input_tokens=sum(c.get('input_tokens',0) for c in calls),output_tokens=sum(c.get('output_tokens',0) for c in calls),
        mean_call_seconds=statistics.mean(c['seconds'] for c in calls) if calls else None,databases_created=0,
        ordinary_model_calls=sum(c['kind']=='add_propose' for c in calls),production_pipeline='V32 unchanged')
    (args.output/'summary.json').write_text(json.dumps(summary,indent=2));print(json.dumps(summary),flush=True)

if __name__=='__main__':main()
