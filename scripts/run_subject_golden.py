"""Run the complete causal Golden on the current experimental pipeline with resumable snapshots."""
from pathlib import Path
from time import monotonic,time,sleep
from collections import Counter
from uuid import uuid4
import argparse,hashlib,json,os
from remi.models import AddRequest,SearchRequest
from remi.storage import TABLES
from remi.providers import ProviderUnavailable
from remi.settings import load_environment,Settings
from remi.engine import DuplicateRequestConflict
from experiments.subject_engine import SubjectGraphEngine,SubjectMemoryStore,PIPELINE
from experiments.subject_providers import experimental_providers
from scripts.golden_adapter import actions,add_payload,input_catalog,score_case,metrics
from scripts.run_golden import CallJournal,verify_baseline,summaries


def code_fingerprint(root):
    files=sorted(p for folder in ['src','scripts','experiments'] for p in (root/folder).rglob('*') if p.is_file() and p.suffix in {'.py','.json'})
    values={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    return hashlib.sha256(json.dumps(values,sort_keys=True).encode()).hexdigest()


def execute(args):
    root=Path(__file__).resolve().parents[1];fingerprint=code_fingerprint(root)
    data=args.dataset.read_bytes();dataset=json.loads(data);baseline=json.loads(args.baseline.read_text())
    if hashlib.sha256(data).hexdigest()!=baseline['dataset_sha256']:raise ValueError('same_golden_dataset_required')
    verify_baseline(dataset,baseline);load_environment(args.env_file)
    args.output.mkdir(parents=True,exist_ok=args.resume,mode=0o700);os.umask(0o077)
    old_path=args.output/'result.json';snapshot_path=args.output/'records.json';store=SubjectMemoryStore()
    report=json.loads(old_path.read_text()) if args.resume else None
    if report:
        if report['runtime_code_fingerprint']!=fingerprint or report['config']['model']!=args.model:raise ValueError('resume_requires_same_code_and_model')
        store.restore(json.loads(snapshot_path.read_text()))
    model,embedder,reranker=experimental_providers(args.model,args.deepseek_key_file)
    settings=Settings.from_environment();engine=SubjectGraphEngine(store,model,embedder,reranker,settings)
    for resource in [model,embedder,reranker]:resource.calls=CallJournal(args.output/'diagnostics.jsonl')
    if report is None:
        token=uuid4().hex
        report=dict(architecture=PIPELINE,complete=False,started_at=time(),dataset=str(args.dataset),dataset_sha256=hashlib.sha256(data).hexdigest(),
            runtime_directory=str(root),runtime_code_fingerprint=fingerprint,user_id='golden:'+token,lifecycle_user_id='lifecycle:'+token,
            config=dict(model=args.model,thinking='disabled' if args.model=='deepseek-flash' else 'non-reasoning',
                json_mode='json_object' if args.model=='deepseek-flash' else 'json_schema',embedding='text-embedding-v4',reranker=reranker.model,settings=vars(settings)),
            add_calls=[],questions=[],lifecycle_add_calls=[],lifecycle_searches=[],protocol_checks=[],errors=[],source_to_ref={},
            input_originals={},diagnostic_calls=[],database=None,records_snapshot=str(snapshot_path),physical_databases_created=0,new_tables_created=0)
    owner=report['user_id'];life_owner=report['lifecycle_user_id'];mapping=report['source_to_ref'];catalog=report['input_originals']
    def save(status):
        report['status']=status;report['updated_at']=time()
        log=args.output/'diagnostics.jsonl'
        report['diagnostic_calls']=[json.loads(line) for line in log.read_text().splitlines() if line] if log.exists() else []
        report['summary']=summaries(report)
        report['summary']['provider_errors']={'|'.join(k):v for k,v in report['summary']['provider_errors'].items()}
        temp=snapshot_path.with_suffix('.tmp');temp.write_text(json.dumps(store.snapshot(),ensure_ascii=False));temp.replace(snapshot_path)
        temp=old_path.with_suffix('.tmp');temp.write_text(json.dumps(report,ensure_ascii=False,indent=2));temp.replace(old_path)
        completed_units=sum(len(r['turn_refs']) for r in report['add_calls'] if r['success'])+sum(r['success'] for r in report['lifecycle_add_calls'])
        (args.output/'run.json').write_text(json.dumps(dict(status=status,model=args.model,completed_units=completed_units,planned_units=len(dataset['input_turns'])+len(dataset['lifecycle']),
            questions_completed=len(report['questions']),planned_questions=len(dataset['cases']),updated_at=time(),result=str(old_path)),indent=2))
    def add(payload):
        started=monotonic()
        for attempt in range(3):
            try:
                result=engine.add(AddRequest.model_validate(payload));return result,monotonic()-started
            except ProviderUnavailable as error:
                report['errors'].append(dict(operation='add_retry',request_id=payload['request_id'],attempt=attempt+1,error_type=type(error).__name__))
                save('retrying')
                if attempt==2:raise
                sleep(.2)
    def search(payload,clock):
        started=monotonic()
        for attempt in range(3):
            try:return engine.search(SearchRequest.model_validate(payload),clock),monotonic()-started
            except ProviderUnavailable as error:
                report['errors'].append(dict(operation='search_retry',attempt=attempt+1,error_type=type(error).__name__))
                save('retrying')
                if attempt==2:raise
                sleep(.2)
    save('running')
    try:
        last=None
        for kind,action in actions(dataset):
            if kind=='add':
                payload=add_payload(action,owner);last=payload
                done=store.get('requests',owner,payload['request_id'])
                if done and done['status']=='complete':
                    refs,originals=input_catalog(payload,action['turns']);mapping.update(refs);catalog.update(originals)
                    if not any(row['request_id']==payload['request_id'] for row in report['add_calls']):
                        report['add_calls'].append(dict(request_id=payload['request_id'],turn_refs=[t['ref'] for t in action['turns']],
                            units=len(action['turns']),seconds=0,success=True,processing_complete=True,http_status=200,execution='recovered_checkpoint'))
                    continue
                _,seconds=add(payload);refs,originals=input_catalog(payload,action['turns']);mapping.update(refs);catalog.update(originals)
                report['add_calls'].append(dict(request_id=payload['request_id'],turn_refs=[t['ref'] for t in action['turns']],units=len(action['turns']),seconds=seconds,
                    success=True,processing_complete=True,http_status=200,execution='same_public_engine_contract'))
                print(json.dumps(dict(model=args.model,add=len(report['add_calls']),completed_units=sum(r['units'] for r in report['add_calls']),seconds=round(seconds,2))),flush=True)
            else:
                question=action['question']
                if any(q['question_id']==question['id'] for q in report['questions']):continue
                result,seconds=search(dict(user_id=owner,query=question['text'],top_k=10),question['day']+'T23:59:59Z')
                candidates=[item.model_dump() for item in result.data];row=score_case(action,candidates,mapping,catalog)
                row.update(seconds=seconds,http_status=200,candidates=candidates,trace=engine.last_search,input_complete=all(ref in mapping.values() for ref in question['evidence']))
                report['questions'].append(row)
                print(json.dumps(dict(model=args.model,question=len(report['questions']),recall=row['evidence_recall'],seconds=round(seconds,2))),flush=True)
            save('running')
        if last and not report['protocol_checks']:
            count=len(store.memories(owner));engine.add(AddRequest.model_validate(last))
            report['protocol_checks'].append(dict(name='idempotent_add',passed=len(store.memories(owner))==count))
            try:engine.add(AddRequest.model_validate({**last,'messages':[dict(role='user',content='changed payload')]}));conflict=False
            except DuplicateRequestConflict:conflict=True
            report['protocol_checks'].append(dict(name='request_id_conflict',passed=conflict))
        for index,case in enumerate(dataset['lifecycle']):
            if any(r['step']==index+1 for r in report['lifecycle_add_calls']):continue
            from datetime import datetime,timezone
            payload=dict(user_id=life_owner,request_id=f'lifecycle-{index}',session_id='lifecycle',messages=[dict(role='user',content=case['text'],
                timestamp=int(datetime.fromisoformat(case['day']).replace(tzinfo=timezone.utc).timestamp()*1000))])
            _,seconds=add(payload)
            report['lifecycle_add_calls'].append(dict(step=index+1,seconds=seconds,http_status=200,success=True,processing_complete=True,
                snapshot=[m.model_dump() for m in store.memories(life_owner)]));save('running')
        queries=[('李明那次从北京搬到上海的搬家最终是哪天完成的？',('搬家今天已完成','2026-09-24')),
                 ('李明在2026年9月24日住在哪里？',('李明现在住上海','2026-09-24')),
                 ('李明现在住在哪里？',('李明现在住杭州','2026-10-01'))]
        for question,expected in queries:
            if any(r['question']==question for r in report['lifecycle_searches']):continue
            result,seconds=search(dict(user_id=life_owner,query=question,top_k=5),'2026-10-02T23:59:59Z')
            candidates=[item.model_dump() for item in result.data];top=candidates[0]['content'] if candidates else ''
            report['lifecycle_searches'].append(dict(question=question,seconds=seconds,candidates=candidates,top1_passed=all(term in top for term in expected),trace=engine.last_search));save('running')
        report['protocol_checks'].append(dict(name='six_logical_collections_no_physical_tables',passed=set(store.records)==TABLES))
        report.update(complete=True,completed_at=time());save('complete')
    except Exception as error:
        report['errors'].append(dict(operation='runner',error_type=type(error).__name__,reason=str(error).split('\n')[0][:160] if isinstance(error,(ValueError,ProviderUnavailable)) else None))
        save('interrupted');print(json.dumps(dict(model=args.model,status='interrupted',error_type=type(error).__name__)),flush=True);raise
    finally:engine.close()
    print(json.dumps(dict(model=args.model,complete=report['complete'],retrieval=report['summary']['retrieval']['10'],result=str(old_path))),flush=True)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--dataset',type=Path,required=True);parser.add_argument('--baseline',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('--env-file',type=Path,required=True)
    parser.add_argument('--model',choices=['gpt-4o-mini','deepseek-flash'],required=True);parser.add_argument('--deepseek-key-file',type=Path)
    parser.add_argument('--resume',action='store_true');execute(parser.parse_args())
if __name__=='__main__':main()
