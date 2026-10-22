"""Real direct subject calls through atomic Add; ordinary-memory providers are explicit offline stubs."""
from __future__ import annotations
import argparse,json
from pathlib import Path
from time import monotonic
from remi.engine import MemoryEngine
from remi.models import AddRequest,AddMessage
from remi.providers import providers_from_environment
from remi.settings import load_environment
from scripts.validate_subject_memory import fingerprint

CASES=[
 ('alias_pet',['My colleague Rhea is also called Ray.','Ray owns the cat Moss, also called Momo.']),
 ('two_namesakes',['Nico the nurse and Nico the baker are different people.']),
 ('employment',['I work at Winter Guild.','I no longer work at Winter Guild.']),
 ('quoted_owner',["Tala wrote: 'I own Silver Finch Studio.'"]),
]

class SubjectOnly:
    def __init__(self,model):self.model=model
    def maintain_subjects(self,state,*,deadline):return self.model.maintain_subjects(state,deadline=deadline)
    def propose(self,state,*,deadline):return {'items':[],'reason':'Subject-only diagnostic; ordinary memory is tested separately.'}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True);parser.add_argument('--env-file',type=Path,required=True)
    args=parser.parse_args();load_environment(args.env_file);args.output.mkdir(parents=True,exist_ok=False)
    frozen=fingerprint();results=[]
    for name,texts in CASES:
        model,embedder,ranker=providers_from_environment();engine=MemoryEngine(model=SubjectOnly(model));checks=[];steps=[]
        def check(label,value):checks.append(dict(name=label,passed=bool(value)))
        before=None
        try:
            for index,text in enumerate(texts):
                started=monotonic();offset=len(model.calls)
                engine.add(AddRequest(user_id=name,session_id='direct-subjects',request_id=str(index),messages=[AddMessage(role='user',content=text,timestamp=1830492000000+index*3600000)]))
                unit=engine.store.get('requests',name,str(index))['units']['0'];objects=engine.store.list('objects',name);memories=engine.store.memories(name)
                check(f'{index}_source_and_graph_checks',all(row['passed'] for row in unit['user_binding_checks']))
                if name=='alias_pet':
                    check(f'{index}_person_aliases',any({'Rhea','Ray'}<={a['name'] for a in o['aliases']} for o in objects))
                    if index==1:
                        check('animal_aliases',any(o['kind']=='animal' and {'Moss','Momo'}<={a['name'] for a in o['aliases']} for o in objects))
                        check('ownership_contact',any(m.endpoint_ids and 'owns' in m.current.content for m in memories))
                elif name=='two_namesakes':check('two_distinct_people',len([o for o in objects if not o['is_user'] and o['kind']=='person'])==2)
                elif name=='employment':
                    if index==0:
                        links=[m for m in memories if m.endpoint_ids];check('employment_created',len(links)==1)
                        if links:before=links[0].model_dump()
                    else:
                        after=next((m for m in memories if before and m.id==before['id']),None)
                        check('same_relation_updated',after is not None and len(after.revisions)==2 and 'no longer' in after.current.content)
                else:
                    check('no_false_api_user',not any(o['is_user'] for o in objects))
                    check('quoted_owner_contact',any(m.endpoint_ids and m.current.mode=='document' for m in memories))
                steps.append(dict(index=index,seconds=monotonic()-started,calls=list(model.calls)[offset:]))
        except Exception as error:check('exception_'+type(error).__name__,False)
        finally:
            result=dict(id=name,checks=checks,steps=steps,passed=bool(checks) and all(c['passed'] for c in checks),
                records={t:engine.store.list(t,name) for t in ['sources','objects','memories','edges','requests']})
            (args.output/(name+'.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2));results.append(result)
            engine.close()
            for resource in [model,embedder,ranker]:
                if resource is not None:resource.close()
    summary=dict(code_sha256=frozen,code_unchanged_during_run=frozen==fingerprint(),cases=len(results),passed_cases=sum(r['passed'] for r in results),
        checks=sum(len(r['checks']) for r in results),passed_checks=sum(c['passed'] for r in results for c in r['checks']),
        text_calls=sum(len(s['calls']) for r in results for s in r['steps']),databases_created=0,ordinary_model_calls=0,
        scope='Real direct subject calls and optional shared repair through Add; ordinary memory extraction is stubbed; all records use MemoryStore.')
    (args.output/'inputs.json').write_text(json.dumps(CASES,ensure_ascii=False,indent=2));(args.output/'summary.json').write_text(json.dumps(summary,indent=2));print(json.dumps(summary),flush=True)

if __name__=='__main__':main()
