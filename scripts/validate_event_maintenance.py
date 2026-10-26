"""Independent source-first event lifecycle smoke test; fixed expectations live only here."""
from pathlib import Path
import argparse, hashlib, json, os, sys
from datetime import datetime, timezone
from time import monotonic
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT)]
from experiments.subject_engine import SubjectGraphEngine,PIPELINE
from experiments.subject_providers import SubjectTextModel
from remi.models import AddRequest,AddMessage
from remi.settings import load_environment,private_key,Settings

CASES=[
    dict(id='english_lifecycle',steps=[
        ('Tessa plans a pottery lesson on November 6.','2031-11-01'),
        ('Tessa moved that pottery lesson to November 8.','2031-11-02'),
        ('Tessa completed that pottery lesson today.','2031-11-08'),
        ('Tessa booked another pottery lesson for November 15.','2031-11-09')]),
    dict(id='chinese_cancel',steps=[
        ('方澈计划在2031年11月6日参加摄影课。','2031-11-01'),
        ('方澈说那次摄影课增加了夜景拍摄环节。','2031-11-02'),
        ('方澈取消了那次摄影课。','2031-11-03')]),
    dict(id='unknown_clock',steps=[('Niko plans a cycling lesson tomorrow.',None)]),
]

def write(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2))

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);parser.add_argument('--env-file',type=Path,required=True)
    args=parser.parse_args();os.umask(0o077);args.output.mkdir(parents=True,exist_ok=False)
    write(args.output/'inputs.json',CASES)
    files=[p for group in ('src','experiments','scripts') for p in (ROOT/group).rglob('*') if p.suffix in {'.py','.json'}]
    fingerprint={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(files)}
    write(args.output/'code-manifest.json',fingerprint)
    load_environment(args.env_file);reports=[];started=monotonic()
    for case in CASES:
        model=SubjectTextModel('gpt-4o-mini',private_key('LLM_API_KEY','LLM_API_KEY_FILE'),os.getenv('LLM_BASE_URL','https://api.quickrouter.us/v1'),timeout=60)
        engine=SubjectGraphEngine(model=model,settings=Settings(add_timeout=180));path=args.output/case['id'];path.mkdir()
        report=dict(id=case['id'],steps=[],checks={});previous_count=0
        try:
            for i,(text,day) in enumerate(case['steps']):
                stamp=int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp()*1000) if day else None
                request=AddRequest(request_id=str(i),user_id=case['id'],session_id='event-smoke',messages=[AddMessage(role='user',content=text,timestamp=stamp)])
                begin=monotonic();engine.add(request)
                unit=engine.store.get('requests',case['id'],str(i))['units']['0']
                report['steps'].append(dict(index=i,seconds=round(monotonic()-begin,2),memories=len(engine.store.memories(case['id'])),
                    repair_stage=unit.get('repair_stage'),subject_issues=unit['identity']['issues'],memory_rejections=unit.get('memory_rejections',[]),
                    pending=sum(d['status']=='pending' for d in unit.get('candidate_dispositions',[]))))
                write(path/'records.json',engine.store.snapshot());write(path/'progress.json',report)
                print(json.dumps(dict(case=case['id'],step=i,memories=report['steps'][-1]['memories'],repair=unit.get('repair_stage'))),flush=True)
            events=[m for m in engine.store.memories(case['id']) if m.record_type=='event']
            checks=dict(subject_count=len(engine.store.list('objects',case['id']))==1,
                no_pending=all(not s['pending'] for s in report['steps']),protocol=all(not s['memory_rejections'] and not s['subject_issues'] for s in report['steps']))
            if case['id']=='english_lifecycle':
                maintained=[m for m in events if len(m.revisions)==3]
                checks.update(two_occurrences=len(events)==2,same_event_three_versions=len(maintained)==1,
                    completion=bool(maintained and maintained[0].current.status=='occurred'),
                    planned_and_actual=bool(maintained and maintained[0].current.planned_at=='2031-11-08T00:00:00+00:00' and maintained[0].current.event_at=='2031-11-08T00:00:00+00:00'))
            elif case['id']=='chinese_cancel':
                checks.update(one_event_three_versions=len(events)==1 and len(events[0].revisions)==3,
                    cancelled=bool(events and events[0].current.status=='cancelled'),
                    date_preserved=bool(events and events[0].current.planned_at=='2031-11-06T00:00:00+00:00'))
            else:
                checks.update(unresolved_clock=len(events)==1 and events[0].current.planned_at is None and bool(events[0].current.time_evidence) and events[0].current.time_evidence[0].anchor is None)
            report.update(checks=checks,passed=all(checks.values()),calls=list(model.calls))
        except Exception as error:
            report.update(passed=False,error_type=type(error).__name__);write(path/'records.json',engine.store.snapshot())
        finally:engine.close()
        write(path/'result.json',report);reports.append(report)
    changed=[str(p.relative_to(ROOT)) for p in files if hashlib.sha256(p.read_bytes()).hexdigest()!=fingerprint[str(p.relative_to(ROOT))]]
    result=dict(pipeline=PIPELINE,model='gpt-4o-mini',context_messages=1,physical_tables_created=0,seconds=monotonic()-started,
        passed=sum(r['passed'] for r in reports),total=len(reports),runtime_changed=changed,results=reports)
    write(args.output/'result.json',result);print(json.dumps({k:v for k,v in result.items() if k!='results'}),flush=True)
if __name__=='__main__':main()
