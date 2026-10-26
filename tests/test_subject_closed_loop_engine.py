"""The identity-to-memory boundary preserves current referents and actionable repair evidence."""
from copy import deepcopy
from time import monotonic

from experiments.subject_engine import SubjectGraphEngine
from remi.providers import OutputInvalid


def reviewed():
    primary=dict(id='source-now',role='user',content='I called the courier.',
                 message_at='2029-08-01T10:00:00+00:00',created_at='2029-08-01T10:00:00+00:00')
    def subject(ref,name,valid):
        return dict(ref=ref,name=name,kind='individual',is_user=ref==0,aliases=[],descriptions=[],
                    current_reference_valid=valid,current_evidence_id='C1' if valid else None)
    state=dict(owner='boundary-owner',new_user_source=primary,source_catalog={primary['id']:primary},
               context_sources=[],history=[],subject_catalog=[],proposal=dict(
                   subjects=[subject(0,'@user',True),subject(1,'Historical neighbor',False),
                             subject(7,'the courier',True)],items=[],reason='reviewed'))
    return dict(state=state,bindings=dict(subject_ids={'0':'user-id'},entity_rows=[]))


def proposal():
    return dict(items=[dict(content='I called the courier.',record_type='event',subject_refs=[0,7],
                           mode='asserted',context=None,status='occurred',time_expression=None,
                           evidence=[dict(source_id='source-now')])],reason='Current participants only.')


class Capture:
    def __init__(self):self.inputs=[]
    def propose(self,state,*,deadline):self.inputs.append(deepcopy(state));return proposal()
    def close(self):pass


def test_memory_view_keeps_grounded_pending_referents_and_excludes_ungrounded_historical_rows():
    model=Capture();engine=SubjectGraphEngine(model=model);cache={}
    engine._memory_after_identity(cache,reviewed(),lambda:None,monotonic()+10)
    view=model.inputs[0]['resolved_subjects']
    assert [(row['ref'],row['status']) for row in view]==[(0,'resolved'),(7,'pending')]
    assert view[0]['is_user'] is True and view[1]['current_evidence_id']=='C1'
    assert cache['items'][0]['subject_refs']==[0,7]
    assert cache['current_participants']==view
    engine.close()


def test_wire_rejection_reaches_shared_repair_with_original_output_and_specific_issue():
    raw=dict(items=[dict(participant_handles=['P88'])],reason='Invalid current handle.')
    issues=[dict(path=['items',0,'participant_handles',0],code='unknown_current_handle',
                 requirement='Choose a current participant handle.',actual='P88',allowed_handles=['P0','P7'])]
    class Fault(Capture):
        def propose(self,state,*,deadline):
            self.inputs.append(deepcopy(state))
            if not state.get('repair_errors'):
                error=OutputInvalid('invalid_current_handle');error.raw_output=raw;error.issues=issues
                raise error
            return proposal()
    model=Fault();engine=SubjectGraphEngine(model=model);cache={};saved=[]
    engine._memory_after_identity(cache,reviewed(),lambda:saved.append(deepcopy(cache)),monotonic()+10)
    assert len(model.inputs)==2
    assert model.inputs[1]['previous_output']==raw
    assert model.inputs[1]['repair_errors']==issues
    assert cache['outputs']['propose']==raw and cache['extraction_protocol_issues']['propose']==issues
    assert cache['repair_used'] and cache['repair_stage']=='propose'
    assert cache['items'][0]['subject_refs']==[0,7]
    assert any(row.get('outputs',{}).get('propose')==raw for row in saved)
    engine.close()
