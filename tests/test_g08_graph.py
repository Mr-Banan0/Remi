"""Joint maintenance, historical graph and evidence invariants, independent of evaluation questions."""
from copy import deepcopy
import pytest

from remi.engine import MemoryEngine
from remi.graph import active_edges
from remi.heat import hot_view, visible_memory
from remi.providers import ProviderUnavailable
from remi.models import MaintenancePlan
from tests.helpers import HashEmbedder, query, request


class Scenario:
    def __init__(self):
        self.proposals = self.maintenances = 0
        self.contexts = []
        self.modify = lambda plan, state: plan

    def propose(self, state, *, deadline):
        self.proposals += 1
        source = state['new_user_source']
        return {'items':[{'id':'c1','content':source['content'], 'tags':['event'] if 'moved' in source['content'] else ['fact'],
            'entities':[{'name':'Rin' if 'Rin moved' in source['content'] else 'Rina','kind':'person'}],
            'evidence':[{'source_id':source['id'],'quote':source['content']}]}], 'reason':'assertion'}

    def maintain(self, state, *, deadline):
        self.maintenances += 1
        self.contexts.append(deepcopy(state))
        source=state['new_user_source']
        evidence=[{'source_id':source['id'],'quote':source['content']}]
        moving='moved' in source['content']
        person=next((e['id'] for e in state['candidate_entities'] if e['name']=='Rina'),None)
        entities=[{'ref':'p','target_id':person,'name':'Rina','kind':'person','is_self':False,
            'aliases':['Rin'] if 'Rin' in source['content'] else [],'evidence':evidence},
            {'ref':'city','target_id':None,'name':'Hillcrest' if moving else 'Lakeview','kind':'place',
             'is_self':False,'aliases':[],'evidence':evidence}]
        target=next((h['id'] for h in state['history'] if 'fact' in h['tags']),None)
        state_edit={'ref':'state','candidate_ids':['c1'],'operation':'write',
            'target_id':target if moving else None,'content':'Rina lives in '+('Hillcrest.' if moving else 'Lakeview.'),
            'change':'replace','tags':['fact'],'evidence':evidence,
            'roles':[{'entity_ref':'p','role':'resident'},{'entity_ref':'city','role':'residence'}],
            'assertions':[{'subject_ref':'p','predicate':'lives in','object_ref':'city'}], 'links':[],
            'time_expression':'today' if moving else None,'end_expression':None,'status':'stated',
            'temporal_scope':'ongoing','impact':'high'}
        edits=[state_edit]
        if moving:
            edits.append({**state_edit,'ref':'move','operation':'write','target_id':None,'change':'replace',
                'content':'Rin moved to Hillcrest today.','tags':['event'],'status':'occurred','temporal_scope':'episodic',
                'assertions':[], 'links':[{'target_ref':'state','label':'establishes the new residence'}]})
        return self.modify({'entities':entities,'edits':edits,'discarded':[]},state)

    def decide(self,state,*,deadline):
        return {'steps':[],'relevant_memory_ids':[]}


def setup_move():
    model=Scenario()
    engine=MemoryEngine(model=model,embedder=HashEmbedder())
    engine.add(request('Rina (Rin) lives in Lakeview.',when='2024-06-02T10:00:00Z'))
    engine.add(request('Rin moved to Hillcrest today; she now lives in Hillcrest.',request_id='r2',when='2024-06-03T10:00:00Z'))
    return engine,model


def test_event_candidate_recalls_and_updates_a_fact():
    engine,model=setup_move()
    assert model.proposals==2 and model.maintenances==2
    assert model.contexts[-1]['proposal']['items'][0]['tags']==['event']
    assert any('fact' in h['tags'] for h in model.contexts[-1]['history'])
    memories=engine.store.memories('u1')
    state=next(m for m in memories if 'fact' in m.tags)
    assert len(memories)==2 and len(state.revisions)==2
    assert state.current.content=='Rina lives in Hillcrest.'
    assert state.revisions[0].content=='Rina lives in Lakeview.'


def test_alias_reuses_one_identity_and_roles_are_version_local():
    engine,_=setup_move()
    people=[e for e in engine.store.list('objects','u1') if e['kind']=='person']
    assert len(people)==1 and {a['name'] for a in people[0]['aliases']}=={'Rina','Rin'}
    state=next(m for m in engine.store.memories('u1') if 'fact' in m.tags)
    cities={e['id']:e['name'] for e in engine.store.list('objects','u1') if e['kind']=='place'}
    assert {cities[i] for i in state.object_ids if i in cities}=={'Hillcrest'}
    assert {cities[i] for i in visible_memory(state,'2024-06-02T23:00:00Z').object_ids if i in cities}=={'Lakeview'}


def test_current_and_past_graph_use_their_own_supporting_revision():
    engine,_=setup_move()
    names={e['id']:e['name'] for e in engine.store.list('objects','u1')}
    old=active_edges(engine.store,'u1','2024-06-02T23:00:00Z')
    new=active_edges(engine.store,'u1','2024-06-04T00:00:00Z')
    assert {names[e.target] for e in old if e.relation=='assertion'}=={'Lakeview'}
    assert {names[e.target] for e in new if e.relation=='assertion'}=={'Hillcrest'}
    assert any(e.relation=='related' for e in new)
    for edge in engine.store.edges_for('u1'):
        memory=engine.store.memories('u1',[edge.memory_id])[0]
        assert edge.revision_id in {r.id for r in memory.revisions}


def test_derived_validity_closes_old_state_without_mutating_evidence():
    engine,_=setup_move()
    state=next(m for m in engine.store.memories('u1') if 'fact' in m.tags)
    assert state.revisions[0].valid_to is None
    view=visible_memory(state,'2024-06-04T00:00:00Z')
    assert view.revisions[0].valid_to.startswith('2024-06-03')
    assert visible_memory(state,'2024-06-02T23:00:00Z').current.valid_to is None


@pytest.mark.parametrize('corrupt', ['foreign_source','foreign_entity','foreign_memory','missing_candidate'])
def test_invalid_joint_plan_never_partially_changes_the_graph(corrupt):
    model=Scenario();engine=MemoryEngine(model=model)
    engine.add(request('Rina (Rin) lives in Lakeview.'))
    before=deepcopy(engine.store.records)
    def modify(plan,state):
        if corrupt=='foreign_source':plan['edits'][0]['evidence'][0]['source_id']='foreign'
        if corrupt=='foreign_entity':plan['entities'][0]['target_id']='foreign'
        if corrupt=='foreign_memory':plan['edits'][0].update(operation='write',target_id='foreign')
        if corrupt=='missing_candidate':plan['edits'][0]['candidate_ids']=['missing']
        return plan
    model.modify=modify
    with pytest.raises(ProviderUnavailable):engine.add(request('Rina lives in Lakeview.',request_id='r2'))
    for table in ['memories','objects','edges','search_documents']:
        assert engine.store.records[table]==before[table]
    assert engine.store.get('requests','u1','r2')['status']=='pending'


def test_unsupported_proposal_can_be_explicitly_discarded():
    model=Scenario()
    model.modify=lambda plan,state:{'entities':[],'edits':[],'discarded':[{'candidate_id':'c1','reason':'assistant suggestion was not confirmed'}]}
    engine=MemoryEngine(model=model)
    engine.add(request('Can you suggest a place?',assistant='Consider Lakeview.'))
    assert not engine.store.memories('u1')
    assert 'Lakeview' in engine.search(query('Lakeview')).data[0].content


def test_situational_state_does_not_become_indefinite_current_background():
    model=Scenario()
    def episodic(plan,state):
        plan['edits'][0].update(temporal_scope='episodic',content='Rina felt tired that day.')
        return plan
    model.modify=episodic
    engine=MemoryEngine(model=model)
    engine.add(request('Rina (Rin) felt tired today in Lakeview.'))
    assert not hot_view(engine.store.memories('u1'),'2025-01-01T00:00:00Z')
    assert hot_view(engine.store.memories('u1'),'2024-06-02T12:00:00Z')


def test_embedding_pause_and_restart_reuse_both_semantic_stages():
    class Failed(HashEmbedder):
        def embed(self,texts,*,deadline):raise ProviderUnavailable('offline pause')
    model=Scenario();engine=MemoryEngine(model=model,embedder=Failed())
    payload=request('Rina (Rin) lives in Lakeview.')
    with pytest.raises(ProviderUnavailable):engine.add(payload)
    assert not engine.store.memories('u1')
    engine.embedder=HashEmbedder()
    engine.add(payload);engine.add(payload)
    assert model.proposals==model.maintenances==1
    assert len(engine.store.memories('u1'))==1


def test_new_schema_contract_describes_entities_roles_and_relations():
    schema=MaintenancePlan.model_json_schema()
    assert {'entities','edits','discarded'}==set(schema['properties'])
    assert {'roles','assertions','links','change'}<=set(schema['$defs']['MemoryEdit']['properties'])


def test_named_third_party_stays_independent_when_model_sets_a_self_flag():
    model=Scenario()
    def wrong_self(plan,state):
        plan['entities'][0]['is_self']=True
        return plan
    model.modify=wrong_self
    engine=MemoryEngine(model=model)
    engine.add(request('Rina (Rin) lives in Lakeview.'))
    person=next(e for e in engine.store.list('objects','u1') if e['name']=='Rina')
    assert person['is_self'] is False


def test_context_only_candidates_are_explicitly_excluded_while_new_claim_survives():
    model=Scenario();engine=MemoryEngine(model=model)
    engine.add(request('Rina (Rin) lives in Lakeview.'))
    original=model.propose
    def with_context(state,*,deadline):
        plan=original(state,deadline=deadline)
        prior=state['context_sources'][0]
        plan['items'].append({'id':'old','content':prior['content'],'tags':['fact'],'entities':[],
            'evidence':[{'source_id':prior['id'],'quote':prior['content']}]})
        return plan
    model.propose=with_context
    engine.add(request('Rin moved to Hillcrest today; she now lives in Hillcrest.',request_id='r2',when='2024-06-03T10:00:00Z'))
    unit=engine.store.get('requests','u1','r2')['units']['0']
    assert unit['source_dispositions']==[{'candidate_id':'old','reason':'context_only_evidence'}]
    assert model.contexts[-1]['proposal']['items'][0]['id']=='c1'
    assert len(model.contexts[-1]['proposal']['items'])==1


def test_related_memory_edges_keep_the_target_version_when_its_state_changes():
    engine,model=setup_move()
    edge=next(e for e in engine.store.edges_for('u1') if e.relation=='related')
    state=engine.store.memories('u1',[edge.target])[0]
    assert edge.target_revision_id==state.current.id
    old_target=edge.target_revision_id
    newer=state.current.model_copy(update={'id':'rev_next','content':'Rina lives elsewhere.',
        'observed_at':'2024-06-05T10:00:00Z','replaces_revision_id':state.current.id})
    state.revisions.append(newer)
    engine.store.put('memories','u1',state.id,state.model_dump())
    assert next(e for e in engine.store.edges_for('u1',origins={edge.origin}) if e.relation=='related').target_revision_id==old_target
    assert old_target!=state.current.id


def test_assertion_object_participation_is_derived_without_a_duplicate_model_field():
    model=Scenario()
    def missing_role(plan,state):
        plan['edits'][0]['roles']=plan['edits'][0]['roles'][:1]
        return plan
    model.modify=missing_role
    engine=MemoryEngine(model=model)
    engine.add(request('Rina (Rin) lives in Lakeview.'))
    assert len(engine.store.memories('u1')[0].object_ids)==2
    assert len([e for e in engine.store.edges_for('u1') if e.relation=='involves'])==2


@pytest.mark.skipif(__import__('os').getenv('REMI_TEST_SCHEMA_APPROVED')!='1',reason='Six-table approval required')
def test_graph_export_is_read_only_and_contains_resolved_versions(tmp_path):
    import hashlib,json
    from remi.storage import SQLiteStore
    from scripts.export_graph import export
    path=tmp_path/'memory.sqlite3';SQLiteStore.initialize(path,allow_create_schema=True)
    engine=MemoryEngine(SQLiteStore(path),model=Scenario())
    engine.add(request('Rina (Rin) lives in Lakeview.'))
    engine.close()
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    summary=export(path,tmp_path/'graph')
    assert summary['quick_check']=='ok' and summary['dangling_edges']==[]
    assert hashlib.sha256(path.read_bytes()).hexdigest()==digest
    data=json.loads((tmp_path/'graph/graph-data.json').read_text())
    assert any(e['relation']=='assertion' for e in data['links'])
    assert all(e['revision_id'] for e in data['links'])


@pytest.mark.skipif(__import__('os').getenv('REMI_TEST_SCHEMA_APPROVED')!='1',reason='Six-table approval required')
def test_opening_a_legacy_graph_requests_replay_and_preserves_the_database(tmp_path):
    import hashlib
    from remi.storage import SQLiteStore
    path=tmp_path/'legacy.sqlite3';SQLiteStore.initialize(path,allow_create_schema=True)
    store=SQLiteStore(path)
    store.put('memories','u1','old',{'id':'old','owner':'u1','kind':'fact','revisions':[]})
    store.close()
    before=hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(RuntimeError,match='source_replay'):SQLiteStore(path)
    assert hashlib.sha256(path.read_bytes()).hexdigest()==before


def test_an_independent_relation_gets_its_own_identity_and_preserves_the_old_claim():
    model=Scenario();engine=MemoryEngine(model=model)
    engine.add(request('Rina (Rin) lives in Lakeview.'))
    before=engine.store.memories('u1')[0].model_dump()
    def overwrite(plan,state):
        plan['edits'][0].update(target_id=before['id'],change='replace')
        plan['edits'][0]['assertions'][0]['predicate']='works at'
        return plan
    model.modify=overwrite
    engine.add(request('Rina works at Lakeview.',request_id='r2'))
    assert engine.store.memories('u1',[before['id']])[0].model_dump()==before
    assert len(engine.store.memories('u1'))==2
    unit=engine.store.get('requests','u1','r2')['units']['0']
    assert unit['target_normalizations'][0]['accepted_target'] is None


def test_changed_relation_value_replaces_state_even_if_model_calls_it_confirmation():
    model=Scenario();engine=MemoryEngine(model=model)
    engine.add(request('Rina (Rin) lives in Lakeview.'))
    def mistaken_confirmation(plan,state):
        plan['edits'][0]['change']='confirm'
        return plan
    model.modify=mistaken_confirmation
    engine.add(request('Rin moved to Hillcrest today; she now lives in Hillcrest.',request_id='r2',when='2024-06-03T10:00:00Z'))
    memory=next(m for m in engine.store.memories('u1') if m.tags==['fact'])
    assert memory.current.change=='replace'
    assert visible_memory(memory,'2024-06-04T00:00:00Z').revisions[0].valid_to.startswith('2024-06-03')


def test_completed_occurrences_on_different_dates_keep_separate_identities():
    from tests.helpers import ScriptedModel,command,TestEngine
    model=ScriptedModel(lambda state,errors:{'commands':[command(state,time='today')]})
    engine=TestEngine(model=model);engine.add(request('I completed the review today.'))
    old=engine.store.memories('u1')[0]
    model.extractor=lambda state,errors:{'commands':[command(state,operation='update',target=old.id,time='today')]}
    engine.add(request('I completed another review today.',request_id='r2',when='2024-06-03T10:00:00Z'))
    assert len(engine.store.memories('u1'))==2
    assert len(engine.store.memories('u1',[old.id])[0].revisions)==1


def test_explicit_correction_can_revise_the_date_of_one_completed_occurrence():
    from tests.helpers import ScriptedModel,command,TestEngine
    model=ScriptedModel(lambda state,errors:{'commands':[command(state,time='today')]})
    engine=TestEngine(model=model);engine.add(request('I completed the review today.'))
    old=engine.store.memories('u1')[0]
    model.extractor=lambda state,errors:{'commands':[{**command(state,operation='update',target=old.id,time='today'),'corrects_occurrence':True}]}
    engine.add(request('Correction: that review was completed today.',request_id='r2',when='2024-06-03T10:00:00Z'))
    assert len(engine.store.memories('u1'))==1
    assert len(engine.store.memories('u1',[old.id])[0].revisions)==2


def test_owner_self_uses_program_identity_and_temporary_attributes_stay_in_memory():
    from tests.helpers import ScriptedModel,TestEngine
    from remi.identity import stable_id
    model=ScriptedModel();original=model.maintain
    def noisy(state,*,deadline):
        plan=original(state,deadline=deadline)
        plan['entities'][0]['target_id']=state['owner']
        plan['entities'].append({'ref':'clock','target_id':None,'name':'6:30 AM','kind':'time','is_self':False,'aliases':[],
            'evidence':[{'source_id':state['new_user_source']['id'],'quote':state['new_user_source']['content']}]})
        plan['edits'][0]['roles'].append({'entity_ref':'clock','role':'time'})
        return plan
    model.maintain=noisy;engine=TestEngine(model=model)
    engine.add(request('I woke at 6:30 this morning.'))
    entities=engine.store.list('objects','u1')
    assert len(entities)==1 and entities[0]['id']==stable_id('obj_','u1','self')
    assert '6:30' in engine.store.memories('u1')[0].current.content


def test_unique_entity_names_resolve_to_declared_local_refs():
    model=Scenario()
    def named(plan,state):
        for edit in plan['edits']:
            for role in edit['roles']:role['entity_ref']={'p':'Rina','city':'Lakeview'}[role['entity_ref']]
            for assertion in edit['assertions']:
                assertion['subject_ref']='Rina';assertion['object_ref']='Lakeview'
        return plan
    model.modify=named;engine=MemoryEngine(model=model)
    engine.add(request('Rina (Rin) lives in Lakeview.'))
    assert len(engine.store.memories('u1')[0].object_ids)==2
    assert any(e.relation=='assertion' for e in engine.store.edges_for('u1'))


def test_literal_relation_values_are_attributes_and_not_entity_nodes():
    model=Scenario()
    def literal(plan,state):
        plan['edits'][0]['assertions'].append({'subject_ref':'p','predicate':'ready for','object_ref':'reset'})
        return plan
    model.modify=literal;engine=MemoryEngine(model=model)
    engine.add(request('Rina (Rin) lives in Lakeview and is ready for a reset.'))
    memory=engine.store.memories('u1')[0]
    assert memory.current.attributes[0].value=='reset'
    assert len(engine.store.list('objects','u1'))==2


def test_aliases_only_register_surface_names_supported_by_their_sources():
    model=Scenario()
    def alias(plan,state):
        plan['entities'][0]['aliases']=['Rin','Invented Nickname']
        return plan
    model.modify=alias;engine=MemoryEngine(model=model)
    engine.add(request('Rina (Rin) lives in Lakeview.'))
    person=next(e for e in engine.store.list('objects','u1') if e['kind']=='person')
    assert {a['name'] for a in person['aliases']}=={'Rina','Rin'}


def test_failure_after_shared_repair_exhaustion_preserves_its_reason():
    model=Scenario();engine=MemoryEngine(model=model)
    def invalid(plan,state):
        plan['edits'][0]['target_id']='foreign'
        return plan
    model.modify=invalid
    with pytest.raises(ProviderUnavailable):
        engine.add(request('Rina lives in Lakeview.'))
    with pytest.raises(ProviderUnavailable):
        engine.add(request('Rina lives in Lakeview.'))
    unit=engine.store.get('requests','u1','r1')['units']['0']
    assert unit['last_failure']['reason']=='memory_target_requires_recalled_history'
    assert unit['last_failure']['repair_budget_exhausted'] is True


def test_model_output_cites_sources_and_program_copies_original_evidence():
    from remi.models import ProposalDraft,MaintenanceDraft
    for model in [ProposalDraft,MaintenanceDraft]:
        schema=model.model_json_schema()
        assert set(schema['$defs']['SourceCitation']['properties'])=={'source_id'}
    model=Scenario()
    def citations(plan,state):
        for group in ['entities','edits']:
            for item in plan[group]:
                item['evidence']=[{'source_id':e['source_id']} for e in item['evidence']]
        return plan
    model.modify=citations;engine=MemoryEngine(model=model)
    text='Rina (Rin) lives in Lakeview.'
    engine.add(request(text))
    assert engine.store.memories('u1')[0].current.evidence[0].quote==text


def test_unsupported_optional_assertion_is_audited_without_creating_an_edge():
    model=Scenario()
    def unsupported(plan,state):
        plan['edits'][0]['assertions'].append({'subject_ref':'p','predicate':'owns','object_ref':'invented yacht'})
        return plan
    model.modify=unsupported;engine=MemoryEngine(model=model)
    engine.add(request('Rina (Rin) lives in Lakeview.'))
    unit=engine.store.get('requests','u1','r1')['units']['0']
    assert unit['graph_dispositions'][0]['reason']=='ungrounded_object'
    assert len(engine.store.memories('u1')[0].current.assertions)==1
    assert not any(e.label=='owns' for e in engine.store.edges_for('u1'))


def test_unused_entity_declarations_are_pruned_before_source_validation():
    model=Scenario()
    def unused(plan,state):
        plan['entities'].append({'ref':'unused','target_id':None,'name':'Unsupported Place','kind':'place',
            'is_self':False,'aliases':[], 'evidence':[{'source_id':'not_supplied'}]})
        return plan
    model.modify=unused;engine=MemoryEngine(model=model)
    engine.add(request('Rina (Rin) lives in Lakeview.'))
    assert len(engine.store.list('objects','u1'))==2


def test_unique_recalled_canonical_entity_is_reused_when_target_is_omitted():
    engine,model=setup_move()
    def omit_target(plan,state):
        plan['entities'][0]['target_id']=None
        return plan
    model.modify=omit_target
    engine.add(request('Rin moved to Hillcrest today; she now lives in Hillcrest.',request_id='r3',when='2024-06-04T10:00:00Z'))
    assert sum(e['kind']=='person' for e in engine.store.list('objects','u1'))==1


@pytest.mark.parametrize('field',['role','subject','link'])
def test_optional_graph_endpoint_errors_are_audited_while_memory_is_indexed(field):
    model=Scenario()
    def corrupt(plan,state):
        if field=='role':plan['edits'][0]['roles'].append({'entity_ref':'unknown','role':'participant'})
        if field=='subject':plan['edits'][0]['assertions'].append({'subject_ref':'walk','predicate':'feels like','object_ref':'p'})
        if field=='link':plan['edits'][0]['links'].append({'target_ref':'unknown','label':'related'})
        return plan
    model.modify=corrupt;engine=MemoryEngine(model=model)
    engine.add(request('Rina (Rin) lives in Lakeview.'))
    unit=engine.store.get('requests','u1','r1')['units']['0']
    assert unit['phase']=='indexed' and unit['graph_dispositions']
    assert len(engine.store.memories('u1'))==1
    assert not any('unknown' in (e.origin,e.target) or e.origin=='walk' for e in engine.store.edges_for('u1'))


def test_ambiguous_entity_ref_preserves_memory_and_excludes_ambiguous_edges():
    model=Scenario()
    def ambiguous(plan,state):
        other=deepcopy(plan['entities'][1]);other['name']='Fitbit';other['kind']='thing'
        plan['entities'].append(other)
        return plan
    model.modify=ambiguous;engine=MemoryEngine(model=model)
    engine.add(request('Rina (Rin) lives in Lakeview and wears Fitbit.'))
    assert len(engine.store.memories('u1'))==1
    assert len(engine.store.list('objects','u1'))==1
    assert not engine.store.memories('u1')[0].current.assertions
    assert any(x['reason']=='ambiguous_entity_ref' for x in engine.store.get('requests','u1','r1')['units']['0']['graph_dispositions'])


def test_entity_local_ref_is_not_mistaken_for_an_existing_identity():
    model=Scenario()
    def local(plan,state):
        for obj in plan['entities']:obj['target_id']=obj['ref']
        return plan
    model.modify=local;engine=MemoryEngine(model=model)
    engine.add(request('Rina (Rin) lives in Lakeview.'))
    assert len(engine.store.list('objects','u1'))==2


def test_unrelated_name_cannot_replace_a_recalled_entity_identity():
    engine,model=setup_move()
    def wrong_identity(plan,state):
        plan['entities'][0]['name']='Alin';plan['entities'][0]['aliases']=[]
        return plan
    model.modify=wrong_identity
    engine.add(request('Alin moved to Hillcrest today; she now lives in Hillcrest.',request_id='r3',when='2024-06-04T10:00:00Z'))
    people=[e for e in engine.store.list('objects','u1') if e['kind']=='person']
    assert {e['name'] for e in people}=={'Rina','Alin'}


def test_maintenance_edit_inherits_selected_candidate_evidence():
    model=Scenario()
    def only_candidate_basis(plan,state):
        for edit in plan['edits']:edit['evidence']=[]
        return plan
    model.modify=only_candidate_basis;engine=MemoryEngine(model=model)
    text='Rina (Rin) lives in Lakeview.'
    engine.add(request(text))
    assert engine.store.memories('u1')[0].current.evidence[0].quote==text
    assert engine.store.get('requests','u1','r1')['status']=='complete'


def test_same_target_compatible_supplements_compose_one_final_revision():
    model=Scenario();engine=MemoryEngine(model=model)
    engine.add(request('Rina (Rin) lives in Lakeview.'))
    target=engine.store.memories('u1')[0].id
    def supplements(plan,state):
        first=plan['edits'][0];first['target_id']=target
        extra=deepcopy(first);extra['ref']='detail';extra['content']='Rina works from home in Lakeview.'
        plan['edits'].append(extra)
        return plan
    model.modify=supplements
    engine.add(request('Rina (Rin) lives in Lakeview and works from home in Lakeview.',request_id='r2',when='2024-06-03T10:00:00Z'))
    memories=engine.store.memories('u1')
    assert len(memories)==1 and len(memories[0].revisions)==2
    assert 'lives in Lakeview' in memories[0].current.content and 'works from home' in memories[0].current.content
    unit=engine.store.get('requests','u1','r2')['units']['0']
    assert unit['write_compositions'][0]['edit_refs']==['state','detail']


def test_coverage_repair_identifies_exact_missing_candidate_for_disposition():
    model=Scenario();propose=model.propose;maintain=model.maintain
    def two_candidates(state,*,deadline):
        plan=propose(state,deadline=deadline)
        extra=deepcopy(plan['items'][0]);extra['id']='c2';extra['content']='The user asks for formatting help.'
        plan['items'].append(extra)
        return plan
    def resolve(state,*,deadline):
        plan=maintain(state,deadline=deadline)
        if state.get('repair_errors'):
            details=state['repair_errors'][0]['details']
            assert details['missing_candidate_ids']==['c2'] and details['unexpected_candidate_ids']==[]
            plan['discarded'].append({'candidate_id':'c2','reason':'Formatting request introduces no enduring assertion.'})
        return plan
    model.propose=two_candidates;model.maintain=resolve;engine=MemoryEngine(model=model)
    engine.add(request('Rina (Rin) lives in Lakeview. Can you format this?'))
    assert model.maintenances==2
    unit=engine.store.get('requests','u1','r1')['units']['0']
    assert unit['validation'][0]['details']['missing_candidate_ids']==['c2']
    assert unit['phase']=='indexed' and len(engine.store.memories('u1'))==1


def test_resubmission_inherits_prior_validation_feedback_and_reuses_proposal():
    model=Scenario();propose=model.propose;maintain=model.maintain
    def two(state,*,deadline):
        plan=propose(state,deadline=deadline);extra=deepcopy(plan['items'][0]);extra['id']='c2'
        plan['items'].append(extra);return plan
    def controlled(state,*,deadline):
        plan=maintain(state,deadline=deadline)
        if state.get('previous_output_failure'):
            previous=state['previous_output_failure']
            assert previous['last_validation'][0]['details']['missing_candidate_ids']==['c2']
            plan['discarded'].append({'candidate_id':'c2','reason':'Request adds no persistent assertion.'})
        return plan
    model.propose=two;model.maintain=controlled;engine=MemoryEngine(model=model)
    payload=request('Rina (Rin) lives in Lakeview.')
    with pytest.raises(ProviderUnavailable):engine.add(payload)
    engine.add(payload)
    assert model.proposals==1 and model.maintenances==3
    unit=engine.store.get('requests','u1','r1')['units']['0']
    assert unit['repair_used'] is True and unit['phase']=='indexed' and 'last_failure' not in unit
