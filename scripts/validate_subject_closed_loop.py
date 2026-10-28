"""Repeated cross-turn subject and memory evaluation through production Add.

Only this evaluator contains synthetic scenarios and expectations. The engine receives ordinary
source text. --dry-run writes the fixed inputs and validates their shape without API clients.
Every live case starts with its own empty MemoryStore; no physical database is created.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import re
from pathlib import Path
import sys
from time import monotonic

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'src'), str(ROOT)]

from remi.engine import MemoryEngine
from remi.storage import MemoryStore
from remi.version import PIPELINE
from remi.providers import GPTModel
from remi.identity import normalized_name, stable_id
from remi.models import AddMessage, AddRequest
from remi.providers import ProviderUnavailable
from remi.settings import Settings, load_environment, private_key


def write_json(path, payload):
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n')


def capture_transport(model, directory):
    directory.mkdir(parents=True, exist_ok=True)
    create = model.client.chat.completions.create
    attempts = []

    def capture(**request):
        started = monotonic()
        row = dict(started_at=datetime.now(timezone.utc).isoformat(),
            request={key:request[key] for key in ['model','temperature','max_completion_tokens',
                                                'messages','response_format','timeout'] if key in request})
        attempts.append(row)
        number = len(attempts)
        try:
            response = create(**request)
            choice = response.choices[0]
            row['response'] = dict(model=getattr(response, 'model', None), id=getattr(response, 'id', None),
                system_fingerprint=getattr(response, 'system_fingerprint', None), content=choice.message.content,
                refusal=getattr(choice.message, 'refusal', None), finish_reason=getattr(choice, 'finish_reason', None),
                usage=response.usage.model_dump() if response.usage else None)
            return response
        except Exception as error:
            row.update(error_type=type(error).__name__, http_status=getattr(error, 'status_code', None))
            raise
        finally:
            row['seconds'] = monotonic()-started
            write_json(directory/f'{number:03d}.json', row)

    model.client.chat.completions.create = capture
    return attempts


def step(text, subjects, **expectations):
    return dict(text=text, subjects=subjects, **expectations)


def cases():
    """Eight general situations, fixed before model outputs are inspected."""
    return [
        dict(id='english_full_to_short', identities={'delia':['Delia Fenn','Delia']}, steps=[
            step('My colleague Delia Fenn helped me sort the supply receipts.', ['user','delia'], new=['delia']),
            step('Delia checked the final totals with me.', ['user','delia'], aliases={'delia':['Delia']}),
            step('Delia sent me the corrected supply list.', ['user','delia'], session='fresh', recall=['delia'], aliases={'delia':['Delia']})]),
        dict(id='chinese_full_to_short', identities={'xingheng':['陆星衡','星衡']}, steps=[
            step('我和同事陆星衡整理了报销票据。', ['user','xingheng'], new=['xingheng']),
            step('星衡又帮我核对了两张发票。', ['user','xingheng'], aliases={'xingheng':['星衡']}),
            step('星衡把核对结果发给我了。', ['user','xingheng'], session='fresh', recall=['xingheng'], aliases={'xingheng':['星衡']})]),
        dict(id='pet_nickname', identities={'bramble':['Bramble','Bram']}, steps=[
            step('My dog Bramble, whom I call Bram, learned a new hand signal.', ['user','bramble'], new=['bramble'], aliases={'bramble':['Bram']}),
            step('Bram followed my hand signal again.', ['user','bramble'], recall=['bramble']),
            step('Bram waited beside me while I packed the training treats.', ['user','bramble'], session='fresh', recall=['bramble'], aliases={'bramble':['Bram']})]),
        dict(id='single_candidate_new_namesake', identities={'neighbor':['Corin'], 'newcomer':['Corin','Cora']}, steps=[
            step('My neighbor Corin showed me a stamp collection.', ['user','neighbor'], new=['neighbor']),
            step('Today I met another person named Corin, a new acquaintance. She asked me to call her Cora.', ['user','newcomer'], new=['newcomer'], aliases={'newcomer':['Cora']}, distinct=[['neighbor','newcomer']]),
            step('Cora and I checked a bus timetable together.', ['user','newcomer'], session='fresh', recall=['newcomer'], distinct=[['neighbor','newcomer']])]),
        dict(id='unnamed_person_gets_name', identities={'courier':['A bicycle courier','bicycle courier','The courier','courier','Kez']}, steps=[
            step('A bicycle courier handed me a parcel. The courier waited while I checked the address.', ['user','courier'], new=['courier']),
            step('The same courier told me her name is Kez and explained how to return the parcel.', ['user','courier'], aliases={'courier':['Kez']}),
            step('Kez collected my return parcel.', ['user','courier'], session='fresh', recall=['courier'], aliases={'courier':['Kez']})]),
        dict(id='quoted_first_person', identities={'soraya':['Soraya Kim','Soraya'], 'pecan':['Pecan']}, steps=[
            step('Soraya Kim wrote: "I adopted a rabbit named Pecan."', ['soraya','pecan'], new=['soraya','pecan'], claims=[
                dict(key='rabbit_adoption', subjects=['soraya','pecan'], optional=[], anchors=[
                    [r'\badopt\w*\b',r'\btook in\b',r'\bbrought home\b',r'\bwelcom\w*\b'],
                    [r'\brabbit\b',r'\bpecan\b']])]),
            step('Soraya wrote: "Pecan enjoyed the new pen."', ['soraya','pecan'], aliases={'soraya':['Soraya']}),
            step('Soraya and Pecan appear in the veterinary follow-up note she sent.', ['soraya','pecan'], session='fresh', recall=['soraya','pecan'])]),
        dict(id='participant_binding_and_scope', identities={'rosa':['Rosa Bell','Rosa'], 'tomas':['Tomas Ives','Tomas']}, steps=[
            step('I met Rosa Bell, a volunteer, and Tomas Ives, a mechanic, at the community hall.', ['user','rosa','tomas'], new=['rosa','tomas']),
            step('Rosa prepared the display board while Tomas repaired the folding stand. I thanked both of them.', ['user','rosa','tomas'], aliases={'rosa':['Rosa'],'tomas':['Tomas']}, claims=[
                dict(key='display_preparation', subjects=['rosa'], optional=['user'], anchors=[
                    [r'\bprepar\w*\b',r'\barrang\w*\b',r'\bset(?:ting)? up\b',r'\bread(?:y|ied)\b'],
                    [r'\b(?:display|exhibit(?:ion)?|presentation) (?:board|panel)\b']]),
                dict(key='stand_repair', subjects=['tomas'], optional=['user'], anchors=[
                    [r'\brepair\w*\b',r'\bfix\w*\b',r'\bmend\w*\b',r'\brestor\w*\b'],
                    [r'\b(?:folding|foldable) stand\b']])]),
            step('Rosa lent me a display board.', ['user','rosa'], recall=['rosa'])]),
        dict(id='historical_candidates_stay_out', identities={'selma':['Selma'], 'oren':['Oren']}, steps=[
            step('My partner Selma and my brother Oren helped me set up the study.', ['user','selma','oren'], new=['selma','oren']),
            step('I worked alone on a price comparison spreadsheet at my desk.', ['user']),
            step('I finished the spreadsheet and saved my notes.', ['user'])]),
    ]


def validate_cases(values):
    ids=set()
    for case in values:
        if not case.get('id') or case['id'] in ids or not case.get('steps'):
            raise ValueError('unique_case_id_and_steps_required')
        ids.add(case['id']); known={'user'}|set(case['identities'])
        for specification in case['steps']:
            if not specification['text'] or not set(specification['subjects'])<=known:
                raise ValueError('fixture_current_subjects_invalid')
            if not set(specification.get('new',[]))<=set(specification['subjects']):
                raise ValueError('fixture_new_identity_requires_current_subject')
            if not set(specification.get('recall',[]))<=set(specification['subjects']):
                raise ValueError('fixture_recall_requires_current_subject')
            for claim in specification.get('claims',[]):
                if not set(claim['subjects'])<=set(specification['subjects']) or not claim.get('anchors'):
                    raise ValueError('fixture_claim_requires_current_subjects_and_anchors')
                for alternatives in claim['anchors']:
                    if not alternatives:
                        raise ValueError('fixture_claim_anchor_alternatives_required')
                    for expression in alternatives:
                        re.compile(expression)
    return values


def names(row):
    values=[row.get('name','')]
    values.extend(value.get('name','') if isinstance(value,dict) else value for value in row.get('aliases',[]))
    return {normalized_name(value) for value in values if value}


def matches(row, anchors):
    return bool(names(row)&{normalized_name(anchor) for anchor in anchors})


def effective_output(unit):
    identity=unit.get('identity',{})
    return identity.get('effective_output', unit.get('outputs',{}).get('subjects_repair',
        unit.get('outputs',{}).get('subjects',{})))


def claim_matches(content, claim):
    text=normalized_name(content)
    return all(any(re.search(pattern,text) for pattern in alternatives) for alternatives in claim['anchors'])


def claim_checks(case, specification, memories, oracle):
    """Keep action ownership attached to each actual record, including combined claims.

    Explicit lexical alternatives belong to these evaluation annotations. A combined record
    may carry the union of its matched claims. A clause explicitly naming another actor is
    checked separately, so swapping action owners cannot pass using the same ID union.
    """
    claims=specification.get('claims',[]); checks=[]
    if not claims:
        return checks
    def check(name,value,**details):
        checks.append(dict(dimension='memory_binding',name=name,passed=bool(value),**details))
    current={oracle[key] for key in specification['subjects'] if key in oracle}
    person_names={key:case['identities'].get(key,[]) for key in specification['subjects'] if key!='user'}
    all_names=sorted({normalized_name(name) for values in person_names.values() for name in values},key=len,reverse=True)
    names_regex='|'.join(re.escape(name) for name in all_names)
    boundary=r'[.;]|\bwhile\b|\bwhereas\b'
    if names_regex:
        boundary+=r'|\band\s+(?=(?:'+names_regex+r')\b)'
    matched={claim['key']:[] for claim in claims}
    for memory in memories:
        content=memory.current.content
        selected=[claim for claim in claims if claim_matches(content,claim)]
        if not selected:
            continue
        ids=set(memory.current.entity_ids)
        required={oracle[key] for claim in selected for key in claim['subjects'] if key in oracle}
        optional={oracle[key] for claim in selected for key in claim.get('optional',[]) if key in oracle}
        missing_keys=[key for claim in selected for key in claim['subjects'] if key not in oracle]
        check('claim_record_'+memory.id+'_participants',not missing_keys and required<=ids and not (ids-current)
              and not (ids-required-optional),memory_id=memory.id,content=content,
              expected_ids=sorted(required),optional_ids=sorted(optional),actual_ids=sorted(ids))
        clauses=[part.strip() for part in re.split(boundary,normalized_name(content)) if part.strip()]
        for claim in selected:
            matched[claim['key']].append(memory.id)
            desired={key for key in claim['subjects'] if key!='user'}
            wrong_clauses=[]
            for clause in clauses:
                if not claim_matches(clause,claim):
                    continue
                named={key for key,values in person_names.items() if any(
                    re.search(r'(?<![a-z0-9_])'+re.escape(normalized_name(value))+r'(?![a-z0-9_])',clause)
                    for value in values)}
                if named and not named.intersection(desired):
                    wrong_clauses.append(clause)
            check('claim_'+claim['key']+'_'+memory.id+'_content_owner',not wrong_clauses,
                  memory_id=memory.id,wrong_owner_clauses=wrong_clauses)
    for claim in claims:
        check('claim_'+claim['key']+'_covered',bool(matched[claim['key']]),matched_memory_ids=matched[claim['key']])
    return checks


def evaluate_step(case, specification, store, owner, unit, oracle, prior_ids):
    """Score names/identities, recall and materialized memory participants independently."""
    objects=store.list('objects',owner); by_id={row['id']:row for row in objects}
    identity=unit.get('identity',{}); bindings=identity.get('bindings',{}).get('subject_ids',{})
    accepted=set(bindings.values()); raw=effective_output(unit)
    rows=raw.get('subjects',[]) if isinstance(raw,dict) else []
    rows=[row for row in rows if isinstance(row,dict)]
    checks=[]
    def check(dimension,name,value,**details):
        checks.append(dict(dimension=dimension,name=name,passed=bool(value),**details))
    for key in specification.get('new',[]):
        candidates=[row for row in objects if row['id'] not in prior_ids and not row['is_user']
                    and matches(row,case['identities'][key])]
        check('identity','new_'+key,len(candidates)==1,candidate_ids=[row['id'] for row in candidates])
        if len(candidates)==1:
            oracle[key]=candidates[0]['id']
    expected={oracle[key] for key in specification['subjects'] if key in oracle}
    missing_keys=[key for key in specification['subjects'] if key not in oracle]
    for key in specification['subjects']:
        anchors=case['identities'].get(key,['@user','user'])
        oid=oracle.get(key)
        if oid in by_id:
            anchors=[*anchors,*names(by_id[oid])]
        represented=any(matches(row,anchors) or (oid is not None and row.get('target_id')==oid)
                        or (oid is not None and bindings.get(str(row.get('ref')))==oid) for row in rows)
        check('coverage','current_'+key,represented)
        check('identity','accepted_'+key,oid is not None and oid in accepted)
    allowed_names=set()
    for key in specification['subjects']:
        allowed_names.update(normalized_name(value) for value in case['identities'].get(key,['@user','user']))
        if oracle.get(key) in by_id:
            allowed_names.update(names(by_id[oracle[key]]))
    extra_rows=[row for row in rows if not names(row)&allowed_names
                and row.get('target_id') not in expected and bindings.get(str(row.get('ref'))) not in expected]
    check('current_scope','no_historical_or_other_current_rows',not extra_rows,extra_refs=[row.get('ref') for row in extra_rows])
    check('identity','no_wrong_accepted_identity',not (accepted-expected),unexpected_ids=sorted(accepted-expected))
    for first,second in specification.get('distinct',[]):
        check('identity','distinct_'+first+'_'+second,first in oracle and second in oracle and oracle[first]!=oracle[second])
    for key,values in specification.get('aliases',{}).items():
        saved=by_id.get(oracle.get(key),{})
        known={normalized_name(value['name']) for value in saved.get('aliases',[])}
        for value in values:
            check('alias','stored_'+key+'_'+value,normalized_name(value) in known)
    route=unit.get('shared_recall',{}).get('maintenance_route',{})
    check('route','subject_first',route.get('order')=='subject_first')
    name_hits={sid for group in route.get('name_matches',[]) for sid in group['subject_ids']}
    for key in specification.get('recall',[]):
        check('recall','current_name_recalls_'+key,key in oracle and oracle[key] in name_hits)
    source_id=unit.get('shared_recall',{}).get('new_user_source',{}).get('id')
    memories=[memory for memory in store.memories(owner) if source_id in memory.current.source_ids]
    participants={oid for memory in memories for oid in memory.current.entity_ids}
    check('memory_binding','current_memory_saved',bool(memories))
    check('memory_binding','all_current_subjects_bound',not missing_keys and expected<=participants,
          missing_identity_keys=missing_keys,missing_ids=sorted(expected-participants))
    check('memory_binding','no_wrong_memory_participants',not (participants-expected),unexpected_ids=sorted(participants-expected))
    checks.extend(claim_checks(case,specification,memories,oracle))
    output_keys=unit.get('outputs',{}).keys()
    normal=[key for key in output_keys if key in {'subjects','propose','memories'}]
    repairs=[key for key in output_keys if key in {'subjects_repair','propose_repair','memories_repair'}]
    check('call_budget','two_normal_stage_outputs',len(normal)==2,stages=normal)
    check('call_budget','shared_at_most_one_repair',len(repairs)<=1,repair_stages=repairs)
    check('protocol','identity_fields_valid',not identity.get('contract_errors') and not identity.get('subject_errors'))
    dimensions={dimension:all(row['passed'] for row in checks if row['dimension']==dimension)
                for dimension in {row['dimension'] for row in checks}}
    return dict(checks=checks,dimensions=dimensions,passed=all(row['passed'] for row in checks),
        expected_subject_ids=sorted(expected),accepted_subject_ids=sorted(accepted),
        current_memory_ids=[memory.id for memory in memories],current_memory_participant_ids=sorted(participants),
        program_issues=identity.get('issues',[]),oracle=deepcopy(oracle))


def json_return_counts(attempts):
    counts=Counter()
    for attempt in attempts:
        response=attempt.get('response')
        if response is None:
            counts['no_response_attempts']+=1
            continue
        counts['responses']+=1
        try:
            value=json.loads(response.get('content') or '')
        except (ValueError,TypeError):
            counts['syntax_invalid']+=1
            continue
        counts['syntax_valid']+=1
        counts['object_valid' if isinstance(value,dict) else 'object_invalid']+=1
    return dict(counts)


def runtime_manifest():
    paths=sorted([*ROOT.glob('src/**/*.py'), Path(__file__).resolve()])
    return {str(path.relative_to(ROOT)):hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def request_for(case,owner,index):
    specification=case['steps'][index]
    clock=datetime(2029,8,1,10,tzinfo=timezone.utc)+timedelta(hours=index)
    return AddRequest(request_id='step-'+str(index),user_id=owner,
        session_id=case['id']+':'+specification.get('session','conversation'),
        messages=[AddMessage(role='user',content=specification['text'],timestamp=int(clock.timestamp()*1000))])


def run_case(case,round_number,output,dry_run=False,max_request_attempts=2):
    directory=output/f'round-{round_number}'/case['id'];directory.mkdir(parents=True,exist_ok=False)
    owner=f'closed-loop:{case["id"]}:round-{round_number}'
    result=dict(id=case['id'],round=round_number,pipeline=PIPELINE,steps=[],checks=[],dimensions={},passed=None,dry_run=dry_run)
    engine=model=None; attempts=[]; started=monotonic()
    if dry_run:
        result.update(inputs=[request_for(case,owner,index).model_dump() for index in range(len(case['steps']))],fixture_valid=True)
        write_json(directory/'result.json',result)
        return result
    try:
        key=private_key('LLM_API_KEY','LLM_API_KEY_FILE')
        if not key:
            raise ValueError('model_key_unavailable')
        model=GPTModel(key,os.getenv('LLM_BASE_URL','https://api.quickrouter.us/v1'),
                               timeout=float(os.getenv('REMI_LLM_TIMEOUT','45')))
        attempts=capture_transport(model,directory/'transport')
        engine=MemoryEngine(MemoryStore(),model,settings=Settings(add_timeout=240))
        oracle={'user':stable_id('obj_',owner,'user')}
        for index,specification in enumerate(case['steps']):
            request=request_for(case,owner,index);prior={row['id'] for row in engine.store.list('objects',owner)}
            call_start=len(model.calls);attempt_errors=[]
            for attempt in range(max_request_attempts):
                try:
                    engine.add(request)
                    break
                except ProviderUnavailable as error:
                    attempt_errors.append(dict(attempt=attempt+1,error_type=type(error).__name__))
                    if attempt+1==max_request_attempts:
                        raise
            progress=engine.store.get('requests',owner,request.request_id)
            unit=progress['units']['0']
            evaluation=evaluate_step(case,specification,engine.store,owner,unit,oracle,prior)
            result['steps'].append(dict(index=index,input=request.model_dump(),**evaluation,
                unit=deepcopy(unit),request_status=progress['status'],calls=list(model.calls)[call_start:],
                request_attempt_errors=attempt_errors))
            write_json(directory/'result.json',result)
        result['checks']=[dict(step=row['index'],**check) for row in result['steps'] for check in row['checks']]
        dimensions={name for row in result['steps'] for name in row['dimensions']}
        result['dimensions']={name:all(row['dimensions'].get(name,True) for row in result['steps']) for name in dimensions}
        result['passed']=all(row['passed'] for row in result['steps'])
    except Exception as error:
        result.update(error_type=type(error).__name__,http_status=getattr(error,'status_code',None))
        if engine is not None:
            result['partial_requests']=engine.store.list('requests',owner)
    finally:
        result.update(seconds=monotonic()-started,calls=list(model.calls) if model is not None else [],
            physical_attempts=len(attempts),json_returns=json_return_counts(attempts),databases_created=0,tables_created=0)
        if engine is not None:
            write_json(directory/'records.json',engine.store.snapshot());engine.close()
        write_json(directory/'result.json',result)
    print(json.dumps(dict(round=round_number,id=case['id'],passed=result['passed'],
        completed_steps=len(result['steps']),failed_dimensions=[key for key,value in result['dimensions'].items() if not value],
        error_type=result.get('error_type')),ensure_ascii=False),flush=True)
    return result


def summarize(results,fixtures,rounds,dry_run,seconds):
    steps=[step for result in results for step in result['steps']]
    dimensions=sorted({name for step in steps for name in step['dimensions']})
    calls=[call for result in results for call in result.get('calls',[])]
    return dict(model='gpt-4o-mini',pipeline=PIPELINE,dry_run=dry_run,rounds=rounds,
        cases=len(fixtures),runs=len(results),planned_steps=sum(len(case['steps']) for case in fixtures)*rounds,
        completed_steps=len(steps),passed_runs=sum(result['passed'] is True for result in results),
        failed_runs=sum(result['passed'] is False for result in results),
        unevaluated_runs=sum(result['passed'] is None for result in results),
        dimensions={name:dict(passed=sum(step['dimensions'].get(name) is True for step in steps),
            evaluated=sum(name in step['dimensions'] for step in steps)) for name in dimensions},
        per_case={case['id']:dict(runs=sum(result['id']==case['id'] for result in results),
            completed_runs=sum(result['id']==case['id'] and len(result['steps'])==len(case['steps']) for result in results),
            passed_runs=sum(result['id']==case['id'] and result['passed'] is True for result in results)) for case in fixtures},
        repair_units=sum(bool(step['unit'].get('repair_used')) for step in steps),
        call_kinds=dict(Counter(call['kind'] for call in calls)),
        call_errors=dict(Counter(call.get('error_type') for call in calls if call.get('error_type'))),
        physical_attempts=sum(result.get('physical_attempts',0) for result in results),
        json_returns=dict(sum((Counter(result.get('json_returns',{})) for result in results),Counter())),
        input_tokens=sum(call.get('input_tokens',0) for call in calls),output_tokens=sum(call.get('output_tokens',0) for call in calls),
        wall_seconds=seconds,databases_created=0,tables_created=0,semantic_identity_accuracy=None,
        interpretation=['Dimensions score explicit independent fixture assertions; no whole-population identity accuracy is inferred.',
            'Coverage, current scope, accepted identity, alias persistence, name recall and materialized memory bindings are separate.',
            'Protocol acceptance can coexist with identity or memory-binding failure.',
            'Network-interrupted cases retain their completed steps and are reported as unevaluated runs.'])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--rounds',type=int,default=3);parser.add_argument('--workers',type=int,default=2)
    parser.add_argument('--cases',type=Path);parser.add_argument('--dry-run',action='store_true')
    parser.add_argument('--max-request-attempts',type=int,default=2)
    args=parser.parse_args()
    if min(args.rounds,args.workers,args.max_request_attempts)<1:
        parser.error('rounds, workers and max-request-attempts must be positive')
    fixtures=validate_cases(json.loads(args.cases.read_text()) if args.cases else cases())
    output=args.output.resolve();output.mkdir(parents=True,exist_ok=False,mode=0o700);os.umask(0o077)
    before=runtime_manifest();write_json(output/'runtime-manifest.json',before);write_json(output/'inputs.json',fixtures)
    if not args.dry_run:
        load_environment(args.env_file.resolve())
    work=[(deepcopy(case),number) for number in range(1,args.rounds+1) for case in fixtures];start=monotonic()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results=list(pool.map(lambda pair:run_case(*pair,output,args.dry_run,args.max_request_attempts),work))
    after=runtime_manifest();summary=summarize(results,fixtures,args.rounds,args.dry_run,monotonic()-start)
    summary.update(runtime_unchanged=before==after,
        runtime_fingerprint=hashlib.sha256(json.dumps(before,sort_keys=True).encode()).hexdigest(),
        input_sha256=hashlib.sha256((output/'inputs.json').read_bytes()).hexdigest())
    write_json(output/'summary.json',summary);print(json.dumps(summary,ensure_ascii=False),flush=True)
    if before!=after:
        raise RuntimeError('runtime_changed_during_evaluation')


if __name__=='__main__':
    main()
