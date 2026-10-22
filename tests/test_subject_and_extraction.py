"""Direct raw-source identities, direct memory extraction, and bounded recovery."""
from copy import deepcopy
import json
from time import monotonic
import httpx,pytest
from openai import OpenAI
from remi.add_pipeline import AddPipeline
from remi.engine import MemoryEngine
from remi.heat import visible_memory
from remi.providers import GPTModel,ProviderUnavailable
from tests.helpers import request,HashEmbedder
from tests.test_subject_memory import SubjectModel,unit

class WireModel(SubjectModel):
    def __init__(self,*args,identity=None,extraction=None,**kwargs):
        super().__init__(*args,**kwargs);self.identity=identity;self.extraction=extraction
    def maintain_subjects(self,state,*,deadline):
        raw=super().maintain_subjects(state,deadline=deadline)
        return self.identity(state,raw) if self.identity else raw
    def propose(self,state,*,deadline):
        raw=super().propose(state,deadline=deadline)
        return self.extraction(state,raw) if self.extraction else raw

def ordinary(**kwargs):return WireModel([('user',True,[]),('Ren',False,[])],[('Ren and I practiced music',[0,1],[],None)],**kwargs)

def test_direct_subject_stage_reads_original_before_any_extraction():
    m=ordinary();e=MemoryEngine(model=m);e.add(request('Ren and I practiced music.'))
    assert [k for k,_ in m.calls]==['subjects','propose']
    first=m.calls[0][1];assert 'proposal' not in first and 'subject_choices' not in first
    assert first['new_user_source']['content']=='Ren and I practiced music.'
    assert all(s['status']=='resolved' for s in m.calls[-1][1]['resolved_subjects'])
    assert set(e.store.memories('u1')[0].entity_ids)==set(unit(e)['bindings']['subject_ids'].values())

def test_same_name_people_keep_two_local_refs_and_two_ids():
    m=SubjectModel([('Alex',False,[]),('Alex',False,[])],force_new=True)
    e=MemoryEngine(model=m);e.add(request('Alex is my colleague. A different Alex is my neighbor.'))
    objects=e.store.list('objects','u1');assert len(objects)==2 and len({o['id'] for o in objects})==2

def test_one_subject_accepts_multiple_names_in_one_source_patch():
    e=MemoryEngine(model=SubjectModel([('Mira',False,['M'])]));e.add(request('Mira is also called M.'))
    objects=e.store.list('objects','u1');assert len(objects)==1
    assert {a['name'] for a in objects[0]['aliases']}=={'Mira','M'}

@pytest.mark.parametrize('fault',['foreign_target','duplicate_ref','unknown_source'])
def test_subject_repair_freezes_independent_valid_sibling(fault):
    def mutate(state,raw):
        row=next(r for r in raw['subjects'] if r['ref']==1)
        if fault=='foreign_target':row.update(operation='reuse',target_id='foreign')
        elif fault=='duplicate_ref':raw['subjects'].append(deepcopy(row))
        else:row['evidence'][0]['source_id']='foreign'
        return raw
    m=ordinary(identity=mutate);e=MemoryEngine(model=m);e.add(request('Ren and I practiced music.'))
    assert len([c for c in m.calls if c[0]=='subjects'])==2
    repair=m.calls[1][1];assert any(r['ref']==0 for r in repair['accepted_subjects'])
    assert any(o['is_user'] for o in e.store.list('objects','u1'))
    assert not e.store.memories('u1')


def test_invalid_self_contact_preserves_valid_subject_identity():
    m=SubjectModel([('Mira',False,['M'])],[('Mira is also called M.',[0],[0,0],'Mira is also called M.')])
    e=MemoryEngine(model=m);e.add(request('Mira is also called M.'))
    assert len(e.store.list('objects','u1'))==1 and not e.store.memories('u1')
    assert unit(e)['connection_dispositions']==[dict(candidate_id='c0',status='pending')]
    assert any(d.id.startswith('doc_pending_') for d in e.store.documents('u1'))


def test_contact_update_keeps_id_revisions_and_time_view():
    e=MemoryEngine(model=SubjectModel([('user',True,[]),('Vela',False,[])],[('job',[0,1],[0,1],'I work at Vela.')]))
    e.add(request('I work at Vela.'));before=deepcopy(e.store.memories('u1')[0])
    def update(state,raw):
        raw['connections'][0].update(operation='update',target_id=before.id,previous_quote=before.current.content);return raw
    e.model=WireModel([('user',True,[]),('Vela',False,[])],[('ended',[0,1],[0,1],'I no longer work at Vela.')],identity=update)
    e.add(request('I no longer work at Vela.',request_id='r2',when='2024-06-05T00:00:00Z'))
    after=e.store.memories('u1')[0]
    assert after.id==before.id and len(after.revisions)==2 and after.revisions[0]==before.current
    assert visible_memory(after,'2024-06-03T00:00:00Z').current.content=='I work at Vela.'

@pytest.mark.parametrize('fault',['direction','mode','previous_quote','target'])
def test_wrong_contact_update_stays_pending_and_preserves_old_record(fault):
    e=MemoryEngine(model=SubjectModel([('Ren',False,[]),('Orin',False,[])],[('coaching',[0,1],[0,1],'Ren coaches Orin.')]))
    e.add(request('Ren coaches Orin.'));before=deepcopy(e.store.memories('u1')[0])
    def update(state,raw):
        row=raw['connections'][0];row.update(operation='update',target_id=before.id,previous_quote=before.current.content)
        if fault=='direction':row['subject_refs']=[1,0]
        elif fault=='mode':row['mode']='desired'
        elif fault=='previous_quote':row['previous_quote']='Unrelated claim'
        else:row['target_id']='foreign'
        return raw
    e.model=WireModel([('Ren',False,[]),('Orin',False,[])],[('new',[0,1],[0,1],'Orin coaches Ren.')],identity=update)
    e.add(request('Orin coaches Ren.',request_id='r2'))
    assert e.store.memories('u1')[0]==before
    assert unit(e,'r2')['connection_dispositions'][0]['status']=='pending'


def test_explicit_keep_makes_no_duplicate_relation_or_revision():
    e=MemoryEngine(model=SubjectModel([('Ren',False,[]),('Orin',False,[])],[('coaching',[0,1],[0,1],'Ren coaches Orin.')]))
    e.add(request('Ren coaches Orin.'));before=deepcopy(e.store.memories('u1')[0])
    def keep(state,raw):
        raw['connections'][0].update(operation='keep',target_id=before.id,previous_quote=before.current.content);return raw
    e.model=WireModel([('Ren',False,[]),('Orin',False,[])],[('same',[0,1],[0,1],'Ren still coaches Orin.')],identity=keep)
    e.add(request('Ren still coaches Orin.',request_id='r2'))
    assert e.store.memories('u1')==[before]
    assert unit(e,'r2')['connection_dispositions'][0]['status']=='keep'


def test_extraction_outage_resumes_without_repeating_subjects():
    ready=False
    def outage(state,raw):
        if not ready:raise ProviderUnavailable('synthetic')
        return raw
    m=ordinary(extraction=outage);e=MemoryEngine(model=m)
    with pytest.raises(ProviderUnavailable):e.add(request('Ren and I practiced music.'))
    ready=True;e.add(request('Ren and I practiced music.'))
    assert [k for k,_ in m.calls]==['subjects','propose','propose']
    assert len(e.store.memories('u1'))==1


def test_reuse_preserves_stored_aliases_and_drives_memory_refs():
    e=MemoryEngine(model=SubjectModel([('Ren',False,[])]));e.add(request('Ren is a neighbor.'))
    before=e.store.list('objects','u1')
    e.model=SubjectModel([('Ren',False,[])],[('Ren likes tea',[0],[],None)])
    e.add(request('Ren likes tea.',request_id='r2'))
    assert e.store.list('objects','u1')==before
    assert e.store.memories('u1')[0].entity_ids==[before[0]['id']]


def test_provider_sends_raw_source_catalogs_and_no_subject_proposal():
    sent=[]
    def handler(request):
        sent.append(json.loads(request.content));return httpx.Response(200,json={'id':'test','object':'chat.completion','created':1,'model':'gpt-4o-mini',
            'choices':[{'index':0,'message':{'role':'assistant','content':json.dumps(dict(subjects=[],connections=[]))},'finish_reason':'stop'}]})
    client=OpenAI(api_key='test',base_url='https://test.invalid/v1',http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    m=GPTModel('test','https://test.invalid/v1',client=client)
    m.maintain_subjects(dict(new_user_source=dict(id='original',role='user',content='A new person arrived.',message_at=None),context_sources=[],subject_catalog=[],relation_catalog=[]),deadline=monotonic()+5)
    m.close();payload=sent[0]['messages'][-1]['content']
    assert 'proposal' not in payload and 'subject_choices' not in payload
    assert 'CURRENT SOURCE' in payload and 'A new person arrived.' in payload
    with pytest.raises(json.JSONDecodeError):json.loads(payload)
    schema=sent[0]['response_format']['json_schema']['schema']
    assert schema['properties']['subjects']['maxItems']==96
    assert 'subject_refs' not in schema['$defs']['SubjectDraft']['properties']


def test_memory_stage_extra_subject_changes_are_rejected():
    def extra(state,raw):raw['subjects']=[{'name':'Injected'}];return raw
    e=MemoryEngine(model=ordinary(extraction=extra))
    with pytest.raises(ProviderUnavailable):e.add(request('Ren and I practiced music.'))
    assert not e.store.memories('u1') and not e.store.list('objects','u1')
    assert e.store.list('sources','u1')


def test_ordinary_candidate_cannot_invent_subject_refs():
    e=MemoryEngine(model=SubjectModel([('user',True,[])],[('Unknown participant',[99],[],None)]))
    with pytest.raises(ProviderUnavailable):e.add(request('I have an unconfirmed participant.'))
    assert not e.store.memories('u1') and e.store.list('sources','u1')


def test_current_user_reuse_cannot_select_another_person():
    e=MemoryEngine(model=SubjectModel([('Ren',False,[])]));e.add(request('Ren is my neighbor.'))
    other=e.store.list('objects','u1')[0]['id']
    def wrong(state,raw):
        evidence=[dict(source_id=state['new_user_source']['id'])]
        return dict(subjects=[dict(ref=0,operation='reuse',target_id=other,name='@user',kind='person',aliases=[],evidence=evidence)],connections=[])
    e.model=WireModel([('user',True,[])],identity=wrong);e.add(request('I spoke to Ren.',request_id='r2'))
    assert len(e.store.list('objects','u1'))==1 and not e.store.list('objects','u1')[0]['is_user']

@pytest.mark.parametrize('bad',[{'subjects':None,'connections':[]},{'subjects':['invalid'],'connections':[]}])
def test_malformed_subject_container_has_bounded_repair_and_keeps_source(bad):
    e=MemoryEngine(model=WireModel([],identity=lambda state,raw:deepcopy(bad)))
    with pytest.raises(ProviderUnavailable):e.add(request('Original source survives.'))
    assert e.store.list('sources','u1') and not e.store.list('objects','u1')


def test_boolean_ref_cannot_invalidate_an_independent_integer_ref():
    def invalid(state,raw):
        row=deepcopy(raw['subjects'][0]);row['ref']=True;raw['subjects'].append(row);return raw
    e=MemoryEngine(model=WireModel([('Ren',False,[]),('Orin',False,[])],identity=invalid))
    e.add(request('Ren and Orin are different people.'))
    assert {o['name'] for o in e.store.list('objects','u1')}=={'Ren','Orin'}

@pytest.mark.parametrize('field,value',[('target_id',['invalid']),('kind',['person'])])
def test_invalid_field_types_are_quarantined_without_losing_valid_sibling(field,value):
    def invalid(state,raw):raw['subjects'][0][field]=value;return raw
    e=MemoryEngine(model=WireModel([('Ren',False,[]),('Orin',False,[])],identity=invalid))
    e.add(request('Ren and Orin are different people.'))
    assert [o['name'] for o in e.store.list('objects','u1')]==['Orin']
    assert unit(e)['identity_errors']
