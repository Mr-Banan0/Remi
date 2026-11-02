"""Sparse related links are evidence-bound enrichment over stable logical memories."""
from remi.graph import active_connections
from remi.memory_linking import render, validate_plan
from remi.models import Edge
from remi.providers import ProviderUnavailable
from remi.settings import Settings

from .helpers import ScriptedModel, TestEngine as MemoryEngine, command, query, request


def linked_commands(state, errors):
    return {'commands': [
        command(state, 'Aster has an ankle injury that still limits walking.', kind='fact'),
        command(state, 'Aster cancelled the hike because the ankle injury still limits walking.',
                kind='event', status='cancelled'),
    ]}


def select_first_pair(state):
    return {'links': [{'pair_id': state['pairs'][0]['id'],
                       'reason': 'Current restriction affects this activity.', 'related': True}]}


def test_explicit_dependency_creates_one_version_bound_evidence_edge():
    model=ScriptedModel(extract=linked_commands,link=select_first_pair)
    engine=MemoryEngine(model=model)
    engine.add(request('Aster cancelled the hike because an ankle injury still limits walking.'))
    edges=active_connections(engine.store,'u1','2025-01-01T00:00:00+00:00')
    assert len(edges)==1 and len(model.link_states)==1
    edge=edges[0];memories={memory.id:memory for memory in engine.store.memories('u1')}
    assert edge.revision_id==memories[edge.origin].current.id
    assert edge.target_revision_id==memories[edge.target].current.id
    assert edge.quote in engine.store.get('sources','u1',edge.source_id)['content']


def test_same_turn_co_mentions_stay_unlinked_when_linker_selects_nothing():
    def extract(state,errors):return {'commands':[
        command(state,'Aster likes jasmine tea.',kind='preference'),
        command(state,"Aster's phone case is blue.",kind='fact')]}
    model=ScriptedModel(extract=extract)
    engine=MemoryEngine(model=model)
    engine.add(request("Aster likes jasmine tea. Aster's phone case is blue."))
    assert len(model.link_states)==1
    assert active_connections(engine.store,'u1','2025-01-01T00:00:00+00:00')==[]


def test_single_memory_skips_the_link_model_call():
    model=ScriptedModel(extract=lambda state,errors:{'commands':[
        command(state,'Aster likes jasmine tea.',kind='preference')]},link=select_first_pair)
    engine=MemoryEngine(model=model)
    engine.add(request('Aster likes jasmine tea.'))
    assert model.link_states==[]


def test_link_provider_failure_keeps_the_memory_write_successful():
    def fail(state):raise ProviderUnavailable('test_link_failure')
    model=ScriptedModel(extract=linked_commands,link=fail)
    engine=MemoryEngine(model=model)
    response=engine.add(request('Aster cancelled the hike because an ankle injury still limits walking.'))
    assert response.request_id=='r1' and len(engine.store.memories('u1'))==2
    unit=engine.store.get('requests','u1','r1')['units']['0']
    assert unit['link_provider_error']=='ProviderUnavailable' and unit['related_edges']==[]


def test_update_revalidates_the_link_against_both_current_revisions():
    model=ScriptedModel(extract=linked_commands,link=select_first_pair)
    engine=MemoryEngine(model=model)
    engine.add(request('Aster cancelled the hike because an ankle injury still limits walking.'))
    fact=next(memory for memory in engine.store.memories('u1') if memory.record_type=='fact')
    model.extractor=lambda state,errors:{'commands':[command(
        state,'Aster changed: the ankle injury no longer limits walking.',kind='fact')]}
    engine.add(request('Aster changed: the ankle injury no longer limits walking.',request_id='r2'))
    current={memory.id:memory for memory in engine.store.memories('u1')}
    assert len(current[fact.id].revisions)==2
    edges=active_connections(engine.store,'u1','2025-01-01T00:00:00+00:00')
    assert len(edges)==1
    assert current[fact.id].current.id in {edges[0].revision_id,edges[0].target_revision_id}
    stored=[edge for edge in engine.store.edges_for('u1') if edge.relation=='related']
    assert len(stored)==2


def test_legacy_related_edge_is_readable_but_never_active():
    edge=Edge(id='old-edge',owner='u1',origin='memory-a',target='memory-b',relation='related',
              revision_id='revision-a',memory_id='memory-a',source_id='source-a')
    assert edge.target_revision_id is None and edge.quote is None


def test_graph_channel_keeps_a_direct_related_neighbor_for_final_reranking():
    model=ScriptedModel(extract=linked_commands,link=select_first_pair)
    engine=MemoryEngine(model=model,settings=Settings(graph_limit=4))
    engine.add(request('Aster cancelled the hike because an ankle injury still limits walking.'))
    memories=engine.store.memories('u1')
    event=next(memory for memory in memories if memory.record_type=='event')
    fact=next(memory for memory in memories if memory.record_type=='fact')
    documents=engine.store.documents('u1')
    seed=next(document for document in documents if document.memory_id==event.id)
    hits=engine._graph_candidates('u1','cancelled hike',[seed],documents,None,'2025-01-01T00:00:00+00:00')
    assert fact.id in {document.memory_id for document,_ in hits}


def test_link_validation_rejects_unsupplied_pair():
    model=ScriptedModel(extract=linked_commands)
    engine=MemoryEngine(model=model)
    engine.add(request('Aster cancelled the hike because an ankle injury still limits walking.'))
    state=model.link_states[0]
    edges,issues=validate_plan({'links':[{'pair_id':'L999','reason':'Specific connection','related':True}]},state)
    assert edges==[] and issues[0]['code']=='link_pair_requires_unique_supplied_handle'
    assert 'L1 | N1 <-> N2 | evidence: E1' in render(state)


def test_link_projection_keeps_individually_selected_triangle():
    handles={name:dict(handle=name,memory_id='memory-'+name,revision_id='revision-'+name,
                       record_type='fact',content=name) for name in ('N1','N2','M1')}
    state=dict(owner='u1',handles=handles,background_links=[],current_memories=[handles['N1'],handles['N2']],
        recalled_memories=[handles['M1']],evidence=[dict(id='E1',source_id='source',text='direct dependency')],
        pairs=[
            dict(id='L1',left='N1',right='N2',allowed_evidence_ids=['E1'],existing=False,current_pair=True),
            dict(id='L2',left='N1',right='M1',allowed_evidence_ids=['E1'],existing=False,current_pair=False),
            dict(id='L3',left='N2',right='M1',allowed_evidence_ids=['E1'],existing=True,current_pair=False)],truncated=False)
    edges,issues=validate_plan({'links':[{'pair_id':pair,'reason':'Specific connection','related':True} for pair in ('L1','L2','L3')]},state)
    assert not issues and len(edges)==3
    assert {frozenset((edge.origin,edge.target)) for edge in edges}=={
        frozenset(('memory-N1','memory-N2')),frozenset(('memory-N2','memory-M1')),
        frozenset(('memory-N1','memory-M1'))}


def test_content_update_retires_old_link_when_model_rejects_new_pair():
    model=ScriptedModel(extract=linked_commands,link=select_first_pair)
    engine=MemoryEngine(model=model)
    engine.add(request('Aster cancelled the hike because an ankle injury still limits walking.'))
    model.extractor=lambda state,errors:{'commands':[command(
        state,'Aster changed: the ankle injury no longer limits walking.',kind='fact')]}
    model.linker=lambda state:{'links':[
        dict(pair_id=pair['id'],reason='The limitation has ended.',related=False)
        for pair in state['pairs']]}
    engine.add(request('Aster changed: the ankle injury no longer limits walking.',request_id='r2'))
    assert len(model.link_states)==2
    assert active_connections(engine.store,'u1','2025-01-01T00:00:00+00:00')==[]
    assert any(edge.relation=='related' for edge in engine.store.edges_for('u1'))


def test_every_pair_has_explicit_decision_and_duplicate_pair_is_rejected():
    model=ScriptedModel(extract=linked_commands)
    engine=MemoryEngine(model=model)
    engine.add(request('Aster cancelled the hike because an ankle injury still limits walking.'))
    state=model.link_states[0]
    edges,issues=validate_plan({'links':[]},state)
    assert not edges and issues[0]['code']=='link_pair_decision_missing'
    choice=dict(pair_id='L1',reason='Current limitation.',related=True)
    edges,issues=validate_plan({'links':[choice,choice]},state)
    assert not edges and all(issue['code']=='link_pair_requires_unique_supplied_handle' for issue in issues)


def test_fewshots_match_current_schema():
    from remi.memory_linking import EXAMPLES, LinkPlan
    for _,answer in EXAMPLES:
        assert LinkPlan.model_validate(answer)


def test_link_packet_preserves_current_and_recalled_times():
    model=ScriptedModel(extract=linked_commands,link=select_first_pair)
    engine=MemoryEngine(model=model)
    engine.add(request('Aster cancelled the hike because an ankle injury still limits walking.'))
    model.extractor=lambda state,errors:{'commands':[command(
        state,'Aster changed: the ankle injury no longer limits walking.',kind='fact')]}
    engine.add(request('Aster changed: the ankle injury no longer limits walking.',request_id='r2'))
    state=model.link_states[-1]
    assert state['current_memories'] and state['recalled_memories']
    for row in state['handles'].values():
        memory=next(m for m in engine.store.memories('u1') if m.id==row['memory_id'])
        for field in ('event_at','planned_at','observed_at','status','mode'):
            assert row[field]==getattr(memory.current,field)
    assert 'Observed time:' in render(state)


def test_link_render_keeps_event_date_distinct_from_observation():
    from scripts.validate_memory_links import packet
    state=packet('A previous inspection.','A new inspection.')
    state['handles']['M1'].update(event_at='2020-02-03',observed_at='2026-01-01')
    state['handles']['N1'].update(event_at='2026-02-03',observed_at='2026-02-04')
    body=render(state)
    assert 'Event time: 2020-02-03; Observed time: 2026-01-01' in body
    assert 'Event time: 2026-02-03; Observed time: 2026-02-04' in body

