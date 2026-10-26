"""Diagnostics stay separate from runtime identity decisions and from the historical score."""
from copy import deepcopy

from scripts.audit_subject_trace import audit_step, audit_trace, fixture_target, names


def row(ref, name, operation='create', target=None, descriptions=(), mention=None):
    return dict(ref=ref, name=name, kind='individual', operation=operation, target_id=target,
                aliases=[], descriptions=list(descriptions), evidence=[{'source_id': 'S1'}],
                reference=dict(source_id='S1', mention=mention, basis_id='B1') if operation == 'reuse' else None)


def trace(subjects, source='', connections=()):
    return dict(id='fixture', source=source, output=dict(subjects=subjects, connections=list(connections)),
                passed=False, checks=[dict(name='0_identity_and_names', passed=False)])


def codes(result):
    return {finding['code'] for finding in result['findings']}


def test_descriptions_never_supply_an_identity_name_anchor():
    subject = row(0, 'someone', descriptions=[dict(text='Nora', source_id='S1')])
    assert 'nora' not in names(subject)
    result = audit_step(trace([subject], 'Nora arrived.'), expectation=dict(identities=[
        dict(key='nora', names=['Nora'], operation='create')]))
    assert result['identity_decisions'][0]['status'] == 'omitted'
    assert result['manual_review']


def test_missing_description_does_not_fail_two_distinct_identity_rows():
    result = audit_step(trace([row(0, 'the first volunteer'), row(1, 'the second volunteer')],
        'The first volunteer and the second volunteer are different people.'), expectation=dict(
        identities=[dict(key='first', names=['the first volunteer'], operation='create'),
                    dict(key='second', names=['the second volunteer'], operation='create')]))
    assert [item['status'] for item in result['identity_decisions']] == ['matches_explicit_fields'] * 2
    assert not any(finding['dimension'].startswith('identity') for finding in result['findings'])


def test_unnecessary_pending_is_a_decision_problem_separate_from_names():
    result = audit_step(trace([row(0, 'Rory', 'pending')], 'I met another Rory.'), expectation=dict(
        identities=[dict(key='new_rory', names=['Rory'], operation='create')]))
    assert 'operation_disagrees_with_explicit_expectation' in codes(result)
    assert result['identity_decisions'][0]['actual_operation'] == 'pending'


def test_existing_id_recreated_is_detected_without_description_matching():
    payload = dict(new_user_source=dict(id='S1', content='Pip arrived.'),
                   subject_catalog=[dict(id='rabbit', name='Pip', is_user=False)])
    result = audit_step(trace([row(0, 'Pip')], 'Pip arrived.'), owner='fixture',
        case=dict(payload=payload), expectation=dict(identities=[
            dict(key='pet', names=['Pip'], operation='reuse', target='rabbit')]))
    assert {'operation_disagrees_with_explicit_expectation',
            'selected_target_disagrees_with_explicit_expectation'} <= codes(result)
    target = fixture_target('fixture', 'rabbit', payload['subject_catalog'])
    reused = row(0, 'Pip', 'reuse', target, mention='Pip')
    correct = audit_step(trace([reused], 'Pip arrived.'), owner='fixture', case=dict(payload=payload),
        expectation=dict(identities=[dict(key='pet', names=['Pip'], operation='reuse', target='rabbit')]))
    assert correct['identity_decisions'][0]['status'] == 'matches_explicit_fields'


def test_same_name_comparison_requires_manual_alignment_until_distinct_anchors_exist():
    case = dict(payload=dict(subject_catalog=[dict(id='E1', name='Ivo')]))
    result = audit_step(trace([row(0, 'Ivo', 'pending')], 'A new Ivo differs from the old Ivo.'),
        owner='fixture', case=case, expectation=dict(identities=[
            dict(key='old', names=['Ivo'], operation='reuse', target='E1'),
            dict(key='new', names=['Ivo'], operation='create')]))
    assert 'several_expected_identities_share_one_row' in codes(result)
    assert result['manual_review']
    # Two same-name rows establish cardinality, while their semantic assignment stays unscored.
    result2 = audit_step(trace([row(0, 'Ivo'), row(1, 'Ivo')]), expectation=dict(
        identities=[dict(key='old', names=['Ivo']), dict(key='new', names=['Ivo'])]))
    assert all(item['status'] == 'manual_review' for item in result2['identity_decisions'])
    assert not result2['findings']


def test_connection_endpoint_failures_do_not_become_json_syntax_failures():
    result = audit_step(trace([row(0, 'Nora')], 'Nora arrived.', connections=[
        dict(subject_refs=[0, 0]), dict(subject_refs=[0, 4])]))
    assert result['parse_status'] == 'saved_parsed_value'
    assert {'connection_requires_two_distinct_refs', 'connection_ref_not_declared'} <= codes(result)
    assert not any(item['dimension'] == 'format' for item in result['findings'])


def test_current_source_additions_and_literal_anchors_are_checked_independently():
    subject = row(0, 'Dax', 'reuse', 'existing', mention='Dax')
    subject['descriptions'] = [dict(text='my neighbor', source_id='S2')]
    result = audit_step(trace([subject], 'I spoke with Dax.'),
        case=dict(payload=dict(new_user_source=dict(id='S1', content='I spoke with Dax.'))))
    assert {'addition_uses_non_current_source', 'addition_not_literal_in_current_source'} <= codes(result)
    assert not any(finding['dimension'] == 'identity_decision' for finding in result['findings'])


def test_unexpected_api_user_and_out_of_scope_row_require_explicit_coverage_annotation():
    subject_trace = trace([row(0, '@user'), row(1, 'router')], 'The router arrived.')
    conservative = audit_step(subject_trace)
    assert 'user_not_referred_to_in_current_source' not in codes(conservative)
    annotated = audit_step(subject_trace, expectation=dict(
        identities=[], coverage_complete=True, user_present=False))
    assert 'user_not_referred_to_in_current_source' in codes(annotated)
    assert len([f for f in annotated['findings'] if f['code'] == 'unexpected_identity_row']) == 2


def test_legacy_results_remain_byte_equivalent_and_semantic_accuracy_is_unscored():
    original = trace([row(0, 'Nora')], 'Nora arrived.')
    before = deepcopy(original)
    result = audit_trace(original, expectation=dict(identities=[dict(key='n', names=['Nora'])]))
    assert original == before
    assert result['legacy_passed'] is False
    assert result['legacy_checks'] == before['checks']
    assert result['semantic_identity_accuracy'] is None
    assert 'mixed_identity_names_descriptions' in result['legacy_dimensions']


def test_effective_sequential_response_and_original_issues_are_preserved():
    first = dict(index=0, source='Nora arrived.', output=dict(subjects=[row(0, 'wrong')], connections=[]),
        effective_output=dict(subjects=[row(0, 'Nora')], connections=[]),
        initial_issues=[dict(code='old_failure')], final_issues=[])
    result = audit_trace(dict(id='seq', steps=[first], passed=False, checks=[]), expectation=dict(
        steps={'0': dict(identities=[dict(key='n', names=['Nora'])])}))
    diagnostic = result['diagnostics'][0]
    assert diagnostic['output_field'] == 'effective_output'
    assert diagnostic['identity_decisions'][0]['status'] == 'matches_explicit_fields'
    assert diagnostic['initial_issues'] == [dict(code='old_failure')]


def test_case_normalization_is_reported_without_inventing_a_protocol_error():
    subject = row(0, 'Vale', 'reuse', 'old', mention='my dog Vale')
    subject['descriptions'] = [dict(text='my dog', source_id='S1')]
    result = audit_step(trace([subject], 'My dog Vale arrived.'),
        case=dict(payload=dict(new_user_source=dict(id='S1', content='My dog Vale arrived.'))))
    assert not result['findings']
    assert len(result['normalization_notes']) == 2


def test_provider_no_reply_is_separate_from_semantic_and_format_errors():
    result = audit_trace(dict(id='unavailable', passed=False, checks=[], error_type='ProviderUnavailable'),
                         case=dict(payload=dict(new_user_source=dict(content='The physician called.'))))
    diagnostic = result['diagnostics'][0]
    assert diagnostic['provider_failure']['excluded_from_semantic_and_format_scoring'] is True
    assert diagnostic['findings'] == diagnostic['identity_decisions'] == []


def test_raw_transport_decisions_exclude_compiler_injected_rows_and_preserve_wire_json():
    from scripts.audit_subject_trace import request_fingerprint
    request = dict(model='gpt-4o-mini', messages=[dict(role='user', content='Nora arrived.')])
    wire = dict(subjects=[dict(ref=0, mention='Nora', operation='create', target_id=None,
        label=dict(name='Nora', kind='individual'), aliases=[], descriptions=[])], connections=[])
    import json
    packet = dict(request=dict(request, timeout=45), response=dict(content=json.dumps(wire)),
                  trace_path='/tmp/packet.json', started_at='2026-10-23T00:00:00Z')
    original = trace([row(0, 'Nora'), row(1, '@user')], 'Nora arrived.')
    original['model_requests'] = [request]
    result = audit_trace(original, decision_layer='raw', packets={request_fingerprint(request): [packet]})
    diagnostic = result['diagnostics'][0]
    assert len(diagnostic['output']['subjects']) == 1
    assert diagnostic['decision_provenance'] == 'transport_raw_model_reply'
    assert diagnostic['raw_model_attempts'][0]['output'] == wire
    assert request_fingerprint(request) == request_fingerprint(packet['request'])


def test_raw_source_label_translation_preserves_the_model_provenance_choice():
    from scripts.audit_subject_trace import raw_decision_step, request_fingerprint
    import json
    request = dict(messages=[dict(role='user', content='Selene arrived.')])
    wire_row = row(0, 'Selene', 'reuse', 'E1', mention='Selene')
    wire_row['aliases'] = [dict(name='Seli', source_id='S2')]
    normalized_row = deepcopy(wire_row)
    normalized_row['target_id'] = 'obj_actual'
    normalized_row['aliases'][0]['source_id'] = 'step-0'
    normalized_row['reference']['source_id'] = 'step-1'
    selected = dict(source='Selene arrived.', output=dict(subjects=[normalized_row], connections=[]),
                    model_requests=[request])
    wire = dict(subjects=[wire_row], connections=[])
    packet = dict(response=dict(content=json.dumps(wire)), trace_path='/tmp/raw.json')
    transformed = raw_decision_step(selected, {}, {request_fingerprint(request): [packet]}, 'step-1')
    assert transformed['normalized_output']['subjects'][0]['aliases'][0]['source_id'] == 'step-0'
    assert transformed['raw_model_attempts'][0]['output'] == wire
    assert transformed['normalized_output']['subjects'][0]['target_id'] == 'obj_actual'
    assert transformed['normalized_output']['subjects'][0]['raw_target_id'] == 'E1'


def compact_contract_request(*, conditional=False):
    from copy import deepcopy
    label = dict(type='object', additionalProperties=False, required=['name', 'kind'], properties=dict(
        name=dict(type='string', minLength=1), kind=dict(type='string', enum=['individual', 'organization'])))
    subject = dict(type='object', additionalProperties=False,
        required=['ref', 'mention', 'operation', 'target_id', 'label', 'aliases', 'descriptions'],
        properties=dict(ref=dict(type='integer', minimum=0), mention=dict(type='string', minLength=1),
            operation=dict(type='string', enum=['create', 'reuse', 'pending']),
            target_id=dict(anyOf=[dict(type='string', enum=['E1']), dict(type='null')]),
            label=dict(anyOf=[{'$ref': '#/$defs/Label'}, dict(type='null')]),
            aliases=dict(type='array', items=dict(type='string')),
            descriptions=dict(type='array', items=dict(type='string'))))
    if conditional:
        branches = []
        for operation in ['create', 'reuse', 'pending']:
            branch = deepcopy(subject)
            branch['properties']['operation'] = dict(type='string', enum=[operation])
            branch['properties']['target_id'] = dict(type='string', enum=['E1']) if operation == 'reuse' else dict(type='null')
            branch['properties']['label'] = dict(type='null') if operation == 'reuse' else {'$ref': '#/$defs/Label'}
            branches.append(branch)
        subject = dict(anyOf=branches)
    schema = {'type': 'object', 'additionalProperties': False, 'required': ['subjects', 'connections'],
        '$defs': dict(Label=label, Choice=subject), 'properties': dict(
            subjects=dict(type='array', items={'$ref': '#/$defs/Choice'}),
            connections=dict(type='array', items=dict(type='object', properties=dict(
                subject_refs=dict(type='array', items=dict(type='integer'), minItems=2, maxItems=2)),
                required=['subject_refs'], additionalProperties=False)))}
    return dict(response_format=dict(type='json_schema', json_schema=dict(name='WirePlan', schema=schema)))


def wire_choice(operation='create'):
    return dict(ref=0, mention='a current referent', operation=operation,
        target_id='E1' if operation == 'reuse' else None,
        label=None if operation == 'reuse' else dict(name='a current referent', kind='individual'),
        aliases=[], descriptions=[])


def check_wire(choice, *, conditional=False):
    import json
    from scripts.audit_subject_trace import wire_contract
    return wire_contract(json.dumps(dict(subjects=[choice], connections=[])), compact_contract_request(conditional=conditional))


def test_compact_create_null_label_is_an_operation_condition_not_a_shape_failure():
    choice = wire_choice(); choice['label'] = None
    result = check_wire(choice)
    assert result['json_syntax_valid'] is result['schema_shape_valid'] is True
    assert result['operation_conditions_valid'] is False
    assert result['operation_condition_errors'][0]['code'] == 'new_or_pending_requires_label_object'


def test_compact_reuse_owned_label_is_an_operation_condition_failure():
    choice = wire_choice('reuse'); choice['label'] = dict(name='a label', kind='individual')
    result = check_wire(choice)
    assert result['schema_shape_valid'] is True
    assert result['operation_conditions_valid'] is False
    assert result['operation_condition_errors'][0]['code'] == 'reuse_requires_null_label'


def test_compact_alias_objects_are_a_shape_failure_separate_from_operation_conditions():
    choice = wire_choice(); choice['aliases'] = [dict(name='a name', source_id='S1')]
    result = check_wire(choice)
    assert result['json_syntax_valid'] is True
    assert result['schema_shape_valid'] is False
    assert result['operation_conditions_valid'] is True


def test_missing_label_is_a_required_field_shape_failure():
    choice = wire_choice(); choice.pop('label')
    result = check_wire(choice)
    assert result['schema_shape_valid'] is False
    assert result['operation_conditions_valid'] is True
    assert any(error['path'] == ['subjects', 0, 'label'] for error in result['schema_shape_errors'])


def test_invalid_json_is_distinct_from_valid_json_with_wrong_top_level_type():
    from scripts.audit_subject_trace import wire_contract
    malformed = wire_contract('{', compact_contract_request())
    assert malformed['json_syntax_valid'] is False
    assert malformed['schema_shape_valid'] is malformed['operation_conditions_valid'] is None
    wrong_type = wire_contract('[]', compact_contract_request())
    assert wrong_type['json_syntax_valid'] is True
    assert wrong_type['schema_shape_valid'] is False


def test_conditional_sent_schema_keeps_combination_failure_separate_from_basic_shapes():
    choice = wire_choice(); choice['label'] = None
    result = check_wire(choice, conditional=True)
    assert result['json_syntax_valid'] is result['schema_shape_valid'] is True
    assert result['strict_sent_schema_valid'] is result['operation_conditions_valid'] is False


def test_conditional_sent_schema_accepts_all_three_supported_operation_combinations():
    for operation in ['create', 'reuse', 'pending']:
        result = check_wire(wire_choice(operation), conditional=True)
        assert result['json_syntax_valid'] is result['schema_shape_valid'] is True
        assert result['strict_sent_schema_valid'] is result['operation_conditions_valid'] is True


def test_candidate_id_enum_is_validated_from_the_captured_schema():
    choice = wire_choice('reuse'); choice['target_id'] = 'E404'
    result = check_wire(choice)
    assert result['schema_shape_valid'] is False
    assert result['operation_conditions_valid'] is True


def test_legacy_wire_has_its_own_shape_and_never_requires_a_compact_label():
    import json
    from scripts.audit_subject_trace import wire_contract
    legacy = row(0, 'a name')
    result = wire_contract(json.dumps(dict(subjects=[legacy], connections=[])), dict(
        response_format=dict(json_schema=dict(name='SubjectConnectionPlan', schema=dict(type='object')))))
    assert result['protocol'] == 'legacy_subject_wire'
    assert result['schema_shape_valid'] is result['operation_conditions_valid'] is True
    assert not result['findings']


def test_new_and_pending_targets_follow_the_same_generic_null_rule():
    for operation in ['create', 'pending']:
        choice = wire_choice(operation); choice['target_id'] = 'E1'
        result = check_wire(choice)
        assert result['schema_shape_valid'] is True
        assert result['operation_conditions_valid'] is False
        assert result['operation_condition_errors'][0]['code'] == 'new_or_pending_target_requires_null'


def test_invalid_compact_identity_label_remains_protocol_unscored_instead_of_a_semantic_omission():
    import json
    from scripts.audit_subject_trace import request_fingerprint
    choice = wire_choice(); choice['label'] = None
    request = compact_contract_request()
    raw = dict(subjects=[choice], connections=[])
    original = trace([dict(ref=0, name=None, kind=None, operation='create', target_id=None,
        aliases=[], descriptions=[], evidence=[], reference=None)], 'a current referent')
    original['model_requests'] = [request]
    packet = dict(response=dict(content=json.dumps(raw)), trace_path='/tmp/raw.json')
    result = audit_trace(original, decision_layer='raw', packets={request_fingerprint(request): [packet]},
        expectation=dict(identities=[dict(key='expected', names=['a current referent'], operation='create')]))
    diagnostic = result['diagnostics'][0]
    assert diagnostic['identity_decisions'][0]['status'] == 'manual_review_protocol_invalid'
    assert diagnostic['wire_identity_unscorable_refs'] == [0]
    assert 'expected_identity_has_no_name_or_target_aligned_row' not in codes(diagnostic)
    assert diagnostic['selected_wire_contract']['operation_conditions_valid'] is False


def test_first_and_last_raw_attempts_keep_their_own_model_decisions_and_contract_results():
    import json
    from scripts.audit_subject_trace import request_fingerprint
    first_request = compact_contract_request(); first_request['messages'] = [dict(role='user', content='first')]
    last_request = compact_contract_request(); last_request['messages'] = [dict(role='user', content='repair')]
    first = wire_choice(); first['label'] = None
    last = wire_choice()
    step = trace([row(0, 'a current referent')], 'a current referent')
    step['model_requests'] = [first_request, last_request]
    step['attempt_outputs'] = [step['output'], step['output']]
    packets = {request_fingerprint(request): [dict(response=dict(content=json.dumps(dict(subjects=[subject], connections=[]))),
        trace_path='/tmp/raw.json')] for request, subject in [(first_request, first), (last_request, last)]}
    initial = audit_trace(step, decision_layer='raw', packets=packets, raw_attempt='first')['diagnostics'][0]
    final = audit_trace(step, decision_layer='raw', packets=packets, raw_attempt='last')['diagnostics'][0]
    assert initial['selected_wire_contract']['operation_conditions_valid'] is False
    assert final['selected_wire_contract']['operation_conditions_valid'] is True
    assert initial['wire_identity_unscorable_refs'] == [0]
    assert final['wire_identity_unscorable_refs'] == []
