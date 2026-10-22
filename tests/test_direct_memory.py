"""Direct typed extraction appends source-grounded records with two model stages."""
from copy import deepcopy
from datetime import timedelta
from time import monotonic
import json

import pytest

from remi.add_pipeline import PIPELINE
from remi.clock import parse_time
from remi.engine import MemoryEngine
from remi.models import ProposalDraft
from remi.providers import GPTModel, OutputInvalid, ProviderUnavailable, strict_schema
from tests.helpers import HashEmbedder, request, query
from tests.test_subject_memory import SubjectModel, unit


def card(cid=None, *, kind='event', content='One occurrence', status='planned', time=None, mode='asserted', context=None):
    return dict(record_type=kind, content=content, mode=mode, context=context,
        status=status if kind=='event' else None, time_expression=time)


class DirectModel(SubjectModel):
    def __init__(self, claims, records):
        names=list(dict.fromkeys(name for _,names in claims for name in names))
        super().__init__([(name,name in {'user','@user'},[]) for name in names],
            [(content,[names.index(name) for name in refs],[],None) for content,refs in claims])
        self.records=records
    def propose(self,state,*,deadline):
        transform=self.transform;self.transform=None
        try:raw=super().propose(state,deadline=deadline)
        finally:self.transform=transform
        for item,record in zip(raw['items'],self.records):item.update(deepcopy(record))
        return transform(state,raw) if transform else raw


def model(text, **kwargs):
    return DirectModel([(text,['user'])],[card(content=text,**kwargs)])


def test_two_model_calls_create_memory_without_a_final_model_decision():
    class NoReview(DirectModel):
        def maintain(self,*args,**kwargs):raise AssertionError('removed memory review was called')
    m=NoReview([('I prefer cool rooms.',['user'])],[card(kind='preference',content='I prefer cool rooms.')])
    e=MemoryEngine(model=m);e.add(request('I prefer cool rooms.'))
    assert [stage for stage,_ in m.calls]==['subjects','propose']
    assert e.store.memories('u1')[0].record_type=='preference'
    assert unit(e)['candidate_dispositions'][0]['reason']=='validated_extracted_memory'
    assert 'maintain' not in unit(e)['outputs'] and 'maintain' not in unit(e)
    assert not hasattr(GPTModel,'maintain')


@pytest.mark.parametrize('kind',['fact','preference','experience','event'])
def test_each_extraction_type_is_saved_with_its_own_participants_and_evidence(kind):
    text='I recorded a source-supported statement.'
    e=MemoryEngine(model=model(text,kind=kind));e.add(request(text))
    m=e.store.memories('u1')[0]
    assert m.record_type==kind and m.current.content==text
    assert m.current.evidence[0].quote==text and len(m.entity_ids)==1
    assert {edge.relation for edge in e.store.edges_for('u1')}=={'mentions','supported_by'}


@pytest.mark.parametrize('terminal',['occurred','cancelled'])
def test_plan_changes_append_new_records_and_preserve_all_previous_records(terminal):
    text='Mira scheduled a rehearsal for June 8. Mira prefers quiet rooms.'
    e=MemoryEngine(model=DirectModel([
        ('Mira scheduled a rehearsal for June 8.',['Mira']),('Mira prefers quiet rooms.',['Mira'])],
        [card(content='Mira scheduled a rehearsal for June 8.',time='June 8'),card(kind='preference',content='Mira prefers quiet rooms.')]))
    e.add(request(text));initial={m.id:m.model_dump() for m in e.store.memories('u1')}
    for rid,text,status,time,clock in [
        ('r2','Mira moved that rehearsal to June 12.','planned','June 12','2024-06-05T10:00:00Z'),
        ('r3','Mira completed that rehearsal today.' if terminal=='occurred' else 'Mira cancelled that rehearsal.',terminal,
         'today' if terminal=='occurred' else None,'2024-06-12T10:00:00Z')]:
        before={m.id:m.model_dump() for m in e.store.memories('u1')}
        e.model=DirectModel([(text,['Mira'])],[card(content=text,status=status,time=time)])
        e.add(request(text,request_id=rid,when=clock))
        assert all(e.store.get('memories','u1',mid)==raw for mid,raw in before.items())
    records=e.store.memories('u1');events=[m for m in records if m.record_type=='event']
    assert len(events)==3 and len({m.id for m in events})==3
    assert all(len(m.revisions)==1 for m in records)
    assert all(e.store.get('memories','u1',mid)==raw for mid,raw in initial.items())
    assert not any(edge.relation=='revises' for edge in e.store.edges_for('u1'))


def test_repeated_content_in_new_request_is_a_separate_source_record():
    text='I prefer soft light.';e=MemoryEngine(model=model(text,kind='preference'))
    e.add(request(text));e.add(request(text,request_id='r2'));e.add(request(text,request_id='r2'))
    records=e.store.memories('u1')
    assert len(records)==2 and len({m.current.source_ids[0] for m in records})==2
    assert len(e.model.calls)==4


def test_invalid_date_stays_pending_while_independent_preference_is_saved():
    m=DirectModel([('I prefer warm light.',['user']),('A booking happened.',[])],
        [card(kind='preference',content='I prefer warm light.'),card(content='A booking happened.',status='occurred',time='next solstice')])
    e=MemoryEngine(model=m,embedder=HashEmbedder());e.add(request('I prefer warm light. A booking happened.'))
    assert [m.record_type for m in e.store.memories('u1')]==['preference']
    assert [d['operation'] for d in unit(e)['candidate_dispositions']]==['create','pending']
    assert unit(e)['candidate_dispositions'][1]['reason']=='event_date_requires_source_support'
    assert any(d.id.startswith('doc_pending_') and d.memory_id is None for d in e.store.documents('u1'))
    assert any('booking' in hit.content for hit in e.search(query('booking')).data)
    assert len(m.calls)==2


def test_unknown_event_date_is_explicit_and_saves_normally():
    text='I attended a rehearsal.';e=MemoryEngine(model=model(text,status='occurred',time=None))
    e.add(request(text));r=e.store.memories('u1')[0].current
    assert r.status=='occurred' and r.event_at is None and r.time_resolution=='unknown'


@pytest.mark.parametrize('status,time,reason',[(None,None,'event_creation_requires_status'),
    ('planned','invented date','event_date_requires_source_support')])
def test_program_checks_event_status_and_date(status,time,reason):
    e=MemoryEngine(model=model('I made a plan.',status=status,time=time));e.add(request('I made a plan.'))
    assert not e.store.memories('u1') and unit(e)['candidate_dispositions'][0]['reason']==reason


@pytest.mark.parametrize('field,value',[('operation','patch_event'),('operation','keep'),('target_id','foreign-memory'),
    ('record_type','relation'),('subject_refs',[True]),('subject_refs',[99])])
def test_extraction_rejects_unsupported_authority_and_invalid_refs(field,value):
    m=model('I made a plan.')
    def attack(state,raw):raw['items'][0][field]=value;return raw
    m.transform=attack;e=MemoryEngine(model=m)
    with pytest.raises(ProviderUnavailable):e.add(request('I made a plan.'))
    assert not e.store.memories('u1') and e.store.list('sources','u1')
    assert [stage for stage,_ in m.calls]==['subjects','propose','propose']


def test_extraction_protocol_repair_uses_one_shared_budget():
    m=model('I prefer soft light.',kind='preference')
    def corrupt(state,raw):
        if not state.get('repair_errors'):raw['items'][0]['evidence'][0]['source_id']='foreign'
        return raw
    m.transform=corrupt;e=MemoryEngine(model=m);e.add(request('I prefer soft light.'))
    assert len(e.store.memories('u1'))==1
    assert [stage for stage,_ in m.calls]==['subjects','propose','propose'] and unit(e)['repair_stage']=='propose'


def test_embedding_failure_reuses_both_model_outputs_and_the_write_plan():
    class Failing(HashEmbedder):
        ready=False
        def embed(self,texts,*,deadline):
            if not self.ready:raise ProviderUnavailable('synthetic index outage')
            return super().embed(texts,deadline=deadline)
    text='I prefer soft light.';m=model(text,kind='preference');embedding=Failing()
    e=MemoryEngine(model=m,embedder=embedding)
    with pytest.raises(ProviderUnavailable):e.add(request(text))
    plan=deepcopy(unit(e)['write_plan']);assert not e.store.memories('u1')
    embedding.ready=True;e.add(request(text))
    assert unit(e)['write_plan']==plan and len(m.calls)==2 and len(e.store.memories('u1'))==1


def test_previous_pipeline_checkpoint_requires_explicit_source_replay():
    text='I prefer soft light.';e=MemoryEngine(model=model(text,kind='preference'));e.add(request(text))
    progress=e.store.get('requests','u1','r1');progress['pipeline']='G11-memory-types-v28'
    e.store.put('requests','u1','r1',progress);before=e.store.memories('u1')
    with pytest.raises(ProviderUnavailable,match='requires_source_replay'):e.add(request(text))
    assert e.store.memories('u1')==before and PIPELINE=='G11-readable-subjects-v32'


def test_missing_message_time_uses_saved_ingestion_time_for_relative_date():
    text='I attended yesterday.';e=MemoryEngine(model=model(text,status='occurred',time='yesterday'))
    e.add(request(text,when=None));r=e.store.memories('u1')[0].current
    source=e.store.sources('u1',r.source_ids)[0]
    assert parse_time(r.event_at).date()==(parse_time(source.created_at)-timedelta(days=1)).date()


def test_extraction_schema_contains_four_types_and_only_creation_fields():
    schema=strict_schema(ProposalDraft);props=schema['$defs']['CandidateDraft']['properties']
    assert props['record_type']['enum']==['fact','preference','experience','event']
    assert set(props)=={'record_type','content','subject_refs','mode','context','status','time_expression','evidence'}
    assert len(json.dumps(schema))<5000


def test_context_only_claim_is_audited_and_current_claim_is_saved():
    e=MemoryEngine(model=SubjectModel([],[]));e.add(request('The room code is 42.'))
    old=e.store.list('sources','u1')[0]['id']
    m=SubjectModel([], [('The room code is 42.',[],[],None),('The booking is planned.',[],[],None)])
    def cite(state,raw):raw['items'][0]['evidence']=[dict(source_id=old)];return raw
    m.transform=cite;e.model=m;e.add(request('The booking is planned.',request_id='r2'))
    assert len(e.store.memories('u1'))==1 and unit(e,'r2')['source_dispositions'][0]['status']=='context_only'


def test_subject_repair_and_extraction_share_a_single_correction_budget():
    m=model('I prefer soft light.',kind='preference')
    def invalid_identity(state,raw):
        raw['subjects'][0]['evidence'][0]['source_id']='foreign'
        return raw
    m.identity_transform=invalid_identity
    m.transform=lambda state,raw:{'invalid':True}
    e=MemoryEngine(model=m)
    with pytest.raises(ProviderUnavailable):e.add(request('I prefer soft light.'))
    assert [stage for stage,_ in m.calls]==['subjects','subjects','propose']



def test_real_provider_wire_uses_two_requests_and_saves_all_four_types_and_relationship():
    import httpx
    from openai import OpenAI
    text=('I work at Vela Guild. The locker code is 4826. I prefer written agendas. '
          'After missed handoffs, I learned to confirm the next owner in writing. I scheduled a review for June 8.')
    sent=[]
    def handler(request):
        wire=json.loads(request.content);sent.append(wire)
        if wire['response_format']['json_schema']['name']=='SubjectMaintenanceDraft':
            assert wire['messages'][-1]['content'].startswith('CURRENT SOURCE')
            assert text in wire['messages'][-1]['content']
            sid=wire['response_format']['json_schema']['schema']['$defs']['ConnectionChangeDraft']['properties']['source_id']['enum'][0]
            result=dict(subjects=[dict(ref=i,operation='create',target_id=None,name=name,kind=kind,aliases=[],
                evidence=[dict(source_id=sid)]) for i,name,kind in [(0,'@user','person'),(1,'Vela Guild','organization')]],
                connections=[dict(ref=0,operation='create',target_id=None,subject_refs=[0,1],source_id=sid,
                    quote='I work at Vela Guild.',mode='asserted',previous_quote=None)])
        else:
            assert len(sent)==2 and wire['response_format']['json_schema']['name']=='ProposalDraft'
            payload=json.loads(wire['messages'][-1]['content']);sid=payload['new_user_source']['id']
            result=dict(items=[dict(record_type=kind,content=content,subject_refs=refs,mode='asserted',context=None,
                status=status,time_expression=time,evidence=[dict(source_id=sid)]) for kind,content,refs,status,time in [
                    ('fact','The locker code is 4826.',[],None,None),
                    ('preference','I prefer written agendas.',[0],None,None),
                    ('experience','After missed handoffs, I learned to confirm the next owner in writing.',[0],None,None),
                    ('event','I scheduled a review for June 8.',[0],'planned','June 8')]],reason='source grounded')
        return httpx.Response(200,json={'id':'offline','object':'chat.completion','created':1,'model':'gpt-4o-mini',
            'choices':[{'index':0,'message':{'role':'assistant','content':json.dumps(result)},'finish_reason':'stop'}]})
    client=OpenAI(api_key='test',base_url='https://test.invalid/v1',
        http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    m=GPTModel('test','https://test.invalid/v1',client=client);e=MemoryEngine(model=m,embedder=HashEmbedder())
    try:
        e.add(request(text))
        assert len(sent)==2 and [c['kind'] for c in m.calls]==['add_subjects','add_propose']
        assert {r.record_type for r in e.store.memories('u1')}=={'relation','fact','preference','experience','event'}
        assert all(c['passed'] for c in unit(e)['user_binding_checks'])
        assert next(r for r in e.store.memories('u1') if r.record_type=='event').current.planned_at.startswith('2024-06-08')
    finally:e.close()


def test_concurrent_relationship_change_invalidates_the_cached_snapshot_before_retry():
    from tests.test_subject_and_extraction import WireModel
    e=MemoryEngine(model=SubjectModel([('user',True,[]),('Vela',False,[])],
        [('job',[0,1],[0,1],'I work at Vela.')]))
    e.add(request('I work at Vela.'));before=deepcopy(e.store.memories('u1')[0])
    def update(state,raw):
        target=state['relation_catalog'][0]
        raw['connections'][0].update(operation='update',target_id=target['id'],previous_quote=target['content'])
        return raw
    class Concurrent(HashEmbedder):
        changed=False
        def embed(self,texts,*,deadline):
            if not self.changed:
                self.changed=True
                memory=e.store.memories('u1',[before.id])[0]
                memory.revisions.append(memory.current.model_copy(update={
                    'id':'concurrent-revision','created_at':(parse_time(memory.current.created_at)+timedelta(microseconds=1)).isoformat(),
                    'operation':'update','replaces_revision_id':memory.current.id}))
                e.store.put('memories','u1',memory.id,memory.model_dump())
            return super().embed(texts,deadline=deadline)
    e.model=WireModel([('user',True,[]),('Vela',False,[])],
        [('ended',[0,1],[0,1],'I no longer work at Vela.')],identity=update)
    e.embedder=Concurrent()
    with pytest.raises(ProviderUnavailable):e.add(request('I no longer work at Vela.',request_id='r2'))
    assert e.store.memories('u1',[before.id])[0].current.id=='concurrent-revision'
    assert 'write_plan' not in unit(e,'r2') and 'shared_recall' not in unit(e,'r2')
    e.add(request('I no longer work at Vela.',request_id='r2'))
    after=e.store.memories('u1',[before.id])[0]
    # The concurrent revision and the current source's valid update are both retained.
    assert len(after.revisions)==3 and any(r.content=='I no longer work at Vela.' for r in after.revisions)

