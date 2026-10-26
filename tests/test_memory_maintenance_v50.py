"""V50 memory maintenance: program diffs, support evidence and atomic current links."""
import json
from time import monotonic
from types import SimpleNamespace

from experiments.event_maintenance import examples, provider_plan
from experiments.subject_engine import SubjectGraphEngine, SubjectMemoryStore
from experiments.subject_recall import pack_history, prepare_candidates, RECALL_BYTES
from remi.graph import active_connections
from tests.test_event_maintenance import Model, OWNER, lifecycle, req, row, update, unit
from tests.test_keyword_subject_recall import add_subject, state, source


def test_update_with_unchanged_view_creates_support_revision_and_appends_evidence():
    engine,model=lifecycle();before=engine.store.memories(OWNER)[0]
    model.writer=lambda packet:[update(packet,content=packet['history'][0]['content'],status='planned')]
    engine.add(req('Aster still plans that workshop.','support',1))
    memory=engine.store.memories(OWNER)[0];current=memory.current
    assert memory.id==before.id and len(memory.revisions)==2
    assert current.operation=='support' and current.content==before.current.content
    assert current.maintenance_patch['fields']==[]
    assert len(current.source_ids)==2
    assert unit(engine,'support')['candidate_dispositions'][0]['operation']=='supported'


def test_long_evidence_history_keeps_current_view_in_recall_budget():
    store=SubjectMemoryStore();add_subject(store,'Edda')
    packet,_=prepare_candidates(store,state('Edda confirmed the schedule.'))
    sources=[source('history-'+str(index),('context '+str(index)+' ')*900) for index in range(50)]
    card=dict(id='long-supported-memory',content='Edda confirmed the schedule.',record_type='fact',
              entity_ids=[],sources=sources)
    filled,trace=pack_history(store,packet,[card])
    assert trace['selected_memory_ids']==['long-supported-memory']
    assert trace['compacted_memory_ids']==['long-supported-memory']
    assert len(filled['history'][0]['sources'])<=3
    assert trace['recall_bytes']<=RECALL_BYTES


def test_same_turn_new_events_resolve_current_refs_atomically():
    model=Model();engine=SubjectGraphEngine(model=model)
    def writer(packet):
        first=row(packet,ref='C1',content='Aster scheduled a review after the workshop.')
        first['links']['add_current']=['C2']
        second=row(packet,ref='C2',content='Aster scheduled the workshop.')
        return [first,second]
    model.writer=writer
    engine.add(req('Aster scheduled a workshop and a review after it.','same-turn'))
    memories=engine.store.memories(OWNER)
    assert len(memories)==2
    by_content={memory.current.content:memory for memory in memories}
    review=by_content['Aster scheduled a review after the workshop.']
    workshop=by_content['Aster scheduled the workshop.']
    edges=[edge for edge in active_connections(engine.store,OWNER,'2031-01-01T00:00:00Z')
           if edge.origin==review.id and edge.relation=='related']
    assert len(edges)==1 and edges[0].target==workshop.id


def test_each_returned_item_receives_created_or_validation_failed_disposition():
    model=Model();engine=SubjectGraphEngine(model=model)
    def writer(packet):
        good=row(packet,ref='C1',content='Aster plans a picnic.')
        bad=row(packet,ref='C2',content='Aster plans a walk.',subject_refs=None)
        return [good,bad]
    model.writer=writer
    engine.add(req('Aster plans a picnic and a walk.','dispositions'))
    dispositions=unit(engine,'dispositions')['candidate_dispositions']
    assert {(entry['candidate_id'],entry['operation']) for entry in dispositions}=={
        ('C1','created'),('C2','validation_failed')
    }


def test_maintenance_prompt_presents_extracted_candidates_with_p_handles():
    state,answer=list(examples())[0]
    state['memory_candidates']=[dict(ref='C1',content='Mira plans another studio visit on June 8.',
        record_type='event',subject_refs=[0],mode='asserted',context=None,status='planned',
        time_expression='June 8',evidence=[dict(source_id='now')])]
    captured=[]
    def create(**request):
        captured.append(request)
        return SimpleNamespace(usage=None,choices=[SimpleNamespace(message=SimpleNamespace(
            content=json.dumps(answer),refusal=None))])
    model=SimpleNamespace(model='test',timeout=10,calls=[],client=SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    provider_plan(model,state,deadline=monotonic()+10)
    request=captured[0];body=request['messages'][-1]['content']
    props=request['response_format']['json_schema']['schema']['$defs']['EventWire']['properties']
    assert '# CANDIDATE MEMORIES' in body and "participants: ['P0']" in body
    assert props['ref']['enum']==['C1']
