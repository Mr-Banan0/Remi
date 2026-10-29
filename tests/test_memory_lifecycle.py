"""Current memory maintenance and deterministic revision lifecycle."""
from copy import deepcopy
from time import monotonic
from types import SimpleNamespace
import json
import re
import pytest

from remi.memory_maintenance import MemoryPlan, eligible_targets, examples, provider_plan, validate_plan
from remi.engine import MemoryEngine
from remi.graph import active_connections
from remi.storage import MemoryStore
from remi.models import AddRequest, AddMessage
from remi.providers import ProviderUnavailable
from tests.test_subject_identity import plan

OWNER='event-test-owner'
STAMP=1893456000000


def req(text,key,day=0,timestamp=True):
    return AddRequest(user_id=OWNER,session_id='session',request_id=key,
        messages=[AddMessage(role='user',content=text,timestamp=STAMP+day*86400000 if timestamp else None)])


def row(state,**kwargs):
    return dict(ref=kwargs.get('ref','C1'),target_id=kwargs.get('target_id'),
        content=kwargs.get('content',state['new_user_source']['content']))


def extracted(state,*,content=None,record_type='event',subject_refs=None,status='planned',time_expression=None,context=None):
    refs=[item['ref'] for item in state.get('resolved_subjects',[]) if item.get('current_evidence_id')] if subject_refs is None else subject_refs
    return dict(content=content or state['new_user_source']['content'],record_type=record_type,subject_refs=refs,
        mode='asserted',context=context,status=status if record_type=='event' else None,
        time_expression=time_expression if record_type=='event' else None,
        evidence=[dict(source_id=state['new_user_source']['id'])])


class Model:
    def __init__(self):self.calls=[];self.packets=[];self.writer=None;self.extractor=None
    def maintain_subjects(self,state,*,deadline):
        self.calls.append('subjects');self.packets.append(('subjects',deepcopy(state)))
        existing=next((s for s in state['subject_catalog'] if s['name']=='Aster'),None)
        if existing:
            basis=next(b for b in state['identity_bases'] if b['target_id']==existing['id'])
            return plan(name='Aster',operation='reuse',target=existing['id'],sid=state['new_user_source']['id'],basis=basis['id'])
        return plan(name='Aster',sid=state['new_user_source']['id'])
    def extract_memories(self,state,*,deadline):
        self.calls.append('extract')
        if self.extractor:return self.extractor(state)
        text=state['new_user_source']['content'];low=text.casefold()
        kind='preference' if any(word in low for word in ('prefer','chooses')) else 'fact' if any(word in low for word in ('works at','locker code')) else 'event'
        status=None if kind!='event' else 'occurred' if any(word in low for word in ('completed','happened','attended')) else 'cancelled' if 'cancel' in low else 'planned'
        match=re.search(r'\b(?:today|tomorrow|yesterday|(?:January|February|March|April|May|June|July|August|September|October|November|December) \d{1,2})\b',text,re.I)
        return dict(items=[extracted(state,record_type=kind,status=status,time_expression=match[0] if match else None)],reason='test candidate')
    def propose(self,state,*,deadline):
        self.calls.append('memory');self.packets.append(('memory',deepcopy(state)))
        return dict(items=self.writer(state) if self.writer else [row(state)],reason='test fixture')
    def close(self):pass


def unit(engine,key):return engine.store.get('requests',OWNER,key)['units']['0']


def lifecycle():
    model=Model();engine=MemoryEngine(model=model)
    model.writer=lambda state:[row(state,content='Aster plans a workshop on 2030-01-03.')]
    engine.add(req('Aster plans a workshop on January 3.','start'))
    return engine,model


def update(state,**kwargs):return row(state,target_id=state['history'][0]['id'],**kwargs)


def test_same_event_reschedule_and_completion_preserve_id_and_planned_time():
    engine,model=lifecycle();original=engine.store.memories(OWNER)[0];mid=original.id
    model.writer=lambda state:[update(state,content='Aster plans the workshop on 2030-01-05.')]
    engine.add(req('Aster moved that workshop to January 5.','move',1))
    moved=engine.store.memories(OWNER)[0]
    assert moved.id==mid and moved.current.planned_at.startswith('2030-01-05')
    model.writer=lambda state:[update(state,content='Aster completed the workshop on 2030-01-06.')]
    engine.add(req('Aster completed the workshop today.','finish',5))
    current=engine.store.memories(OWNER)[0].current
    assert current.planned_at.startswith('2030-01-05') and current.event_at.startswith('2030-01-06')
    assert len(engine.store.memories(OWNER)[0].revisions)==3
    assert model.calls==['subjects','extract','memory']*3


def test_new_recurring_event_creates_distinct_memory():
    engine,model=lifecycle();model.writer=lambda state:[row(state,content='Aster plans another workshop on 2030-01-09.')]
    engine.add(req('Aster plans another workshop on January 9.','again',1))
    assert len(engine.store.memories(OWNER))==2


def test_unknown_clock_preserves_relative_expression_and_unknown_structured_time():
    model=Model();engine=MemoryEngine(model=model)
    engine.add(req('Aster plans a picnic tomorrow.','import',timestamp=False))
    current=engine.store.memories(OWNER)[0].current
    assert current.planned_at is None and current.time_precision=='unknown'
    assert 'tomorrow' in current.content and current.time_evidence[0].expression=='tomorrow'



def test_program_uses_one_unambiguous_source_date_when_candidate_omits_time_expression():
    engine,model=lifecycle()
    model.extractor=lambda state:dict(items=[extracted(state,status='planned',time_expression=None)],reason='date omitted')
    model.writer=lambda state:[update(state,content='Aster plans the workshop on 2030-01-05.')]
    engine.add(req('Aster moved that workshop to January 5.','fallback-date',1))
    assert engine.store.memories(OWNER)[0].current.planned_at.startswith('2030-01-05')

def test_cancellation_preserves_existing_planned_date():
    engine,model=lifecycle();model.writer=lambda state:[update(state,content='Aster cancelled the workshop.')]
    engine.add(req('Aster cancelled the workshop.','cancel',1))
    current=engine.store.memories(OWNER)[0].current
    assert current.status=='cancelled' and current.planned_at.startswith('2030-01-03')


def test_unchanged_view_creates_support_revision_with_new_evidence():
    engine,model=lifecycle();before=engine.store.memories(OWNER)[0]
    model.writer=lambda state:[update(state,content=state['history'][0]['content'])]
    engine.add(req('Aster still plans that workshop.','support',1))
    current=engine.store.memories(OWNER)[0].current
    assert current.operation=='support' and len(current.source_ids)==2
    assert unit(engine,'support')['candidate_dispositions'][0]['operation']=='supported'


def test_ambiguous_target_creates_independent_searchable_memory():
    engine,model=lifecycle();before=engine.store.list('memories',OWNER)
    model.writer=lambda state:[row(state,target_id=None)]
    engine.add(req('Aster mentioned a workshop but its identity is unclear.','pending',1))
    assert len(engine.store.list('memories',OWNER))==len(before)+1
    assert unit(engine,'pending')['candidate_dispositions'][0]['operation']=='created'
    assert not any(doc.id.startswith('doc_pending_') for doc in engine.store.documents(OWNER))


def test_invalid_target_is_repaired_once_then_retained_as_validation_failure():
    engine,model=lifecycle();model.writer=lambda state:[row(state,operation='update',target_id='foreign')]
    before=engine.store.list('memories',OWNER);engine.add(req('Aster corrected a different meeting.','bad',1))
    assert len(engine.store.list('memories',OWNER))==len(before)
    disposition=unit(engine,'bad')['candidate_dispositions'][0]
    assert disposition['operation']=='rejected' and disposition['status']=='validation_failed'
    assert 'target_requires_recalled_same_kind_memory' in {issue['code'] for issue in unit(engine,'bad')['memory_rejections']}
    assert model.calls[-2:]==['memory','memory']


def test_update_unions_current_participants_with_historical_participants():
    engine,model=lifecycle();pid=engine.store.memories(OWNER)[0].current.entity_ids
    model.writer=lambda state:[update(state,content='Aster added details to the workshop.')]
    engine.add(req('Aster added details to the workshop.','union',1))
    assert engine.store.memories(OWNER)[0].current.entity_ids==pid


def test_provider_schema_contains_only_target_decision_fields_and_decodes_target():
    packet,answer=list(examples())[1];captured=[]
    def call(**kwargs):
        captured.append(kwargs);return SimpleNamespace(usage=None,choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(answer),refusal=None))])
    model=SimpleNamespace(model='test',timeout=30,calls=[],client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=call))))
    result=provider_plan(model,packet,deadline=monotonic()+30)
    assert result['items'][0]['target_id']=='old'
    schema=captured[0]['response_format']['json_schema']['schema']
    assert set(schema['$defs']['MemoryDecision']['properties'])=={'ref','target_id','content','related_target_ids'}
    assert all(set(item)=={'ref','target_id','content','related_target_ids'} for _,answer in examples() for item in answer['items'])


def test_examples_match_live_schema_and_validate_without_issues():
    for state,answer in examples():
        MemoryPlan.model_validate(answer)
        internal=deepcopy(answer)
        mapping={'M1':state['history'][0]['id']} if state['history'] else {}
        for item in internal['items']:
            item['target_id']=mapping.get(item['target_id'],item['target_id'])
        accepted,issues=validate_plan(internal,state,state['resolved_subjects'])
        assert not issues and len(accepted)==1


def test_candidate_target_gate_requires_concrete_topic_continuity():
    packet,_=list(examples())[1]
    packet['memory_candidates'][0]['content']='Mira plans a pottery lesson on June 4.'
    assert eligible_targets(packet)['C1']==[]
    packet['memory_candidates'][0]['content']='Mira moved the studio visit to June 4.'
    assert eligible_targets(packet)['C1']==['old']


def test_terminal_event_cannot_be_reused_for_a_later_occurrence():
    packet,_=list(examples())[3]
    answer=dict(items=[dict(ref='C1',target_id='completed',
        content='Mira plans another studio visit for June 8.')],reason='invalid reuse')
    accepted,issues=validate_plan(answer,packet,packet['resolved_subjects'])
    assert not accepted
    assert 'terminal_event_cannot_return_to_planned' in {issue['code'] for issue in issues}


def test_terminal_event_with_a_different_actual_date_is_a_separate_occurrence():
    packet,_=list(examples())[3]
    packet['memory_candidates'][0].update(status='occurred',content='Mira completed another studio visit on June 8.')
    answer=dict(items=[dict(ref='C1',target_id='completed',
        content='Mira completed another studio visit on June 8.')],reason='invalid reuse')
    accepted,issues=validate_plan(answer,packet,packet['resolved_subjects'])
    assert not accepted
    assert 'terminal_event_date_identifies_separate_occurrence' in {issue['code'] for issue in issues}


def test_embedding_failure_resumes_without_repeating_model_stages():
    from tests.test_subject_reference_pipeline import FlakyEmbedder
    model=Model();engine=MemoryEngine(model=model,embedder=FlakyEmbedder());request=req('Aster plans a picnic.','retry')
    with pytest.raises(ProviderUnavailable):engine.add(request)
    engine.add(request)
    assert model.calls==['subjects','extract','memory'] and len(engine.store.memories(OWNER))==1


def test_invalid_target_uses_one_memory_repair_call():
    model=Model();engine=MemoryEngine(model=model)
    model.writer=lambda state:[dict(row(state),target_id='foreign')]
    engine.add(req('Aster plans a picnic.','invalid-target'))
    assert model.calls==['subjects','extract','memory','memory']
    assert len(engine.store.memories(OWNER))==0
    assert unit(engine,'invalid-target')['candidate_dispositions'][0]['operation']=='rejected'


def test_invalid_target_repair_can_return_a_valid_create_decision():
    model=Model();engine=MemoryEngine(model=model)
    model.writer=lambda state:[dict(row(state),target_id=None if state.get('previous_memory_output') else 'foreign')]
    engine.add(req('Aster plans a picnic.','repaired-target'))
    assert model.calls==['subjects','extract','memory','memory']
    assert len(engine.store.memories(OWNER))==1
    assert unit(engine,'repaired-target')['candidate_dispositions'][0]['operation']=='created'


def test_cross_kind_hybrid_recall_can_materialize_a_related_memory_edge():
    model=Model();engine=MemoryEngine(model=model)
    model.extractor=lambda state:dict(items=[extracted(state,record_type='fact',status=None,
        content='Aster has an ankle injury.')],reason='injury fact')
    engine.add(req('Aster has an ankle injury.','injury'))
    fact_id=engine.store.memories(OWNER)[0].id
    model.extractor=lambda state:dict(items=[extracted(state,record_type='event',status='cancelled',
        content='Aster cancelled the hike because of the ankle injury.')],reason='cancelled event')
    model.writer=lambda state:[dict(row(state),related_target_ids=[fact_id])]
    engine.add(req('Aster cancelled the hike because of the ankle injury.','hike',1))
    event_id=next(memory.id for memory in engine.store.memories(OWNER) if memory.id!=fact_id)
    related=[edge for edge in active_connections(engine.store,OWNER,'2031-01-01T00:00:00+00:00')
             if edge.relation=='related' and edge.memory_id==event_id]
    assert [(edge.origin,edge.target) for edge in related]==[(event_id,fact_id)]


def test_snapshot_restore_requires_existing_six_collections():
    store=MemoryStore();snapshot=store.snapshot()
    assert set(snapshot)=={'sources','memories','objects','edges','requests','search_documents'}
