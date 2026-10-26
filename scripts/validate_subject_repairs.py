"""One real repair of saved V35 initial outputs, keeping the same pre-call store and context."""
from pathlib import Path
from collections import Counter
from time import monotonic
import argparse,json
from remi.settings import load_environment
from experiments.subject_engine import SubjectMemoryStore
from experiments.subject_connections import review,maintain
from experiments.subject_repair import build_repair_state,finish_repair
from experiments.subject_providers import experimental_providers
from remi.providers import OutputInvalid


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--cases',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--env-file',required=True);parser.add_argument('--model',required=True);parser.add_argument('--deepseek-key-file')
    args=parser.parse_args();load_environment(args.env_file);args.output.mkdir(parents=True,exist_ok=False,mode=0o700)
    model,embedding,reranker=experimental_providers(args.model,args.deepseek_key_file);rows=[]
    try:
        for case in json.loads(args.cases.read_text()):
            store=SubjectMemoryStore();store.restore(case['store']);state=case['state'];first=review(case['initial_output'],state,store)
            repair=build_repair_state(case['initial_output'],first,state);started=monotonic()
            row=dict(id=case['id'],initial_issues=first['issues'],baseline_final_issues=case['baseline_final_issues'])
            try:
                try:output,wire=maintain(model,repair,deadline=monotonic()+120)
                except OutputInvalid:output={'output_error':'model_json_invalid'};wire=None
                raw_review=review(output,state,store);result=finish_repair(repair,output,state,store)
                initial_ids=first['bindings']['subject_ids'];final_ids=result['bindings']['subject_ids']
                before_bad={str(e['ref']) for e in first['issues'] if e['scope']=='identity'}
                row.update(output=output,model_request=wire,raw_final_issues=raw_review['issues'],effective_final_issues=result['issues'],
                    effective_output=result['effective_output'],repair_audit=result['repair_audit'],
                    raw_regressed_refs=[ref for ref,key in initial_ids.items() if raw_review['bindings']['subject_ids'].get(ref)!=key],
                    effective_regressed_refs=[ref for ref,key in initial_ids.items() if final_ids.get(ref)!=key],
                    recovered_bound_refs=sorted(before_bad.intersection(final_ids)),
                    before_bound_refs=sorted(initial_ids),after_bound_refs=sorted(final_ids))
            except Exception as error:row['error_type']=type(error).__name__
            row['seconds']=monotonic()-started;rows.append(row)
            (args.output/(case['id']+'.json')).write_text(json.dumps(row,ensure_ascii=False,indent=2))
            print(json.dumps(dict(model=args.model,case=case['id'],remaining_issues=len(row.get('effective_final_issues',[])),error_type=row.get('error_type'))),flush=True)
    finally:
        calls=list(model.calls)
        for resource in [model,embedding,reranker]:resource.close()
    summary=dict(model=args.model,cases=len(rows),initial_issues=sum(len(r['initial_issues']) for r in rows),
        baseline_clean_cases=sum(not r['baseline_final_issues'] for r in rows),
        raw_clean_cases=sum('raw_final_issues' in r and not r['raw_final_issues'] for r in rows),
        effective_clean_cases=sum('effective_final_issues' in r and not r['effective_final_issues'] for r in rows),
        raw_regressed_refs=sum(len(r.get('raw_regressed_refs',[])) for r in rows),
        effective_regressed_refs=sum(len(r.get('effective_regressed_refs',[])) for r in rows),
        recovered_bound_refs=sum(len(r.get('recovered_bound_refs',[])) for r in rows),
        protection_actions=dict(Counter(e['code'] for r in rows for e in r.get('repair_audit',[]))),
        model_attempts=len(calls),calls=calls,ordinary_model_calls=0,physical_databases_created=0,
        errors=dict(Counter(r['error_type'] for r in rows if r.get('error_type'))))
    (args.output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2));print(json.dumps({k:v for k,v in summary.items() if k!='calls'}),flush=True)
if __name__=='__main__':main()
