"""V53 closed-loop scenarios for subject scope and unbound memory maintenance."""
from copy import deepcopy
import pytest

from experiments.subject_engine import SubjectGraphEngine
from remi.models import Memory
from remi.heat import visible_memory
from remi.providers import ProviderUnavailable
from tests.test_event_maintenance import Model, OWNER, extracted, req, row, unit, lifecycle, update
from tests.test_individual_subjects_experiment import plan


class FlowModel(Model):
    def __init__(self):super().__init__();self.people=[];self.pending=False
    def maintain_subjects(self,state,*,deadline):
        self.calls.append('subjects');self.packets.append(('subjects',deepcopy(state)))
        result=dict(subjects=[])
        for ref,name in enumerate(self.people):
            old=next((item for item in state['subject_catalog'] if item['name']==name),None)
            basis=next((item['id'] for item in state['identity_bases'] if old and item['target_id']==old['id']),None)
            operation='pending' if self.pending and not state.get('recovery') else 'reuse' if old else 'create'
            value=plan(name=name,operation=operation,target=old['id'] if operation=='reuse' else None,
                basis=basis,sid=state['new_user_source']['id'])['subjects'][0]
            value['ref']=ref;result['subjects'].append(value)
        return result


def test_unbound_event_gains_subject_without_changing_memory_id():
    model=FlowModel();engine=SubjectGraphEngine(model=model)
    model.extractor=lambda state:dict(items=[extracted(state,subject_refs=[])],reason='unbound')
    engine.add(req('A workshop is planned.','unbound'));original=engine.store.memories(OWNER)[0]
    model.people=['Aster'];model.extractor=None
    model.writer=lambda state:[update(state,content='Aster will host the workshop.')]
    engine.add(req('Aster will host that workshop.','bound',1))
    memory=engine.store.memories(OWNER)[0]
    assert memory.id==original.id and len(memory.current.entity_ids)==1 and len(memory.revisions)==2
    assert unit(engine,'bound')['memory_recall_trace']['lanes'][memory.id]=='unbound'


def test_relationship_statement_is_a_fact_with_both_subjects():
    model=FlowModel();model.people=['Mara','Cedar Lab'];engine=SubjectGraphEngine(model=model)
    base=model.maintain_subjects
    def subjects(state,*,deadline):
        result=base(state,deadline=deadline);result['subjects'][1]['kind']='organization';return result
    model.maintain_subjects=subjects
    model.extractor=lambda state:dict(items=[extracted(state,record_type='fact',subject_refs=[0,1],status=None)],reason='relationship fact')
    engine.add(req('Mara works at Cedar Lab.','relationship'))
    memory=engine.store.memories(OWNER)[0]
    assert memory.record_type=='fact' and len(memory.current.entity_ids)==2
    assert memory.current.content=='Mara works at Cedar Lab.'


def test_program_supplement_excludes_memories_owned_only_by_other_subjects():
    model=FlowModel();model.people=['Aster'];engine=SubjectGraphEngine(model=model)
    engine.add(req('Aster planned a workshop.','first'));old=engine.store.memories(OWNER)[0]
    model.people=['Beryl'];model.writer=lambda state:[row(state,content='Beryl joined a workshop.')]
    engine.add(req('Beryl joined the workshop.','new-person',1))
    trace=unit(engine,'new-person')['memory_recall_trace']
    assert old.id not in trace['allowed_memory_ids'] and trace['supplement_triggered']
    assert len(engine.store.memories(OWNER))==2


@pytest.mark.parametrize('kind,text',[('fact','Aster works at Cedar Lab.'),('preference','Aster prefers written notes.'),('experience','Aster learned to confirm handoffs.')])
def test_all_statement_kinds_update_the_same_memory(kind,text):
    model=Model();engine=SubjectGraphEngine(model=model)
    model.extractor=lambda state:dict(items=[extracted(state,record_type=kind,status=None)],reason='statement')
    engine.add(req(text,'first'));mid=engine.store.memories(OWNER)[0].id
    model.writer=lambda state:[update(state,content=text+' Confirmed.')]
    engine.add(req(text+' Confirmed.','second',1))
    memory=engine.store.memories(OWNER)[0]
    assert memory.id==mid and len(memory.revisions)==2 and memory.record_type==kind


def test_pending_identity_stores_an_unbound_memory():
    model=FlowModel();model.people=['Aster'];model.pending=True;engine=SubjectGraphEngine(model=model)
    engine.add(req('Aster planned a workshop.','pending'))
    memories=engine.store.memories(OWNER)
    assert len(memories)==1 and memories[0].current.entity_ids==[]
    assert any(document.memory_id==memories[0].id for document in engine.store.documents(OWNER))


def test_unbound_memory_keeps_its_original_event_and_observation_clock():
    model=FlowModel();model.people=['Aster'];model.pending=True;engine=SubjectGraphEngine(model=model)
    engine.add(req('Aster attended the workshop today.','pending'))
    model.pending=False
    engine.add(req('Aster at that workshop is my neighbor.','clarified',5))
    memory=next(memory for memory in engine.store.memories(OWNER) if 'attended' in memory.current.content)
    assert memory.current.event_at.startswith('2030-01-01')
    assert memory.current.observed_at.startswith('2030-01-01') and len(memory.current.source_ids)==1
    assert visible_memory(memory,'2030-01-02T00:00:00Z') is not None
    assert set(engine.store.records)=={'sources','memories','objects','edges','requests','search_documents'}


def test_program_recall_snapshot_detects_concurrent_change():
    model=FlowModel();model.people=['Aster'];engine=SubjectGraphEngine(model=model)
    engine.add(req('Aster planned a workshop.','first'));mid=engine.store.memories(OWNER)[0].id;mutated=[]
    def propose(state,*,deadline):
        if not mutated:
            stored=engine.store.get('memories',OWNER,mid);later=deepcopy(stored['revisions'][0])
            later.update(id='concurrent-future',observed_at='2030-01-10T00:00:00Z',created_at='2030-01-10T00:00:00Z',maintenance_patch=None)
            stored['revisions'].append(later);engine.store.put('memories',OWNER,mid,stored);mutated.append(True)
        return dict(items=[update(state)],reason='selected target')
    model.propose=propose
    with pytest.raises(ProviderUnavailable,match='event_history_changed_retry'):
        engine.add(req('Aster added refreshments to the workshop.','concurrent',2))
    assert unit(engine,'concurrent')['concurrent_recomputations']==1


def test_append_first_creates_no_pending_recovery_job():
    model=FlowModel();model.people=['Aster'];model.pending=True;engine=SubjectGraphEngine(model=model)
    engine.add(req('Aster planned a workshop.','pending'))
    assert engine.store.memories(OWNER)
    assert not unit(engine,'pending').get('pending_candidates')
