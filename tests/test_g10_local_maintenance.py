"""Current contract tests use independent synthetic conversations and direct G10 DTOs."""
from copy import deepcopy
from datetime import timedelta
from time import monotonic

import pytest

from remi.clock import parse_time, resolve_time
from remi.engine import MemoryEngine
from remi.heat import hot_view, visible_memory
from remi.identity import resolve_mentions, source_name_present
from remi.models import AliasEvidence, GraphObject, MaintenanceDraft, Mention, ProposalDraft
from remi.providers import OutputInvalid, ProviderUnavailable, strict_schema
from remi.storage import MemoryStore
from tests.helpers import HashEmbedder, query, request


def card(cid, *, kind='event', content='One occurrence', status='planned', mode='asserted', time=None, context='personal'):
    return {'candidate_id':cid,'operation':'create','target_id':None,'record_type':kind,'content':content,
            'mode':mode,'context':context,'status':status if kind=='event' else None,
            'event_change':None,'time_expression':time,'reason':'source-supported independent card'}


def action(cid, operation, target=None, change=None, time=None):
    return {'candidate_id':cid,'operation':operation,'target_id':target,'record_type':None,'content':None,
            'mode':None,'context':None,'status':None,'event_change':change,'time_expression':time,'reason':'independent fixture'}


class DirectModel:
    def __init__(self, claims, decisions):
        self.claims, self.decisions = claims, decisions
        self.proposals, self.maintenance, self.searches = [], [], []
    def propose(self, state, *, deadline):
        self.proposals.append(deepcopy(state))
        if callable(self.claims):
            return self.claims(state)
        return {'items':[{'content':content,'mentions':[{'name':n} for n in names],
                         'evidence':[{'source_id':state['new_user_source']['id']}]} for i,(content,names) in enumerate(self.claims)],'reason':'independent source fixture'}
    def maintain(self, state, *, deadline):
        self.maintenance.append(deepcopy(state))
        return self.decisions(state) if callable(self.decisions) else {'decisions':self.decisions}
    def decide(self, state, *, deadline):
        self.searches.append(deepcopy(state))
        return {'steps':[],'relevant_memory_ids':[]}


def model(claim, decision, names=('self',)):
    return DirectModel([(claim,list(names))],[decision])


def disposition(engine, rid):
    return engine.store.get('requests','u1',rid)['units']['0']['candidate_dispositions']


@pytest.mark.parametrize('terminal,expected,day',[('complete','occurred','2024-06-12'),('cancel','cancelled',None)])
def test_plan_reschedule_terminal_and_new_occurrence_are_independent(terminal, expected, day):
    m=DirectModel([('Mira arranges the rehearsal',['Mira']),('Mira prefers quiet rooms',['Mira'])],
        [card('0',content='Mira rehearsal',time='June 8'),card('1',kind='relation',content='Mira prefers quiet rooms')])
    engine=MemoryEngine(model=m,embedder=HashEmbedder())
    engine.add(request('Mira scheduled the rehearsal for June 8. Mira prefers quiet rooms.'))
    event=next(x for x in engine.store.memories('u1') if x.record_type=='event')
    relation=next(x for x in engine.store.memories('u1') if x.record_type=='relation')
    engine.model=model('Mira rescheduled the rehearsal', action('0','patch_event',event.id,'reschedule','June 12'), ['Mira'])
    engine.add(request('Mira moved that rehearsal to June 12.',request_id='r2',when='2024-06-05T10:00:00Z'))
    engine.model=model('Mira ended the rehearsal plan',action('0','patch_event',event.id,terminal,'today' if day else None),['Mira'])
    engine.add(request('Mira completed the rehearsal today.' if day else 'Mira cancelled the rehearsal.',request_id='r3',when='2024-06-12T10:00:00Z'))
    updated=engine.store.memories('u1',[event.id])[0]
    assert updated.current.status==expected and updated.current.planned_at.startswith('2024-06-12')
    assert (updated.current.event_at[:10] if updated.current.event_at else None)==day
    assert len(updated.revisions)==3 and updated.current.content=='Mira rehearsal'
    assert updated.current.entity_ids==event.current.entity_ids
    assert updated.current.context==event.current.context and updated.current.mode==event.current.mode
    assert len(updated.current.evidence)==3 and all(e.quote in [s.content for s in engine.store.sources('u1',[e.source_id])] for e in updated.current.evidence)
    assert engine.store.memories('u1',[relation.id])[0]==relation
    assert visible_memory(updated,'2024-06-03T00:00:00Z').current.planned_at.startswith('2024-06-08')
    engine.model=model('Mira arranges another rehearsal',card('0',content='A second Mira rehearsal',time='June 20'),['Mira'])
    engine.add(request('Mira scheduled a separate rehearsal for June 20.',request_id='r4',when='2024-06-13T10:00:00Z'))
    assert len([x for x in engine.store.memories('u1') if x.record_type=='event'])==2
    assert len(engine.store.list('objects','u1'))==1
    assert {e.relation for e in engine.store.edges_for('u1')}=={'mentions','supported_by','revises'}


def test_independent_bad_candidate_is_repaired_alone_and_pending_is_only_a_hint():
    m=DirectModel([('I prefer warm light',['self']),('A booking has uncertain time',[])],lambda state:
        {'decisions':[card(c['id'],kind='relation',content=c['content']) if c['id']=='0' else
                      card(c['id'],time='next solstice') for c in state['proposal']['items']]})
    engine=MemoryEngine(model=m,embedder=HashEmbedder())
    engine.add(request('I prefer warm light. The booking happened at an unknown time.'))
    assert [x.current.content for x in engine.store.memories('u1')]==['I prefer warm light']
    assert [c['id'] for c in m.maintenance[1]['proposal']['items']]==['1']
    assert [x['status'] for x in disposition(engine,'r1')]==['accepted','pending']
    docs=[d for d in engine.store.documents('u1') if d.id.startswith('doc_pending_')]
    assert len(docs)==1 and docs[0].memory_id is None and not docs[0].memory_evidence
    assert 'booking' in engine.search(query('booking')).data[0].content
    assert len(hot_view(engine.store.memories('u1'),'2024-06-03T00:00:00Z'))==1
    assert all('booking' not in x.current.content for x in engine.store.memories('u1'))


@pytest.mark.parametrize('change,time',[('reschedule','June 10'),('complete',None),('cancel',None)])
def test_relation_is_immutable_even_when_model_selects_it(change,time):
    engine=MemoryEngine(model=model('I prefer paper notes',card('0',kind='relation',content='I prefer paper notes')))
    engine.add(request('I prefer paper notes.'))
    before=engine.store.memories('u1')[0]
    engine.model=model('My plan changes',action('0','patch_event',before.id,change,time))
    engine.add(request('My plan changes on June 10.',request_id='r2'))
    assert engine.store.memories('u1')[0]==before and disposition(engine,'r2')[0]['status']=='pending'


@pytest.mark.parametrize('status,mode',[('occurred','asserted'),('cancelled','asserted'),('planned','desired'),('planned','document')])
def test_only_asserted_planned_events_are_patchable(status,mode):
    engine=MemoryEngine(model=model('I discuss a workshop',card('0',content='Workshop',status=status,mode=mode)))
    engine.add(request('I discuss a workshop.'))
    before=engine.store.memories('u1')[0]
    engine.model=model('Workshop update',action('0','patch_event',before.id,'complete'))
    engine.add(request('I discuss a workshop update.',request_id='r2'))
    assert engine.store.memories('u1')[0]==before and disposition(engine,'r2')[0]['status']=='pending'


def test_conflicting_event_group_does_not_block_independent_state():
    engine=MemoryEngine(model=model('I arrange a visit',card('0',content='Visit')))
    engine.add(request('I arrange a visit.'))
    target=engine.store.memories('u1')[0]
    engine.model=DirectModel([('Visit June 10',[]),('Visit June 11',[]),('I prefer daylight',['self'])],
        [action('0','patch_event',target.id,'reschedule','June 10'),action('1','patch_event',target.id,'reschedule','June 11'),
         card('2',kind='relation',content='I prefer daylight')])
    engine.add(request('Visit June 10, or June 11; I prefer daylight.',request_id='r2'))
    assert engine.store.memories('u1',[target.id])[0]==target
    assert [x['status'] for x in disposition(engine,'r2')]==['pending','pending','accepted']
    assert len(engine.store.memories('u1'))==2


def test_duplicate_identical_patch_is_one_revision_with_shared_evidence():
    engine=MemoryEngine(model=model('I arrange a visit',card('0',content='Visit')))
    engine.add(request('I arrange a visit.'))
    target=engine.store.memories('u1')[0]
    engine.model=DirectModel([('Visit cancelled',[]),('I cancel the visit',['self'])],
        [action(str(i),'patch_event',target.id,'cancel') for i in range(2)])
    engine.add(request('I cancelled the visit.',request_id='r2'))
    after=engine.store.memories('u1')[0]
    assert len(after.revisions)==2 and len(after.current.evidence)==2


def test_shared_repair_budget_and_invalid_json_stay_bounded():
    def propose(state):
        if not state.get('repair_errors'):
            raise OutputInvalid('invalid JSON')
        return {'items':[{'content':'I made a plan','mentions':[],'evidence':[{'source_id':state['new_user_source']['id']}]}],'reason':'repaired'}
    m=DirectModel(propose,lambda state: (_ for _ in ()).throw(OutputInvalid('invalid maintenance JSON')))
    engine=MemoryEngine(model=m)
    engine.add(request('I made a plan.'))
    assert len(m.proposals)==2 and len(m.maintenance)==1
    assert not engine.store.memories('u1') and disposition(engine,'r1')[0]['status']=='pending'


def test_retry_reuses_good_sibling_and_only_retries_failed_provider_stage():
    ready=False
    def maintain(state):
        if state.get('repair_errors') and not ready:
            raise ProviderUnavailable('offline outage')
        return {'decisions':[card(c['id'],kind='relation',content=c['content']) if ready or c['id']=='0' else {'candidate_id':'1'}
                             for c in state['proposal']['items']]}
    m=DirectModel([('I like shade',['self']),('I like calm',['self'])],maintain)
    engine=MemoryEngine(model=m)
    with pytest.raises(ProviderUnavailable): engine.add(request('I like shade and calm.'))
    assert not engine.store.memories('u1')
    ready=True
    engine.add(request('I like shade and calm.'))
    assert len(m.proposals)==1 and len(m.maintenance)==3 and len(engine.store.memories('u1'))==2
    assert [c['id'] for c in m.maintenance[-1]['proposal']['items']]==['1']


@pytest.mark.parametrize('name,text,expected',[('Ann','Anniversary celebration',False),('Ann','Ann visited',True),
    ('Lee','Leeds office',False),('李明','李明参加',True),('R&D','R&D group',True)])
def test_source_names_use_generic_literal_boundaries(name,text,expected):
    assert source_name_present(name.casefold(),text.casefold()) is expected


def test_known_alias_is_reused_and_ambiguous_alias_requires_clarification():
    store=MemoryStore(); stamp='2024-06-01T10:00:00Z'
    one=GraphObject(id='one',owner='u1',name='Alexandra',observed_at=stamp,
        aliases=[AliasEvidence(name='Alex',source_id='prior',observed_at=stamp)])
    store.put('objects','u1',one.id,one.model_dump())
    sources=[{'id':'new','content':'Alex visited.','message_at':stamp,'created_at':stamp}]
    ids,_,issues=resolve_mentions(store,'u1',[Mention(name='Alex')],sources,stamp)
    assert ids==['one'] and not issues
    two=one.model_copy(update={'id':'two','name':'Alexander'})
    store.put('objects','u1','two',two.model_dump())
    ids,_,issues=resolve_mentions(store,'u1',[Mention(name='Alex')],sources,stamp)
    assert not ids and issues[0]['reason']=='ambiguous_exact_identity'


def test_missing_source_clock_uses_immutable_ingestion_clock():
    engine=MemoryEngine(model=model('I finished a visit',card('0',content='Visit',status='occurred',time='yesterday')))
    engine.add(request('I finished a visit yesterday.',when=None))
    revision=engine.store.memories('u1')[0].current
    source=engine.store.sources('u1',revision.source_ids)[0]
    assert parse_time(revision.event_at).date()==(parse_time(source.created_at)-timedelta(days=1)).date()


@pytest.mark.parametrize('phrase,text',[('2024年6月9日','6月9日完成'),('2024-06-09','June 9 completed'),('June 9','June 9 completed')])
def test_equivalent_date_encodings_are_source_grounded(phrase,text):
    assert resolve_time(phrase,text,'2024-06-02T10:00:00Z').startswith('2024-06-09')
    assert resolve_time(phrase,'No date here.','2024-06-02T10:00:00Z') is None


def test_strict_contract_stays_small_and_excludes_semantic_role_editing():
    import json
    for cls,limit in [(ProposalDraft,2000),(MaintenanceDraft,4200)]:
        encoded=json.dumps(strict_schema(cls))
        assert len(encoded)<limit
        for field in ['relation_category','subject_ref','object_ref','changed_fields','connections','remove_participants']:
            assert field not in encoded


def test_unspecified_context_uses_explicit_program_default():
    engine=MemoryEngine(model=model('I prefer shade',card('0',kind='relation',content='I prefer shade',context=None)))
    engine.add(request('I prefer shade.'))
    assert engine.store.memories('u1')[0].current.context=='source_reported'


def test_equivalent_english_year_is_grounded_in_source_month_day():
    assert resolve_time('March 8, 2027','Booked for March 8.','2027-03-01T10:00:00Z').startswith('2027-03-08')
    assert resolve_time('March 8, 2028','Booked for March 8.','2027-03-01T10:00:00Z') is None


@pytest.mark.parametrize('bad',[None,[],{'id':'c','content':'bad','mentions':[],'evidence':['bad']}])
def test_malformed_candidate_protocol_is_bounded(bad):
    m=DirectModel(lambda state:{'items':[bad],'reason':'invalid fixture'},[])
    engine=MemoryEngine(model=m)
    with pytest.raises(ProviderUnavailable): engine.add(request('My original claim remains saved.'))
    assert len(m.proposals)==2 and not engine.store.memories('u1')
    assert engine.store.list('sources','u1')


def test_context_only_candidates_are_audited_without_spending_repair_budget():
    engine=MemoryEngine(model=DirectModel([],[]))
    engine.add(request('I prefer a quiet room.'))
    old=engine.store.list('sources','u1')[0]['id']
    def proposed(state):
        return {'items':[{'content':'I prefer a quiet room','mentions':[], 'evidence':[{'source_id':old}]},
            {'content':'I made a booking','mentions':[], 'evidence':[{'source_id':state['new_user_source']['id']}]}],'reason':'fixture'}
    engine.model=DirectModel(proposed,[card('1',content='Booking')])
    engine.add(request('I made a booking.',request_id='r2'))
    assert len(engine.store.memories('u1'))==1 and len(engine.model.proposals)==1 and len(engine.model.maintenance)==1
    unit=engine.store.get('requests','u1','r2')['units']['0']
    assert unit['source_dispositions'][0]['status']=='context_only'


@pytest.mark.parametrize('cid', ['0','S1','M1'])
def test_maintenance_wire_exposes_only_operation_fields(cid):
    from remi.providers import scoped_schema
    schema=scoped_schema(MaintenanceDraft,{'new_user_source':{'id':'S1'},'proposal':{'items':[{'id':cid}]},'history':[]})
    assert set(schema['$defs'])=={'CreateDraft','PendingDraft'}
    schema=scoped_schema(MaintenanceDraft,{'new_user_source':{'id':'S1'},'proposal':{'items':[{'id':cid}]},'history':[{'id':'M2'}]})
    props=schema['$defs']['EventPatchDraft']['properties']
    assert set(props)=={'candidate_id','operation','target_id','event_change','time_expression','reason'}
    assert props['target_id']['enum']==['M2']


def test_program_assigns_candidate_ids_and_keeps_operation_namespaces_separate():
    m=DirectModel([('I prefer blue',['self']),('I prefer quiet',['self'])],lambda state:
        {'decisions':[card(c['id'],kind='relation',content=c['content']) for c in state['proposal']['items']]})
    engine=MemoryEngine(model=m)
    engine.add(request('I prefer blue and quiet.'))
    assert [c['id'] for c in m.maintenance[0]['proposal']['items']]==['0','1']
    assert 'id' not in ProposalDraft.model_json_schema()['$defs']['CandidateDraft']['properties']


def test_stale_model_history_is_recalled_again_before_any_patch_is_applied():
    engine=MemoryEngine(model=model('Mira arranges a visit',card('0',content='Mira visit'),['Mira']))
    engine.add(request('Mira arranges a visit.'))
    target=engine.store.memories('u1')[0]
    called=0
    def maintain(state):
        nonlocal called
        called+=1
        if called==1:
            current=engine.store.memories('u1',[target.id])[0]
            current.revisions.append(current.current.model_copy(update={'id':'concurrent-revision','created_at':(parse_time(current.current.created_at)+timedelta(microseconds=1)).isoformat(),'planned_at':'2024-06-19T00:00:00Z','operation':'reschedule','replaces_revision_id':current.current.id}))
            engine.store.put('memories','u1',target.id,current.model_dump())
        return {'decisions':[action('0','patch_event',target.id,'cancel')]}
    m=DirectModel([('Mira cancels that visit',['Mira'])],maintain)
    engine.model=m
    with pytest.raises(ProviderUnavailable): engine.add(request('Mira cancelled that visit.',request_id='r2'))
    current=engine.store.memories('u1',[target.id])[0]
    assert current.current.id=='concurrent-revision' and current.current.status=='planned'
    unit=engine.store.get('requests','u1','r2')['units']['0']
    assert 'maintenance_context' not in unit and unit['history_conflicts']
    engine.add(request('Mira cancelled that visit.',request_id='r2'))
    assert len(m.proposals)==1 and len(m.maintenance)==2
    assert engine.store.memories('u1',[target.id])[0].current.status=='cancelled'


def test_index_time_concurrent_future_revision_survives_transaction_conflict():
    engine=MemoryEngine(model=model('I arrange a visit',card('0',content='Visit')),embedder=HashEmbedder())
    engine.add(request('I arrange a visit.'))
    target=engine.store.memories('u1')[0]
    class ConcurrentEmbedder(HashEmbedder):
        def embed(self,texts,*,deadline):
            if any('Card event;' in text for text in texts):
                current=engine.store.memories('u1',[target.id])[0]
                current.revisions.append(current.current.model_copy(update={'id':'future-revision','observed_at':'2025-01-01T00:00:00Z','operation':'reschedule','replaces_revision_id':current.current.id}))
                engine.store.put('memories','u1',target.id,current.model_dump())
            return super().embed(texts,deadline=deadline)
    engine.model=model('I cancel that visit',action('0','patch_event',target.id,'cancel'))
    engine.embedder=ConcurrentEmbedder()
    with pytest.raises(ProviderUnavailable):engine.add(request('I cancelled that visit.',request_id='r2'))
    assert engine.store.memories('u1',[target.id])[0].current.id=='future-revision'
    assert len(engine.store.memories('u1',[target.id])[0].revisions)==2


def test_concurrent_entity_attestations_survive_new_card_commit():
    engine=MemoryEngine(model=model('Mira likes shade',card('0',kind='relation',content='Mira likes shade'),['Mira']))
    engine.add(request('Mira likes shade.'))
    obj=engine.store.list('objects','u1')[0]
    def maintain(state):
        latest=engine.store.get('objects','u1',obj['id'])
        latest['aliases'].append({'name':'MIRA','source_id':latest['aliases'][0]['source_id'],'observed_at':latest['observed_at']})
        engine.store.put('objects','u1',obj['id'],latest)
        return {'decisions':[card('0',kind='relation',content='Mira likes calm')]}
    engine.model=DirectModel([('Mira likes calm',['Mira'])],maintain)
    engine.add(request('Mira likes calm.',request_id='r2'))
    assert any(a['name']=='MIRA' for a in engine.store.get('objects','u1',obj['id'])['aliases'])


@pytest.mark.parametrize('phrase,source',[('March 12, 2027','Completed today.'),('2027年3月12日','今天完成。')])
def test_normalized_calendar_date_is_equivalent_to_source_relative_day(phrase,source):
    assert resolve_time(phrase,source,'2027-03-12T10:00:00Z').startswith('2027-03-12')
    assert resolve_time(phrase,source,'2027-03-13T10:00:00Z') is None



def test_cancellation_uses_status_only_and_repair_preserves_planned_date():
    engine=MemoryEngine(model=model('I arrange a visit',card('0',content='Visit',time='June 8')))
    engine.add(request('I arrange a visit on June 8.'))
    target=engine.store.memories('u1')[0]
    m=DirectModel([('I cancel that visit',['self'])],lambda state:{'decisions':[
        action('0','patch_event',target.id,'cancel',None if state.get('repair_errors') else 'June 8')]})
    engine.model=m
    engine.add(request('I cancelled that visit.',request_id='r2'))
    after=engine.store.memories('u1')[0]
    assert after.current.status=='cancelled' and after.current.planned_at==target.current.planned_at
    assert after.current.event_at is None and len(m.maintenance)==2
    assert m.maintenance[-1]['repair_errors'][0]['error']=='cancel_changes_status_with_no_time_argument'
