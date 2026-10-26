"""Independent lifecycle scenarios using deterministic decisions and real Add/Search code."""
from copy import deepcopy
import pytest
from experiments.subject_engine import SubjectGraphEngine
from experiments.event_maintenance import EventPlan, examples, validate_plan
from experiments.event_recall import recall_memories
from experiments.pending_recovery import pending_jobs
from remi.models import Source, Memory
from remi.graph import active_connections
from remi.heat import visible_memory
from remi.providers import ProviderUnavailable
from tests.test_event_maintenance import Model, OWNER, req, row, unit, lifecycle, update
from tests.test_individual_subjects_experiment import plan


class FlowModel(Model):
    def __init__(self):
        super().__init__();self.people=[];self.pending=False
    def maintain_subjects(self,state,*,deadline):
        self.calls.append('subjects');self.packets.append(('subjects',deepcopy(state)))
        result=dict(subjects=[],connections=[])
        for ref,name in enumerate(self.people):
            old=next((r for r in state['subject_catalog'] if r['name']==name),None)
            basis=next((b['id'] for b in state['identity_bases'] if old and b['target_id']==old['id']),None)
            decision='pending' if self.pending and not state.get('recovery') else 'reuse' if old else 'create'
            value=plan(name=name,operation=decision,target=old['id'] if decision=='reuse' else None,
                basis=basis,sid=state['new_user_source']['id'])['subjects'][0]
            value['ref']=ref;result['subjects'].append(value)
        return result
def test_unbound_event_gains_subject_without_changing_id():
    model=FlowModel();e=SubjectGraphEngine(model=model)
    model.writer=lambda s:[row(s,subject_refs=[])]
    e.add(req('A workshop is planned.','unbound'))
    original=e.store.memories(OWNER)[0]
    assert original.current.entity_ids==[]
    model.people=['Aster']
    def enrich(s):
        assert original.id in {h['id'] for h in s['history']}
        return [update(s,content='Aster will host the workshop.',participant_mode='add',
            **{})|{'changed_fields':['content','participants']}]
    model.writer=enrich;e.add(req('Aster will host that workshop.','bound',1))
    memory=e.store.memories(OWNER)[0]
    assert len(e.store.memories(OWNER))==1 and memory.id==original.id
    assert len(memory.current.entity_ids)==1 and len(memory.revisions)==2
    assert unit(e,'bound')['memory_recall_trace']['lanes'][memory.id]=='unbound'


def test_untyped_subject_link_and_relationship_fact_are_saved_independently():
    model=FlowModel();model.people=['Mara','Cedar Lab'];engine=SubjectGraphEngine(model=model)
    base_maintain=model.maintain_subjects
    def subjects(state,*,deadline):
        result=base_maintain(state,deadline=deadline)
        result['subjects'][1]['kind']='organization'
        result['connections']=[dict(subject_refs=[0,1],evidence=[dict(source_id=state['new_user_source']['id'])])]
        return result
    model.maintain_subjects=subjects
    model.writer=lambda state:[row(state,record_type='fact',status=None,subject_refs=[0,1],
        content='Mara works at Cedar Lab.')]
    engine.add(req('Mara works at Cedar Lab.','relationship'))
    link=next(row for row in engine.store.list('edges',OWNER) if 'endpoint_ids' in row)
    memory=engine.store.memories(OWNER)[0]
    assert set(link)=={'id','owner','endpoint_ids','source_ids'}
    assert len(link['endpoint_ids'])==2
    assert memory.record_type=='fact' and len(memory.current.entity_ids)==2
    assert memory.current.content=='Mara works at Cedar Lab.'


def test_both_recall_lanes_have_budget_and_foreign_memories_are_excluded():
    e,m=lifecycle();base=e.store.memories(OWNER)[0]
    for index in range(12):
        clone=base.model_copy(deep=True);clone.id='bound-'+str(index)
        clone.revisions[0].content='Aster workshop '+str(index)
        e.store.put('memories',OWNER,clone.id,clone.model_dump())
    orphan=base.model_copy(deep=True);orphan.id='unbound';orphan.revisions[0].entity_ids=[]
    orphan.revisions[0].content='The workshop has a projector.'
    e.store.put('memories',OWNER,orphan.id,orphan.model_dump())
    foreign=orphan.model_copy(deep=True);foreign.owner='other';foreign.id='foreign'
    e.store.put('memories','other',foreign.id,foreign.model_dump())
    m.writer=lambda s:[];e.add(req('Aster asked about the workshop projector.','recall',1))
    history=unit(e,'recall')['memory_recall']['history'];ids={h['id'] for h in history}
    assert 'unbound' in ids and 'foreign' not in ids and len(ids)<=6
    assert any(h['entity_ids'] for h in history)


def test_program_supplement_excludes_memories_owned_only_by_other_subjects():
    model=FlowModel();model.people=['Aster'];e=SubjectGraphEngine(model=model)
    e.add(req('Aster planned a workshop.','first'));old=e.store.memories(OWNER)[0]
    old_revision=deepcopy(old.current)
    model.people=['Beryl']
    model.writer=lambda s:[row(s,content='Beryl joined a workshop.')]
    e.add(req('Beryl joined the workshop.','new-person',1))
    trace=unit(e,'new-person')['memory_recall_trace']
    assert trace['selected_memory_ids']==[]
    assert old.id not in trace['allowed_memory_ids']
    assert trace['supplement_triggered']
    assert len(e.store.memories(OWNER))==2
    assert e.store.get('memories',OWNER,old.id)['revisions'][-1]==old_revision.model_dump()
    assert model.calls==['subjects','memory','subjects','memory']


def test_two_dates_and_participant_remove_are_expressible():
    model=FlowModel();model.people=['Aster','Beryl'];e=SubjectGraphEngine(model=model)
    model.writer=lambda s:[row(s,subject_refs=[0,1])]
    e.add(req('Aster and Beryl planned a workshop.','start'))
    model.people=['Beryl']
    def change(s):
        return [update(s,content='The workshop was planned for 2030-01-03 and happened on 2030-01-05; Beryl withdrew.',
            status='occurred',participant_mode='remove',time_updates=[dict(field='planned_at',expression='January 3'),
                dict(field='event_at',expression='January 5')])|
            {'changed_fields':['content','participants','planned_at','event_at','status']}]
    model.writer=change;e.add(req('Beryl withdrew. The workshop was planned for January 3 and happened on January 5.','finish',5))
    current=e.store.memories(OWNER)[0].current
    assert current.planned_at.startswith('2030-01-03') and current.event_at.startswith('2030-01-05')
    assert len(current.time_evidence)==2 and len(current.entity_ids)==1
    assert e.store.get('objects',OWNER,current.entity_ids[0])['name']=='Aster'


@pytest.mark.parametrize('kind',['fact','preference','experience'])
def test_statement_update_and_keep_preserve_identity_and_distinct_scope(kind):
    model=Model();e=SubjectGraphEngine(model=model)
    model.writer=lambda s:[row(s,record_type=kind,status=None)]
    e.add(req('Aster chooses written notes for work.','first'));mid=e.store.memories(OWNER)[0].id
    model.writer=lambda s:[update(s,record_type=kind,status=None)]
    e.add(req('Aster now chooses voice notes for work.','update',1))
    assert len(e.store.memories(OWNER))==1 and len(e.store.memories(OWNER)[0].revisions)==2
    model.writer=lambda s:[row(s,record_type=kind,status=None,operation='keep',target_id=mid)]
    e.add(req('Aster still chooses voice notes for work.','keep',2))
    assert len(e.store.memories(OWNER)[0].revisions)==2
    model.writer=lambda s:[row(s,record_type=kind,status=None,context='at home')]
    e.add(req('Aster chooses paper notes at home.','scope',3))
    assert len(e.store.memories(OWNER))==2


def test_pending_recovery_uses_later_identity_but_original_date_anchor_once():
    model=FlowModel();model.people=['Aster'];model.pending=True;e=SubjectGraphEngine(model=model)
    def writer(s):
        if s.get('recovery'):
            return [row(s,status='occurred',time_updates=[dict(field='event_at',expression='today')],
                evidence=[dict(source_id=s['new_user_source']['id']),dict(source_id=s['context_sources'][-1]['id'])])]
        if 'neighbor' in s['new_user_source']['content']:return []
        return [row(s,status='occurred',time_updates=[dict(field='event_at',expression='today')])]
    model.writer=writer;e.add(req('Aster attended the workshop today.','unclear'))
    assert not e.store.memories(OWNER)
    assert any(d.id.startswith('doc_pending_') for d in e.store.documents(OWNER))
    model.pending=False;clarification=req('Aster at that workshop is my neighbor.','clarified',5)
    e.add(clarification)
    memories=e.store.memories(OWNER);assert len(memories)==1
    current=memories[0].current
    assert current.event_at.startswith('2030-01-01') and current.observed_at.startswith('2030-01-06')
    assert len(current.entity_ids)==1 and len(current.source_ids)==2
    assert visible_memory(memories[0],'2030-01-02T00:00:00Z') is None
    assert not any(d.id.startswith('doc_pending_') for d in e.store.documents(OWNER))
    calls=list(model.calls);e.add(clarification);assert calls==model.calls
    source=e.store.sources(OWNER,e.store.get('requests',OWNER,'clarified')['source_ids'])[0]
    assert pending_jobs(e.store,OWNER,source)==[]
    assert set(e.store.records)=={'sources','memories','objects','edges','requests','search_documents'}


def test_backfill_replays_dates_membership_evidence_and_keeps_narrative_history():
    e,m=lifecycle()
    def moved(s):return [update(s,time_updates=[dict(field='planned_at',expression='January 9')])|{'changed_fields':['content','planned_at']}]
    m.writer=moved;e.add(req('Aster moved the workshop to January 9.','moved',5))
    m.writer=lambda s:[update(s,content='The workshop is in room Amber with Aster.')]
    e.add(req('Aster added room Amber to the workshop.','room',2))
    memory=e.store.memories(OWNER)[0]
    assert memory.current.planned_at.startswith('2030-01-09')
    assert 'room Amber' in memory.current.content and 'January 9' in memory.current.content
    assert len(memory.current.source_ids)==3
    historic=visible_memory(memory,'2030-01-04T00:00:00Z').current
    assert historic.planned_at.startswith('2030-01-03') and 'January 9' not in historic.content
    assert memory.current.maintenance_patch['values']['content']=='Aster moved the workshop to January 9.'


def test_event_edges_are_traversed_and_retraction_preserves_historical_graph():
    model=FlowModel();e=SubjectGraphEngine(model=model);model.writer=lambda s:[row(s,subject_refs=[])]
    e.add(req('A workshop is planned.','first'));first=e.store.memories(OWNER)[0].id
    model.writer=lambda s:[row(s,subject_refs=[],related_memory_ids=[first],change_quote=s['new_user_source']['content'])]
    e.add(req('A review follows that workshop.','second',1));second=next(m.id for m in e.store.memories(OWNER) if m.id!=first)
    def graph_ids(clock):
        docs=e.store.documents(OWNER);seeds=[d for d in docs if d.memory_id==first]
        return {d.memory_id for d,_ in e._graph_candidates(OWNER,'review',seeds,docs,None,clock)}
    assert second in graph_ids('2030-01-03T00:00:00Z')
    model.writer=lambda s:[row(s,subject_refs=[],operation='update',target_id=second,changed_fields=['content'],
        change_quote=s['new_user_source']['content'],removed_related_memory_ids=[first])]
    e.add(req('The review is independent of the workshop.','unlink',3))
    assert not active_connections(e.store,OWNER,'2030-01-05T00:00:00Z')
    assert len(active_connections(e.store,OWNER,'2030-01-03T00:00:00Z'))==1
    # Use separate sessions to exclude adjacent-message fallback when checking graph removal.
    for source in e.store.list('sources',OWNER):
        source['session_id']=source['id'];e.store.put('sources',OWNER,source['id'],source)
    assert second not in graph_ids('2030-01-05T00:00:00Z')


def test_examples_match_the_live_plan_schema():
    for state,answer in examples():EventPlan.model_validate(answer)


def test_recovery_network_resume_reuses_normal_stage_and_saved_identity():
    model=FlowModel();model.people=['Aster'];model.pending=True;e=SubjectGraphEngine(model=model);attempts=[]
    def writer(s):
        if s.get('recovery'):
            attempts.append('recovery')
            if len(attempts)==1:raise ProviderUnavailable('simulated_network_failure')
            return [row(s,evidence=[dict(source_id=s['new_user_source']['id']),dict(source_id=s['context_sources'][-1]['id'])])]
        return [] if 'neighbor' in s['new_user_source']['content'] else [row(s)]
    model.writer=writer;e.add(req('Aster plans a workshop.','pending'))
    model.pending=False;r=req('Aster is the neighbor from the workshop.','clarify',1)
    with pytest.raises(ProviderUnavailable,match='simulated_network_failure'):e.add(r)
    subject_calls=model.calls.count('subjects');e.add(r)
    assert model.calls.count('subjects')==subject_calls
    assert len(e.store.memories(OWNER))==1
    assert len([r for r in e.store.list('requests',OWNER) if r.get('status')=='resolved'])==1


def test_unresolved_recovery_stays_searchable_and_gets_no_false_resolution():
    model=FlowModel();model.people=['Aster'];model.pending=True;e=SubjectGraphEngine(model=model)
    def writer(s):
        if s.get('recovery'):
            return [row(s,operation='pending',evidence=[dict(source_id=s['new_user_source']['id']),
                dict(source_id=s['context_sources'][-1]['id'])])]
        return [] if 'unclear' in s['new_user_source']['content'] else [row(s)]
    model.writer=writer;e.add(req('Aster planned a workshop.','pending'))
    e.add(req('Which Aster is still unclear.','unclear',1))
    assert not e.store.memories(OWNER)
    assert any(d.id.startswith('doc_pending_') for d in e.store.documents(OWNER))
    assert not any(r.get('status')=='resolved' for r in e.store.list('requests',OWNER))


def test_program_supplement_is_bounded_and_checkpoint_reuses_one_memory_reply():
    model=FlowModel();e=SubjectGraphEngine(model=model)
    model.writer=lambda s:[row(s,subject_refs=[],content='A workshop needs a projector.')]
    e.add(req('A workshop needs a projector.','unbound'))
    model.people=['Aster']
    model.writer=lambda s:[row(s,operation='pending',target_id=s['history'][0]['id'])]
    e.add(req('Aster refers to that earlier workshop.','missing'))
    cache=unit(e,'missing')
    assert cache['candidate_dispositions'][0]['status']=='pending'
    assert cache['memory_recall_trace']['supplemented_unbound_memory_ids']
    assert model.calls==['subjects','memory','subjects','memory']
    snapshot=e.store.snapshot();e.add(req('Aster refers to that earlier workshop.','missing'))
    assert e.store.snapshot()==snapshot and model.calls==['subjects','memory','subjects','memory']


def test_graph_traversal_reaches_two_hops_but_stops_before_third():
    model=FlowModel();e=SubjectGraphEngine(model=model);previous=None;ids=[]
    for i in range(4):
        model.writer=lambda s:[row(s,subject_refs=[],related_memory_ids=[previous] if previous else [],
            change_quote=s['new_user_source']['content'] if previous else None)]
        e.add(req('The workshop chain step '+str(i)+' follows the preceding workshop.','step-'+str(i),i))
        previous=next(m.id for m in e.store.memories(OWNER) if m.id not in ids);ids.append(previous)
    for source in e.store.list('sources',OWNER):
        source['session_id']=source['id'];e.store.put('sources',OWNER,source['id'],source)
    docs=e.store.documents(OWNER);seeds=[d for d in docs if d.memory_id==ids[0]]
    result={d.memory_id for d,_ in e._graph_candidates(OWNER,'workshop',seeds,docs,None,'2030-01-10T00:00:00Z')}
    assert ids[2] in result and ids[3] not in result


def test_bad_date_membership_and_statement_fields_produce_repair_feedback():
    e,m=lifecycle();state=unit(e,'start')['memory_recall'];view=unit(e,'start')['current_participants']
    memory=e.store.memories(OWNER)[0]
    state['history']=[dict(id=memory.id,record_type='event',content=memory.current.content,
        status='planned',sources=[],related_memory_ids=[])]
    r=update(state,time_updates=[dict(field='planned_at',expression='January 3'),dict(field='planned_at',expression='January 3')],participant_mode='add')
    accepted,issues=validate_plan(dict(items=[r]),state,view)
    assert not accepted
    assert {'one_edit_per_time_field','list_participants_when_editing_membership'} <= {i['code'] for i in issues}


def test_recovery_related_candidates_include_preceding_claim_without_keyword_overlap():
    model=FlowModel();model.people=['Aster'];model.pending=True;e=SubjectGraphEngine(model=model)
    model.writer=lambda s:[row(s)];e.add(req('Aster planned a workshop.','pending'))
    clarification=Source(id='clarification',owner=OWNER,session_id='session',request_id='later',ordinal=0,
        role='user',content='Yes, that neighbor.',message_at='2030-01-02T00:00:00Z',created_at='2030-01-02T00:00:00Z')
    assert len(pending_jobs(e.store,OWNER,clarification))==1


def test_program_recall_preserves_original_snapshot_for_concurrent_future_revision():
    model=FlowModel();model.people=['Aster'];e=SubjectGraphEngine(model=model)
    e.add(req('Aster planned a workshop.','first'));mid=e.store.memories(OWNER)[0].id
    mutated=[]
    def propose(state,*,deadline):
        if not mutated:
            stored=e.store.get('memories',OWNER,mid)
            later=deepcopy(stored['revisions'][0]);later.update(id='concurrent-future',
                observed_at='2030-01-10T00:00:00Z',created_at='2030-01-10T00:00:00Z',maintenance_patch=None)
            stored['revisions'].append(later);e.store.put('memories',OWNER,mid,stored);mutated.append(True)
        return dict(items=[update(state)],reason='Use the program-selected candidate.')
    model.propose=propose
    with pytest.raises(ProviderUnavailable,match='event_history_changed_retry'):
        e.add(req('Aster added refreshments to the workshop.','concurrent',2))
    assert len(e.store.memories(OWNER)[0].revisions)==2
    assert unit(e,'concurrent')['concurrent_recomputations']==1


def test_recovery_connection_requires_later_clarification_for_historical_visibility():
    from experiments.subject_connections import review
    from experiments.subject_engine import visible_links
    e,m=lifecycle()
    # A second known identity makes an old connection potentially visible before clarification.
    from tests.test_subject_routing import add_subject
    other=add_subject(e.store,'Beryl',owner=OWNER,when='2030-01-01T00:00:00+00:00')
    source=Source(id='old-pair',owner=OWNER,session_id='session',request_id='pair',ordinal=0,
        role='user',content='Aster met Beryl.',message_at='2030-01-02T00:00:00Z',created_at='2030-01-02T00:00:00Z')
    clarification=source.model_copy(update=dict(id='new-proof',content='Those were Aster and Beryl from the workshop.',
        message_at='2030-01-05T00:00:00Z',created_at='2030-01-05T00:00:00Z'))
    for original in (source,clarification):e.store.put('sources',OWNER,original.id,original.model_dump())
    from experiments.subject_recall import prepare_candidates
    state,_=prepare_candidates(e.store,dict(owner=OWNER,new_user_source=source.model_dump(),context_sources=[clarification.model_dump()],
        recovery_clock=clarification.message_at,recovery=dict(key='pending-pair',content=source.content)))
    model=FlowModel();model.people=['Aster','Beryl']
    raw=model.maintain_subjects(state,deadline=0)
    raw['connections']=[dict(subject_refs=[0,1],evidence=[dict(source_id=source.id)])]
    result=review(raw,state,e.store)
    assert result['connections']
    for link in result['connections']:e.store.put('edges',OWNER,link['id'],link)
    objects={r['id']:r for r in e.store.list('objects',OWNER)}
    assert not visible_links(e.store,OWNER,'2030-01-03T00:00:00Z',objects)
    assert len(visible_links(e.store,OWNER,'2030-01-06T00:00:00Z',objects))==1


def test_recovering_second_claim_preserves_accepted_first_claim_from_same_source():
    model=FlowModel();model.people=['Aster'];model.pending=True;e=SubjectGraphEngine(model=model)
    def writer(s):
        if s.get('recovery'):
            return [row(s,content='Aster planned a workshop.',evidence=[dict(source_id=s['new_user_source']['id']),
                dict(source_id=s['context_sources'][-1]['id'])])]
        if 'neighbor' in s['new_user_source']['content']:return []
        return [row(s,subject_refs=[],record_type='fact',status=None,content='The locker code is 1234.'),
                row(s,content='Aster planned a workshop.')]
    model.writer=writer;e.add(req('The locker code is 1234. Aster planned a workshop.','mixed'))
    fact=e.store.memories(OWNER)[0];model.pending=False
    e.add(req('Aster at the workshop is my neighbor.','clarify',1))
    memories=e.store.memories(OWNER)
    assert len(memories)==2 and len({m.id for m in memories})==2
    assert Memory.model_validate(e.store.get('memories',OWNER,fact.id))==fact
    assert any(m.record_type=='event' for m in memories)


def test_recovery_new_subject_ref_can_differ_from_original_ref_allocation():
    from experiments.subject_recall import prepare_candidates
    from experiments.subject_connections import review,commit
    source=Source(id='pair-source',owner=OWNER,session_id='session',request_id='pair',ordinal=0,
        role='user',content='Aster and Beryl planned a workshop.',message_at='2030-01-01T00:00:00Z',created_at='2030-01-01T00:00:00Z')
    e=SubjectGraphEngine();e.store.put('sources',OWNER,source.id,source.model_dump())
    initial,_=prepare_candidates(e.store,dict(owner=OWNER,new_user_source=source.model_dump(),context_sources=[]))
    first=review(plan(name='Aster',sid=source.id),initial,e.store);commit(first,e.store)
    original=first['bindings']['subject_ids']['0']
    recovery,_=prepare_candidates(e.store,dict(owner=OWNER,new_user_source=source.model_dump(),context_sources=[],
        recovery_clock='2030-01-02T00:00:00Z',recovery=dict(key='pending-beryl',content='Beryl planned a workshop.')))
    second=review(plan(name='Beryl',sid=source.id),recovery,e.store)
    assert not second['issues']
    assert second['bindings']['subject_ids']['0']!=original
