"""Run a frozen subject protocol against fixed inputs; records stay in MemoryStore.

Run one process per independent round. Runtime and input fingerprints bind every result.
Transport traces include every physical request, including network retries and invalid replies.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys
from time import monotonic
from uuid import uuid4


def digest_files(runtime: Path) -> dict[str, str]:
    paths=sorted([*runtime.glob('src/**/*.py'), *runtime.glob('experiments/*.py'),
                  *runtime.glob('experiments/*.json'), *runtime.glob('scripts/*.py')])
    return {str(path.relative_to(runtime)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')


def operation_contract_violations(content, response_format):
    """Audit operation branches from the actual sent schema without changing a reply."""
    schema=response_format.get('json_schema',{}).get('schema',{})
    definition=schema.get('$defs',{}).get('WireSubjectChoice',{})
    if 'anyOf' not in definition:return None
    try:raw=json.loads(content)
    except (json.JSONDecodeError,TypeError):return [{'code':'response_not_json_for_branch_audit'}]
    if not isinstance(raw,dict) or not isinstance(raw.get('subjects'),list):
        return [{'code':'subjects_not_array_for_branch_audit'}]
    branches={choice['properties']['operation']['enum'][0]:choice['properties'] for choice in definition['anyOf']}
    issues=[]
    for index,row in enumerate(raw['subjects']):
        if not isinstance(row,dict):
            issues.append({'index':index,'code':'subject_not_object'});continue
        operation=row.get('operation');branch=branches.get(operation)
        if branch is None:
            issues.append({'index':index,'code':'operation_has_no_allowed_schema_branch','operation':operation});continue
        if operation=='reuse':
            if row.get('label') is not None:issues.append({'index':index,'code':'reuse_label_not_null'})
            if row.get('target_id') not in branch['target_id'].get('enum',[]):issues.append({'index':index,'code':'reuse_target_not_allowed'})
        else:
            if row.get('target_id') is not None:issues.append({'index':index,'code':'create_pending_target_not_null'})
            if not isinstance(row.get('label'),dict):issues.append({'index':index,'code':'create_pending_label_not_object'})
    return issues


def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime',type=Path,required=True)
    parser.add_argument('--version',required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--inputs',type=Path,required=True)
    parser.add_argument('--env-file',type=Path,required=True)
    parser.add_argument('--model',choices=['gpt-4o-mini'],default='gpt-4o-mini')
    parser.add_argument('--workers',type=int,default=2)
    args=parser.parse_args()
    runtime=args.runtime.resolve();out=args.output.resolve();inputs=args.inputs.resolve()
    runtime_before=digest_files(runtime)
    out.mkdir(parents=True,exist_ok=False)
    write_json(out/'runtime-manifest.json',runtime_before)
    original_cwd=Path.cwd()
    sys.path[:0]=[str(runtime/'src'),str(runtime)]
    os.chdir(runtime)
    from remi.settings import load_environment
    from experiments.subject_providers import experimental_providers
    from scripts import validate_subject_connections as benchmark
    load_environment(args.env_file.resolve())
    transport=out/'transport';transport.mkdir()

    def providers():
        model,embedder,ranker=experimental_providers(args.model)
        model.timeout=45
        stream=transport/uuid4().hex;stream.mkdir()
        original_create=model.client.chat.completions.create
        attempt=0
        def capture(**kwargs):
            nonlocal attempt
            attempt+=1;started=monotonic()
            row={'started_at':datetime.now(timezone.utc).isoformat(),
                 'request':{key:kwargs[key] for key in ['model','temperature','max_completion_tokens','messages','response_format','timeout'] if key in kwargs}}
            try:
                response=original_create(**kwargs)
                row['response']={'model':getattr(response,'model',None),
                                 'system_fingerprint':getattr(response,'system_fingerprint',None),
                                 'id':getattr(response,'id',None),
                                 'content':response.choices[0].message.content,
                                 'refusal':getattr(response.choices[0].message,'refusal',None),
                                 'finish_reason':response.choices[0].finish_reason,
                                 'usage':response.usage.model_dump() if response.usage else None}
                try:
                    row['operation_branch_violations']=operation_contract_violations(response.choices[0].message.content,kwargs.get('response_format',{}))
                except Exception as audit_error:
                    row['operation_branch_audit_error']=type(audit_error).__name__
                return response
            except Exception as error:
                row.update(error_type=type(error).__name__,http_status=getattr(error,'status_code',None))
                raise
            finally:
                row['seconds']=monotonic()-started
                write_json(stream/f'{attempt:03d}.json',row)
        model.client.chat.completions.create=capture
        return model,embedder,ranker
    benchmark.providers_from_environment=providers
    start=monotonic();results=[];input_hashes={}
    for mode in ['direct','holdout','sequential','extra','blind']:
        folder=out/mode;folder.mkdir()
        if mode in {'direct','holdout','blind'}:
            source=inputs/f'{mode}.json';payload=source.read_bytes()
            (folder/'inputs.json').write_bytes(payload)
            input_hashes[mode]=hashlib.sha256(payload).hexdigest()
            cases=json.loads(payload)['cases']
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                rows=list(pool.map(lambda case:benchmark.run_direct(case,folder),cases))
        else:
            extra=mode=='extra';cases=benchmark.EXTRA_CASES if extra else benchmark.CASES
            write_json(folder/'inputs.json',cases)
            input_hashes[mode]=hashlib.sha256((folder/'inputs.json').read_bytes()).hexdigest()
            rows=benchmark.run_sequential(folder,extra=extra)
        results.extend((mode,row) for row in rows)
        print(json.dumps({'version':args.version,'mode':mode,'passed':sum(row['passed'] for row in rows),'cases':len(rows)},ensure_ascii=False),flush=True)
    calls=[call for _,row in results for call in row['calls']]
    modes={mode:{'cases':sum(m==mode for m,_ in results),
                 'passed':sum(row['passed'] for m,row in results if m==mode),
                 'checks':sum(len(row['checks']) for m,row in results if m==mode),
                 'passed_checks':sum(check['passed'] for m,row in results if m==mode for check in row['checks'])}
           for mode in ['direct','holdout','sequential','extra','blind']}
    runtime_after=digest_files(runtime)
    seconds=[call['seconds'] for call in calls]
    summary={'version':args.version,'model':args.model,'runtime':str(runtime),'modes':modes,
             'regression_cases':46,'regression_passed':sum(row['passed'] for m,row in results if m!='blind'),
             'additional_cases':12,'additional_passed':sum(row['passed'] for m,row in results if m=='blind'),
             'cases':len(results),'passed':sum(row['passed'] for _,row in results),
             'calls':len(calls),'normal_calls':sum(call['kind']=='add_subjects' for call in calls),
             'repairs':sum(call['kind']=='add_subjects_repair' for call in calls),
             'provider_errors':[call for call in calls if call.get('error_type') or call.get('error')],
             'input_tokens':sum(call.get('input_tokens',0) for call in calls),
             'output_tokens':sum(call.get('output_tokens',0) for call in calls),
             'call_seconds':sum(seconds),'mean_call_seconds':statistics.mean(seconds) if seconds else None,
             'median_call_seconds':statistics.median(seconds) if seconds else None,
             'wall_seconds':monotonic()-start,'runtime_unchanged':runtime_before==runtime_after,
             'runtime_fingerprint':hashlib.sha256(json.dumps(runtime_before,sort_keys=True).encode()).hexdigest(),
             'input_sha256':input_hashes,
             'failures':[{'mode':mode,'id':row['id'],'failed_checks':[check['name'] for check in row['checks'] if not check['passed']],
                          'error_type':row.get('error_type')} for mode,row in results if not row['passed']],
             'databases_created':0,'tables_created':0}
    write_json(out/'summary.json',summary)
    print(json.dumps(summary,ensure_ascii=False),flush=True)
    os.chdir(original_cwd)
    if not summary['runtime_unchanged']:raise RuntimeError('frozen_runtime_changed')


if __name__=='__main__':main()
