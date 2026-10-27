"""One current-participant handle namespace, readable historical evidence and exact repair feedback."""
from copy import deepcopy
import json
from time import monotonic
from types import SimpleNamespace

import httpx
import pytest

from experiments.subject_extraction import (
    ExtractionInvalid, ExtractionPlan, PROMPT, decode, examples, feedback, prepare, propose,
    render, schema,
)
from remi.models import ProposalDraft
from remi.providers import OutputInvalid, ProviderUnavailable


def capture(state,reply,*,errors=()):
    calls=[];remaining_errors=list(errors)
    def create(**body):
        calls.append(body)
        if remaining_errors:raise remaining_errors.pop(0)
        content=reply if isinstance(reply,str) else json.dumps(reply)
        return SimpleNamespace(usage=None,choices=[SimpleNamespace(message=SimpleNamespace(content=content,refusal=None))])
    model=SimpleNamespace(model='gpt-4o-mini',timeout=10,calls=[],client=SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create))))
    result=propose(model,state,deadline=monotonic()+10)
    return result,calls,model.calls


def user_and_dog():
    state=deepcopy(examples()[1][0])
    state['new_user_source']['content']='My dog Cedar needs me to book a vet visit.'
    state['resolved_subjects'].insert(0,dict(ref=2,id='stored-current-user',name='@user',kind='person',
                                          aliases=[],status='resolved',is_user=True))
    return state


def test_every_fewshot_executes_the_live_wire_schema_and_restores_the_internal_contract():
    kinds=set()
    for state,reply in examples():
        ExtractionPlan.model_validate(reply)
        packet,context=prepare(state);result=decode(reply,context);ProposalDraft.model_validate(result)
        handles=set(schema(packet)['$defs']['ExtractionItem']['properties']['participant_handles']['items'].get('enum',[]))
        for wire,item in zip(reply['items'],result['items']):
            assert set(wire['participant_handles'])<=handles
            assert item['subject_refs']==[int(handle[1:]) for handle in wire['participant_handles']]
            assert item['evidence']==[dict(source_id=state['new_user_source']['id'])]
            kinds.add(item['record_type'])
    assert kinds=={'fact','preference','experience','event'}


def test_handles_remain_stable_after_current_view_reordering_or_filtering():
    state=user_and_dog();packet,context=prepare(state)
    assert context['handle_refs']=={'P2':2,'P7':7}
    reordered=deepcopy(state);reordered['resolved_subjects'].reverse()
    assert prepare(reordered)[1]['handle_refs']=={'P7':7,'P2':2}
    filtered=deepcopy(state);filtered['resolved_subjects']=filtered['resolved_subjects'][1:]
    assert prepare(filtered)[1]['handle_refs']=={'P7':7}
    assert [row['handle'] for row in packet['current_participants']]==['P2','P7']


def test_stored_ids_and_integer_refs_stay_out_of_the_rendered_current_and_historical_metadata():
    state=user_and_dog();packet,context=prepare(state);body=render(packet)
    assert 'P2 | name: @user' in body and 'P7 | name: Cedar' in body
    assert 'Historical participant A | known as: Cedar' in body
    assert 'Historical participant B | known as: Cedar | same stored identity as current P7' in body
    assert 'Historical participants: Historical participant A' in body
    assert 'Historical participants: Historical participant B' in body
    for hidden in ['stored-current-user','dog-identity','neighbor-identity','old-neighbor','old-dog','ref: 7','ref=7','subject_refs']:
        assert hidden not in body
    assert schema(packet)['$defs']['ExtractionItem']['properties']['participant_handles']['items']['enum']==['P2','P7']


def test_pending_current_participant_remains_selectable_and_is_not_replaced_by_historical_namesakes():
    state,reply=deepcopy(examples()[3]);packet,context=prepare(state);body=render(packet)
    assert 'P14 | name: Norah | kind: person | status: pending' in body
    assert 'P2 | name: @user | kind: person | status: resolved' in body
    assert [row['current_handle'] for row in packet['historical_participants']]==[None,None]
    assert decode(reply,context)['items'][0]['subject_refs']==[14,2]
    assert schema(packet)['$defs']['ExtractionItem']['properties']['participant_handles']['items']['enum']==['P2','P14']


def test_current_grounding_is_displayed_as_original_words_without_another_participant_id():
    state=user_and_dog()
    state['current_evidence']=[dict(id='C1',text=state['new_user_source']['content'])]
    for participant in state['resolved_subjects']:participant['current_evidence_id']='C1'
    packet,_=prepare(state);body=render(packet)
    assert 'Current-source grounding: '+state['new_user_source']['content'] in body
    assert 'C1' not in body
    assert 'Identity: the canonical conversation user.' in body


@pytest.mark.parametrize('handle',['P99','M1','S1','E2','Historical participant A',7])
def test_wrong_or_historical_handle_is_rejected_as_a_protocol_failure(handle):
    state,reply=deepcopy(examples()[1]);packet,context=prepare(state)
    reply['items'][0]['participant_handles']=[handle]
    with pytest.raises(ExtractionInvalid) as caught:decode(reply,context)
    assert caught.value.raw_output==reply and caught.value.issues
    assert caught.value.issues[0]['path'][:3]==['items',0,'participant_handles']
    assert isinstance(caught.value,OutputInvalid)


def test_duplicate_handle_is_a_distinct_structured_protocol_error():
    state,reply=deepcopy(examples()[1]);_,context=prepare(state)
    reply['items'][0]['participant_handles']=['P7','P7']
    with pytest.raises(ExtractionInvalid) as caught:decode(reply,context)
    issue=caught.value.issues[0]
    assert issue['code']=='extraction_participant_handles_must_be_unique'
    assert issue['allowed_handles']==['P7'] and issue['path']==['items',0,'participant_handles']


def test_unknown_source_is_separate_from_handle_and_schema_errors():
    state,reply=deepcopy(examples()[1]);_,context=prepare(state)
    reply['items'][0]['evidence']=[dict(source_id='S99')]
    with pytest.raises(ExtractionInvalid) as caught:decode(reply,context)
    issue=next(row for row in caught.value.issues if row['code']=='extraction_requires_supplied_source_handle')
    assert issue['code']=='extraction_requires_supplied_source_handle'
    assert issue['allowed_source_ids']==['S1','S2','S3']


def test_a_legal_handle_can_still_select_the_wrong_semantic_participant():
    state=user_and_dog();_,context=prepare(state)
    reply=deepcopy(examples()[1][1]);reply['items'][0].update(content='Cedar needs a vet visit.',participant_handles=['P2'])
    structural_result=decode(reply,context)
    assert structural_result['items'][0]['subject_refs']==[2]
    dog_ref=7
    assert structural_result['items'][0]['subject_refs']!=[dog_ref]
    assert 'A legal handle alone supplies no semantic proof.' in PROMPT


def test_no_current_participants_allows_empty_refs_without_promoting_historical_participants():
    state=deepcopy(examples()[1][0]);state['resolved_subjects']=[]
    packet,context=prepare(state);prop=schema(packet)['$defs']['ExtractionItem']['properties']['participant_handles']
    assert prop['maxItems']==0 and context['handle_refs']=={}
    assert packet['historical_participants'] and all(row['current_handle'] is None for row in packet['historical_participants'])
    reply=deepcopy(examples()[1][1]);reply['items'][0]['participant_handles']=[]
    assert decode(reply,context)['items'][0]['subject_refs']==[]


def test_repair_translates_internal_previous_output_and_errors_into_the_same_handle_protocol():
    state,wire=deepcopy(examples()[3]);_,context=prepare(state)
    state['previous_output']=decode(wire,context)
    state['repair_errors']=[dict(path=['items',0,'subject_refs'],actual=dict(subject_refs=[14,2]),expected_refs=[2,14],
        target_id='nursery-norah',requirement='Keep ref 14; stored identity nursery-norah is historical.')]
    packet,_=prepare(state);previous=packet['previous_output'];error=packet['repair_errors'][0]
    assert previous['items'][0]['participant_handles']==['P14','P2']
    assert previous['items'][0]['evidence']==[dict(source_id='S1')]
    assert error['path']==['items',0,'participant_handles']
    assert error['actual']==dict(participant_handles=['P14','P2'])
    assert error['expected_handles']==['P2','P14']
    serialized=json.dumps([previous,error])
    assert 'subject_refs' not in serialized and 'nursery-norah' not in serialized and 'ref 14' not in serialized
    assert error['participant']=='Historical participant A'


def test_repair_accepts_a_raw_wire_answer_and_preserves_the_invalid_current_handle_for_correction():
    state,wire=deepcopy(examples()[1]);wire['items'][0]['participant_handles']=['P99']
    state['previous_output']=wire
    state['repair_errors']=[dict(path=['items',0,'participant_handles',0],code='extraction_requires_current_participant_handle',
                                 actual='P99',allowed_handles=['P7'])]
    result,calls,traces=capture(state,examples()[1][1])
    assert result['items'][0]['subject_refs']==[7]
    messages=calls[0]['messages']
    assert json.loads(messages[-2]['content'])['items'][0]['participant_handles']==['P99']
    assert 'P7' in messages[-1]['content'] and 'extraction_requires_current_participant_handle' in messages[-1]['content']
    assert traces[0]['kind']=='add_propose_repair'


def test_malformed_previous_internal_refs_are_rendered_without_crashing_or_revealing_new_ids():
    state,wire=deepcopy(examples()[1]);_,context=prepare(state)
    previous=decode(wire,context);previous['items'][0]['subject_refs']=[{'target_id':'neighbor-identity'},True,95]
    state['previous_output']=previous;state['repair_errors']=[dict(ref={'invalid':1},target_id='E99')]
    packet,_=prepare(state)
    assert packet['previous_output']['items'][0]['participant_handles']==['(invalid participant)','(invalid participant)','(unavailable participant)']
    assert 'neighbor-identity' not in json.dumps(packet['previous_output'])
    assert 'E99' not in json.dumps(packet['repair_errors'])


def test_transport_uses_live_schema_and_readable_independent_fewshots_and_restores_source_ids():
    state,wire=deepcopy(examples()[1]);result,calls,traces=capture(state,wire)
    assert result['items'][0]['subject_refs']==[7]
    assert result['items'][0]['evidence']==[dict(source_id='example-current')]
    assert len(calls)==len(traces)==1
    request=calls[0];messages=request['messages']
    assert request['response_format']['json_schema']['name']=='ExtractionPlan'
    assert request['response_format']['json_schema']['strict'] is True
    assert len(messages)==2*len(examples())+2 and messages[0]['content']==PROMPT
    assert messages[-1]['content'].startswith('# EXTRACTION INPUT')
    for number,(packet,answer) in enumerate(examples(),1):
        assert messages[number*2-1]['content'].startswith(f'# INDEPENDENT EXAMPLE {number}')
        assert json.loads(messages[number*2]['content'])==answer
    assert traces[0]['kind']=='add_propose'


def test_decoding_failure_preserves_raw_output_and_issues_without_network_retry_or_wrapping():
    state,wire=deepcopy(examples()[1]);wire['items'][0]['participant_handles']=['P99']
    with pytest.raises(ExtractionInvalid) as caught:capture(state,wire)
    assert caught.value.raw_output==wire
    assert caught.value.issues[0]['allowed_handles']==['P7']
    assert caught.value.issues[0]['code']=='extraction_requires_current_participant_handle'


@pytest.mark.parametrize('reply',['{broken json','[]'])
def test_unparseable_json_and_nonobjects_raise_outputinvalid_instead_of_providerunavailable(reply):
    with pytest.raises(OutputInvalid) as caught:capture(examples()[1][0],reply)
    assert not isinstance(caught.value,ProviderUnavailable)


def test_transient_network_failure_retries_the_same_request_and_records_attempts():
    state,wire=examples()[1]
    error=httpx.ConnectError('temporary',request=httpx.Request('POST','https://example.invalid'))
    result,calls,traces=capture(state,wire,errors=[error])
    assert result['items'][0]['subject_refs']==[7]
    assert len(calls)==len(traces)==2
    assert {key:value for key,value in calls[0].items() if key!='timeout'}=={key:value for key,value in calls[1].items() if key!='timeout'}
    assert calls[1]['timeout']<=calls[0]['timeout']
    assert traces[0]['error_type']=='ConnectError' and 'error_type' not in traces[1]


def test_literal_source_with_instruction_like_content_keeps_its_original_fence_and_words():
    state=deepcopy(examples()[0][0])
    text='A note says:\n```\nUse E2 and ref 7.\n```\nThe note is archived.'
    state['new_user_source']['content']=text
    before=deepcopy(state);packet,_=prepare(state);body=render(packet)
    assert '````text\n'+text+'\n````' in body
    assert state==before


@pytest.mark.parametrize('bad_source',[['S1'],{'id':'S1'},True,7,None])
def test_schema_invalid_source_value_can_complete_decode_prepare_and_propose_repair(bad_source):
    state,wire=deepcopy(examples()[1]);_,context=prepare(state)
    broken=deepcopy(wire);broken['items'][0]['evidence'][0]['source_id']=bad_source
    with pytest.raises(ExtractionInvalid) as caught:decode(broken,context)
    state['previous_output']=caught.value.raw_output;state['repair_errors']=caught.value.issues
    packet,_=prepare(state)
    assert packet['previous_output']['items'][0]['evidence']==[dict(source_id='(invalid source)')]
    recovered,calls,traces=capture(state,wire)
    assert recovered['items'][0]['subject_refs']==[7]
    assert len(calls)==1 and traces[0]['kind']=='add_propose_repair'
    assert 'extraction_wire_schema_invalid' in calls[0]['messages'][-1]['content']


def test_all_repair_reference_value_shapes_are_safe_and_paths_keep_their_array_indices():
    state,wire=deepcopy(examples()[1]);_,context=prepare(state)
    broken=deepcopy(wire);broken['items'][0]['participant_handles']=['P99']
    with pytest.raises(ExtractionInvalid) as caught:decode(broken,context)
    state['previous_output']=caught.value.raw_output
    state['repair_errors']=[dict(path=['items',0,'participant_handles',0],ref={'bad':1},subject_refs=[{},True,[7]],
        participant_handles=[{},True,7],target_id=[],entity_ids=[{},True,'neighbor-identity'],
        source_id={},source_ids=[{},7,'example-current'],memory_ids=[{},True,'old-neighbor'],
        allowed_handles=[[],None,'P7'],requirement='Select a current participant handle.')]
    packet,_=prepare(state);issue=packet['repair_errors'][0]
    assert issue['path']==['items',0,'participant_handles',0]
    assert issue['participant_handle']=='(invalid participant)'
    assert issue['participant']=='(invalid identity)'
    assert issue['participant_handles']==['(invalid participant)','(invalid participant)','P7']
    assert issue['source_id']=='(invalid source)'
    assert issue['source_ids']==['(invalid source)','(invalid source)','S1']
    assert issue['memory_ids']==['(invalid memory)','(invalid memory)','M1']
    recovered,calls,_=capture(state,wire)
    assert recovered['items'][0]['subject_refs']==[7] and len(calls)==1


def test_repair_keeps_source_content_conditions_dates_and_reason_literal_in_the_actual_messages():
    state,wire=deepcopy(examples()[1])
    source_text='A note about an E2 visa contains the literal code dog-identity.'
    state['new_user_source']['content']=source_text
    wire['items'][0].update(content=source_text,context='if the E2 visa remains active',
                           record_type='event',status='planned',time_expression='2028-05-10 (E2 visa appointment)')
    wire['reason']='The E2 visa and dog-identity strings are part of this note.'
    _,context=prepare(state);broken=deepcopy(wire);broken['items'][0]['participant_handles']=['P99']
    with pytest.raises(ExtractionInvalid) as caught:decode(broken,context)
    state['previous_output']=caught.value.raw_output
    state['repair_errors']=[*caught.value.issues,dict(path=['items',0,'content'],actual=dict(
        content=source_text,context='if the E2 visa remains active',time_expression='2028-05-10 (E2 visa appointment)'),
        requirement='Keep the source text literal.')]
    packet,_=prepare(state)
    assert packet['previous_output']['reason']==wire['reason']
    for field in ['content','context','time_expression']:
        assert packet['previous_output']['items'][0][field]==wire['items'][0][field]
        assert packet['repair_errors'][-1]['actual'][field]==wire['items'][0][field]
    recovered,calls,_=capture(state,wire)
    previous=json.loads(calls[0]['messages'][-2]['content'])
    assert previous['reason']==wire['reason']
    for field in ['content','context','time_expression']:
        assert previous['items'][0][field]==wire['items'][0][field]
        assert recovered['items'][0][field]==wire['items'][0][field]
    assert 'historical identity visa' not in calls[0]['messages'][-2]['content']


def test_current_and_historical_evidence_can_both_support_the_same_memory():
    state,wire=deepcopy(examples()[1]);_,context=prepare(state)
    wire['items'][0]['evidence']=[dict(source_id=context['current_source_handle']),dict(source_id='S3')]
    result=decode(wire,context)
    assert result['items'][0]['evidence']==[dict(source_id='example-current'),dict(source_id='dog-source')]


def test_only_historical_evidence_enters_shared_repair_and_the_corrected_plan_keeps_both_sources():
    state,wire=deepcopy(examples()[1]);_,context=prepare(state)
    broken=deepcopy(wire);broken['items'][0]['evidence']=[dict(source_id='S3')]
    with pytest.raises(ExtractionInvalid) as caught:decode(broken,context)
    issue=caught.value.issues[0]
    assert issue==dict(path=['items',0,'evidence'],code='extraction_requires_current_source',
        requirement='Include CURRENT SOURCE for every new memory item.',current_source_handle=context['current_source_handle'],actual=['S3'])
    state['previous_output']=caught.value.raw_output;state['repair_errors']=caught.value.issues
    wire['items'][0]['evidence']=[dict(source_id=context['current_source_handle']),dict(source_id='S3')]
    result,calls,traces=capture(state,wire)
    assert len(calls)==1 and traces[0]['kind']=='add_propose_repair'
    assert result['items'][0]['evidence']==[dict(source_id='example-current'),dict(source_id='dog-source')]
    assert 'extraction_requires_current_source' in calls[0]['messages'][-1]['content']


def test_current_source_requirement_uses_context_identity_instead_of_a_fixed_s_label():
    state,wire=deepcopy(examples()[1]);_,context=prepare(state)
    context['current_source_handle']='S-current'
    context['source_originals']['S-current']='example-current'
    with pytest.raises(ExtractionInvalid) as caught:decode(wire,context)
    assert caught.value.issues[0]['code']=='extraction_requires_current_source'
    assert caught.value.issues[0]['current_source_handle']=='S-current'
    wire['items'][0]['evidence']=[dict(source_id='S-current')]
    assert decode(wire,context)['items'][0]['evidence']==[dict(source_id='example-current')]


def test_live_extraction_and_repair_keep_only_the_latest_context_message():
    for repair in [False,True]:
        state,wire=deepcopy(examples()[0])
        state['context_sources']=[dict(id='older-context',role='user',content='OLDER_CONTEXT_MARKER'),
                                  dict(id='latest-context',role='assistant',content='LATEST_CONTEXT_MARKER')]
        if repair:
            state.update(previous_output=deepcopy(wire),repair_errors=[dict(code='review_current_evidence')])
        before=deepcopy(state)
        result,calls,traces=capture(state,wire)
        body=next(message['content'] for message in calls[0]['messages']
                  if message['role']=='user' and message['content'].startswith('# EXTRACTION INPUT'))
        assert 'LATEST_CONTEXT_MARKER' in body and 'OLDER_CONTEXT_MARKER' not in body
        assert state==before and result['items']
