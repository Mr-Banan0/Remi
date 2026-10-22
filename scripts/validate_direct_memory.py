"""Real-provider validation of subject maintenance and append-only typed memory extraction."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from time import monotonic
from remi.clock import parse_time
from remi.engine import MemoryEngine
from remi.models import AddMessage, AddRequest
from remi.providers import providers_from_environment, ProviderUnavailable
from remi.settings import load_environment, Settings
from remi.storage import SQLiteStore
from scripts.validate_subject_memory import fingerprint

# These inputs and expectations stay in the experiment harness.
STEPS=[
    ('I prefer paper notebooks when taking notes.','2027-05-01T10:00:00Z'),
    ('I plan to rehearse at the community hall on May 11, 2027.','2027-05-01T11:00:00Z'),
    ('I moved that rehearsal to May 13, 2027.','2027-05-04T10:00:00Z'),
    ('I completed that rehearsal today.','2027-05-13T10:00:00Z'),
    ('I scheduled a separate rehearsal at the community hall for May 19, 2027.','2027-05-14T10:00:00Z')]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('--env-file',type=Path,required=True)
    args=parser.parse_args();load_environment(args.env_file);args.output.mkdir(parents=True,exist_ok=False)
    frozen=fingerprint();path=args.output/'memory.sqlite3';SQLiteStore.initialize(path,allow_create_schema=True)
    model,embedder,ranker=providers_from_environment()
    engine=MemoryEngine(SQLiteStore(path),model,embedder,ranker,Settings(add_timeout=180))
    report={'code_sha256':frozen,'checks':[],'steps':[]};owner='direct-memory-lifecycle';first=None;preference=None
    def check(name,value):report['checks'].append({'name':name,'passed':bool(value)})
    try:
        for i,(text,stamp) in enumerate(STEPS):
            started=monotonic();before=len(model.calls);failure=None
            req=AddRequest(user_id=owner,session_id='fresh-lifecycle',request_id=str(i),messages=[
                AddMessage(role='user',content=text,timestamp=int(parse_time(stamp).timestamp()*1000))])
            for attempt in range(2):
                try:engine.add(req);break
                except ProviderUnavailable as error:
                    failure=type(error).__name__
                    if attempt:raise
            records=engine.store.memories(owner);events=[m for m in records if m.record_type=='event']
            traces=list(model.calls)[before:]
            check(f'{i}_memory_extraction_called',any(c['kind']=='add_propose' for c in traces))
            check(f'{i}_two_stage_call_contract',all(c['kind'] in {'add_subjects','add_subjects_repair','add_propose','add_propose_repair'} for c in traces))
            if i==0:
                states=[m for m in records if m.record_type=='preference']
                check('preference_saved_independently',len(states)==1)
                if len(states)==1:preference=states[0].model_dump()
            if i==1:
                check('one_planned_occurrence',len(events)==1 and events[0].current.status=='planned')
                if len(events)==1:
                    first=events[0].model_dump()
                    check('initial_planned_date',events[0].current.planned_at=='2027-05-11T00:00:00+00:00')
            if i in {2,3,4}:
                check(f'{i}_new_source_record',len(events)==i and len({e.id for e in events})==i)
                check(f'{i}_initial_record_unchanged',any(e.model_dump()==first for e in events) if first else False)
                if i==2:check('new_rescheduled_plan',any(e.current.planned_at=='2027-05-13T00:00:00+00:00' and e.current.status=='planned' for e in events))
                if i==3:check('new_completion_statement',any(e.current.event_at=='2027-05-13T00:00:00+00:00' and e.current.status=='occurred' for e in events))
                if i==4:check('separate_new_plan',any(e.current.planned_at=='2027-05-19T00:00:00+00:00' and e.current.status=='planned' for e in events))
            check(f'{i}_one_revision_per_event',all(len(e.revisions)==1 for e in events))
            if preference:check(f'{i}_preference_unchanged',engine.store.get('memories',owner,preference['id'])==preference)
            check(f'{i}_graph_integrity',all(c['passed'] for u in engine.store.get('requests',owner,str(i))['units'].values() for c in u.get('user_binding_checks',[])))
            report['steps'].append({'index':i,'seconds':monotonic()-started,'provider_calls':traces,'transient_failure':failure})
        report['tables']=[r[0] for r in engine.store.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        check('six_tables',len(report['tables'])==6)
        check('sqlite_quick_check',engine.store.connection.execute('PRAGMA quick_check').fetchone()[0]=='ok')
    except Exception as error:report['error_type']=type(error).__name__
    finally:
        report['code_unchanged_during_run']=frozen==fingerprint()
        report['passed']=len(report['steps'])==len(STEPS) and all(c['passed'] for c in report['checks']) and report['code_unchanged_during_run']
        (args.output/'inputs.json').write_text(json.dumps(STEPS,ensure_ascii=False,indent=2))
        (args.output/'records.json').write_text(json.dumps({t:engine.store.list(t,owner) for t in ['sources','objects','memories','edges','requests']},ensure_ascii=False,indent=2))
        (args.output/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
        engine.close()
    print(json.dumps({'passed':report['passed'],'failed':[c['name'] for c in report['checks'] if not c['passed']],
        'error_type':report.get('error_type'),'checks':len(report['checks']),'text_calls':sum(len(s['provider_calls']) for s in report['steps'])}),flush=True)

if __name__=='__main__':main()
