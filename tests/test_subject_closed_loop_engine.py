"""The identity-to-memory boundary preserves current referents and actionable repair evidence."""
from copy import deepcopy
from time import monotonic

from remi.engine import MemoryEngine
from remi.providers import OutputInvalid


def reviewed():
    primary=dict(id='source-now',role='user',content='I called the courier.',
                 message_at='2029-08-01T10:00:00+00:00',created_at='2029-08-01T10:00:00+00:00')
    def subject(ref,name,valid):
        return dict(ref=ref,name=name,kind='person',is_user=ref==0,aliases=[],
                    current_reference_valid=valid,current_evidence_id='C1' if valid else None)
    state=dict(owner='boundary-owner',new_user_source=primary,source_catalog={primary['id']:primary},
               context_sources=[],history=[],subject_catalog=[],proposal=dict(
                   subjects=[subject(0,'@user',True),subject(1,'Historical neighbor',False),
                             subject(7,'the courier',True)],items=[],reason='reviewed'))
    return dict(state=state,bindings=dict(subject_ids={'0':'user-id'},entity_rows=[]))


def proposal():
    from tests.test_memory_lifecycle import row
    state=dict(new_user_source=dict(id='source-now',content='I called the courier.'))
    return dict(items=[row(state,content='I called the courier.',record_type='event',subject_refs=[0,7],
                           status='occurred')],reason='Current participants only.')


class Capture:
    def __init__(self):self.inputs=[]
    def extract_memories(self,state,*,deadline):
        return dict(items=[dict(content=state['new_user_source']['content'],record_type='event',subject_refs=[0,7],mode='asserted',
            context=None,status='occurred',time_expression=None,evidence=[dict(source_id=state['new_user_source']['id'])])],reason='test candidate')
    def propose(self,state,*,deadline):self.inputs.append(deepcopy(state));return proposal()
    def close(self):pass


def test_memory_view_keeps_grounded_pending_referents_and_excludes_ungrounded_historical_rows():
    model=Capture();engine=MemoryEngine(model=model);cache={}
    engine._memory_after_identity(cache,reviewed(),lambda:None,monotonic()+10)
    view=model.inputs[0]['resolved_subjects']
    assert [(row['ref'],row['status']) for row in view]==[(0,'resolved'),(7,'pending')]
    assert view[0]['is_user'] is True and view[1]['current_evidence_id']=='C1'
    assert cache['items'][0]['subject_refs']==[0,7]
    assert cache['current_participants']==view
    engine.close()


def test_wire_rejection_is_recorded_and_repaired_once_before_rejection():
    raw=dict(items=[dict(participant_handles=['P88'])],reason='Invalid current handle.')
    issues=[dict(path=['items',0,'participant_handles',0],code='unknown_current_handle',
                 requirement='Choose a current participant handle.',actual='P88',allowed_handles=['P0','P7'])]
    class Fault(Capture):
        def propose(self,state,*,deadline):
            self.inputs.append(deepcopy(state))
            error=OutputInvalid('invalid_current_handle');error.raw_output=raw;error.issues=issues
            raise error
    model=Fault();engine=MemoryEngine(model=model);cache={};saved=[]
    engine._memory_after_identity(cache,reviewed(),lambda:saved.append(deepcopy(cache)),monotonic()+10)
    assert len(model.inputs)==2
    assert cache['outputs']['propose']==raw and cache['memory_protocol_issues']['propose']==issues
    assert cache['memory_initial_issues'][:1]==issues
    assert cache['items']==[]
    assert cache['memory_validation_dispositions'][0]['operation']=='rejected'
    assert any(row.get('outputs',{}).get('propose')==raw for row in saved)
    engine.close()
