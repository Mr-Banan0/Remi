"""Subject identity and verbatim connections use independent conversations."""
from copy import deepcopy
import os
from time import monotonic
import pytest
from remi.engine import MemoryEngine
from remi.add_pipeline import AddPipeline
from remi.identity import stable_id, subject_hits
from remi.models import GraphObject
from remi.providers import ProviderUnavailable
from remi.storage import SQLiteStore
from tests.helpers import request, HashEmbedder, query


class SubjectModel:
    def __init__(self,subjects,items=(),*,force_new=False,pending=(),kinds=None):
        self.subjects=subjects;self.items=items;self.force_new=force_new;self.pending=set(pending);self.kinds=kinds or {}
        self.calls=[];self.transform=None;self.identity_transform=None
    def maintain_subjects(self,state,*,deadline):
        from tests.helpers import direct_subjects_fixture
        self.calls.append(('subjects',deepcopy(state)))
        names=['@user' if user else name for name,user,aliases in self.subjects]
        result=direct_subjects_fixture(state,names,aliases={key:row[2] for key,row in zip(names,self.subjects)},
            kinds=self.kinds,force_new=self.force_new,pending=self.pending)
        sid=state['new_user_source']['id']
        result['connections']=[dict(ref=i,operation='create',target_id=None,subject_refs=endpoints,source_id=sid,
            quote=quote,mode='asserted',previous_quote=None) for i,(_,refs,endpoints,quote) in enumerate(self.items) if endpoints]
        return self.identity_transform(state,result) if self.identity_transform else result
    def propose(self,state,*,deadline):
        self.calls.append(('propose',deepcopy(state)));sid=state['new_user_source']['id']
        result={'items':[dict(content=content,subject_refs=refs,evidence=[dict(source_id=sid)],
            record_type='fact',mode='asserted',context=None,status=None,time_expression=None)
            for content,refs,endpoints,quote in self.items if not endpoints],'reason':'independent typed fixture'}
        return self.transform(state,result) if self.transform else result
    def decide(self,state,*,deadline):return {'query_mode':'direct','action_time_expression':None,
                                               'searches':[],'relevant_memory_ids':[]}


def setup_model():
    return SubjectModel([('user',True,['Lin']),('Vela Labs',False,['Vela']),('Taro',False,['dog','pet','T'])],
        [('Lin works at Vela Labs',[0,1],[0,1],'I work at Vela Labs'),('Taro is my dog',[0,2],[0,2],'My pet dog is Taro')])


def setup_text(): return 'I am Lin. I work at Vela Labs, also called Vela. My pet dog is Taro, also called T.'

def unit(engine,rid='r1',owner='u1'): return engine.store.get('requests',owner,rid)['units']['0']


def test_subject_aliases_and_explicit_connections_form_one_graph():
    model=setup_model(); engine=MemoryEngine(model=model,embedder=HashEmbedder())
    engine.add(request(setup_text()))
    objects=engine.store.list('objects','u1'); assert len(objects)==3
    assert {o['name'] for o in objects}=={'user','Vela Labs','Taro'}
    dog=next(o for o in objects if o['name']=='Taro')
    assert {a['name'] for a in dog['aliases']}=={'Taro','dog','pet','T'}
    assert all(a['quote']==setup_text() for a in dog['aliases'])
    assert all(len(m.endpoint_ids)==2 and m.connection_evidence.quote in setup_text() for m in engine.store.memories('u1'))
    assert len([e for e in engine.store.edges_for('u1') if e.relation=='related'])==2
    assert [x[0] for x in model.calls]==['subjects','propose']
    assert all(c['passed'] for c in unit(engine)['user_binding_checks'])


@pytest.mark.parametrize('alias',['Taro','dog','pet','T'])
def test_alias_search_returns_stable_subject_and_related_source(alias):
    engine=MemoryEngine(model=setup_model(),embedder=HashEmbedder()); engine.add(request(setup_text()))
    ids=[o.id for o in subject_hits(engine.store,'u1',alias,'2024-06-03T00:00:00Z')]
    assert len(ids)==1 and engine.store.get('objects','u1',ids[0])['name']=='Taro'
    assert any('Taro' in hit.content for hit in engine.search(query(alias)).data)


def test_alias_only_update_reuses_identity_without_rewriting_old_memory():
    engine=MemoryEngine(model=setup_model()); engine.add(request(setup_text()))
    before=deepcopy(engine.store.list('memories','u1'))
    dog=next(o for o in engine.store.list('objects','u1') if o['name']=='Taro')
    engine.model=SubjectModel([('Taro',False,['Sunny'])])
    engine.add(request('Taro is also called Sunny.',request_id='r2'))
    assert engine.store.list('memories','u1')==before
    assert subject_hits(engine.store,'u1','Sunny','2024-06-03T00:00:00Z')[0].id==dog['id']
    assert len(engine.store.list('objects','u1'))==3


def test_other_persons_explicit_alias_keeps_same_identity():
    engine=MemoryEngine(model=SubjectModel([('Renata',False,[])])); engine.add(request('Renata is a neighbor.'))
    ident=engine.store.list('objects','u1')[0]['id']
    engine.model=SubjectModel([('Renata',False,['Ren'])]); engine.add(request('Renata is also called Ren.',request_id='r2'))
    assert subject_hits(engine.store,'u1','Ren','2024-06-03T00:00:00Z')[0].id==ident


def test_same_name_can_create_two_subjects_and_later_stays_ambiguous():
    engine=MemoryEngine(model=SubjectModel([('Alex',False,[])])); engine.add(request('Alex is a coworker.'))
    first=engine.store.list('objects','u1')[0]['id']
    engine.model=SubjectModel([('Alex',False,[])],force_new=True)
    engine.add(request('A different Alex is my neighbor.',request_id='r2'))
    objects=engine.store.list('objects','u1')
    assert len(objects)==2 and len({o['id'] for o in objects})==2
    assert all(o['id']!=stable_id('obj_','u1','alex') for o in objects)
    engine.model=SubjectModel([('Alex',False,[])],[('Alex likes tea',[0],[],None)])
    engine.add(request('Alex likes tea.',request_id='r3'))
    assert not engine.store.memories('u1') and unit(engine,'r3')['candidate_dispositions'][0]['status']=='pending'
    assert len(engine.store.list('objects','u1'))==2


def test_shared_descriptive_alias_retrieves_multiple_pets():
    engine=MemoryEngine(model=SubjectModel([('Taro',False,['dog']),('Luna',False,['dog'])]))
    engine.add(request('Taro is a dog and Luna is another dog.'))
    assert len(subject_hits(engine.store,'u1','dog','2024-06-03T00:00:00Z'))==2


def test_short_chinese_alias_and_source_boundaries():
    engine=MemoryEngine(model=SubjectModel([('阿三',False,['狗','三儿']),('Ann',False,[])]))
    engine.add(request('这只狗叫阿三，也叫三儿。Ann来看它。'))
    assert len(subject_hits(engine.store,'u1','三儿','2024-06-03T00:00:00Z'))==1
    assert len(subject_hits(engine.store,'u1','狗','2024-06-03T00:00:00Z'))==1
    assert not subject_hits(engine.store,'u1','Anniversary','2024-06-03T00:00:00Z')


def test_unresolved_subject_preserves_independent_claim_and_source():
    model=SubjectModel([('user',True,[]),('Someone',False,[])],
        [('I like tea',[0],[],None),('Someone likes coffee',[1],[],None)],pending=[1])
    engine=MemoryEngine(model=model);engine.add(request('I like tea. Someone likes coffee.'))
    assert [m.current.content for m in engine.store.memories('u1')]==['I like tea']
    assert unit(engine)['candidate_dispositions'][1]['status']=='pending'
    assert any(d.id.startswith('doc_pending_') for d in engine.store.documents('u1'))


@pytest.mark.parametrize('fault',['extra_field','foreign_id','pending_subject','duplicate_subject'])
def test_invalid_subject_plan_preserves_valid_sibling(fault):
    class Broken(SubjectModel):
        def maintain_subjects(self,state,*,deadline):
            result=super().maintain_subjects(state,deadline=deadline)
            target=next(s for s in result['subjects'] if s['ref']==1)
            if fault=='extra_field':target['unexpected']=['Unstated']
            elif fault=='foreign_id':target.update(operation='reuse',target_id='foreign')
            elif fault=='pending_subject':target['operation']='pending'
            else:result['subjects'].append(target)
            return result
    model=Broken([('user',True,[]),('Renata',False,[])],[('I like tea',[0],[],None),('Renata likes tea',[1],[],None)])
    engine=MemoryEngine(model=model);engine.add(request('I like tea. Renata likes tea.'))
    assert len(model.calls)==(2 if fault=='pending_subject' else 3)
    assert [m.current.content for m in engine.store.memories('u1')]==['I like tea']
    assert unit(engine)['candidate_dispositions'][1]['status']=='pending'


@pytest.mark.parametrize('fault',['missing_related','wrong_related_endpoint','missing_node','missing_mentions','wrong_owner'])
def test_graph_fault_blocks_atomic_commit(monkeypatch,fault):
    original=AddPipeline._materialize
    def broken(*args):
        objects,memories,edges,expected=original(*args)
        if fault=='missing_related': edges=[e for e in edges if e.relation!='related']
        elif fault=='missing_mentions': edges=[e for e in edges if e.relation!='mentions']
        elif fault=='missing_node': objects=[]
        else:
            for e in edges:
                if e.relation=='related':
                    if fault=='wrong_related_endpoint': e.target='missing'
                    else: e.owner='other'
        return objects,memories,edges,expected
    monkeypatch.setattr(AddPipeline,'_materialize',broken)
    engine=MemoryEngine(model=setup_model(),embedder=HashEmbedder())
    with pytest.raises(ProviderUnavailable): engine.add(request(setup_text()))
    assert not engine.store.memories('u1') and not engine.store.list('objects','u1') and not engine.store.list('edges','u1')
    assert not engine.embedder.calls and engine.store.list('sources','u1')



def test_identity_aliases_are_time_scoped_and_owner_scoped():
    engine=MemoryEngine(model=SubjectModel([('Taro',False,[])]));engine.add(request('Taro is a dog.'))
    engine.model=SubjectModel([('Taro',False,['Sunny'])]);engine.add(request('Taro is also called Sunny.',request_id='r2',when='2024-06-04T00:00:00Z'))
    assert not subject_hits(engine.store,'u1','Sunny','2024-06-03T00:00:00Z')
    assert subject_hits(engine.store,'u1','Sunny','2024-06-05T00:00:00Z')
    assert not subject_hits(engine.store,'u2','Sunny','2024-06-05T00:00:00Z')


def test_same_payload_is_idempotent_and_index_failure_reuses_subject_plan():
    class Failing(HashEmbedder):
        ready=False
        def embed(self,texts,*,deadline):
            if not self.ready: raise ProviderUnavailable('simulated outage')
            return super().embed(texts,deadline=deadline)
    embedder=Failing();model=setup_model();engine=MemoryEngine(model=model,embedder=embedder)
    with pytest.raises(ProviderUnavailable): engine.add(request(setup_text()))
    embedder.ready=True;engine.add(request(setup_text()));engine.add(request(setup_text()))
    assert len(model.calls)==2 and len(engine.store.list('objects','u1'))==3 and len(engine.store.memories('u1'))==2


def test_first_person_node_accepts_name_alias_without_duplicate_person():
    engine=MemoryEngine(model=SubjectModel([('user',True,['Lin'])]));engine.add(request('I am Lin.'))
    assert len(engine.store.list('objects','u1'))==1
    obj=engine.store.list('objects','u1')[0]
    assert obj['id']==stable_id('obj_','u1','user') and obj['aliases'][0]['name']=='Lin'


def test_no_connection_is_created_from_joint_mentions_alone():
    engine=MemoryEngine(model=SubjectModel([('Renata',False,[]),('Vela',False,[])],[('Renata discussed Vela',[0,1],[],None)]))
    engine.add(request('Renata discussed Vela.'))
    assert not [e for e in engine.store.edges_for('u1') if e.relation=='related']


@pytest.mark.skipif(os.getenv('REMI_TEST_SCHEMA_APPROVED')!='1',reason='Enable approved six-table tests')
def test_subjects_aliases_connections_survive_sqlite_reopen(tmp_path):
    path=tmp_path/'memory.sqlite3';SQLiteStore.initialize(path,allow_create_schema=True)
    engine=MemoryEngine(SQLiteStore(path),setup_model());engine.add(request(setup_text()));engine.close()
    engine=MemoryEngine(SQLiteStore(path),setup_model())
    assert len(subject_hits(engine.store,'u1','pet','2024-06-03T00:00:00Z'))==1
    assert len([e for e in engine.store.edges_for('u1') if e.relation=='related'])==2
    assert len(engine.store.connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall())==6
    engine.close()


def test_subject_alias_conflict_during_embedding_retries_without_losing_evidence():
    engine=MemoryEngine(model=SubjectModel([('Renata',False,[])]));engine.add(request('Renata is a neighbor.'))
    obj=engine.store.list('objects','u1')[0]
    class Concurrent(HashEmbedder):
        changed=False
        def embed(self,texts,*,deadline):
            if not self.changed and any(t.startswith('user:') for t in texts):
                self.changed=True
                latest=engine.store.get('objects','u1',obj['id'])
                latest['aliases'].append({'name':'RENATA','source_id':latest['aliases'][0]['source_id'],'quote':'Renata is a neighbor.','observed_at':latest['observed_at']})
                engine.store.put('objects','u1',obj['id'],latest)
            return super().embed(texts,deadline=deadline)
    engine.embedder=Concurrent();engine.model=SubjectModel([('Renata',False,['Ren'])])
    with pytest.raises(ProviderUnavailable):engine.add(request('Renata is also called Ren.',request_id='r2'))
    engine.add(request('Renata is also called Ren.',request_id='r2'))
    aliases={a['name'] for a in engine.store.get('objects','u1',obj['id'])['aliases']}
    assert {'RENATA','Ren','Renata'}<=aliases


def test_backdated_current_user_reuses_same_stable_identity():
    engine=MemoryEngine(model=SubjectModel([('user',True,[])]));engine.add(request('I have a preference.'))
    engine.add(request('I have an older preference.',request_id='r2',when='2024-05-01T00:00:00Z'))
    assert len(engine.store.list('objects','u1'))==1
    assert engine.store.list('objects','u1')[0]['observed_at'].startswith('2024-05-01')


@pytest.mark.skipif(os.getenv('REMI_TEST_SCHEMA_APPROVED')!='1',reason='Enable approved six-table tests')
def test_old_json_contract_requires_explicit_source_replay(tmp_path):
    path=tmp_path/'old.sqlite3';SQLiteStore.initialize(path,allow_create_schema=True)
    store=SQLiteStore(path);store.put('objects','u1','old',{'id':'old','schema_version':11});store.close()
    with pytest.raises(RuntimeError,match='subject_memory_source_replay'):SQLiteStore(path)


def test_personal_alias_search_reaches_current_user():
    engine=MemoryEngine(model=SubjectModel([('user',True,['Lin'])]))
    engine.add(request('My name is Lin.'))
    assert [o.id for o in subject_hits(engine.store,'u1','Lin','2024-06-03T00:00:00Z')]==[stable_id('obj_','u1','user')]
    assert not subject_hits(engine.store,'u1','user','2024-06-03T00:00:00Z')
